"""Candidate coincident-observation cells and their elevation time series.

For a built scene: bin every mission's points into H3 cells and fixed time windows, keep cells observed
in >= min_bins distinct windows, remove each cell's surface slope with a fixed-effects plane (one level per
(window, mission) group, one shared slope from the spread WITHIN each group, so change never becomes slope), and
report each window's median residual as a height anomaly -> a time series, zeroed at the reference mission's level
(ATL06 by default, see _reference_set). Positions are first propagated to a common epoch (plate motion).

Deliberately NOT applied (and surfaced to the user): inter-campaign / inter-sensor bias adjustment and
GIA. The plane-fit is what keeps slope from masquerading as elevation change; a raw median-per-window
would be dominated by which part of a sloped cell each epoch happened to sample."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import h3

from . import coreg
from . import scene as scene_mod
from .access import pool_size

log = logging.getLogger("aicesat.timeseries")

MISSION_LABEL = {"GLAS": "ICESat-1", "ICESSN": "IceBridge ATM", "ATL06": "ICESat-2 land ice",
                 "ICESAT2": "ICESat-2 photons", "GEDI": "GEDI", "GPSTRUTH": "Summit GPS traverse"}
_MIN_BIN_PTS = 3                # a time window needs this many points in the cell to be a usable series point
_MIN_REF_PTS = 6               # floor for estimating the plane's residual scatter at all -- NOT a trust measure: a count
                               # cannot tell 20 points spread across a cell from 20 on one straight track (see below)
PLANE_ERR_GATE_M = 1.0         # slope-removal error (m) at a window's samples above which a cell is never better than
                               # "low". Set from the error budget, not from which cells pass: the instruments measure
                               # smooth ice to ~0.1-0.3 m, so past ~1 m the geometry term dominates the window's error.
                               # (Across 855 res-8 Jakobshavn cells: median 0.29 m, p90 1.5 m, single-track planes
                               # 500-4000 m; count gates passed collinear 14-21-point planes.)
_GATED_CONF = 0.34             # just under "medium", so a gated cell also ranks below every cell that passed
_BLUNDER_MAD = 6.0            # drop points beyond this many (scaled) MADs from their OWN time window's median
DEFAULT_REF_MISSION = "ATL06"  # densest single sensor, today's surface: the zero, and where a cell qualifies (see below)
# Parallelism for the per-cell reference-plane fit + robust stats (see candidates). Below this many cells the loop is
# left serial; AICESAT_TS_WORKERS overrides the worker count.
_CANDIDATE_MIN_CELLS = 64
_CANDIDATE_WORKER_CAP = 8


def _reference_set(ref_missions, present) -> set:
    """The reference missions: their level reads zero, and a cell qualifies only with >= _MIN_REF_PTS of their
    points. Explicit choice wins when any of it is in the scene; otherwise ATL06 if present, else every mission.
    With the fixed-effects plane the reference no longer tilts the slope (every mission's within-window spread fits
    it), so the choice sets the zero and which cells qualify. ATL06 by default: the densest single sensor and the
    present-day surface, so residuals read as 'height relative to today's surface' and a cell qualifies wherever
    ICESat-2 measured it -- the same choice as the change map and elevation_change (api.CHANGE_REF). Until
    2026-10-01 the default was GLAS (when one plane was fitted to the reference points alone)."""
    present = set(present)
    chosen = (set(ref_missions) & present) if ref_missions else set()
    if chosen:
        return chosen
    return {DEFAULT_REF_MISSION} if DEFAULT_REF_MISSION in present else present


def _propagate_series(arrays: dict, lon, lat, h, yr, common_epoch: float, native: str):
    """Propagate one series to the common epoch. Returns (lon, lat, h, note): `note` is None when every point was
    propagated, else which points could not be and why (those stay at their observed positions).

    Rows carrying `itrf_year` go through their own realization, because one ICESSN extract spans campaigns in
    different frames (ITRF2005 for 2011, ITRF2008 for 2012-2016, ITRF2014 from 2017). Year 0 means the granule
    header named no frame, which cannot be propagated and is reported as such."""
    years = arrays.get("itrf_year")
    if years is None:
        try:
            plon, plat, ph = coreg.propagate(lon, lat, h, yr, common_epoch, native)
            return plon, plat, ph, None
        except Exception as e:
            return lon, lat, h, f"{native}: {e}"
    years = np.asarray(years)
    lon, lat, h = lon.copy(), lat.copy(), h.copy()
    failed = []
    for y in np.unique(years):
        m = years == y
        frame = f"ITRF{int(y)}" if y else "no frame in the granule header"
        try:
            lon[m], lat[m], h[m] = coreg.propagate(lon[m], lat[m], h[m], yr[m], common_epoch, frame)
        except Exception as e:
            failed.append(f"{int(m.sum())} points in {frame}: {e}")
    return lon, lat, h, ("; ".join(failed) or None)


def _load_all(doc: dict, common_epoch: float) -> list[dict]:
    """Reload every scene series' full arrays (lon/lat/h/t), propagate to the common epoch, project to
    the scene's local frame. Returns one record per mission with x/y/h/yr arrays, plus `propagated` and, when it is
    False, `frame_note` saying which points were left at their observed positions and why. A failure used to be a
    log line only, so an unpropagated series (every IceBridge point, while its frame label was vague) looked the same
    in the output as a propagated one."""
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
        # Every point is binned. A linspace thin to 80k used to live here, justified by the median being robust to
        # subsampling — true of the median, false of the COUNTS around it: _MIN_BIN_PTS, _MIN_REF_PTS and the n_ref
        # term in _confidence are thresholds, so thinning moved which cells qualified as candidates at all.
        native = meta.get("native_frame", "ITRF2014")
        lon, lat, h, note = _propagate_series(arrays, lon, lat, h, yr, common_epoch, native)
        if note:
            log.warning("%s not fully plate-motion propagated (observed positions kept): %s", mission, note)
        x, y = scene_mod.to_local(frame, lon, lat)
        # Surface slopes the PRODUCT measured, per point (ILATM2 platelet plane fits: dz/dnorth, dz/deast), when the
        # series carries them -- sample_geometry sets them beside the slopes our own fits imply.
        sn, we = arrays.get("sn_slope"), arrays.get("we_slope")
        same = sn is not None and we is not None and len(sn) == len(h) and len(we) == len(h)
        recs.append({"mission": mission, "lat": np.asarray(lat, "f8"), "lon": np.asarray(lon, "f8"),
                     "x": np.asarray(x, "f8"), "y": np.asarray(y, "f8"), "h": np.asarray(h, "f8"), "yr": np.asarray(yr, "f8"),
                     "sn": np.asarray(sn, "f8") if same else None, "we": np.asarray(we, "f8") if same else None,
                     "propagated": note is None, "frame_note": note})
    return recs


# The product fields a measured slope comes from, per mission (GLAH06 carries none; ATL06's dh_fit_dx/dy are not yet
# carried by our index -- issue #19).
SLOPE_SOURCE = {"ICESSN": "ILATM2 platelet plane fits (sn_slope, we_slope)", "ATL06": "ATL06 dh_fit_dx / dh_fit_dy"}
_MIN_SPREAD_M = 25.0   # a mission's points must spread at least this far across their narrowest direction to imply a slope


def _plane(x, y, h):
    xc, yc = float(x.mean()), float(y.mean())
    coef, *_ = np.linalg.lstsq(np.column_stack([np.ones(x.size), x - xc, y - yc]), h, rcond=None)
    return coef, xc, yc


def _slope_row(gx: float, gy: float) -> dict:
    return {"dh_dx_m_per_km": round(gx * 1000, 1), "dh_dy_m_per_km": round(gy * 1000, 1),
            "slope_deg": round(float(np.degrees(np.arctan(np.hypot(gx, gy)))), 2)}


def sample_geometry(doc: dict, h3_cell: str, reported_series: list, delta_t: float = 1.0,
                    common_epoch: float = 2005.0) -> dict:
    """Why a cell's series needs its sample geometry, in numbers.

    The missions sample different spots inside a cell, on sloping ground. This says where each one sampled (centroid
    offsets from the cell's sample centroid), what slope its own points imply, what slope the product itself measured
    where it carries one, and what the first-to-last change would read two naive ways -- differencing raw heights
    (ignoring positions), and removing one plane fitted across all eras (which mistakes change for slope) -- beside
    the change reported with a single-era reference plane."""
    recs = _load_all(doc, common_epoch)
    res, target = h3.get_resolution(h3_cell), h3.str_to_int(h3_cell)
    parts = [(r, m) for r in recs for m in [_cells_of(r["lat"], r["lon"], res) == target] if m.any()]
    if not parts:
        raise ValueError(f"no points in cell {h3_cell}")
    t0 = min(float(r["yr"].min()) for r in recs if r["yr"].size)
    frame = doc["frame"]
    E = np.asarray(frame.get("east_xy") or [1.0, 0.0], "f8"); N = np.asarray(frame.get("north_xy") or [0.0, 1.0], "f8")
    X = np.concatenate([r["x"][m] for r, m in parts]); Y = np.concatenate([r["y"][m] for r, m in parts])
    H = np.concatenate([r["h"][m] for r, m in parts]); YR = np.concatenate([r["yr"][m] for r, m in parts])
    cx, cy = float(X.mean()), float(Y.mean())

    missions, measured, centroids = {}, {}, {}
    for r, m in parts:
        x, y, hh = r["x"][m], r["y"][m], r["h"][m]
        # The slope a mission's points imply, from ONE time window: fitted across its own epochs, the change between
        # them becomes slope wherever its tracks moved (GLAS 2004-09 read 13 deg on a cell ICESat-2 measured at 2.9).
        # And only from points spread across the cell both ways -- one track cannot say how the ground slopes across
        # it. None says "not measurable from this mission's samples here".
        fitted = None
        wy = np.floor((r["yr"][m] - t0) / delta_t).astype(int)
        for w in sorted(np.unique(wy), key=lambda w: -int((wy == w).sum())):
            k = wy == w
            if int(k.sum()) < _MIN_REF_PTS:
                break
            xw, yw = x[k], y[k]
            minor = float(np.sqrt(max(np.linalg.eigvalsh(np.cov(np.c_[xw - xw.mean(), yw - yw.mean()].T))[0], 0.0)))
            if minor >= _MIN_SPREAD_M:
                coef, _, _ = _plane(xw, yw, hh[k])
                fitted = {**_slope_row(float(coef[1]), float(coef[2])), "window_year": int(r["yr"][m][k].mean()),
                          "n": int(k.sum())}
                break
        centroids[r["mission"]] = (float(x.mean()), float(y.mean()))
        missions[r["mission"]] = {"n": int(x.size), "centroid_offset_m": [round(float(x.mean()) - cx), round(float(y.mean()) - cy)],
                                  "fitted_slope": fitted}
        if r.get("sn") is not None and r.get("we") is not None:
            s_, w_ = r["sn"][m], r["we"][m]
            ok = np.isfinite(s_) & np.isfinite(w_)
            if ok.any():
                gs, gw = float(np.median(s_[ok])), float(np.median(w_[ok]))   # dz/dnorth, dz/deast -> local x/y
                measured[r["mission"]] = {"n": int(ok.sum()), **_slope_row(gw * E[0] + gs * N[0], gw * E[1] + gs * N[1]),
                                          "source": SLOPE_SOURCE.get(r["mission"], "product slope fields")}

    tb = np.floor((YR - t0) / delta_t).astype(int)

    def change(v):
        med = [float(np.median(v[tb == w])) for w in np.unique(tb) if int((tb == w).sum()) >= _MIN_BIN_PTS]
        return round(med[-1] - med[0], 1) if len(med) >= 2 else None

    coef, xc, yc = _plane(X, Y, H)
    joint = change(H - (coef[0] + coef[1] * (X - xc) + coef[2] * (Y - yc)))
    naive = change(H)
    reported = round(reported_series[-1]["value_m"] - reported_series[0]["value_m"], 1)
    pts = list(centroids.values())
    sep = max((float(np.hypot(a[0] - b[0], a[1] - b[1])) for a in pts for b in pts), default=0.0)
    biggest = max(parts, key=lambda p: int(p[1].sum()))[0]["mission"]
    slope = (missions[biggest]["fitted_slope"] or {}).get("slope_deg")
    ground = f" on ground sloping about {slope:.1f} deg" if slope is not None else ""
    fmt = lambda v: "n/a" if v is None else f"{v:+.1f} m"
    explanation = (f"The missions sampled different parts of this cell -- their sample centres are up to {sep:.0f} m "
                   f"apart{ground}. Differencing the raw heights would read {fmt(naive)}, and one plane fitted across "
                   f"all eras {fmt(joint)}; taking the slope only from each year's own samples gives {fmt(reported)}.")
    return {"missions": missions, "measured_slope": measured,
            "change_m": {"reported": reported, "ignoring_positions": naive, "one_plane_across_all_eras": joint},
            "explanation": explanation}


def _cells_of(lat, lon, res) -> np.ndarray:
    """H3 cell (u8) of every point. Vectorized (matches index_atl06): it re-runs on every h3_res/delta_t sweep."""
    try:
        from h3ronpy.vector import coordinates_to_cells
        return np.asarray(coordinates_to_cells(np.asarray(lat, "f8"), np.asarray(lon, "f8"), int(res)), dtype="u8")
    except Exception:
        return np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), int(res))) for la, lo in zip(lat, lon)],
                        dtype="u8")


def _confidence(roughness: float, n_bins: int, span: float, n_ref: int, plane_err_max: float = 0.0) -> tuple:
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
    # Hard gate on the quality of the EVIDENCE, never on the size of the answer. The weighted score let maxed-out
    # epochs and span carry 8806f200d3fffff (res 8, ref ATL06: 8 collinear plane points, -263 m/yr) up to "medium".
    # What made it garbage is not its count or its roughness but where its plane was ASKED to predict: the other
    # windows' samples sat off the one track the plane was fitted to, ~3.6 km of prediction error.
    gated = []
    if not plane_err_max <= PLANE_ERR_GATE_M:          # also catches NaN / inf from a singular reference
        gated.append(f"slope removal uncertain by {plane_err_max:.1f} m where some windows sampled: they lie outside "
                     "the ground the reference points cover, or those points fall on a single track")
    if gated:
        conf = min(conf, _GATED_CONF)
    level = "high" if conf >= 0.6 else "medium" if conf >= 0.35 else "low"
    limiters = []
    if s_rough < 0.5: limiters.append(f"rough within-cell surface (scatter {roughness:.1f} m) — samples disagree at one time")
    if s_epochs < 0.5: limiters.append(f"only {n_bins} time windows")
    if s_span < 0.5: limiters.append(f"short {span:.1f}-yr baseline")
    if s_ref < 0.5: limiters.append(f"sparse reference ({n_ref} pts)")
    if gated:
        why = "Low confidence — gated: " + "; ".join(gated)
    elif limiters:
        why = f"{level.capitalize()} confidence — " + "; ".join(limiters[:2])
    else:
        why = f"{level.capitalize()} confidence — smooth cell (scatter {roughness:.1f} m), {n_bins} windows over {span:.1f} yr"
    comps = {"roughness_m": round(roughness, 2), "epochs": int(n_bins), "span_yr": round(span, 1), "ref_pts": int(n_ref),
             "scores": {"roughness": round(s_rough, 2), "epochs": round(s_epochs, 2), "span": round(s_span, 2), "density": round(s_ref, 2)},
             "plane_err_max_m": round(float(plane_err_max), 3) if np.isfinite(plane_err_max) else None, "gated": gated}
    return round(conf, 2), level, why, comps


def _trend_cm_yr(series) -> float:
    """Unweighted least-squares rate through the window medians, in cm/yr.

    Ported from the UI's linfit so the rate a caller is handed and the rate the chart prints are the same number
    computed once, not two implementations that can drift. It inherits every caveat in `params["notes"]`: no
    inter-campaign / inter-sensor bias adjustment and no GIA, so a mission changeover mid-series biases it."""
    if len(series) < 2:
        return 0.0
    x = np.array([p["year"] for p in series], "f8")
    y = np.array([p["value_m"] for p in series], "f8")
    dx = x - x.mean()
    sxx = float((dx * dx).sum())
    if sxx <= 0.0:                       # every window landed on the same year: no baseline, no rate
        return 0.0
    return round(100.0 * float((dx * (y - y.mean())).sum()) / sxx, 2)


def candidates(doc: dict, h3_res: int = 8, delta_t: float = 1.0, ref_missions=None,
               min_bins: int = 3, common_epoch: float = 2005.0) -> dict:
    recs = _load_all(doc, common_epoch)
    present = [r["mission"] for r in recs]
    ref_set = _reference_set(ref_missions, present)
    not_propagated = {r["mission"]: r.get("frame_note") for r in recs if not r.get("propagated", True)}
    propagation = ("positions plate-motion propagated" if not not_propagated else
                   "positions plate-motion propagated EXCEPT " + ", ".join(sorted(not_propagated)) +
                   " (observed positions used; see not_propagated)")
    params = {"h3_res": int(h3_res), "delta_t": float(delta_t), "min_bins": int(min_bins),
              "common_epoch": common_epoch, "ref_missions": sorted(ref_set), "missions_present": present,
              "not_propagated": not_propagated,
              "notes": "heights about a per-cell surface plane whose slope comes only from the spread of samples "
                       "within each (year, mission) group, so change between years is never read as slope; "
                       f"ref_missions set which level reads 0; {propagation}; "
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
    cells = _cells_of(LAT, LON, h3_res)

    order = np.argsort(cells, kind="mergesort")
    cs = cells[order]
    uniq, starts = np.unique(cs, return_index=True)
    ends = np.append(starts[1:], cs.size)

    # The cells are independent; each one is a reference-plane lstsq fit + robust MAD stats + confidence. pyproj's
    # cached Transformer (via scene_mod.to_local) is NOT thread-safe, so its two tiny per-cell transforms are guarded
    # by this lock — everything heavy (lstsq, the MAD medians, the h3 boundary) runs unlocked in parallel.
    proj_lock = threading.Lock()

    def _fit_cell(u, a, b):
        """Compute one cell's candidate (or None to drop it). Byte-for-byte the serial body — only which worker runs
        it changes. Reads shared arrays read-only; builds nothing shared except under proj_lock."""
        gi = order[a:b]                             # indices of points in this cell
        bins_here = tbin[gi]
        if np.unique(bins_here).size < min_bins:
            return None
        rmask = isref[gi]
        if int(rmask.sum()) < _MIN_REF_PTS:
            return None
        gx, gy, gh, mic = X[gi], Y[gi], H[gi], misi[gi]
        # The surface slope, from WITHIN-window spatial spread only: a fixed-effects plane, one level per (time
        # window, mission) group and one shared slope. Any single plane fitted across windows -- all missions, or even
        # one mission's own years -- turns the change between them into slope wherever the passes moved, and its
        # residual scatter then grows with the change (the gate read the answer: same geometry, 0 -> -3 m/yr went
        # high -> gated). With a level per group, change cannot enter the slope or the error estimate.
        xc, yc = float(gx.mean()), float(gy.mean())
        dx, dy = gx - xc, gy - yc
        _, inv = np.unique(bins_here.astype("i8") * 64 + mic.astype("i8"), return_inverse=True)
        cnt = np.bincount(inv).astype("f8")
        tx = dx - (np.bincount(inv, dx) / cnt)[inv]
        ty = dy - (np.bincount(inv, dy) / cnt)[inv]
        th = gh - (np.bincount(inv, gh) / cnt)[inv]
        normal = np.array([[tx @ tx, tx @ ty], [tx @ ty, ty @ ty]])
        try:
            slope_inv = np.linalg.inv(normal)                # near-singular (every pass on one line) -> huge, as it should be
            sb, sc = slope_inv @ np.array([tx @ th, ty @ th])
        except np.linalg.LinAlgError:
            slope_inv, sb, sc = None, 0.0, 0.0
        within = th - sb * tx - sc * ty                      # residual inside each group: carries no change at all
        resid = gh - (sb * dx + sc * dy)                     # slope removed, every window's change kept
        level0 = float(np.median(resid[rmask]))              # the reference missions' level reads 0
        resid = resid - level0
        coef = (level0, sb, sc)
        # Blunder clip: distance from the point's OWN time window's median, scaled by the pooled within-window
        # scatter. Clipping about the cell-wide median would drop real change between windows (or a whole
        # inter-sensor offset) once it exceeds a few MADs of the scatter -- with a single-epoch reference that
        # silently removed the reference epoch itself from the series.
        dev = np.empty_like(resid)
        for bval in np.unique(bins_here):
            m = bins_here == bval
            dev[m] = resid[m] - np.median(resid[m])
        scale = float(1.4826 * np.median(np.abs(dev))) or 1e-6
        good = np.abs(dev) <= _BLUNDER_MAD * scale
        wg = within[good]                                    # noise about the plane inside each group (no change in it)
        plane_rms = float(1.4826 * np.median(np.abs(wg - np.median(wg)))) if wg.size >= 3 else scale

        series = []; rough_pool = []
        for bval in np.unique(bins_here):
            m = good & (bins_here == bval)
            if int(m.sum()) < _MIN_BIN_PTS:
                continue
            r = resid[m]; rmed = float(np.median(r))
            rough_pool.append(r - rmed)                      # within-window residuals -> pooled spatial roughness
            # How wrong the slope removal can be WHERE THIS WINDOW SAMPLED: the slope's uncertainty projected on how far
            # the window's samples sit from the cell's sample centre. Large only when windows are offset in a direction
            # no window's own spread constrains (every pass on one line, and the passes moved across it).
            pv = np.array([float(dx[m].mean()), float(dy[m].mean())])
            perr = float(plane_rms * np.sqrt(max(float(pv @ slope_inv @ pv), 0.0))) if slope_inv is not None else float("inf")
            series.append({"year": round(float(YR[gi][m].mean()), 3), "value_m": round(rmed, 3),
                           "mad_m": round(float(np.median(np.abs(r - rmed))), 3), "n": int(m.sum()),
                           "plane_err_m": round(min(perr, 1e6), 3),
                           "missions": sorted({recs[j]["mission"] for j in np.unique(mic[m])})})
        if len(series) < min_bins:
            return None
        series.sort(key=lambda d: d["year"])

        hexstr = h3.int_to_str(int(u))
        bnd = h3.cell_to_boundary(hexstr)
        clat, clon = h3.cell_to_latlng(hexstr)
        with proj_lock:                                      # pyproj Transformer is not thread-safe; transforms are tiny
            bx, by = scene_mod.to_local(doc["frame"], np.array([p[1] for p in bnd]), np.array([p[0] for p in bnd]))
            cx, cy = scene_mod.to_local(doc["frame"], np.array([clon]), np.array([clat]))
        span_years = round(series[-1]["year"] - series[0]["year"], 2)
        pooled = np.concatenate(rough_pool)                          # spatial scatter after removing slope + per-window signal
        roughness = float(1.4826 * np.median(np.abs(pooled - np.median(pooled))))
        conf, level, why, comps = _confidence(roughness, len(series), span_years, int(rmask.sum()),
                                              max(p["plane_err_m"] for p in series))
        return {"h3": hexstr, "lat": round(float(clat), 5), "lon": round(float(clon), 5),
                "center": [round(float(cx[0]), 2), round(float(cy[0]), 2), round(float(coef[0] - z0), 2)],
                "xy": [[round(float(px), 2), round(float(py), 2)] for px, py in zip(bx, by)],
                "n_bins": len(series), "span_years": span_years, "trend_cm_yr": _trend_cm_yr(series),
                "slope_deg": round(float(np.degrees(np.arctan(np.hypot(coef[1], coef[2])))), 3),
                "n_points": int(gi.size), "n_ref": int(rmask.sum()),
                "confidence": conf, "level": level, "why": why, "components": comps, "series": series}

    triples = list(zip(uniq, starts, ends))
    nw = pool_size(len(triples), cap=_CANDIDATE_WORKER_CAP, min_items=_CANDIDATE_MIN_CELLS, env="AICESAT_TS_WORKERS")
    if nw == 1:
        results = [_fit_cell(u, a, b) for u, a, b in triples]
    else:                                                    # ex.map preserves input (uniq) order -> deterministic
        with ThreadPoolExecutor(nw) as ex:
            results = list(ex.map(lambda t: _fit_cell(*t), triples))
    out = [c for c in results if c is not None]               # same cells, same order the serial loop appended them

    out.sort(key=lambda c: (c["confidence"], c["n_bins"], c["span_years"]), reverse=True)
    return {"params": params, "candidates": out}          # every qualifying cell; ranking is the UI's to truncate
