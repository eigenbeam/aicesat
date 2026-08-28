"""Operation IceBridge ATM L2 icessn (ILATM2 v2) along-track surface elevation over a bbox.

Airborne laser altimetry that fills the ICESat -> ICESat-2 gap (2009-2019). Each granule is a small CSV of
along-track "platelets"; we keep the **nadir** platelet (`track == 0`) for a clean single-line profile. `elevation`
is height above the **WGS84 ellipsoid** (m), directly comparable to ICESat-2/GLAS — no datum conversion. Longitude is
delivered 0..360 E (normalized to -180..180). Time = the filename's UTC date + the record's seconds-of-day.

Format: NSIDC ILATM2 v2, DOI 10.5067/CPRXXK3F39RV; 11 comma-delimited columns, `#` header lines:
  seconds, lat(+N/-S), lon(0..360E), elev(WGS84 m), SN_slope, WE_slope, RMS(cm), npt_used, npt_edit, distance, track.
Parser cross-checked against tsutterley/read-ATM2-icessn. Row identity: (granule, along-track index).

ATM measures **both** surface-slope components directly (`SN_slope`, `WE_slope`, rise/run, from the per-platelet
plane fit), so slope here is read rather than re-derived — same units as ATL06's dh_fit_dx/dh_fit_dy.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import numpy as np

from . import auth, cache, coverage, geom

log = logging.getLogger(__name__)

MAX_RMS_CM = 50.0          # platelets whose plane-fit RMS exceeds 0.5 m are rough/unreliable -> drop
_NAME_RE = re.compile(r"(?:ILATM2|BLATM2)_(\d{8})_(\d{6})")
# "# International Terrestrial Reference Frame: ITRF05". Case varies across campaigns (2019 granules say
# "itrf14"), and the year is written two-digit.
_ITRF_RE = re.compile(r"International\s+Terrestrial\s+Reference\s+Frame\s*:\s*ITRF\s*(\d{2,4})", re.I)
ITRF_UNKNOWN = 0           # itrf_year sentinel when the header carries no frame line


def _itrf_year(path: str) -> int:
    """The granule's ITRF realization year from its header, or ITRF_UNKNOWN.

    The frame is campaign-dependent and really does change mid-record: over the EGIG box, ILATM2 reports ITRF05
    for 2011, ITRF08 for 2012-2016 and ITRF14 from 2017 — so this must be read per granule, not assumed.
    """
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if not line.startswith("#"):
                    break                                   # header is the leading '#' block only
                m = _ITRF_RE.search(line)
                if m:
                    y = int(m.group(1))
                    if y < 100:                             # two-digit: ITRF97 -> 1997, ITRF05 -> 2005
                        y += 1900 if y >= 80 else 2000
                    return y
    except OSError as ex:
        log.debug("%s: could not read header for ITRF: %s", path, ex)
    return ITRF_UNKNOWN


def _frame_name(year: int) -> str:
    return f"ITRF{year}" if year else "ITRF (unknown; granule header carried no frame)"


def _parse_file(path: str, bbox) -> dict[str, np.ndarray] | None:
    w, s, e, n = bbox
    a = np.genfromtxt(path, comments="#", delimiter=",", dtype="f8")   # '****' fill -> NaN
    if a.ndim != 2 or a.shape[0] == 0 or a.shape[1] < 11:
        return None
    seconds, lat, lon = a[:, 0], a[:, 1], a[:, 2]
    elev, rms_cm = a[:, 3], a[:, 6]
    sn_slope, we_slope = a[:, 4], a[:, 5]   # ATM measures both slope components directly (rise/run)
    track = a[:, 10]
    lon = ((lon + 180.0) % 360.0) - 180.0                             # 0..360 E -> -180..180
    keep = (track == 0) & np.isfinite(elev) & np.isfinite(lat) & np.isfinite(lon) & (rms_cm < MAX_RMS_CM)
    keep &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    if not keep.any():
        return None
    m = _NAME_RE.search(path.rsplit("/", 1)[-1])
    if not m:
        return None
    day = datetime.strptime(m.group(1), "%Y%m%d")
    t0 = np.datetime64(day.isoformat(), "ms")
    t = t0 + (seconds[keep] * 1000).astype("timedelta64[ms]")
    return {"lon": lon[keep], "lat": lat[keep], "h": elev[keep], "t": t,
            "rms_cm": rms_cm[keep], "npt_used": a[:, 7][keep].astype("i4"),
            "sn_slope": sn_slope[keep], "we_slope": we_slope[keep],
            # Per-row, because a single extract spans campaigns in different ITRF realizations.
            "itrf_year": np.full(int(keep.sum()), _itrf_year(path), dtype="i2")}


def extract(bbox, window, max_granules: int = 12, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    k = cache.key("icessn", coverage.ICESSN_VERSION, bbox, window, max_granules, MAX_RMS_CM, polygon)
    hit = cache.load(k)
    if hit:
        log.info("icessn cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    import earthaccess

    auth.login()
    granules = coverage.search(coverage.ICESSN_SHORT_NAME, coverage.ICESSN_VERSION, bbox, window)
    if not granules:
        raise RuntimeError(f"no ILATM2 granules over {bbox} in {window}")
    n_found = len(granules)
    granules = granules[:max_granules]
    raw_dir = cache.DATA_DIR / "raw" / "ilatm2"     # small CSVs (~0.5-10 MB): download beats remote text reads
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = earthaccess.download(granules, local_path=str(raw_dir), threads=8, show_progress=False)
    parts, prov = [], []
    for path in sorted(map(str, paths)):
        name = path.rsplit("/", 1)[-1]
        t0 = time.time()
        try:
            d = _parse_file(path, bbox)
        except Exception as ex:
            log.warning("%s: ICESSN parse failed: %s", name, ex)
            continue
        if d is None:
            continue
        d["granule_idx"] = np.full(d["lon"].size, len(prov), dtype="i2")
        parts.append(d)
        prov.append({"granule": name, "n": int(d["lon"].size), "seconds": round(time.time() - t0, 2),
                     "native_frame": _frame_name(int(d["itrf_year"][0]))})
        log.info("%s: %d icessn nadir platelets in bbox", name, d["lon"].size)
    if not parts:
        raise RuntimeError("ILATM2 granules found but no usable nadir platelets in bbox")
    arrays = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
        if arrays["lon"].size == 0:
            raise RuntimeError("no ICESSN platelets inside the polygon")
    years = np.unique(arrays["t"].astype("datetime64[Y]")).astype(str).tolist()
    # One extract can span several ITRF realizations; name the single one when it is single, and refuse to pick a
    # winner when it is not (coreg.propagate needs an exact realization, so "mixed" must not look transformable).
    itrf = sorted({int(v) for v in np.unique(arrays["itrf_year"])})
    frame = (_frame_name(itrf[0]) if len(itrf) == 1
             else "ITRF (mixed: " + ", ".join(_frame_name(y) for y in itrf) + "; see itrf_year per row)")
    meta = {"mission": "ICESSN", "product": f"ILATM2 v{coverage.ICESSN_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": frame, "native_frame_years": itrf,
            "native_frame_source": "granule header 'International Terrestrial Reference Frame'",
            "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (icessn elevation native WGS84 ellipsoid)",
            "slope_source": "ILATM2 South-to-North_Slope + West-to-East_Slope (measured, per platelet plane fit)",
            "slope_deg_median": geom.slope_deg_median(arrays["sn_slope"], arrays["we_slope"]),
            "quality_filter": f"track==0 (nadir), plane-fit RMS < {MAX_RMS_CM:.0f} cm", "years": years,
            "n": int(arrays["lon"].size), "n_granules_found": n_found, "n_granules_read": len(granules),
            "granules": prov, "polygon": polygon}
    meta["cache_key"] = k
    cache.save(k, arrays, meta)
    return arrays, meta
