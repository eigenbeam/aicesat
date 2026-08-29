"""IceBridge ATM L2 icessn (ILATM2 v2) addressing index — the sub-granule idea applied to a CSV.

ILATM2 granules are comma-delimited text with variable-length lines, so there is no HDF5 chunk to address: instead we
build a LINE-OFFSET index. At build we scan each file once, assign every nadir platelet (track == 0) to an H3 cell,
and record the byte span of the lines in each cell. At query time we byte-range GET only those spans, re-split into
whole lines, and re-parse/filter — no whole-file download, and no CMR round trip (the index is the discovery layer).

The date comes from the filename (ILATM2_YYYYMMDD_...). Height is `elevation` (WGS84 ellipsoid, no datum conversion),
kept where track == 0 and plane-fit RMS < 50 cm, exactly as icessn.py.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from .icessn import MAX_RMS_CM, _NAME_RE

log = logging.getLogger(__name__)

ICESSN_RES = 5   # match ATL06/GLAS so a query cell maps to the same index cells across missions
ICESSN_INDEX_VERSION = "1"
ICESSN_INDEX_DIR = cache.DATA_DIR / "index" / "icessn"


def _index_dir(res: int):
    return ICESSN_INDEX_DIR / f"res{res}"


def indexed_icessn_granules(res: int = ICESSN_RES) -> set[str]:
    out = set()
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_icessn_index_version", b"").decode() == ICESSN_INDEX_VERSION:
            out.add(p.stem)
    return out


def _parse_fields(ln: bytes):
    """(lat, lon_180, elev, rms, track, sn_slope, we_slope) for a data line, or None if it is a comment/short/
    unparseable line. sn_slope/we_slope are the ILATM2 platelet's plane-fit slopes (South->North, West->East;
    metres per metre) — the platelet's own orientation, used to render it as a tilted facet."""
    if not ln or ln[:1] == b"#":
        return None
    f = ln.split(b",")
    if len(f) < 11:
        return None
    try:
        lat = float(f[1]); lon = float(f[2]); elev = float(f[3]); sn = float(f[4]); we = float(f[5])
        rms = float(f[6]); track = float(f[10])
    except ValueError:
        return None
    lon = ((lon + 180.0) % 360.0) - 180.0
    return lat, lon, elev, rms, track, sn, we


