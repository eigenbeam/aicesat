"""IS2TGPSSS (Summit GPS traverse) line-offset index -- the ICESSN pattern applied to the survey CSVs.

Each survey is one small CSV (0.1-1 MB) with a header row. At build we read it once, assign every epoch to an H3 cell,
and record the byte span of each cell's lines together with the survey's SLED GEOMETRY (antenna height above the sled
base, sled sinkage, whether the sinkage was measured), resolved from the traverse metadata file. At query time we
byte-range GET only those spans, prepend the CSV header, and reduce each epoch to a snow-surface elevation with
gpstruth.parse_rows -- the same reduction the whole-file parser applies.

Why the geometry lives on the index rows and the reduction happens at fetch: the metadata file only exists at build
time (a fetch never goes back to CMR), and the lake is an evictable cache that must be rebuildable from the index
alone. The metadata file's hash is stamped on every index file, so a revised metadata file makes the index stale at
the next build instead of silently keeping the old reduction.
"""
from __future__ import annotations

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from . import index as index_mod   # shared index-table typing
from .index_icessn import _merge   # the same [start, end) span union

log = logging.getLogger(__name__)

GPSTRUTH_RES = 5    # match the other index missions, so a query cell maps to the same index cells
GPSTRUTH_INDEX_VERSION = "1"
GPSTRUTH_INDEX_DIR = cache.DATA_DIR / "index" / "gpstruth"
METADATA_KEY = "aicesat_gpstruth_metadata_sha1"
WORLD = (-180.0, -90.0, 180.0, 90.0)   # parse_rows' bbox when every epoch of the fetched cells is wanted

MISSION = "GPSTRUTH"
BEAM = "na"          # no beams; the cache unit is the (granule, cell) pair, as for ICESSN
CHUNK = 0
_EMPTY = ("lon", "lat", "h", "t")
EXTRAS = ("sdhgt_m", "gdop", "track_depth_cm", "track_depth_known", "arp_to_sled_m")
_DIRECT = (*_EMPTY, *EXTRAS)


def _index_dir(res: int):
    return GPSTRUTH_INDEX_DIR / f"res{res}"


def indexed_gpstruth_granules(res: int = GPSTRUTH_RES, metadata_sha1: str | None = None) -> set[str]:
    """Granules with a current index file. Stale files are deleted, as for the other index missions. A file built
    from a different traverse-metadata file is stale too when the builder passes the current file's hash: its sled
    geometry, and so every height reduced from it, would otherwise outlive the metadata it came from."""
    out, stale = set(), False
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        try:
            meta = pq.read_schema(p).metadata or {}
        except Exception as e:
            log.warning("index %s is unreadable (%s); rebuilding", p.name, e)
            p.unlink(missing_ok=True)
            stale = True
            continue
        current = meta.get(b"aicesat_gpstruth_index_version", b"").decode() == GPSTRUTH_INDEX_VERSION
        same_meta = metadata_sha1 is None or meta.get(METADATA_KEY.encode(), b"").decode() == metadata_sha1
        if current and same_meta:
            out.add(p.stem)
        else:
            log.warning("index %s is %s; rebuilding", p.name,
                        "from an older traverse-metadata file" if current else "an old schema")
            p.unlink()
            stale = True
    if stale:
        index_mod.invalidate_claim(d, "granule files were rebuilt for a new schema or metadata version")
    return out


def _latlon(ln: bytes):
    f = ln.split(b",", 2)
    try:
        return float(f[0]), float(f[1])
    except (ValueError, IndexError):
        return None


def _gdate(line: bytes) -> str:
    """YYYYMMDD from a data line's year (col 6) and day of year (col 5). The date gates every window query, so a line
    that cannot be dated raises rather than being guessed."""
    f = line.split(b",")
    year, doy = int(float(f[5])), int(float(f[4]))
    return (datetime(year, 1, 1) + timedelta(days=doy - 1)).strftime("%Y%m%d")


