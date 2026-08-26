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


def extract(bbox, window, max_granules: int = 20, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    k = cache.key("atl06", coverage.ATL06_VERSION, bbox, window, max_granules, polygon)
    hit = cache.load(k)
    if hit:
        log.info("atl06 cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    import earthaccess

    auth.login()
    granules = coverage.search(coverage.ATL06_SHORT_NAME, coverage.ATL06_VERSION, bbox, window)
    if not granules:
        raise RuntimeError(f"no ATL06 granules over {bbox} in {window}")
    n_found = len(granules)
    granules = granules[:max_granules]
    files = earthaccess.open(granules)   # fsspec file objects; h5py reads only the chunks a small bbox touches
    parts, prov = [], []
    for gr, fh in zip(granules, files):
        name = coverage.granule_name(gr)
        t0 = time.time()
        try:
            with h5py.File(fh, "r") as f:
                d = _extract_granule(f, bbox)
        except Exception as ex:
            log.warning("%s: ATL06 read failed: %s", name, ex)
            continue
        if d is None:
            log.info("%s: no good ATL06 segments in bbox", name)
            continue
        d["granule_idx"] = np.full(d["lon"].size, len(prov), dtype="i2")
        parts.append(d)
        prov.append({"granule": name, "n": int(d["lon"].size), "seconds": round(time.time() - t0, 1)})
        log.info("%s: %d ATL06 segments in bbox (%.1fs)", name, d["lon"].size, time.time() - t0)
    if not parts:
        raise RuntimeError("ATL06 granules found but no good segments in bbox")
    arrays = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
        if arrays["lon"].size == 0:
            raise RuntimeError("no ATL06 segments inside the polygon")
    meta = {"mission": "ATL06", "product": f"ATL06 v{coverage.ATL06_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (h_li native WGS84 ellipsoid, ITRF2014; same applied corrections as ATL03)",
            "quality_filter": "atl06_quality_summary == 0", "n": int(arrays["lon"].size),
            "n_granules_found": n_found, "n_granules_read": len(granules), "granules": prov, "polygon": polygon}
    meta["cache_key"] = k
    cache.save(k, arrays, meta)
    return arrays, meta
