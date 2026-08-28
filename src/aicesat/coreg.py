"""ITRF frame + epoch co-registration (plate motion), co-location, and the comparability block.

What this does: propagates every measurement from its own observation epoch to a common epoch in
ITRF2014 using the ITRF2014 plate motion model (Altamimi et al. 2017) for the North American plate,
on which Greenland sits. GLAS (ITRF2008) is first moved to ITRF2014 (mm-level Helmert).
What it does NOT do (surfaced in every output): ice flow, GIA, geoid/tide, firn, vertical datum.

PROJ hazard: the helmert step evaluates rates as dP*(t - t_epoch) with t taken from the 4th
coordinate; if t is omitted PROJ uses t_epoch and the transform is a silent identity. We always
pass t and assert a nonzero displacement.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree

from . import scene as scene_mod

log = logging.getLogger(__name__)

COMMON_FRAME = "ITRF2014"
# ITRF2014-PMM, NOAM, rotation rates in arc-seconds / year (PROJ data/ITRF2014), position-vector convention.
NOAM_RATES = {"drx": 0.000024, "dry": -0.000694, "drz": -0.000063}
UNRESOLVED = ["ice_flow?", "GIA", "geoid/tide", "firn compaction", "GLAS intercampaign bias", "vertical_datum (geoid)"]
DYNAMIC_ICE_NOTE = ("unknown: no ice-velocity field in this build; the region must be validated as slow-flowing "
                    "by the collaborator (spec B.9)")
GROSS_PAIR_M = 50.0  # |dh| beyond this is a cloud/blunder pair, dropped and counted, never averaged


@dataclass(frozen=True)
class Params:
    common_epoch: float = 2005.0
    colocation_radius_m: float = 35.0
    exaggeration: float = 0.0  # <= 0 -> auto: displayed offset ~5% of the scene span (always labelled)


def params(**kw) -> dict:
    return asdict(Params(**kw))


def decimal_year(t: np.ndarray) -> np.ndarray:
    t = np.asarray(t, dtype="datetime64[ms]")
    years = t.astype("datetime64[Y]")
    y0 = years.astype("datetime64[ms]")
    y1 = (years + np.timedelta64(1, "Y")).astype("datetime64[ms]")
    frac = (t - y0).astype("f8") / (y1 - y0).astype("f8")
    return years.astype(int) + 1970 + frac


def _frame_pipeline(from_frame: str) -> Transformer | None:
    """Native frame -> ITRF2014 (14-parameter Helmert, evaluated at the observation epoch). None if already ITRF2014.
    PROJ's ITRF2014 init file gives ITRF2014 -> ITRFxx *forward*, so the step is inverted here."""
    if from_frame == "ITRF2014":
        return None
    if from_frame == "ITRF2008":
        return Transformer.from_pipeline(
            "+proj=pipeline +ellps=GRS80 "
            "+step +proj=unitconvert +xy_in=deg +xy_out=rad "
            "+step +proj=cart +ellps=GRS80 "
            "+step +inv +init=ITRF2014:ITRF2008 "
            "+step +inv +proj=cart +ellps=GRS80 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg")
    raise ValueError(f"unsupported native frame {from_frame}")


def _pmm_pipeline(src_epoch: float) -> Transformer:
    """ITRF2014 @ src_epoch -> ITRF2014 @ t (4th coordinate) on the NOAM plate."""
    return Transformer.from_pipeline(
        "+proj=pipeline +ellps=GRS80 "
        "+step +proj=unitconvert +xy_in=deg +xy_out=rad "
        "+step +proj=cart +ellps=GRS80 "
        "+step +proj=helmert +drx={drx} +dry={dry} +drz={drz} +t_epoch={e} +convention=position_vector "
        "+step +inv +proj=cart +ellps=GRS80 "
        "+step +proj=unitconvert +xy_in=rad +xy_out=deg".format(e=src_epoch, **NOAM_RATES))