def build_gpstruth_index(granule, geometry: tuple[float, float, bool, str], metadata_sha1: str,
                         res: int = GPSTRUTH_RES, cells=None) -> pa.Table:
    """Scan one survey CSV once (the only full read) into per-(cell) byte-span rows carrying the survey's sled
    geometry `(arp_to_sled_m, track_depth_cm, track_depth_known, rinex_key)`. Pass `cells` for a regional index."""
    from .access import RangeReader, access_url
    from .coverage import granule_name

    auth.login()
    url = granule.data_links()[0]
    name = granule_name(granule)
    s3 = (granule.data_links(access="direct") or [""])[0]
    arp_m, depth_cm, depth_known, rinex_key = geometry
    t0 = time.time()

    data = RangeReader().read_all(access_url(url, s3))
    size = len(data)
    header, first = None, None
    lats, lons, starts, ends = [], [], [], []
    pos = 0
    for ln in data.split(b"\n"):
        start = pos; pos = pos + len(ln) + 1        # +1 for the stripped newline
        if header is None:
            header = ln.rstrip(b"\r")
            continue
        p = _latlon(ln)
        if p is None or not (np.isfinite(p[0]) and np.isfinite(p[1])):
            continue                                # blank, truncated or unparseable: parse_rows skips it too
        first = first or ln
        lats.append(p[0]); lons.append(p[1])
        starts.append(start); ends.append(min(pos, size))
    gdate = _gdate(first) if first else ""

    keep = index_mod.cells_filter(cells)
    lat_a = np.asarray(lats, "f8"); lon_a = np.asarray(lons, "f8")
    st_a = np.asarray(starts, "i8"); en_a = np.asarray(ends, "i8")
    if lat_a.size == 0:
        cell_a = np.array([], dtype="u8")
    else:
        from . import planner
        cell_a = planner._cells_vectorized(lat_a, lon_a, res)
    if keep is not None:      # regional index: whole cells only
        keep_arr = np.fromiter(sorted(keep), dtype="u8", count=len(keep))
        m = np.isin(cell_a, keep_arr)
        lat_a, lon_a, st_a, en_a, cell_a = lat_a[m], lon_a[m], st_a[m], en_a[m], cell_a[m]

    base = {k: [] for k in ("granule", "url", "s3url", "gdate", "h3_cell", "byte_start", "byte_end", "n_lines",
                            "lat_min", "lat_max", "lon_min", "lon_max", "header", "rinex_key",
                            "arp_to_sled_m", "track_depth_cm", "track_depth_known")}
    for c in np.unique(cell_a):
        mk = cell_a == c
        base["granule"].append(name); base["url"].append(url); base["s3url"].append(s3); base["gdate"].append(gdate)
        base["h3_cell"].append(int(c))
        base["byte_start"].append(int(st_a[mk].min())); base["byte_end"].append(int(en_a[mk].max()))
        base["n_lines"].append(int(mk.sum()))
        base["lat_min"].append(float(lat_a[mk].min())); base["lat_max"].append(float(lat_a[mk].max()))
        base["lon_min"].append(float(lon_a[mk].min())); base["lon_max"].append(float(lon_a[mk].max()))
        base["header"].append(header.decode("utf-8", "replace"))
        base["rinex_key"].append(rinex_key or "")
        base["arp_to_sled_m"].append(float(arp_m)); base["track_depth_cm"].append(float(depth_cm))
        base["track_depth_known"].append(bool(depth_known))

    tbl = index_mod.typed_table(base)
    tbl = tbl.replace_schema_metadata({"aicesat_gpstruth_index_version": GPSTRUTH_INDEX_VERSION, "h3_res": str(res),
                                       METADATA_KEY: metadata_sha1,
                                       "built_at": datetime.now(timezone.utc).isoformat(),
                                       **index_mod.cells_metadata(cells)})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")
    log.info("indexed GPSTRUTH %s: %d cells over %d epochs (%.1f KB scanned, %.1fs)",
             name, tbl.num_rows, len(lats), size / 1e3, time.time() - t0)
    return tbl


