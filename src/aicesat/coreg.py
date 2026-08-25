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


def propagate(lon, lat, h, t_obs, common_epoch: float, from_frame: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Propagate (lon, lat, h) observed at decimal-year t_obs in from_frame to ITRF2014 @ common_epoch."""
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
        "surface_slope_method": "least-squares plane over all ICESat-2 photons in the bbox (regional gradient)",
        "effective_slope_from_artifact_deg": effective_slope_deg,
        "horizontal_to_vertical_sensitivity": sens,
        "plate_motion_corrected": True,
        "plate_motion_model": "ITRF2014-PMM (Altamimi et al. 2017), NOAM Euler pole",
        "ellipsoid_correction_applied": ellipsoid_note,
        "unresolved": list(UNRESOLVED),
        "dynamic_ice_flag": dynamic_ice_flag,
        "dynamic_ice_note": DYNAMIC_ICE_NOTE if dynamic_ice_flag is None else None,
    }


def coregister_scene(doc: dict, common_epoch: float = 2005.0, colocation_radius_m: float = 35.0,
                     exaggeration: float = 0.0, dynamic_ice_flag: bool | None = None) -> dict:
    """Run the live co-registration over a scene document (both series required). Returns the result block."""
    from . import cache

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
    # along-track projection of the relative displacement (only this component is observable along one beam)
    vrel = _relative_vector(I, Gd)
    years_apart = float(abs(np.median(I["t"]) - np.median(Gd["t"])))

    exag = p.exaggeration
    if exag <= 0:
        span = max(np.ptp(I["x0"]), np.ptp(I["y0"]), 1.0)
        exag = float(f"{0.05 * span / max(rel, 1e-3):.1g}")  # one significant figure, e.g. 10000
    log.info("exaggeration x%g (relative displacement %.3f m)", exag, rel)

    def display(m):
        d = data[m]
        st = d["stride"]
        dx, dy = (d["x1"] - d["x0"]) * exag, (d["y1"] - d["y0"]) * exag
        pos = np.column_stack([d["x0"] + dx, d["y0"] + dy, d["h1"] - z0])[::st].astype("f4")
        return np.round(pos, 3).ravel().tolist()

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
        "relative_shift_vector_m": vrel.tolist(),
        "along_track_slope_deg": along_slope_deg,  # median |local along-beam slope| at the pairs (the observable component)
        "dh_estimator": f"local along-track linear fit of ICESat-2 photons within {p.colocation_radius_m} m, evaluated at the GLAS footprint centre",
        "exaggeration": exag,
        "exaggeration_auto": p.exaggeration <= 0,
        "pair_display_indices": {"GLAS": (pn[pn % Gd["stride"] == 0] // Gd["stride"]).tolist()},
        "n_pairs": {"native": int(pn.size), "coreg": int(pc.size), "common": int(common.size),
                    "gross_outliers_dropped": {"native": gross_n, "coreg": gross_c, "threshold_m": GROSS_PAIR_M}},
        "stats": {"native": _stats(dhn), "coreg": _stats(dhc), "artifact": _stats(artifact),
                  "dh_range": [float(lo - pad), float(hi + pad)],
                  "artifact_range": [float(art_lo - apad), float(art_hi + apad)]},
        "dh_native": np.round(dhn, 4).tolist(),
        "dh_coreg": np.round(dhc, 4).tolist(),
        "artifact": np.round(artifact, 5).tolist(),
        "display_positions": {m: display(m) for m in data},
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