def build_icessn_index(granule, res: int = ICESSN_RES, bbox=None) -> pa.Table:
    """Scan one ILATM2 CSV once (the only full read) into per-(cell) byte-span rows. Pass `bbox` to index only the
    nadir platelets inside it (a regional index)."""
    from .access import RangeReader, access_url

    auth.login()
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    s3 = (granule.data_links(access="direct") or [""])[0]
    m = _NAME_RE.search(name)
    gdate = m.group(1) if m else "00000000"
    t0 = time.time()

    data = RangeReader().read_all(access_url(url, s3))   # in-region: S3-direct whole-file GET; else cloud presign+GET
    size = len(data)

    lats, lons, starts, ends = [], [], [], []
    pos = 0
    for ln in data.split(b"\n"):
        start = pos; pos = pos + len(ln) + 1        # +1 for the stripped newline
        p = _parse_fields(ln)
        if p is None:
            continue
        lat, lon, _elev, _rms, track, _sn, _we = p
        if track != 0 or not (np.isfinite(lat) and np.isfinite(lon)):
            continue                                # index every nadir platelet; the rms<50 cut is re-applied at fetch
        if bbox is not None and not (bbox[1] <= lat <= bbox[3] and bbox[0] <= lon <= bbox[2]):
            continue                                # regional index: only platelets inside the build bbox
        lats.append(lat); lons.append(lon)
        starts.append(start); ends.append(min(pos, size))
    if not lats:   # no platelets inside bbox: write a schema-matched EMPTY parquet so the granule counts as done
        d = _index_dir(res)
        ref = next(iter(d.glob("*.parquet")), None)
        if ref is None:
            return pa.table({"granule": pa.array([], type=pa.string())})   # no sibling to match the schema yet; skip
        empty = pq.read_schema(ref).empty_table()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.parquet.tmp"
        pq.write_table(empty, tmp)
        tmp.replace(d / f"{name}.parquet")
        log.info("indexed ICESSN %s: 0 platelets in bbox -> empty parquet", name)
        return empty
    lat_a = np.asarray(lats, "f8"); lon_a = np.asarray(lons, "f8")
    st_a = np.asarray(starts, "i8"); en_a = np.asarray(ends, "i8")
    try:
        from h3ronpy.vector import coordinates_to_cells
        cell_a = np.asarray(coordinates_to_cells(lat_a, lon_a, res), dtype="u8")
    except Exception:
        import h3
        cell_a = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res)) for a, o in zip(lat_a, lon_a)], dtype="u8")

    base = {k: [] for k in ("granule", "url", "s3url", "gdate", "h3_cell",
                            "byte_start", "byte_end", "n_lines", "lat_min", "lat_max", "lon_min", "lon_max")}
    for c in np.unique(cell_a):
        mk = cell_a == c
        base["granule"].append(name); base["url"].append(url); base["s3url"].append(s3); base["gdate"].append(gdate)
        base["h3_cell"].append(int(c))
        base["byte_start"].append(int(st_a[mk].min())); base["byte_end"].append(int(en_a[mk].max()))
        base["n_lines"].append(int(mk.sum()))
        base["lat_min"].append(float(lat_a[mk].min())); base["lat_max"].append(float(lat_a[mk].max()))
        base["lon_min"].append(float(lon_a[mk].min())); base["lon_max"].append(float(lon_a[mk].max()))

    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in base.items()})
    tbl = tbl.replace_schema_metadata({"aicesat_icessn_index_version": ICESSN_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")
    log.info("indexed ICESSN %s: %d cells over %d nadir platelets (%.1f KB scanned, %.1fs)",
             name, tbl.num_rows, len(lats), size / 1e3, time.time() - t0)
    return tbl


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union overlapping/adjacent [start, end) byte spans so each physical line is fetched and parsed exactly once."""
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


MISSION = "ICESSN"
BEAM = "na"          # ILATM2 has no beam; chunk_index is fixed at 0 — the cache unit is the (granule, cell) pair
CHUNK = 0
_EMPTY = ("lon", "lat", "h", "t")
_DIRECT = (*_EMPTY, "sn_slope", "we_slope")   # the direct golden also carries platelet slopes, so it stays key-for-key equal to the lake path


def _index_rows(bbox, window, res: int, polygon=None) -> tuple[list[int], list[dict]]:
    """Query the ICESSN line-offset index for the (granule, cell) byte spans whose cell touches the bbox (+window).
    With `polygon` the touched-cell set is narrowed to the cells the polygon actually overlaps (not the whole
    bounding rectangle)."""
    import duckdb

    from . import planner

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no ICESSN index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res, polygon=polygon)
    cols = ["granule", "url", "s3url", "gdate", "h3_cell", "byte_start", "byte_end"]
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


def _parse_span_points(blobs, gdate: str, res: int) -> dict:
    """Parse fetched line spans into the FULL set of usable nadir platelets (track==0, finite, RMS<50 — every filter
    except the bbox), each tagged with its own H3 cell at `res`. Returns lon/lat/h/t + cell, plus the platelet
    plane-fit slopes sn_slope/we_slope (S->N, W->E; used to render each platelet as a tilted facet)."""
    from . import planner

    t0 = np.datetime64(datetime.strptime(gdate, "%Y%m%d").isoformat(), "ms")
    lon, lat, h, t, sn, we = [], [], [], [], [], []
    for blob in blobs:
        for ln in blob.split(b"\n"):
            p = _parse_fields(ln)
            if p is None:
                continue
            la, lo, elev, rms, track, sn_s, we_s = p
            if track != 0 or not (np.isfinite(elev) and np.isfinite(la) and np.isfinite(lo)) or rms >= MAX_RMS_CM:
                continue
            sec = float(ln.split(b",")[0])
            lon.append(lo); lat.append(la); h.append(elev); t.append(t0 + np.timedelta64(int(sec * 1000), "ms"))
            sn.append(sn_s); we.append(we_s)
    lon = np.asarray(lon, "f8"); lat = np.asarray(lat, "f8")
    cell = planner._cells_vectorized(lat, lon, res) if lon.size else np.array([], "u8")
    return {"lon": lon, "lat": lat, "h": np.asarray(h, "f8"),
            "t": np.asarray(t, "datetime64[ms]") if t else np.array([], "datetime64[ms]"), "cell": cell,
            "sn_slope": np.asarray(sn, "f8"), "we_slope": np.asarray(we, "f8")}


def fetch_bbox(bbox, window=None, res: int = ICESSN_RES, force: bool = False, clip_cells: bool = False,
               polygon=None, on_granule=None) -> tuple[dict, dict]:
    """Lake-first index-driven ILATM2 fetch. The cache unit is the (granule, cell) line span; only the spans for cells
    not yet materialized are byte-range fetched — missing granules fetched concurrently (per-granule pool + in-region
    S3-direct / presigned via access_url). Each fetched granule's spans are parsed to their FULL usable platelets (every
    filter but bbox) and written to the lake for exactly the fetched cells (a span materialises only the cells it was
    fetched for — its overlap with a neighbouring cell's span must not partially rewrite that neighbour). The result is
    read back filtered to bbox (+window via granule selection). A repeat query over the same/overlapping area issues zero GETs.

    `clip_cells` (opt-in) + `polygon`: address (and read back) by the H3 cells the selection actually touches instead of
    the rectangular bounding bbox. When True the read keeps points by cell-membership at res `res` (query_points drops
    the rectangular predicate); a `polygon` further narrows the touched-cell set to the drawn shape. Default (False,
    polygon=None) is byte-for-byte the pre-existing rectangular-bbox behaviour.

    `on_granule` (opt-in): a callback for per-granule progressive streaming on a cache MISS. As each fetched granule's
    spans are parsed, its DISPLAY points — the parsed platelets restricted to the cells actually FETCHED for this
    granule (mirroring write_point_chunk's only_cells, so a span's overlap into a neighbour cell is not previewed), +
    the rectangular bbox unless `clip_cells` — are emitted ONCE as {'lon','lat','h','t','granule'}, a strict SUBSET of
    the final authoritative read (never a superset). Fires only for `todo` (cache-miss) granules. When None (the
    default) the path is byte-for-byte the pre-existing behaviour."""
    from . import lake
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, AccessStats, RangeReader, access_url,
                         pool_size)

    want_cells, rows = _index_rows(bbox, window, res, polygon=polygon)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {"chunks_from_lake": 0, "chunks_from_nasa": 0, "cells": len(want_cells)}
    names = sorted({r["granule"] for r in rows})
    have = set() if force else lake.ingested_chunk_cells(MISSION, names)
    # group the MISSING (granule, cell) spans by granule URL; a cached cell contributes no span (no re-fetch)
    by_url: dict[str, dict] = {}
    n_lake = 0
    for r in rows:
        if not force and (r["granule"], BEAM, CHUNK, int(r["h3_cell"])) in have:
            n_lake += 1; continue
        u = by_url.setdefault(access_url(r["url"], r["s3url"]),
                              {"granule": r["granule"], "gdate": r["gdate"], "cells": set(), "spans": []})
        u["cells"].add(int(r["h3_cell"])); u["spans"].append((int(r["byte_start"]), int(r["byte_end"])))

    reader = None
    n_nasa = sum(len(u["cells"]) for u in by_url.values())
    if by_url:
        reader = RangeReader()
        reader.presign_all([u for u in by_url if not u.startswith("s3://")])

        def _ingest_granule(url) -> dict:
            """Fetch the granule's missing spans, parse, materialise ONLY the fetched cells (only_cells guards the
            partial-cell bug); return {chunk: fetched cells} to mark. Parquet writes hit distinct files (pool-safe)."""
            u = by_url[url]
            merged = _merge(u["spans"])                    # union the spans so every physical line is fetched once
            blobs = reader.fetch(url, [(a, b - a) for a, b in merged])
            pts = _parse_span_points(blobs, u["gdate"], res)
            lake.write_point_chunk(MISSION, u["granule"], BEAM, CHUNK, pts, res, only_cells=u["cells"],
                                   extras=("sn_slope", "we_slope"))   # carry each platelet's plane-fit orientation
            if on_granule is not None:                     # display SUBSET: exactly the platelets written to the lake
                lon, lat = pts["lon"], pts["lat"]          # (cells fetched for this granule) that query_points returns
                if lon.size:
                    keep = np.isin(pts["cell"], np.asarray(sorted(u["cells"]), dtype="u8"))
                    if not clip_cells:
                        w, s, e, n = bbox
                        keep &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
                else:
                    keep = np.array([], bool)
                on_granule({"granule": u["granule"], "lon": lon[keep], "lat": lat[keep],
                            "h": pts["h"][keep], "t": pts["t"][keep],
                            "sn_slope": pts["sn_slope"][keep], "we_slope": pts["we_slope"][keep]})
            # mark every fetched cell (even one whose platelets all fail RMS -> no file) so it is never re-fetched
            return {"granule": u["granule"], "cells": sorted(u["cells"])}

        urls = list(by_url)
        nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV)
        if nw == 1:
            parts = [_ingest_granule(u) for u in urls]
        else:
            with ThreadPoolExecutor(nw) as ex:
                parts = list(ex.map(_ingest_granule, urls))   # ex.map preserves urls order
        for pr in parts:
            lake.mark_ingested(MISSION, pr["granule"], BEAM, {CHUNK: pr["cells"]})

    arrays = lake.query_points(bbox, want_cells, MISSION, granules=names, beams=[BEAM], clip_cells=clip_cells,
                               extra_cols=("sn_slope", "we_slope"))   # slopes present for chunks ingested since this feature
    evicted = lake.enforce_global_limit(protect=want_cells, reason="limit (ICESSN fetch)") if reader else []  # only when the lake grew
    st = reader.stats.as_dict() if reader else AccessStats().as_dict()
    st.update({"chunks_from_lake": n_lake, "chunks_from_nasa": n_nasa, "chunks_fetched": n_nasa,
               "cells": len(want_cells), "evicted_for_limit": evicted, "res": res})
    return arrays, st


def _fetch_direct(bbox, window=None, res: int = ICESSN_RES) -> tuple[dict, dict]:
    """Reference (pre-lake) path == integration's fetch_bbox: byte-range GET every matching line span, parse, apply the
    full bbox filter, concat — concurrently per granule, no lake. The golden the lake-first path is validated against."""
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, RangeReader, access_url, pool_size)

    w, s, e, n = bbox
    _want, rows = _index_rows(bbox, window, res)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {}
    by_url: dict[str, dict] = {}
    for r in rows:
        u = by_url.setdefault(access_url(r["url"], r["s3url"]), {"gdate": r["gdate"], "spans": []})
        u["spans"].append((int(r["byte_start"]), int(r["byte_end"])))
    reader = RangeReader()
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])

    def _fetch_granule(url) -> dict:
        u = by_url[url]
        merged = _merge(u["spans"])
        blobs = reader.fetch(url, [(a, b - a) for a, b in merged])
        pts = _parse_span_points(blobs, u["gdate"], res)
        m = (pts["lat"] >= s) & (pts["lat"] <= n) & (pts["lon"] >= w) & (pts["lon"] <= e) if pts["lon"].size else np.array([], bool)
        return {k: pts[k][m] for k in _DIRECT}

    urls = list(by_url)
    nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV)
    if nw == 1:
        parts = [_fetch_granule(u) for u in urls]
    else:
        with ThreadPoolExecutor(nw) as ex:
            parts = list(ex.map(_fetch_granule, urls))   # ex.map preserves urls order
    out = {k: [] for k in _DIRECT}
    for loc in parts:
        for k in out:
            out[k].append(loc[k])
    arrays = {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}
    return arrays, reader.stats.as_dict()