_ROW_COLS = ["granule", "url", "s3url", "gdate", "h3_cell", "byte_start", "byte_end", "header",
             "arp_to_sled_m", "track_depth_cm", "track_depth_known"]


def _index_rows(bbox, window, res: int, polygon=None) -> tuple[list[int], list[dict]]:
    """The (granule, cell) byte spans, with each survey's header and sled geometry, whose cell touches the area."""
    import duckdb

    from . import coverage, planner

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no GPSTRUTH index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res, polygon=polygon)
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND gdate BETWEEN '{lo}' AND '{hi}'"
    files = coverage.index_files_for_cells(MISSION, want_cells)
    if files is not None and not files:
        return want_cells, []
    src = coverage.read_parquet_src(d, files)
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT {', '.join(_ROW_COLS)} FROM {src} WHERE {where}").fetchall()
    finally:
        con.close()
    return want_cells, [dict(zip(_ROW_COLS, r)) for r in rows]


def _parse_span_points(blobs, header: str, geometry: tuple[float, float, bool], res: int) -> dict:
    """Fetched line spans -> every usable epoch (SDHGT gate, finite; everything but a bbox), reduced to a snow-surface
    elevation with the survey's sled geometry, each tagged with its H3 cell at `res`."""
    from . import gpstruth, planner

    arp_m, depth_cm, depth_known = geometry
    parts = []
    for blob in blobs:
        d = gpstruth.parse_rows(io.BytesIO(header.encode() + b"\n" + blob), WORLD, arp_m, depth_cm, depth_known)
        if d is not None:
            parts.append(d)
    if not parts:
        out = {k: np.array([]) for k in _DIRECT}
        out["t"] = np.array([], "datetime64[ms]")
        out["cell"] = np.array([], "u8")
        return out
    out = {k: np.concatenate([p[k] for p in parts]) for k in _DIRECT}
    out["cell"] = planner._cells_vectorized(out["lat"], out["lon"], res)
    return out


def _by_url(rows, have=frozenset(), force: bool = True):
    """Group index rows by access URL: {url: {granule, header, geometry, cells, spans}}, skipping cells in `have`."""
    from .access import access_url

    by_url: dict[str, dict] = {}
    n_skipped = 0
    for r in rows:
        if not force and (r["granule"], BEAM, CHUNK, int(r["h3_cell"])) in have:
            n_skipped += 1
            continue
        u = by_url.setdefault(access_url(r["url"], r["s3url"]), {
            "granule": r["granule"], "header": r["header"], "cells": set(), "spans": [],
            "geometry": (float(r["arp_to_sled_m"]), float(r["track_depth_cm"]), bool(r["track_depth_known"]))})
        u["cells"].add(int(r["h3_cell"])); u["spans"].append((int(r["byte_start"]), int(r["byte_end"])))
    return by_url, n_skipped