def _pipeline(src_epoch: float, from_frame: str) -> Transformer:  # kept for tests: PMM step only
    if from_frame != "ITRF2014":
        raise ValueError("frame conversion is a separate step; see _frame_pipeline")
    return _pmm_pipeline(src_epoch)


# GRS80
_A, _F = 6378137.0, 1 / 298.257222101
_E2 = _F * (2 - _F)
_ARCSEC = np.pi / 648000.0


def _geodetic_to_ecef(lon, lat, h):
    lam, phi = np.radians(lon), np.radians(lat)
    sphi, cphi = np.sin(phi), np.cos(phi)
    N = _A / np.sqrt(1 - _E2 * sphi * sphi)
    return (N + h) * cphi * np.cos(lam), (N + h) * cphi * np.sin(lam), (N * (1 - _E2) + h) * sphi


def _ecef_to_geodetic(x, y, z):
    """Vermeille (2002) closed form; sub-nanometre agreement with PROJ's cart inverse on the ice sheet."""
    lam = np.arctan2(y, x)
    p2 = x * x + y * y
    a2 = _A * _A
    e4 = _E2 * _E2
    pp = p2 / a2
    q = (1 - _E2) * z * z / a2
    r = (pp + q - e4) / 6
    s_ = e4 * pp * q / (4 * r ** 3)
    t = np.cbrt(1 + s_ + np.sqrt(s_ * (2 + s_)))
    u = r * (1 + t + 1 / t)
    v = np.sqrt(u * u + e4 * q)
    w = _E2 * (u + v - q) / (2 * v)
    k = np.sqrt(u + v + w * w) - w
    D = k * np.sqrt(p2) / (k + _E2)
    dz = np.sqrt(D * D + z * z)
    phi = 2 * np.arctan2(z, D + dz)
    hh = (k + _E2 - 1) / k * dz
    return np.degrees(lam), np.degrees(phi), hh


