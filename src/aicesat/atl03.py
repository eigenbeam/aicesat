"""ICESat-2 ATL03 (v007) photon extraction over a bbox.

Subset decisions (spec Appendix D, 1a):
  * strong beams only, chosen from /orbit_info/sc_orient (0 -> gt?l strong, 1 -> gt?r strong, 2 -> skip)
  * land-ice signal confidence signal_conf_ph[:, 3] >= MIN_CONF (3 = medium, 4 = high)
  * h_ph with lon_ph / lat_ph; clipped to bbox BEFORE concatenation
Photon index ranges are located from the 20 m segment geolocation group so only the in-bbox slice
of the (huge) photon arrays is read from the remote file.
Frame: ITRF2014 at observation epoch, WGS84 ellipsoid heights (ATL03 user guide).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np

from . import cache, coverage

log = logging.getLogger(__name__)

LAND_ICE_COL = 3
MIN_CONF = 3
BEAM_PAIRS = ("gt1", "gt2", "gt3")
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
# GPS - UTC leap seconds after 2017-01-01 (valid for the whole ICESat-2 mission so far).
GPS_UTC_LEAP = 18


def strong_beams(sc_orient: int) -> list[str]:
    if sc_orient == 0:
        return [p + "l" for p in BEAM_PAIRS]
    if sc_orient == 1:
        return [p + "r" for p in BEAM_PAIRS]
    return []


def _extract_beam(f: h5py.File, beam: str, bbox) -> dict[str, np.ndarray] | None:
    w, s, e, n = bbox
    geo = f[f"{beam}/geolocation"]
    seg_lat = geo["reference_photon_lat"][:]
    seg_lon = geo["reference_photon_lon"][:]
    in_box = (seg_lat >= s) & (seg_lat <= n) & (seg_lon >= w) & (seg_lon <= e)
    if not in_box.any():
        return None
    idx = np.flatnonzero(in_box)
    ph_beg = geo["ph_index_beg"][:]  # 1-based index of first photon of each segment (0 = none)
    ph_cnt = geo["segment_ph_cnt"][:]
    valid = idx[ph_beg[idx] > 0]
    if valid.size == 0:
        return None
    i0 = int(ph_beg[valid].min() - 1)
    i1 = int((ph_beg[valid] + ph_cnt[valid]).max() - 1)
    h = f[f"{beam}/heights"]
    lat = h["lat_ph"][i0:i1]
    lon = h["lon_ph"][i0:i1]
    hp = h["h_ph"][i0:i1]
    conf = h["signal_conf_ph"][i0:i1, LAND_ICE_COL]
    dt = h["delta_time"][i0:i1]
    keep = (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e) & (conf >= MIN_CONF)
    if not keep.any():
        return None
    ph_index = np.arange(i0, i1)[keep]
    return {"lon": lon[keep].astype("f8"), "lat": lat[keep].astype("f8"), "h": hp[keep].astype("f8"),
            "conf": conf[keep].astype("i1"), "delta_time": dt[keep].astype("f8"), "ph_index": ph_index}


def delta_time_to_utc(delta_time: np.ndarray, sdp_epoch_gps_s: float) -> np.ndarray:
    """ATL03 delta_time is seconds since the ATLAS SDP epoch (itself GPS seconds). Returns datetime64[ms] UTC."""
    gps_s = delta_time + sdp_epoch_gps_s
    utc = GPS_EPOCH + timedelta(seconds=-GPS_UTC_LEAP)
    base = np.datetime64(utc.replace(tzinfo=None), "ms")
    return base + (gps_s * 1000).astype("timedelta64[ms]")


def extract(bbox, window, force: bool = False, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    """Index-driven path (spec §4–§8): planner makes the lake sufficient, DuckDB answers. Same output contract as before.
    polygon: optional [(lon, lat), ...]; bbox must then be its bounding box (see geom.normalize_area)."""
    from . import geom, lake, planner

    k = cache.key("atl03-lake", coverage.ATL03_VERSION, bbox, window, MIN_CONF, polygon)
    hit = cache.load(k)
    if hit and not force:
        log.info("atl03 cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    plan = planner.ensure(bbox, window, force=force, polygon=polygon)
    q = lake.query_photons(bbox, plan["cells"], MIN_CONF, granules=plan["granules"])
    glist = q.pop("_granules")
    arrays = {key: v for key, v in q.items()}
    if polygon is not None:
        keep = geom.points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
    n = arrays["lon"].size
    if n == 0:
        raise RuntimeError("lake query returned no land-ice signal photons in bbox")
    meta = {"mission": "ICESAT2", "product": f"ATL03 v{coverage.ATL03_VERSION}", "bbox": list(bbox), "window": list(window),
            "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid", "n_total_in_bbox": int(n), "n": int(n), "min_conf": MIN_CONF,
            "granules": [{"granule": g} for g in glist], "beam_pairs": list(BEAM_PAIRS), "access_path": "index+byte-range+lake",
            "access": plan["stats"], "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    return arrays, meta