def fetch_bbox(bbox, window=None, res: int = GPSTRUTH_RES, force: bool = False, clip_cells: bool = False,
               polygon=None, on_granule=None, on_plan=None) -> tuple[dict, dict]:
    """Lake-first index-driven fetch, exactly as index_icessn.fetch_bbox: the cache unit is the (granule, cell) line
    span, only cells not yet in the lake are fetched, each fetched span materialises only the cells it was fetched
    for, and the result is read back filtered to the area. A repeat query issues zero GETs."""
    from . import lake
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, AccessStats, RangeReader, pool_size)

    want_cells, rows = _index_rows(bbox, window, res, polygon=polygon)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {"chunks_from_lake": 0, "chunks_from_nasa": 0, "cells": len(want_cells)}
    names = sorted({r["granule"] for r in rows})

    lake.drain_writes(MISSION, want_cells)
    have = set() if force else lake.ingested_chunk_cells(MISSION, names)
    _stream = (lambda r: on_granule({"granule": "lake", **r})) if on_granule is not None else None
    cached = None if force else lake.query_points(bbox, want_cells, MISSION, granules=names, beams=[BEAM],
                                                  clip_cells=clip_cells, extra_cols=EXTRAS, on_batch=_stream)
    by_url, n_lake = _by_url(rows, have, force)

    reader, fresh_parts = None, []
    n_nasa = sum(len(u["cells"]) for u in by_url.values())
    if by_url:
        reader = RangeReader()
        reader.presign_all([u for u in by_url if not u.startswith("s3://")])
        if on_plan is not None:
            on_plan({"granules": len(by_url), "chunks": n_nasa, "cached": n_lake})

        def _ingest_granule(url) -> dict:
            u = by_url[url]
            blobs = reader.fetch(url, [(a, b - a) for a, b in _merge(u["spans"])])
            pts = _parse_span_points(blobs, u["header"], u["geometry"], res)
            cells = tuple(sorted(u["cells"]))
            lake.submit_writes(MISSION, res, [lake.ChunkWrite(u["granule"], BEAM, CHUNK, pts, only_cells=cells,
                                                              mark_cells=cells)], want_cells, extras=EXTRAS)
            keep = np.isin(pts["cell"], np.asarray(cells, dtype="u8")) if pts["lon"].size else np.array([], bool)
            if pts["lon"].size and not clip_cells:
                w, s, e, n = bbox
                keep &= (pts["lat"] >= s) & (pts["lat"] <= n) & (pts["lon"] >= w) & (pts["lon"] <= e)
            return {"granule": u["granule"], **{k: pts[k][keep] for k in _DIRECT}}

        urls = list(by_url)
        nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                       cpu_bound=False)
        if nw == 1:
            parts = [_ingest_granule(u) for u in urls]
        else:
            with ThreadPoolExecutor(nw) as ex:
                parts = list(ex.map(_ingest_granule, urls))
        for pr in parts:
            fresh_parts.append(pr)
            if on_granule is not None:
                on_granule({k: pr[k] for k in ("granule", "lon", "lat", "h", "t")})
        if not lake.async_writes_enabled():
            lake.drain_writes(MISSION, want_cells)
    elif on_plan is not None:
        on_plan({"granules": 0, "chunks": 0, "cached": n_lake})
    arrays = lake.concat_arrays([cached, *fresh_parts], _DIRECT)
    if reader:
        lake.enforce_global_limit_async(protect=want_cells, reason="limit (GPSTRUTH fetch)")
    st = reader.stats.as_dict() if reader else AccessStats().as_dict()
    st.update({"chunks_from_lake": n_lake, "chunks_from_nasa": n_nasa, "chunks_fetched": n_nasa,
               "cells": len(want_cells), "evicted_for_limit": [], "res": res})
    return arrays, st


def _fetch_direct(bbox, window=None, res: int = GPSTRUTH_RES) -> tuple[dict, dict]:
    """Reference path with no lake: GET every matching span, reduce, apply the bbox. The golden the lake-first path
    is checked against."""
    from .access import RangeReader

    w, s, e, n = bbox
    _want, rows = _index_rows(bbox, window, res)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {}
    by_url, _ = _by_url(rows)
    reader = RangeReader()
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])
    out = {k: [] for k in _DIRECT}
    for url, u in by_url.items():
        pts = _parse_span_points(reader.fetch(url, [(a, b - a) for a, b in _merge(u["spans"])]),
                                 u["header"], u["geometry"], res)
        m = ((pts["lat"] >= s) & (pts["lat"] <= n) & (pts["lon"] >= w) & (pts["lon"] <= e)) if pts["lon"].size \
            else np.array([], bool)
        for k in out:
            out[k].append(pts[k][m])
    return {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}, reader.stats.as_dict()
