"""ICESat/GLAS GLAH06 (release 34) 40 Hz shot extraction over a bbox.

Native frame ITRF2008 (NSIDC, EPSG:5332). Heights are delivered on the TOPEX/Poseidon ellipsoid;
we convert to WGS84 ellipsoid heights with the product's own d_deltaEllip (T/P minus WGS84, ~0.70 m)
and apply the saturation correction d_satElevCorr, which is NOT applied in d_elev.
Row identity: (granule, i_rec_ndx, i_shot_count).
"""
from __future__ import annotations

import logging
import time
from datetime import date

import h5py
import numpy as np

from . import auth, cache, coverage
from .campaigns import campaign_for

log = logging.getLogger(__name__)

G = "Data_40HZ"
V = {
    "lat": f"{G}/Geolocation/d_lat",
    "lon": f"{G}/Geolocation/d_lon",
    "elev": f"{G}/Elevation_Surfaces/d_elev",
    "sat_corr": f"{G}/Elevation_Corrections/d_satElevCorr",
    "delta_ellip": f"{G}/Geophysical/d_deltaEllip",
    "rec_ndx": f"{G}/Time/i_rec_ndx",
    "shot": f"{G}/Time/i_shot_count",
    "time": f"{G}/DS_UTCTime_40",
    "elev_use": f"{G}/Quality/elev_use_flg",
    "sat_flag": f"{G}/Quality/sat_corr_flg",
}
J2000 = np.datetime64("2000-01-01T12:00:00", "ms")
MAX_SAT_FLAG = 2  # 0 none, 1 minor, 2 corrected; >=3 not correctable / unusable


def _read(f: h5py.File, name: str, sl=slice(None)) -> np.ndarray:
    ds = f[V[name]]
    a = ds[sl].astype("f8") if ds.dtype.kind == "f" else ds[sl]
    fill = ds.attrs.get("_FillValue")
    if fill is not None and a.dtype.kind == "f":
        a = np.where(a == np.asarray(fill).astype("f8"), np.nan, a)
    return a


