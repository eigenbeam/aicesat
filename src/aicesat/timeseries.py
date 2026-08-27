"""Candidate coincident-observation cells and their elevation time series.

For a built scene: bin every mission's points into H3 cells and fixed time windows, keep cells observed
in >= min_bins distinct windows, fit ONE local reference plane per cell (from the user-chosen
mission(s)) to remove surface slope, and report each window's median residual about that plane as a
height anomaly -> a time series. Positions are first propagated to a common epoch (plate motion).

Deliberately NOT applied (and surfaced to the user): inter-campaign / inter-sensor bias adjustment and
GIA. The plane-fit is what keeps slope from masquerading as elevation change; a raw median-per-window
would be dominated by which part of a sloped cell each epoch happened to sample."""
import logging

import numpy as np
import h3

from . import coreg
from . import scene as scene_mod

log = logging.getLogger("aicesat.timeseries")

MISSION_LABEL = {"GLAS": "ICESat-1", "ICESSN": "IceBridge ATM", "ATL06": "ICESat-2 land ice", "ICESAT2": "ICESat-2 photons"}
_MAX_PTS_PER_MISSION = 80_000   # cap per-point H3 assignment cost; median residuals are robust to subsampling
_MIN_BIN_PTS = 3                # a time window needs this many points in the cell to be a usable series point
_MIN_REF_PTS = 6               # minimum reference points to fit a stable local plane
_BLUNDER_MAD = 6.0            # drop residuals beyond this many (scaled) MADs from the cell median


def _load_all(doc: dict, common_epoch: float) -> list[dict]:
    """Reload every scene series' full arrays (lon/lat/h/t), propagate to the common epoch, project to
    the scene's local frame. Returns one record per mission with x/y/h/yr arrays."""
    frame = doc["frame"]
    recs = []
    for mission, s in doc["series"].items():
        try:
            arrays, meta = coreg._reload_arrays(s)
        except Exception as e:
            log.warning("reload %s failed: %s", mission, e); continue
        if arrays.get("t") is None:
            continue
        lon = np.asarray(arrays["lon"], "f8"); lat = np.asarray(arrays["lat"], "f8"); h = np.asarray(arrays["h"], "f8")
        yr = coreg.decimal_year(arrays["t"])
        n = lon.size
        if n > _MAX_PTS_PER_MISSION:                       # thin dense missions for the cell scan
            idx = np.linspace(0, n - 1, _MAX_PTS_PER_MISSION).astype("i8")
            lon, lat, h, yr = lon[idx], lat[idx], h[idx], yr[idx]
        native = meta.get("native_frame", "ITRF2014")
        try:
            lon, lat, h = coreg.propagate(lon, lat, h, yr, common_epoch, native)
        except Exception as e:
            log.warning("propagate %s failed: %s; using raw positions", mission, e)
        x, y = scene_mod.to_local(frame, lon, lat)
        recs.append({"mission": mission, "lat": np.asarray(lat, "f8"), "lon": np.asarray(lon, "f8"),
                     "x": np.asarray(x, "f8"), "y": np.asarray(y, "f8"), "h": np.asarray(h, "f8"), "yr": np.asarray(yr, "f8")})
    return recs


def _confidence(roughness: float, n_bins: int, span: float, n_ref: int) -> tuple:
    """Deterministic 0-1 confidence from four measurables (roughness dominates — it's the failure mode:
    on rough/crevassed cells different missions sample different sub-cell relief, faking a trend). Roughness
    is the WITHIN-window scatter (median per-window MAD) so real between-window change is not mistaken for it.
    Returns (confidence, level, why, components); sub-scores and raw values are exposed for the UI."""
    clamp = lambda v: max(0.0, min(1.0, v))
    s_rough = clamp(1.0 - roughness / 1.5)      # <=0 m smooth -> 1 ; >=1.5 m rough -> 0
    s_epochs = clamp((n_bins - 3) / 4.0)        # 3 windows -> 0 ; 7+ -> 1
    s_span = clamp(span / 12.0)                 # 12+ yr -> 1
    s_ref = clamp(n_ref / 30.0)                 # 30+ reference pts -> 1
    conf = 0.55 * s_rough + 0.20 * s_epochs + 0.15 * s_span + 0.10 * s_ref
    level = "high" if conf >= 0.6 else "medium" if conf >= 0.35 else "low"
    limiters = []
    if s_rough < 0.5: limiters.append(f"rough within-cell surface (scatter {roughness:.1f} m) — samples disagree at one time")
    if s_epochs < 0.5: limiters.append(f"only {n_bins} time windows")
    if s_span < 0.5: limiters.append(f"short {span:.1f}-yr baseline")
    if s_ref < 0.5: limiters.append(f"sparse reference ({n_ref} pts)")
    why = (f"{level.capitalize()} confidence — " + "; ".join(limiters[:2])) if limiters else \
          f"{level.capitalize()} confidence — smooth cell (scatter {roughness:.1f} m), {n_bins} windows over {span:.1f} yr"
    comps = {"roughness_m": round(roughness, 2), "epochs": int(n_bins), "span_yr": round(span, 1), "ref_pts": int(n_ref),
             "scores": {"roughness": round(s_rough, 2), "epochs": round(s_epochs, 2), "span": round(s_span, 2), "density": round(s_ref, 2)}}
    return round(conf, 2), level, why, comps