def propagate_numpy(lon, lat, h, t_obs, common_epoch: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ITRF2014 @ t_obs -> ITRF2014 @ common_epoch with the NOAM plate-motion model, in numpy.
    Identical maths to the PROJ helmert step used by propagate(): rates only, position-vector convention,
    small-angle rotation (PROJ's default, no +exact): X' = X + dt * (omega x X). ~50x faster than pyproj per point;
    validated against propagate() to < 0.1 mm in tests. ITRF2014 input only — other frames go through pyproj."""
    lon, lat, h = (np.asarray(a, dtype="f8") for a in (lon, lat, h))
    dt = common_epoch - np.asarray(t_obs, dtype="f8")
    x, y, z = _geodetic_to_ecef(lon, lat, h)
    wx, wy, wz = (NOAM_RATES[k] * _ARCSEC for k in ("drx", "dry", "drz"))
    # position-vector rotation by the vector dt*omega: X' = X + (dt*omega) x X
    x1 = x + dt * (wy * z - wz * y)
    y1 = y + dt * (wz * x - wx * z)
    z1 = z + dt * (wx * y - wy * x)
    return _ecef_to_geodetic(x1, y1, z1)


def propagate(lon, lat, h, t_obs, common_epoch: float, from_frame: str, engine: str = "auto") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Propagate (lon, lat, h) observed at decimal-year t_obs in from_frame to ITRF2014 @ common_epoch.
    engine: 'auto' uses numpy for ITRF2014 input (bulk photon materialization) and pyproj otherwise; 'pyproj' forces
    the reference path."""
    if engine == "auto" and from_frame == "ITRF2014":
        return propagate_numpy(lon, lat, h, t_obs, common_epoch)
    lon, lat, h = (np.asarray(a, dtype="f8") for a in (lon, lat, h))
    t_obs = np.asarray(t_obs, dtype="f8")
    fr = _frame_pipeline(from_frame)
    if fr is not None:
        lon, lat, h, _ = fr.transform(lon, lat, h, t_obs)  # frame step at the observation epoch
        lon, lat, h = (np.asarray(a, dtype="f8") for a in (lon, lat, h))
    out_lon, out_lat, out_h = np.empty_like(lon), np.empty_like(lat), np.empty_like(h)
    # t_epoch is a pipeline constant -> one transformer per observation day (cheap; a few hundred at most).
    day = np.round(t_obs * 365.25) / 365.25
    for d in np.unique(day):
        m = day == d
        x, y, z, _ = _pmm_pipeline(float(d)).transform(lon[m], lat[m], h[m], np.full(m.sum(), common_epoch))
        out_lon[m], out_lat[m], out_h[m] = x, y, z
    return out_lon, out_lat, out_h


def horizontal_displacement_m(lon0, lat0, lon1, lat1) -> np.ndarray:
    """Local-tangent-plane distance between two lon/lat arrays (metres); fine for cm-scale offsets."""
    R = 6_371_000.0
    dlat = np.radians(np.asarray(lat1) - np.asarray(lat0))
    dlon = np.radians(np.asarray(lon1) - np.asarray(lon0)) * np.cos(np.radians(np.asarray(lat0)))
    return R * np.hypot(dlat, dlon)


def fit_slope_deg(x, y, z) -> tuple[float, tuple[float, float]]:
    """Least-squares plane z = a x + b y + c over local metres; returns (slope_deg, (a, b))."""
    A = np.column_stack([x, y, np.ones_like(x)])
    (a, b, _), *_ = np.linalg.lstsq(A, z, rcond=None)
    return float(np.degrees(np.arctan(np.hypot(a, b)))), (float(a), float(b))


def colocate(gx, gy, gh, px, py, ph, radius_m: float, min_photons: int = 12):
    """For each GLAS shot: the ICESat-2 surface height AT the shot centre, from a local along-track linear fit of
    the photons within radius_m. Returns (pair index into GLAS, dh = h_fit(centre) - gh, n_gross, slope_along).

    Why a fit and not a median: a median over ~10^3 photons is an order statistic; moving the disc by 30 cm swaps
    a few edge photons and the median moves by zero or one rank step, so it cannot resolve a sub-cm slope
    artifact. The fitted line evaluated at the footprint centre is continuous in position: shifting the centre
    by ds changes it by slope_along * ds, which is exactly the misregistration effect we want to measure.
    Photons lie along a single beam, so only the along-track gradient is observable; |slope_along| is returned.
    """
    tree = cKDTree(np.column_stack([px, py]))
    idx_lists = tree.query_ball_point(np.column_stack([gx, gy]), r=radius_m)
    pairs, dh, slopes, gross = [], [], [], 0
    for i, lst in enumerate(idx_lists):
        if len(lst) < min_photons:
            continue
        lst = np.asarray(lst)
        dx, dy, hh = px[lst] - gx[i], py[lst] - gy[i], ph[lst]
        # principal (along-track) direction of the photon footprint
        cov = np.cov(np.vstack([dx, dy]))
        w, v = np.linalg.eigh(cov)
        u = v[:, int(np.argmax(w))]
        sdist = dx * u[0] + dy * u[1]
        A = np.column_stack([sdist, np.ones_like(sdist)])
        (a, c), *_ = np.linalg.lstsq(A, hh, rcond=None)
        resid = hh - (a * sdist + c)
        mad = np.median(np.abs(resid - np.median(resid))) or 1e-3
        keep = np.abs(resid) < 4 * 1.4826 * mad
        if keep.sum() >= min_photons:
            (a, c), *_ = np.linalg.lstsq(A[keep], hh[keep], rcond=None)
        d = c - gh[i]
        if abs(d) > GROSS_PAIR_M:
            gross += 1
            continue
        pairs.append(i)
        dh.append(d)
        slopes.append(abs(a))  # the PCA axis sign is arbitrary, so only the slope magnitude is meaningful
    return np.asarray(pairs, dtype="i8"), np.asarray(dh, dtype="f8"), gross, np.asarray(slopes, dtype="f8")


def _stats(v: np.ndarray) -> dict:
    if v.size == 0:
        return {"n": 0, "median": None, "mad": None, "mean": None}
    med = float(np.median(v))
    return {"n": int(v.size), "median": med, "mad": float(np.median(np.abs(v - med))), "mean": float(v.mean())}


def comparability_block(*, coverage_coincides: bool, radius_m: float, slope_deg: float | None,
                        displacement_m: float, dynamic_ice_flag: bool | None, ellipsoid_note: str,
                        effective_slope_deg: float | None = None) -> dict:
    if slope_deg is None:
        sens = "slope unknown"
    else:
        dv = displacement_m * np.tan(np.radians(slope_deg))
        sens = f"{displacement_m * 100:.1f} cm horiz @ {slope_deg:.2f}° slope → ~{dv * 100:.2f} cm vertical"
    return {
        "coverage_coincides": coverage_coincides,
        "colocation_radius_m": radius_m,
        "surface_slope_deg": slope_deg,
        "surface_slope_method": "least-squares plane over all ICESat-2 photons in the bbox (regional gradient); see dem_slope_deg for the DEM-derived local slope",
        "effective_slope_from_artifact_deg": effective_slope_deg,
        "horizontal_to_vertical_sensitivity": sens,
        "plate_motion_corrected": True,
        "plate_motion_model": "ITRF2014-PMM (Altamimi et al. 2017), NOAM Euler pole",
        "ellipsoid_correction_applied": ellipsoid_note,
        "unresolved": list(UNRESOLVED),
        "dynamic_ice_flag": dynamic_ice_flag,
        "dynamic_ice_note": DYNAMIC_ICE_NOTE if dynamic_ice_flag is None else None,
    }


def _gia_block(doc: dict, t_is2: float, t_glas: float) -> dict | None:
    """Present-day GIA solid-Earth vertical motion between the two epochs, expressed as an additive shift to the
    co-located Δh (ICESat-2 − GLAS). Referencing both heights to the common epoch gives
        dh_corr = dh_obs − u·(t_IS2 − t_GLAS),   u = uplift rate (m/yr, + up)
    so the toggle simply shifts the Δh histogram by dh_shift_m = −u·(t_IS2 − t_GLAS). GIA is smooth, so u is sampled
    at the scene-bbox centre (corners give a range). Returns None if the model grid is unavailable."""
    try:
        from . import gia
    except Exception:
        return None
    try:
        w, s, e, n = doc["bbox"]
        lon = np.array([(w + e) / 2, w, e, w, e], dtype="f8")
        lat = np.array([(s + n) / 2, s, s, n, n], dtype="f8")
        u_mm = np.asarray(gia.uplift_rate_mm_yr(lon, lat), dtype="f8")
        u_c = u_mm[0] if np.isfinite(u_mm[0]) else np.nanmedian(u_mm)
        fin = u_mm[np.isfinite(u_mm)]
        if not np.isfinite(u_c) or fin.size == 0:
            return None
        years_signed = t_is2 - t_glas
        dh_shift = -(float(u_c) / 1000.0) * years_signed
        return {"model": gia.MODEL, "citation": gia.CITATION,
                "uplift_rate_mm_per_yr": round(float(u_c), 3),
                "uplift_rate_range_mm_per_yr": [round(float(fin.min()), 3), round(float(fin.max()), 3)],
                "years_apart_signed": round(float(years_signed), 3),
                "dh_shift_m": round(float(dh_shift), 4),
                "unresolved_key": "GIA",
                "note": ("removes present-day GIA solid-Earth vertical motion between the epochs (uplift rate × years "
                         "apart), referencing both heights to the common epoch; does not remove ice-dynamic, firn, or "
                         "elastic-loading signals")}
    except Exception as ex:
        log.warning("GIA block failed: %s", ex)
        return None


def coregister_scene(doc: dict, common_epoch: float = 2005.0, colocation_radius_m: float = 35.0,
                     exaggeration: float = 0.0, dynamic_ice_flag: bool | None = None) -> dict:
    """Run the live co-registration over a scene document (both series required). Returns the result block."""
    p = Params(common_epoch, colocation_radius_m, exaggeration)
    if "ICESAT2" not in doc["series"] or "GLAS" not in doc["series"]:
        raise ValueError("scene needs both ICESAT2 and GLAS series before co-registration")
    frame = doc["frame"]
    data = {}
    for mission in ("ICESAT2", "GLAS"):
        s = doc["series"][mission]
        arrays, meta = _reload_arrays(s)
        t = decimal_year(arrays["t"])
        clon, clat, ch = propagate(arrays["lon"], arrays["lat"], arrays["h"], t, p.common_epoch, meta["native_frame"])
        disp = horizontal_displacement_m(arrays["lon"], arrays["lat"], clon, clat)
        if not np.nanmax(disp) > 0.001 and abs(float(np.median(t)) - p.common_epoch) > 0.1:
            raise RuntimeError(f"{mission}: propagation produced no displacement — silent-identity trap (check t argument)")
        x0, y0 = scene_mod.to_local(frame, arrays["lon"], arrays["lat"])
        x1, y1 = scene_mod.to_local(frame, clon, clat)
        data[mission] = dict(x0=x0, y0=y0, h0=arrays["h"], x1=x1, y1=y1, h1=ch, t=t, disp=disp,
                             stride=s["stride"], native_frame=meta["native_frame"])
        log.info("%s: epoch %.2f→%.1f, median horizontal displacement %.3f m", mission, float(np.median(t)), p.common_epoch, float(np.median(disp)))

    I, Gd = data["ICESAT2"], data["GLAS"]
    z0 = doc["z0"]
    slope_deg, _ = fit_slope_deg(I["x0"], I["y0"], I["h0"])
    dem_slope = None
    try:
        from . import dem
        dem_slope = dem.slope_deg(doc.get("surface"), Gd["x0"], Gd["y0"])
    except Exception:
        pass
    # Three co-locations:
    #   native : native positions, native heights                       -> OFF histogram
    #   horiz  : co-registered positions, NATIVE heights                -> isolates the horizontal re-pairing artifact
    #   coreg  : co-registered positions, frame-corrected heights       -> ON histogram (adds the mm-level frame shift)
    # Plate rotation leaves heights unchanged to < 1 mm, but the ITRF2008->ITRF2014 Helmert has a ~2 mm translation
    # whose vertical projection would otherwise masquerade as a slope artifact in the per-pair difference.
    pn, dhn, gross_n, sl_n = colocate(Gd["x0"], Gd["y0"], Gd["h0"], I["x0"], I["y0"], I["h0"], p.colocation_radius_m)
    ph_, dhh, _, _ = colocate(Gd["x1"], Gd["y1"], Gd["h0"], I["x1"], I["y1"], I["h0"], p.colocation_radius_m)
    pc, dhc, gross_c, _ = colocate(Gd["x1"], Gd["y1"], Gd["h1"], I["x1"], I["y1"], I["h1"], p.colocation_radius_m)
    common = np.intersect1d(pn, ph_)
    dn = dict(zip(pn.tolist(), dhn)); dh_ = dict(zip(ph_.tolist(), dhh))
    artifact = np.asarray([dh_[i] - dn[i] for i in common])  # horizontal misregistration only
    frame_shift = {m: float(np.median(data[m]["h1"] - data[m]["h0"])) for m in data}
    along_slope_deg = float(np.degrees(np.arctan(np.median(sl_n)))) if sl_n.size else None

    # relative displacement between the two clouds: vector difference of the median shifts
    rel = _relative_displacement(I, Gd)
    eff_slope = (float(np.degrees(np.arctan(abs(np.median(artifact)) / rel))) if artifact.size and rel > 1e-4 else None)
    years_apart = float(abs(np.median(I["t"]) - np.median(Gd["t"])))
    gia_block = _gia_block(doc, float(np.median(I["t"])), float(np.median(Gd["t"])))

    allv = np.concatenate([dhn, dhc]) if dhn.size else np.array([0.0])
    lo, hi = np.percentile(allv, [1, 99]) if allv.size > 10 else (allv.min() - 1, allv.max() + 1)
    pad = 0.1 * (hi - lo) or 0.5
    art_lo, art_hi = (np.percentile(artifact, [0.5, 99.5]) if artifact.size > 10 else (-0.05, 0.05))
    apad = 0.2 * (art_hi - art_lo) or 0.01
    return {
        "params": asdict(p),
        "common_frame": COMMON_FRAME,
        "common_epoch": p.common_epoch,
        "epochs": {"ICESAT2": float(np.median(I["t"])), "GLAS": float(np.median(Gd["t"]))},
        "years_apart": years_apart,
        "displacement_m": rel,
        "displacement_each_m": {"ICESAT2": float(np.median(I["disp"])), "GLAS": float(np.median(Gd["disp"]))},
        "frame_vertical_shift_m": frame_shift,  # height change from the native-frame -> ITRF2014 Helmert (not plate motion)
        "gia": gia_block,  # present-day GIA vertical bedrock motion between epochs, as an additive dh(IS2-GLAS) shift (or None)
        "along_track_slope_deg": along_slope_deg,  # median |local along-beam slope| at the pairs (the observable component)
        "dem_slope_deg": dem_slope,               # median ArcticDEM slope at the GLAS shots (None without a DEM)
        "dh_estimator": f"local along-track linear fit of ICESat-2 photons within {p.colocation_radius_m} m, evaluated at the GLAS footprint centre",
        "pair_display_indices": {"GLAS": (pn[pn % Gd["stride"] == 0] // Gd["stride"]).tolist()},
        "n_pairs": {"native": int(pn.size), "coreg": int(pc.size), "common": int(common.size),
                    "gross_outliers_dropped": {"native": gross_n, "coreg": gross_c, "threshold_m": GROSS_PAIR_M}},
        "stats": {"native": _stats(dhn), "coreg": _stats(dhc), "artifact": _stats(artifact),
                  "dh_range": [float(lo - pad), float(hi + pad)],
                  "artifact_range": [float(art_lo - apad), float(art_hi + apad)]},
        "dh_native": np.round(dhn, 4).tolist(),
        "dh_coreg": np.round(dhc, 4).tolist(),
        "artifact": np.round(artifact, 5).tolist(),
        "comparability": comparability_block(
            coverage_coincides=pn.size > 0, radius_m=p.colocation_radius_m, slope_deg=slope_deg,
            displacement_m=rel, dynamic_ice_flag=dynamic_ice_flag, effective_slope_deg=eff_slope,
            ellipsoid_note=doc["series"]["GLAS"]["meta"].get("ellipsoid_correction", "none")),
        "native_frames": {m: data[m]["native_frame"] for m in data},
    }


def _relative_vector(I, Gd) -> np.ndarray:
    """(ICESat-2 shift) - (GLAS shift), local metres (x east-ish, y north-ish in EPSG:3413)."""
    vi = np.array([np.median(I["x1"] - I["x0"]), np.median(I["y1"] - I["y0"])])
    vg = np.array([np.median(Gd["x1"] - Gd["x0"]), np.median(Gd["y1"] - Gd["y0"])])
    return vi - vg


def _relative_displacement(I, Gd) -> float:
    """Magnitude of the relative shift between the two clouds: how far they move relative to each other."""
    return float(np.hypot(*_relative_vector(I, Gd)))


def _reload_arrays(series: dict) -> tuple[dict, dict]:
    """Scene series store positions only; reload the full cached arrays (lon/lat/h/t) via the cache key."""
    from . import cache

    hit = cache.load(series["cache_key"])
    if hit is None:
        raise RuntimeError(f"cache entry {series['cache_key']} missing; re-run extraction")
    return hit