def _extract_granule(f: h5py.File, bbox) -> dict[str, np.ndarray] | None:
    w, s, e, n = bbox
    lat = _read(f, "lat")
    lon = _read(f, "lon")
    lon = np.where(lon > 180, lon - 360, lon)  # GLAS delivers 0..360
    inb = (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    if not inb.any():
        return None
    i0, i1 = int(np.flatnonzero(inb).min()), int(np.flatnonzero(inb).max() + 1)
    sl = slice(i0, i1)
    keep = inb[sl]
    elev = _read(f, "elev", sl)
    sat = _read(f, "sat_corr", sl)
    dell = _read(f, "delta_ellip", sl)
    use = f[V["elev_use"]][sl]
    satf = f[V["sat_flag"]][sl]
    keep &= np.isfinite(elev) & np.isfinite(dell) & (use == 0) & (satf <= MAX_SAT_FLAG)
    if not keep.any():
        return None
    sat = np.where(np.isfinite(sat), sat, 0.0)
    h_wgs84 = elev + sat - dell
    t = J2000 + (_read(f, "time", sl)[keep] * 1000).astype("timedelta64[ms]")
    return {"lon": lon[sl][keep], "lat": lat[sl][keep], "h": h_wgs84[keep], "h_tp": elev[keep],
            "delta_ellip": dell[keep], "sat_corr": sat[keep], "t": t,
            "rec_ndx": f[V["rec_ndx"]][sl][keep].astype("i8"), "shot": f[V["shot"]][sl][keep].astype("i4")}


def _index_covers(bbox) -> bool:
    """True if the GLAS sub-granule index was built over a region that contains this bbox."""
    import json

    from . import index_glas
    mf = index_glas._index_dir(index_glas.GLAS_RES) / "_build.json"
    if not mf.exists():
        return False
    try:
        b = json.loads(mf.read_text()).get("bbox")
        w, s, e, n = bbox
        return b[0] <= w and b[1] <= s and e <= b[2] and n <= b[3]
    except Exception:
        return False


def _extract_via_index(bbox, window, polygon, k) -> tuple[dict[str, np.ndarray], dict]:
    from . import index_glas
    # clip_cells: build from the H3 cells the selection actually touches (a box gains a hex-aligned fringe; a polygon no
    # longer expands to its bounding rectangle). The precise points_in_polygon below still trims a polygon to its exact
    # shape; the win is that only the touched hexes are addressed/materialized, not the whole bbox rectangle.
    arr, st = index_glas.fetch_bbox(bbox, window=window, res=index_glas.GLAS_RES, polygon=polygon, clip_cells=True)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no usable GLAS shots over {bbox} in {window} (index)")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    days = arrays["t"].astype("datetime64[D]")
    camp: dict = {}
    for d0 in np.unique(days):
        c = campaign_for(date.fromisoformat(str(d0)))
        camp[c] = camp.get(c, 0) + int((days == d0).sum())
    meta = {"mission": "GLAS", "product": f"GLAH06 v{coverage.GLAS_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2008", "height_ref": "WGS84 ellipsoid (converted)",
            "ellipsoid_correction": "h = d_elev + d_satElevCorr - d_deltaEllip (TOPEX/Poseidon -> WGS84)",
            "n": int(arrays["lon"].size), "campaigns": dict(sorted(camp.items())), "max_sat_flag": MAX_SAT_FLAG,
            "source": "sub-granule H3 index (byte-range)", "access": st, "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("glas via index: %d shots, %d GETs, %.1f MB", arrays["lon"].size, st.get("requests", 0), st.get("bytes", 0) / 1e6)
    return arrays, meta


def extract(bbox, window, max_granules: int = 400, polygon=None) -> tuple[dict[str, np.ndarray], dict]:
    k = cache.key("glas", coverage.GLAS_VERSION, bbox, window, max_granules, MAX_SAT_FLAG, polygon)
    hit = cache.load(k)
    if hit:
        log.info("glas cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    if _index_covers(bbox):   # discovery path: byte-range the indexed chunks, no CMR + no whole-granule download
        return _extract_via_index(bbox, window, polygon, k)
    import earthaccess

    auth.login()
    granules = coverage.search(coverage.GLAS_SHORT_NAME, coverage.GLAS_VERSION, bbox, window)
    if not granules:
        raise RuntimeError(f"no GLAH06 granules over {bbox} in {window}")
    n_found = len(granules)
    granules = coverage.sample_evenly(granules, max_granules)
    # GLAH06 granules are ~4 MB each: bulk parallel download beats hundreds of remote opens.
    raw_dir = cache.DATA_DIR / "raw" / "glah06"
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = earthaccess.download(granules, local_path=str(raw_dir), threads=8, show_progress=False)
    parts, prov = [], []
    for path in sorted(map(str, paths)):  # download order is not guaranteed: provenance is the file itself
        name = path.rsplit("/", 1)[-1]  # already the .h5 basename
        t0 = time.time()
        with h5py.File(path, "r") as f:
            d = _extract_granule(f, bbox)
        if d is None:
            log.info("%s: no usable shots in bbox", name)
            continue
        d["granule_idx"] = np.full(d["lon"].size, len(prov), dtype="i2")
        parts.append(d)
        prov.append({"granule": name, "n": int(d["lon"].size), "seconds": round(time.time() - t0, 1)})
        log.info("%s: %d shots in bbox (%.1fs)", name, d["lon"].size, time.time() - t0)
    if not parts:
        raise RuntimeError("GLAH06 granules found but no usable shots in bbox")
    arrays = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
        if arrays["lon"].size == 0:
            raise RuntimeError("no usable GLAS shots inside the polygon")
    days = arrays["t"].astype("datetime64[D]")
    camp = {}
    for d0 in np.unique(days):
        c = campaign_for(date.fromisoformat(str(d0)))
        camp[c] = camp.get(c, 0) + int((days == d0).sum())
    meta = {"mission": "GLAS", "product": f"GLAH06 v{coverage.GLAS_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2008", "height_ref": "WGS84 ellipsoid (converted)",
            "ellipsoid_correction": "h = d_elev + d_satElevCorr - d_deltaEllip (TOPEX/Poseidon -> WGS84)",
            "mean_delta_ellip_m": float(np.nanmean(arrays["delta_ellip"])),
            "n": int(arrays["lon"].size), "n_granules_found": n_found, "n_granules_read": len(granules),
            "campaigns": dict(sorted(camp.items())), "granules": prov, "max_sat_flag": MAX_SAT_FLAG, "polygon": polygon}
    meta["cache_key"] = k
    cache.save(k, arrays, meta)
    return arrays, meta
