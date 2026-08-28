"""ICESat/GLAS GLAH06 addressing index — the ATL06 sub-granule pattern (index_atl06.py) applied to 40 Hz shots.

GLAH06 is flat (one `Data_40HZ` group, no beams) and gzip-chunked exactly like ATL06's land-ice segments, so the
same chunk-manifest / byte-range / decode machinery applies with the beam dimension dropped. Index-only: at build we
read each granule's structure + the shot lat/lon (to assign H3 cells) once; the elevation/time/quality bulk is fetched
later, on demand, by byte range — no whole-granule downloads at query time, and no CMR round trip (the index is the
granule-discovery layer).

GLAH06 granule names do NOT encode the date (unlike ATL06), so we store the granule's start date for window filtering.
Height is reconstructed as in glas.py: h_wgs84 = d_elev + d_satElevCorr - d_deltaEllip (TOPEX/Poseidon -> WGS84).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import h3
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from .index import _chunk_manifest, _filters   # reuse the ATL03/ATL06 HDF5 helpers verbatim

log = logging.getLogger(__name__)

GLAS_RES = 5   # match ATL06 (the two are coregistered/compared): a query cell maps to the same index cells for both.
GLAS_INDEX_VERSION = "1"
GLAS_INDEX_DIR = cache.DATA_DIR / "index" / "glas"
J2000 = np.datetime64("2000-01-01T12:00:00", "ms")
MAX_SAT_FLAG = 2

# key -> HDF5 path. lat/lon assign cells; the rest reconstruct height/time/quality at fetch. floats get fill->NaN.
GLAS_DATASETS = (
    ("lat", "Data_40HZ/Geolocation/d_lat"),
    ("lon", "Data_40HZ/Geolocation/d_lon"),
    ("elev", "Data_40HZ/Elevation_Surfaces/d_elev"),
    ("sat_corr", "Data_40HZ/Elevation_Corrections/d_satElevCorr"),
    ("delta_ellip", "Data_40HZ/Geophysical/d_deltaEllip"),
    ("time", "Data_40HZ/DS_UTCTime_40"),
    ("elev_use", "Data_40HZ/Quality/elev_use_flg"),
    ("sat_flag", "Data_40HZ/Quality/sat_corr_flg"),
)
GLAS_KEYS = [k for k, _ in GLAS_DATASETS]
_FLOAT_KEYS = {"lat", "lon", "elev", "sat_corr", "delta_ellip", "time"}


def _index_dir(res: int):
    return GLAS_INDEX_DIR / f"res{res}"


def indexed_glas_granules(res: int = GLAS_RES) -> set[str]:
    out = set()
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_glas_index_version", b"").decode() == GLAS_INDEX_VERSION:
            out.add(p.stem)
    return out


def _fill(ds: h5py.Dataset) -> float:
    fv = ds.attrs.get("_FillValue")
    if fv is None:
        return float("nan")
    arr = np.asarray(fv).astype("f8").ravel()   # GLAH06 stores some _FillValue attrs as 1-element arrays
    return float(arr[0]) if arr.size else float("nan")


def _nan_fill(a: np.ndarray, fill: float) -> np.ndarray:
    """Apply GLAH06 _FillValue -> NaN for a float array (mirrors glas._read)."""
    a = a.astype("f8")
    if np.isfinite(fill):
        a = np.where(a == fill, np.nan, a)
    return a


def build_glas_index(granule, res: int = GLAS_RES, bbox=None) -> pa.Table:
    """Parse one GLAH06 granule's structure (the only time its HDF5 b-trees are read) into addressing rows.
    GLAH06 granules are long orbit arcs; pass `bbox` to index only the cells inside it (a regional index) rather
    than the whole pole-to-pole track — chunk byte ranges are unchanged, so fetch over that bbox is identical."""
    from . import access

    auth.login()
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    s3 = (granule.data_links(access="direct") or [""])[0]
    t0 = time.time()
    base_cols = ["granule", "url", "s3url", "revision", "gdate", "chunk_index", "seg_start", "seg_end",
                 "h3_cell", "lat_min", "lat_max", "lon_min", "lon_max"]
    rows: dict[str, list] = {k: [] for k in base_cols}
    for key in GLAS_KEYS:
        for suf in ("offset", "size", "filters", "dtype", "mask", "fill"):
            rows[f"{key}_{suf}"] = []

    with h5py.File(access.cloud_hdf5_file(url, s3), "r") as f:   # in-region: s3fs direct; else one presign to CloudFront
        dsets = {key: f[path] for key, path in GLAS_DATASETS}
        C = int(dsets["lat"].chunks[0])
        nchunks = dsets["lat"].id.get_num_chunks()
        for key, ds in dsets.items():
            if int(ds.chunks[0]) != C or ds.id.get_num_chunks() != nchunks:
                raise ValueError(f"{key}: chunking differs from d_lat ({ds.chunks[0]} vs {C})")
            bad = [x for x in _filters(ds).split(",") if x and x not in ("gzip", "shuffle")]
            if bad:
                raise ValueError(f"{key}: unsupported HDF5 filters {bad}")
        fills = {key: _fill(ds) for key, ds in dsets.items()}
        manifests = {key: _chunk_manifest(ds) for key, ds in dsets.items()}
        meta = {key: (_filters(ds), str(ds.dtype)) for key, ds in dsets.items()}

        lat = _nan_fill(dsets["lat"][:], fills["lat"])
        lon = _nan_fill(dsets["lon"][:], fills["lon"])
        lon = np.where(lon > 180, lon - 360, lon)   # GLAS delivers 0..360 E
        tsec = _nan_fill(dsets["time"][:1], fills["time"])   # first shot -> granule date (names carry no date)
        gdate = "00000000"
        if tsec.size and np.isfinite(tsec[0]):
            gdate = str((J2000 + int(tsec[0] * 1000) * np.timedelta64(1, "ms")).astype("datetime64[D]")).replace("-", "")

        n = int(lat.shape[0])
        seg = np.arange(n)
        ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90)
        if bbox is not None:   # regional index: keep only cells inside the build bbox (chunk ranges stay whole)
            bw, bs, be, bn = bbox
            ok &= (lat >= bs) & (lat <= bn) & (lon >= bw) & (lon <= be)
        ks = (seg[ok] // C).astype("i8")
        latok, lonok = lat[ok].astype("f8"), lon[ok].astype("f8")
        try:
            from h3ronpy.vector import coordinates_to_cells
            cells = np.asarray(coordinates_to_cells(latok, lonok, res), dtype="u8")
        except Exception:
            cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res)) for a, o in zip(latok, lonok)], dtype="u8")

        box = {}
        for k in np.unique(ks):
            m = ks == k
            box[int(k)] = (float(latok[m].min()), float(latok[m].max()), float(lonok[m].min()), float(lonok[m].max()))

        for k, cell in sorted(set(zip(ks.tolist(), cells.tolist()))):
            assert h3.is_valid_cell(h3.int_to_str(int(cell))), cell
            rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
            rows["revision"].append(str(granule["meta"].get("revision-id", ""))); rows["gdate"].append(gdate)
            rows["chunk_index"].append(int(k)); rows["seg_start"].append(int(k * C)); rows["seg_end"].append(int(min((k + 1) * C, n)))
            rows["h3_cell"].append(int(cell))
            b = box[int(k)]
            rows["lat_min"].append(b[0]); rows["lat_max"].append(b[1]); rows["lon_min"].append(b[2]); rows["lon_max"].append(b[3])
            for key in GLAS_KEYS:
                ci = manifests[key][int(k)]
                fl, dt = meta[key]
                rows[f"{key}_offset"].append(int(ci.byte_offset)); rows[f"{key}_size"].append(int(ci.size))
                rows[f"{key}_filters"].append(fl); rows[f"{key}_dtype"].append(dt)
                rows[f"{key}_mask"].append(int(ci.filter_mask)); rows[f"{key}_fill"].append(float(fills[key]))

    if not rows["granule"]:   # no data inside bbox: write a schema-matched EMPTY parquet so the granule counts as done
        d = _index_dir(res)
        ref = next(iter(d.glob("*.parquet")), None)
        if ref is None:
            return pa.table({"granule": pa.array([], type=pa.string())})   # no sibling to match the schema yet; skip
        empty = pq.read_schema(ref).empty_table()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.parquet.tmp"
        pq.write_table(empty, tmp)
        tmp.replace(d / f"{name}.parquet")
        log.info("indexed GLAS %s: 0 rows (no shots in bbox) -> empty parquet", name)
        return empty
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    tbl = tbl.replace_schema_metadata({"aicesat_glas_index_version": GLAS_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")
    log.info("indexed GLAS %s: %d (chunk,cell) rows, %.1fs (%d GETs, %.1f MB)",
             name, tbl.num_rows, time.time() - t0, 0, 0.0)
    return tbl


def fetch_bbox(bbox, window=None, res: int = GLAS_RES) -> tuple[dict, dict]:
    """Index-driven GLAH06 fetch: only the chunks whose cells touch the bbox are byte-range fetched and decoded.
    Returns (arrays, stats). `window` filters granules by their stored start date (YYYY-MM-DD)."""
    import duckdb

    from . import planner
    from .access import RangeReader, access_url, decode_chunk

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no GLAS index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res)
    w, s, e, n = bbox

    dscols = ", ".join(f"{key}_offset, {key}_size, {key}_dtype, {key}_filters, {key}_mask, {key}_fill" for key in GLAS_KEYS)
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND gdate BETWEEN '{lo}' AND '{hi}'"
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT url, s3url, chunk_index, seg_start, seg_end, {dscols} "
                           f"FROM read_parquet('{d}/*.parquet') WHERE {where}").fetchall()
    finally:
        con.close()
    empty = {k: np.array([]) for k in ("lon", "lat", "h", "t", "quality")}
    if not rows:
        return empty, {}
    cols = ["url", "s3url", "chunk_index", "seg_start", "seg_end"]
    for key in GLAS_KEYS:
        cols += [f"{key}_offset", f"{key}_size", f"{key}_dtype", f"{key}_filters", f"{key}_mask", f"{key}_fill"]
    by_url: dict[str, list] = {}
    for r in rows:
        rec = dict(zip(cols, r)); by_url.setdefault(access_url(rec["url"], rec["s3url"]), []).append(rec)

    reader = RangeReader()   # in-region: s3:// keys (S3-direct, no presign); else HTTPS presigned in one parallel pass
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])
    out = {k: [] for k in ("lon", "lat", "h", "t", "quality")}
    for url, rs in by_url.items():
        ranges, keys = [], []
        for r in rs:
            for key in GLAS_KEYS:
                ranges.append((r[f"{key}_offset"], r[f"{key}_size"])); keys.append((r["chunk_index"], key))
        raws = dict(zip(keys, reader.fetch(url, ranges)))
        for r in rs:
            seg_n = r["seg_end"] - r["seg_start"]
            dec = {}
            for key in GLAS_KEYS:
                a = decode_chunk(raws[(r["chunk_index"], key)], r[f"{key}_dtype"], r[f"{key}_filters"], 1, r[f"{key}_mask"])[:seg_n]
                dec[key] = _nan_fill(a, r[f"{key}_fill"]) if key in _FLOAT_KEYS else a
            lat, lon = dec["lat"], dec["lon"]
            lon = np.where(lon > 180, lon - 360, lon)
            elev, sat, dell = dec["elev"], dec["sat_corr"], dec["delta_ellip"]
            sat = np.where(np.isfinite(sat), sat, 0.0)
            h = elev + sat - dell
            m = (np.isfinite(elev) & np.isfinite(dell) & (dec["elev_use"] == 0) & (dec["sat_flag"] <= MAX_SAT_FLAG)
                 & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e))
            if not m.any():
                continue
            out["lon"].append(lon[m].astype("f8")); out["lat"].append(lat[m].astype("f8")); out["h"].append(h[m].astype("f8"))
            out["t"].append(J2000 + (dec["time"][m] * 1000).astype("timedelta64[ms]")); out["quality"].append(dec["elev_use"][m])
    arrays = {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}
    return arrays, reader.stats.as_dict()
