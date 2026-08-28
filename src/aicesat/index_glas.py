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
from concurrent.futures import ThreadPoolExecutor
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


MISSION = "GLAS"
BEAM = "na"          # GLAH06 is beamless; a fixed beam token keeps the (mission,granule,beam,chunk,cell) coverage key
_EMPTY = ("lon", "lat", "h", "t", "quality")


def _index_rows(bbox, window, res: int, polygon=None) -> tuple[list[int], list[dict]]:
    """Query the GLAS index for the (granule, chunk, cell) refs whose cell touches the bbox (+window). With `polygon`
    the touched-cell set is narrowed to the cells the polygon actually overlaps (not the whole bounding rectangle)."""
    import duckdb

    from . import planner

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no GLAS index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res, polygon=polygon)
    cols = ["granule", "url", "s3url", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    for key in GLAS_KEYS:
        cols += [f"{key}_offset", f"{key}_size", f"{key}_dtype", f"{key}_filters", f"{key}_mask", f"{key}_fill"]
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND gdate BETWEEN '{lo}' AND '{hi}'"
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT {', '.join(cols)} FROM read_parquet('{d}/*.parquet') WHERE {where}").fetchall()
    finally:
        con.close()
    return want_cells, [dict(zip(cols, r)) for r in rows]


def _decode_chunk(raws: dict, r: dict) -> dict:
    """Decode + fill-clean one chunk's FULL seg range, then reconstruct WGS84 height and time (pre-mask)."""
    from .access import decode_chunk

    seg_n = r["seg_end"] - r["seg_start"]
    dec = {}
    for key in GLAS_KEYS:
        a = decode_chunk(raws[(r["chunk_index"], key)], r[f"{key}_dtype"], r[f"{key}_filters"], 1, r[f"{key}_mask"])[:seg_n]
        dec[key] = _nan_fill(a, r[f"{key}_fill"]) if key in _FLOAT_KEYS else a
    lon = np.where(dec["lon"] > 180, dec["lon"] - 360, dec["lon"])
    sat = np.where(np.isfinite(dec["sat_corr"]), dec["sat_corr"], 0.0)
    h = dec["elev"] + sat - dec["delta_ellip"]                       # TOPEX/Poseidon -> WGS84 ellipsoid
    t = J2000 + (dec["time"] * 1000).astype("timedelta64[ms]")
    valid = (np.isfinite(dec["elev"]) & np.isfinite(dec["delta_ellip"]) & (dec["elev_use"] == 0)
             & (dec["sat_flag"] <= MAX_SAT_FLAG) & np.isfinite(dec["lat"]) & np.isfinite(lon))   # bbox-independent
    return {"lat": dec["lat"], "lon": lon, "h": h, "t": t, "quality": dec["elev_use"], "valid": valid}


def fetch_bbox(bbox, window=None, res: int = GLAS_RES, force: bool = False, clip_cells: bool = False,
               polygon=None) -> tuple[dict, dict]:
    """Lake-first index-driven GLAH06 fetch (mirrors the ATL03 planner / ATL06 path). Only chunks whose wanted cells
    are not yet materialized are byte-range fetched — missing granules fetched concurrently (per-granule pool + in-region
    S3-direct / presigned via access_url). Each fetched chunk's FULL pre-mask shots are written to the lake partitioned
    by each shot's own res-`res` cell, then read back filtered to bbox (+window via granule selection). A repeat query
    over the same/overlapping area issues zero NASA GETs. Returns (arrays, stats) with chunks_from_lake / chunks_from_nasa.

    `clip_cells` (opt-in) + `polygon`: address (and read back) by the H3 cells the selection actually touches instead of
    the rectangular bounding bbox. When True the read keeps points by cell-membership at res `res` (query_points drops
    the rectangular predicate); a `polygon` further narrows the touched-cell set to the drawn shape. Default (False,
    polygon=None) is byte-for-byte the pre-existing rectangular-bbox behaviour."""
    from . import lake
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, AccessStats, RangeReader, access_url,
                         pool_size)

    want_cells, rows = _index_rows(bbox, window, res, polygon=polygon)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {"chunks_from_lake": 0, "chunks_from_nasa": 0, "cells": len(want_cells)}
    names = sorted({r["granule"] for r in rows})
    have = set() if force else lake.ingested_chunk_cells(MISSION, names)
    chunk_cells, chunk_row = {}, {}
    for r in rows:
        k = (r["granule"], r["chunk_index"])
        chunk_cells.setdefault(k, set()).add(int(r["h3_cell"])); chunk_row.setdefault(k, r)
    todo = [k for k, cs in chunk_cells.items() if any((k[0], BEAM, k[1], c) not in have for c in cs)]
    n_lake = len(chunk_cells) - len(todo)

    reader = None
    if todo:
        reader = RangeReader()
        by_url: dict[str, list] = {}
        for k in todo:
            r = chunk_row[k]; by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
        reader.presign_all([u for u in by_url if not u.startswith("s3://")])

        def _ingest_granule(url) -> dict:
            """Fetch + decode + materialise one granule's missing chunks; return its {granule: {chunk: cells}} map.
            Parquet writes hit distinct files (pool-safe); the DuckDB mark runs serially after the pool drains."""
            rs = by_url[url]
            ranges, keys = [], []
            for r in rs:
                for key in GLAS_KEYS:
                    ranges.append((r[f"{key}_offset"], r[f"{key}_size"])); keys.append((r["chunk_index"], key))
            raws = dict(zip(keys, reader.fetch(url, ranges)))
            local: dict[str, dict] = {}
            for r in rs:
                dec = _decode_chunk(raws, r); v = dec["valid"]
                mats = {"lon": dec["lon"][v].astype("f8"), "lat": dec["lat"][v].astype("f8"),
                        "h": dec["h"][v].astype("f8"), "t": dec["t"][v], "quality": dec["quality"][v]}
                cc = lake.write_point_chunk(MISSION, r["granule"], BEAM, r["chunk_index"], mats, res, extras=("quality",))
                # mark every wanted cell of the chunk (even an all-fill one) so it is not re-fetched forever
                k = (r["granule"], r["chunk_index"])
                local.setdefault(r["granule"], {})[r["chunk_index"]] = sorted(set(cc[r["chunk_index"]]) | chunk_cells[k])
            return local

        urls = list(by_url)
        nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV)
        if nw == 1:
            parts = [_ingest_granule(u) for u in urls]
        else:
            with ThreadPoolExecutor(nw) as ex:
                parts = list(ex.map(_ingest_granule, urls))   # ex.map preserves urls order
        ingest: dict[str, dict] = {}
        for loc in parts:
            for g, cm in loc.items():
                ingest.setdefault(g, {}).update(cm)
        for g, cm in ingest.items():
            lake.mark_ingested(MISSION, g, BEAM, cm)

    arrays = lake.query_points(bbox, want_cells, MISSION, granules=names, beams=[BEAM], extra_cols=("quality",), clip_cells=clip_cells)
    evicted = lake.enforce_global_limit(protect=want_cells, reason="limit (GLAS fetch)") if reader else []  # only when the lake grew
    st = reader.stats.as_dict() if reader else AccessStats().as_dict()
    st.update({"chunks_from_lake": n_lake, "chunks_from_nasa": len(todo), "chunks_fetched": len(todo),
               "cells": len(want_cells), "evicted_for_limit": evicted, "res": res})
    return arrays, st


