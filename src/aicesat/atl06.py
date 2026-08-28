"""ICESat-2 ATL06 (Land Ice Height) v007 extraction over a bbox.

Per-beam land-ice segments (40 m windows posted every 20 m). `h_li` is height above the **WGS84 ellipsoid in
ITRF2014** — the same frame and applied-correction set as ATL03 (solid-earth + pole tides, ocean loading, atmospheric
delay applied; ocean tide + DAC NOT applied), so the ITRF2014 plate-motion co-registration carries over unchanged and
no ellipsoid conversion is needed. Strong beams are chosen from `/orbit_info/sc_orient`. Row identity:
(granule, beam, segment_id).

Granules are ~100 MB (full RGT), so we read them remotely with earthaccess.open (fsspec chunk cache) and slice the
`land_ice_segments` of the strong beams — far cheaper than downloading whole files.
"""
from __future__ import annotations

import logging
import time

import h5py
import numpy as np

from . import auth, cache, coverage

log = logging.getLogger(__name__)

ATLAS_EPOCH = np.datetime64("2018-01-01T00:00:00", "ms")   # ATLAS SDP epoch; delta_time is seconds after it
FILL = 3.0e38                                              # h_li / lat / lon fill ~ 3.4028235e38
QUALITY_GOOD = 0                                           # atl06_quality_summary == 0 -> best


def _strong_beams(sc_orient) -> list[str]:
    v = int(np.asarray(sc_orient).ravel()[0])
    if v == 0:
        return ["gt1l", "gt2l", "gt3l"]     # backward orientation
    if v == 1:
        return ["gt1r", "gt2r", "gt3r"]     # forward orientation
    return []                               # sc_orient == 2: yaw-flip transition -> skip


def _extract_granule(f: h5py.File, bbox) -> dict[str, np.ndarray] | None:
    w, s, e, n = bbox
    if "orbit_info/sc_orient" not in f:
        return None
    parts = []
    for bi, b in enumerate(_strong_beams(f["orbit_info/sc_orient"][:])):
        grp = f"{b}/land_ice_segments"
        if grp not in f or "latitude" not in f[grp]:
            continue
        g = f[grp]
        lat = g["latitude"][:]
        lon = g["longitude"][:]
        keep = (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
        if not keep.any():
            continue
        h = g["h_li"][:]
        q = g["atl06_quality_summary"][:]
        keep &= (q == QUALITY_GOOD) & (h < FILL) & (lat < FILL) & (lon < FILL)
        if not keep.any():
            continue
        dt = g["delta_time"][:][keep]
        parts.append({"lon": lon[keep], "lat": lat[keep], "h": h[keep].astype("f8"),
                      "t": ATLAS_EPOCH + (dt * 1000).astype("timedelta64[ms]"),
                      "segment_id": g["segment_id"][:][keep].astype("i8"),
                      "beam": np.full(int(keep.sum()), bi, dtype="i1")})
    if not parts:
        return None
    return {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}


def _index_covers(bbox) -> bool:
    """True if the ATL06 sub-granule index was built over a region that contains this bbox (full-record window)."""
    import json

    from . import index_atl06
    mf = index_atl06._index_dir(index_atl06.ATL06_RES) / "_build.json"
    if not mf.exists():
        return False
    try:
        b = json.loads(mf.read_text()).get("bbox")
        w, s, e, n = bbox
        return b[0] <= w and b[1] <= s and e <= b[2] and n <= b[3]
    except Exception:
        return False


def extract(bbox, window, max_granules: int = 20, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    k = cache.key("atl06", coverage.ATL06_VERSION, bbox, window, max_granules, polygon)
    hit = cache.load(k)
    if hit:
        log.info("atl06 cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    # Index-only: byte-range fetch just the chunks whose H3 cell touches the bbox. The sub-granule index is
    # always built for the area of interest first, so there is no whole-granule fallback.
    if not _index_covers(bbox):
        raise RuntimeError(f"ATL06 not indexed over {bbox} \u2014 build the sub-granule index first (scripts/build_atl06_index.py)")
    from . import index_atl06
    arr, st = index_atl06.fetch_bbox(bbox, window=window, res=index_atl06.ATL06_RES)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no ATL06 segments over {bbox} in {window}")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    meta = {"mission": "ATL06", "product": f"ATL06 v{coverage.ATL06_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (h_li native WGS84 ellipsoid, ITRF2014)",
            "quality_filter": "atl06_quality_summary == 0", "n": int(arrays["lon"].size),
            "source": "sub-granule H3 index (byte-range)", "access": st, "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("atl06 via index: %d segments, %d GETs, %.1f MB", arrays["lon"].size, st.get("requests", 0), st.get("bytes", 0) / 1e6)
    return arrays, meta