def candidates(doc: dict, h3_res: int = 9, delta_t: float = 1.0, ref_missions=None,
               min_bins: int = 3, common_epoch: float = 2005.0, max_candidates: int = 60) -> dict:
    recs = _load_all(doc, common_epoch)
    present = [r["mission"] for r in recs]
    ref_set = (set(ref_missions) & set(present)) if ref_missions else set(present)
    if not ref_set:
        ref_set = set(present)
    params = {"h3_res": int(h3_res), "delta_t": float(delta_t), "min_bins": int(min_bins),
              "common_epoch": common_epoch, "ref_missions": sorted(ref_set), "missions_present": present,
              "notes": "residuals about a per-cell reference plane; positions plate-motion propagated; "
                       "no inter-campaign/inter-sensor bias adjustment and no GIA correction applied"}
    if not recs:
        return {"params": params, "candidates": []}
    z0 = float(doc.get("z0") or 0.0)

    # flat table across all missions
    misi = np.concatenate([np.full(r["x"].size, i, "i2") for i, r in enumerate(recs)])
    X = np.concatenate([r["x"] for r in recs]); Y = np.concatenate([r["y"] for r in recs])
    H = np.concatenate([r["h"] for r in recs]); YR = np.concatenate([r["yr"] for r in recs])
    LAT = np.concatenate([r["lat"] for r in recs]); LON = np.concatenate([r["lon"] for r in recs])
    isref = np.array([recs[i]["mission"] in ref_set for i in misi])

    t0 = float(YR.min())
    tbin = np.floor((YR - t0) / delta_t).astype("i4")
    cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), int(h3_res))) for la, lo in zip(LAT, LON)], dtype="u8")

    order = np.argsort(cells, kind="mergesort")
    cs = cells[order]
    uniq, starts = np.unique(cs, return_index=True)
    ends = np.append(starts[1:], cs.size)

    out = []
    for u, a, b in zip(uniq, starts, ends):
        gi = order[a:b]                             # indices of points in this cell
        bins_here = tbin[gi]
        if np.unique(bins_here).size < min_bins:
            continue
        rmask = isref[gi]
        if int(rmask.sum()) < _MIN_REF_PTS:
            continue
        gx, gy, gh, mic = X[gi], Y[gi], H[gi], misi[gi]
        xc = float(gx[rmask].mean()); yc = float(gy[rmask].mean())
        A = np.column_stack([np.ones(int(rmask.sum())), gx[rmask] - xc, gy[rmask] - yc])
        try:
            coef, *_ = np.linalg.lstsq(A, gh[rmask], rcond=None)
        except Exception:
            continue
        resid = gh - (coef[0] + coef[1] * (gx - xc) + coef[2] * (gy - yc))
        med = float(np.median(resid)); mad = float(np.median(np.abs(resid - med))) or 1e-6
        good = np.abs(resid - med) <= _BLUNDER_MAD * 1.4826 * mad
        ref_resid = resid[rmask & good]                      # reference-point residuals about the plane
        plane_rms = float(1.4826 * np.median(np.abs(ref_resid - np.median(ref_resid)))) if ref_resid.size >= 3 else float(1.4826 * mad)

        series = []; rough_pool = []
        for bval in np.unique(bins_here):
            m = good & (bins_here == bval)
            if int(m.sum()) < _MIN_BIN_PTS:
                continue
            r = resid[m]; rmed = float(np.median(r))
            rough_pool.append(r - rmed)                      # within-window residuals -> pooled spatial roughness
            series.append({"year": round(float(YR[gi][m].mean()), 3), "value_m": round(rmed, 3),
                           "mad_m": round(float(np.median(np.abs(r - rmed))), 3), "n": int(m.sum()),
                           "missions": sorted({recs[j]["mission"] for j in np.unique(mic[m])})})
        if len(series) < min_bins:
            continue
        series.sort(key=lambda d: d["year"])

        hexstr = h3.int_to_str(int(u))
        bnd = h3.cell_to_boundary(hexstr)
        bx, by = scene_mod.to_local(doc["frame"], np.array([p[1] for p in bnd]), np.array([p[0] for p in bnd]))
        clat, clon = h3.cell_to_latlng(hexstr)
        cx, cy = scene_mod.to_local(doc["frame"], np.array([clon]), np.array([clat]))
        span_years = round(series[-1]["year"] - series[0]["year"], 2)
        pooled = np.concatenate(rough_pool)                          # spatial scatter after removing slope + per-window signal
        roughness = float(1.4826 * np.median(np.abs(pooled - np.median(pooled))))
        conf, level, why, comps = _confidence(roughness, len(series), span_years, int(rmask.sum()))
        out.append({"h3": hexstr, "lat": round(float(clat), 5), "lon": round(float(clon), 5),
                    "center": [round(float(cx[0]), 2), round(float(cy[0]), 2), round(float(coef[0] - z0), 2)],
                    "xy": [[round(float(px), 2), round(float(py), 2)] for px, py in zip(bx, by)],
                    "n_bins": len(series), "span_years": span_years,
                    "slope_deg": round(float(np.degrees(np.arctan(np.hypot(coef[1], coef[2])))), 3),
                    "n_points": int(gi.size), "n_ref": int(rmask.sum()),
                    "confidence": conf, "level": level, "why": why, "components": comps, "series": series})

    out.sort(key=lambda c: (c["confidence"], c["n_bins"], c["span_years"]), reverse=True)
    return {"params": params, "candidates": out[:max_candidates]}