def _fetch_direct(bbox, window=None, res: int = GLAS_RES) -> tuple[dict, dict]:
    """Reference (pre-lake) path == integration's fetch_bbox: fetch every matching chunk, decode + height-reconstruct,
    apply the full bbox mask, concat — concurrently per granule, no lake. The golden the lake-first path is validated
    against (and for in-region byte-identity checks)."""
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, RangeReader, access_url, pool_size)

    w, s, e, n = bbox
    _want, rows = _index_rows(bbox, window, res)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {}
    chunk_row = {}
    for r in rows:
        chunk_row.setdefault((r["granule"], r["chunk_index"]), r)
    by_url: dict[str, list] = {}
    for r in chunk_row.values():
        by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
    reader = RangeReader()
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])

    def _fetch_granule(url) -> dict:
        rs = by_url[url]
        ranges, keys = [], []
        for r in rs:
            for key in GLAS_KEYS:
                ranges.append((r[f"{key}_offset"], r[f"{key}_size"])); keys.append((r["chunk_index"], key))
        raws = dict(zip(keys, reader.fetch(url, ranges)))
        local = {k: [] for k in _EMPTY}
        for r in rs:
            dec = _decode_chunk(raws, r)
            m = dec["valid"] & (dec["lat"] >= s) & (dec["lat"] <= n) & (dec["lon"] >= w) & (dec["lon"] <= e)
            if not m.any():
                continue
            local["lon"].append(dec["lon"][m].astype("f8")); local["lat"].append(dec["lat"][m].astype("f8")); local["h"].append(dec["h"][m].astype("f8"))
            local["t"].append(dec["t"][m]); local["quality"].append(dec["quality"][m])
        return local

    urls = list(by_url)
    nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV)
    if nw == 1:
        parts = [_fetch_granule(u) for u in urls]
    else:
        with ThreadPoolExecutor(nw) as ex:
            parts = list(ex.map(_fetch_granule, urls))   # ex.map preserves urls order
    out = {k: [] for k in _EMPTY}
    for loc in parts:
        for k in out:
            out[k].extend(loc[k])
    arrays = {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}
    return arrays, reader.stats.as_dict()
