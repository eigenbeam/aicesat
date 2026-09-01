"""ATL06 addressing index — the ATL03 index pattern (index.py) applied to land-ice segments.

Each land_ice_segments dataset is one value per ~40 m segment, and on v007 they are all chunked identically
(10 000 segments/chunk), so segment i lives in chunk i//C and ONE index row per (granule, beam, chunk, h3_cell)
carries the byte range of every dataset for that chunk. Index-only: we read the granule's metadata + the segment
lat/lon (to assign cells); the elevation/time/quality bulk is fetched later, on demand, by byte range.

Prototype (vertical slice) for extending the ATL03 sub-granule index to the time-series collections.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import h3
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import auth, cache
from . import index as index_mod   # shared HDF5 helpers + index-table typing
from .index import BEAMS, _chunk_manifest, _filters, strong_beams   # reuse the ATL03 HDF5 helpers verbatim

log = logging.getLogger(__name__)

H3_RES = 6
ATL06_RES = 5   # per-collection choice: ATL06's 400 km chunks make res 5 the sweet spot (finer buys ~nothing on
                # scene-sized queries but multiplies index/scan cost) — see the resolution analysis.
ATL06_INDEX_VERSION = "2"   # v2: rows filtered to the build's cells (v1 held the whole track)
ATL06_DATASETS = ("latitude", "longitude", "h_li", "delta_time", "atl06_quality_summary")
ATL06_INDEX_DIR = cache.DATA_DIR / "index" / "atl06"


def _index_dir(res: int):
    return ATL06_INDEX_DIR / f"res{res}"


def indexed_atl06_granules(res: int = ATL06_RES) -> set[str]:
    """Granule names (stems, with .h5) already indexed at this res with the current schema — for resumable builds."""
    out = set()
    d = _index_dir(res)
    for p in (d.glob("*.parquet") if d.exists() else []):
        meta = pq.read_schema(p).metadata or {}
        if meta.get(b"aicesat_atl06_index_version", b"").decode() == ATL06_INDEX_VERSION:
            out.add(p.stem)
        else:
            # Deleted, not just skipped: queries read every *.parquet in the directory, so a stale file
            # keeps serving its old-semantics rows until something overwrites it.
            log.warning("index %s has an old schema; rebuilding", p.name)
            p.unlink()
    return out
_NAME_RE = re.compile(r"ATL06_(\d{14})_(\d{4})(\d{2})(\d{2})_(\d{3})_(\d{2})\.h5")


def parse_granule_name(name: str) -> dict:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"unexpected ATL06 granule name {name}")
    return {"start": m.group(1), "rgt": int(m.group(2)), "cycle": int(m.group(3)), "region": int(m.group(4)),
            "version": int(m.group(5)), "release": int(m.group(6))}


def build_atl06_index(granule, res: int = ATL06_RES, cells=None) -> pa.Table:
    """Parse one ATL06 granule's structure (the only time its HDF5 b-trees are read) into addressing rows.

    `cells` (opt-in): keep only rows whose H3 cell is in the set this build was asked for — see
    index.build_granule_index for why indexing a granule's whole track made "indexed" mean less than it looked."""
    auth.login()
    keep = index_mod.cells_filter(cells)
    from .access import RangeReader, access_url, cloud_hdf5_file, decode_chunk
    from .coverage import granule_name

    url = granule.data_links()[0]
    name = granule_name(granule)
    info = parse_granule_name(name)
    s3 = (granule.data_links(access="direct") or [""])[0]
    t0 = time.time()
    base_cols = ["granule", "url", "s3url", "revision", "sc_orient", "sdp_epoch", "beam", "strong", "cycle", "rgt",
                 "chunk_index", "seg_start", "seg_end", "h3_cell", "lat_min", "lat_max", "lon_min", "lon_max"]
    rows: dict[str, list] = {k: [] for k in base_cols}
    for ds in ATL06_DATASETS:
        for k in ("offset", "size", "filters", "dtype", "ncols", "mask"):
            rows[f"{ds}_{k}"] = []

    reader = RangeReader()
    with h5py.File(cloud_hdf5_file(url, s3, reader=reader), "r") as f:   # in-region: s3fs; else one shared presign
        sc_orient = int(f["orbit_info/sc_orient"][0])
        sdp = float(f["ancillary_data/atlas_sdp_gps_epoch"][0])
        strong = strong_beams(sc_orient)

        def read_via_chunks(ds: h5py.Dataset) -> np.ndarray:
            infos = _chunk_manifest(ds)
            fl = _filters(ds)
            raws = reader.fetch(access_url(url, s3), [(int(ci.byte_offset), int(ci.size)) for ci in infos])
            parts = [decode_chunk(raw, str(ds.dtype), fl, 1, int(ci.filter_mask)) for raw, ci in zip(raws, infos)]
            return np.concatenate(parts)[: ds.shape[0]]

        for beam in BEAMS:
            base = f"{beam}/land_ice_segments"
            if base not in f or f"{base}/latitude" not in f:
                continue
            lis = f[base]
            dsets = {d: lis[d] for d in ATL06_DATASETS}
            C = int(dsets["latitude"].chunks[0])
            nchunks = dsets["latitude"].id.get_num_chunks()
            for d, ds in dsets.items():
                if int(ds.chunks[0]) != C or ds.id.get_num_chunks() != nchunks:
                    raise ValueError(f"{beam}/{d}: chunking differs from latitude ({ds.chunks[0]} vs {C})")
                if ds.ndim > 1:
                    raise ValueError(f"{beam}/{d}: unexpected 2-D land-ice-segment dataset")
                bad = [x for x in _filters(ds).split(",") if x and x not in ("gzip", "shuffle")]
                if bad:
                    raise ValueError(f"{beam}/{d}: unsupported HDF5 filters {bad}")
            lat = read_via_chunks(dsets["latitude"])
            lon = read_via_chunks(dsets["longitude"])
            n = int(lat.shape[0])
            manifests = {d: _chunk_manifest(ds) for d, ds in dsets.items()}
            meta = {d: (_filters(ds), str(ds.dtype), 1) for d, ds in dsets.items()}

            seg = np.arange(n)
            ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90)
            ks = (seg[ok] // C).astype("i8")
            latok, lonok = lat[ok].astype("f8"), lon[ok].astype("f8")
            try:
                from h3ronpy.vector import coordinates_to_cells
                cell_ids = np.asarray(coordinates_to_cells(latok, lonok, res), dtype="u8")
            except Exception:
                cell_ids = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), res)) for a, o in zip(latok, lonok)], dtype="u8")

            box = {}
            for k in np.unique(ks):
                m = ks == k
                box[int(k)] = (float(latok[m].min()), float(latok[m].max()), float(lonok[m].min()), float(lonok[m].max()))

            for k, cell in sorted(set(zip(ks.tolist(), cell_ids.tolist()))):
                if keep is not None and int(cell) not in keep:
                    continue                      # outside the cells this build was asked for
                assert h3.is_valid_cell(h3.int_to_str(int(cell))), cell
                rows["granule"].append(name); rows["url"].append(url); rows["s3url"].append(s3)
                rows["revision"].append(str(granule["meta"].get("revision-id", ""))); rows["sc_orient"].append(sc_orient)
                rows["sdp_epoch"].append(sdp); rows["beam"].append(beam); rows["strong"].append(beam in strong)
                rows["cycle"].append(info["cycle"]); rows["rgt"].append(info["rgt"])
                rows["chunk_index"].append(int(k)); rows["seg_start"].append(int(k * C)); rows["seg_end"].append(int(min((k + 1) * C, n)))
                rows["h3_cell"].append(int(cell))
                b = box[int(k)]
                rows["lat_min"].append(b[0]); rows["lat_max"].append(b[1]); rows["lon_min"].append(b[2]); rows["lon_max"].append(b[3])
                for d in ATL06_DATASETS:
                    ci = manifests[d][int(k)]
                    fl, dt, nc = meta[d]
                    rows[f"{d}_offset"].append(int(ci.byte_offset)); rows[f"{d}_size"].append(int(ci.size))
                    rows[f"{d}_filters"].append(fl); rows[f"{d}_dtype"].append(dt); rows[f"{d}_ncols"].append(nc); rows[f"{d}_mask"].append(int(ci.filter_mask))

    tbl = index_mod.typed_table(rows)
    tbl = tbl.replace_schema_metadata({"aicesat_atl06_index_version": ATL06_INDEX_VERSION, "h3_res": str(res),
                                       "built_at": datetime.now(timezone.utc).isoformat()})
    d = _index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.parquet.tmp"
    pq.write_table(tbl, tmp)
    tmp.replace(d / f"{name}.parquet")   # atomic rename: a concurrent reader never sees a partial file
    log.info("indexed ATL06 %s: %d (chunk,cell) rows, %d beams, %.1fs (%d GETs, %.1f MB)",
             name, tbl.num_rows, len({*rows["beam"]}), time.time() - t0, reader.stats.requests, reader.stats.bytes / 1e6)
    return tbl


def _atlas_epoch_years(delta_time: np.ndarray, sdp_epoch_gps_s: float) -> np.ndarray:
    """delta_time (s since the ATLAS SDP epoch) -> datetime64[ms]; sdp epoch is GPS seconds since 1980-01-06."""
    gps0 = np.datetime64("1980-01-06T00:00:00", "ms")
    base = gps0 + np.timedelta64(int(round(sdp_epoch_gps_s * 1000)), "ms")
    return base + (delta_time * 1000.0).astype("timedelta64[ms]")


MISSION = "ATL06"
_EMPTY = ("lon", "lat", "h", "t", "quality")
PRECACHE_ENV = "AICESAT_PRECACHE_ADJACENT_CELLS"


def precache_adjacent() -> bool:
    """Whether a fetched chunk also materialises the cells OUTSIDE the request (default: no).

    The smallest thing a byte-range fetch can read is one 10,000-segment block, which covers far more track than a
    scene-sized box — measured at 1.02 blocks per (granule, beam) for a 33 km box, i.e. one block spans the whole
    request. Writing the entire decoded strip pre-caches the neighbouring cells in case the user later pans along the
    track. Measured price on the box: 114.5 -> 43.9 write thread-seconds, so 2.6x the write work on EVERY build to
    save a re-fetch on the minority that happen to be adjacent to a previous one. With fetch wall now ~12 s a miss is
    cheap, so the trade no longer pays. Set to 1 to restore the old behaviour (or to A/B it again).
    """
    import os
    return os.environ.get(PRECACHE_ENV, "0").lower() in ("1", "true", "yes")


def _index_rows(bbox, window, res: int, strong_only: bool, polygon=None) -> tuple[list[int], list[dict]]:
    """Query the ATL06 index for the (granule, beam, chunk, cell) refs whose cell touches the bbox (+window). DuckDB
    pushes the cell predicate into the Parquet scan. `strong_only` keeps integration's weak-beam support: False fetches
    all six beams (the index carries a `strong` flag). With `polygon` the touched-cell set is narrowed to the cells the
    polygon actually overlaps (not the whole bounding rectangle). Returns (want_cells, rows) — one row per
    (granule,beam,chunk,cell) carrying that chunk's byte refs (identical across the chunk's cells)."""
    import duckdb

    from . import planner

    d = _index_dir(res)
    if not d.exists():
        raise RuntimeError(f"no ATL06 index built at res {res} yet")
    want_cells = planner.cells_for_bbox(bbox, res=res, polygon=polygon)
    cols = ["granule", "url", "s3url", "sdp_epoch", "beam", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    for ds in ATL06_DATASETS:
        cols += [f"{ds}_offset", f"{ds}_size", f"{ds}_dtype", f"{ds}_filters", f"{ds}_mask"]
    where = f"h3_cell IN ({','.join(str(int(c)) for c in want_cells)})"
    if strong_only:
        where += " AND strong"
    if window:
        lo, hi = window[0].replace("-", ""), window[1].replace("-", "")
        where += f" AND substr(granule, 7, 8) BETWEEN '{lo}' AND '{hi}'"
    from . import coverage
    files = coverage.index_files_for_cells("ATL06", want_cells)   # name only the granule files that touch these cells
    if files is not None and not files:
        return want_cells, []                                     # manifest says no granule touches them
    src = coverage.read_parquet_src(d, files)
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT {', '.join(cols)} FROM {src} WHERE {where}").fetchall()
    finally:
        con.close()
    return want_cells, [dict(zip(cols, r)) for r in rows]


def _decode_chunk(raws: dict, r: dict) -> dict:
    """Decode one chunk's FULL seg_start:seg_end arrays for every dataset (pre-mask)."""
    from .access import decode_chunk

    seg_n = r["seg_end"] - r["seg_start"]
    return {ds: decode_chunk(raws[(r["beam"], r["chunk_index"], ds)], r[f"{ds}_dtype"], r[f"{ds}_filters"], 1, r[f"{ds}_mask"])[:seg_n]
            for ds in ATL06_DATASETS}


def fetch_bbox(bbox, window=None, res: int = ATL06_RES, strong_only: bool = True, quality_zero: bool = True,
               force: bool = False, clip_cells: bool = False, polygon=None, on_granule=None, on_plan=None) -> tuple[dict, dict]:
    """Lake-first index-driven ATL06 fetch (mirrors the ATL03 planner). Only the chunks whose wanted cells are NOT yet
    materialized are byte-range fetched from NASA — the missing granules fetched concurrently (integration's per-granule
    pool + in-region S3-direct / presigned CloudFront via access_url). Each fetched chunk's FULL pre-mask points are
    written to the lake partitioned by each point's own res-`res` cell, then read back filtered to bbox (+window via
    granule selection, +quality). A repeat query over the same/overlapping area issues zero NASA GETs. `force`
    re-fetches. Weak beams are preserved via `strong_only`. Returns (arrays, stats) with chunks_from_lake /
    chunks_from_nasa alongside the byte-range access counters.

    `clip_cells` (opt-in) + `polygon`: address (and read back) by the H3 cells the selection actually touches instead of
    the rectangular bounding bbox. When True the read keeps points by cell-membership at res `res` (query_points drops
    the rectangular predicate); a `polygon` further narrows the touched-cell set to the drawn shape. Default (False,
    polygon=None) is byte-for-byte the pre-existing rectangular-bbox behaviour.

    `on_granule` (opt-in): a callback for progressive streaming. The lake's cached points are emitted first, in cell
    groups, then each fetched granule's freshly decoded points as they land — the same arrays this call returns, so the
    stream is exactly the result, never a superset.

    Ordering: the lake is READ BEFORE anything new is written, and the freshly fetched points are returned from memory
    rather than round-tripped through disk. That is what lets the Parquet write move to lake's background writer (39%
    of a cold leg's thread-time). Duplication is impossible by construction — the read precedes every new write, and
    any cell of a re-fetched chunk that was ALREADY materialized is excluded from the fresh points, because the lake
    read has already returned it."""
    from . import lake, planner
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, AccessStats, RangeReader, access_url,
                         pool_size)

    want_cells, rows = _index_rows(bbox, window, res, strong_only, polygon=polygon)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {"chunks_from_lake": 0, "chunks_from_nasa": 0, "cells": len(want_cells)}
    names = sorted({r["granule"] for r in rows})
    beams = sorted({r["beam"] for r in rows})   # exactly the beams the query selected (strong-only vs all-6)
    want_arr = np.asarray(sorted(int(c) for c in want_cells), dtype="u8")

    # Settle any background write over these cells before reading them, so `have` and the files agree.
    lake.drain_writes(MISSION, want_cells)
    have = set() if force else lake.ingested_chunk_cells(MISSION, names)

    # READ FIRST. On `force` skip it: every chunk is re-fetched below, so the fresh points already cover the whole
    # request and reading the pre-existing rows too would double them.
    _stream = (lambda r: on_granule({"granule": "lake", **r})) if on_granule is not None else None
    cached = None if force else lake.query_points(
        bbox, want_cells, MISSION, granules=names, beams=beams, extra_cols=("quality",),
        quality_zero=quality_zero, clip_cells=clip_cells, on_batch=_stream)

    chunk_cells, chunk_row = {}, {}      # (granule,beam,chunk) -> wanted cells it touches / a representative index row
    for r in rows:
        k = (r["granule"], r["beam"], r["chunk_index"])
        chunk_cells.setdefault(k, set()).add(int(r["h3_cell"])); chunk_row.setdefault(k, r)
    # cell-aware skip (like ATL03): fetch a chunk if ANY of its wanted cells is not yet materialized
    todo = [k for k, cs in chunk_cells.items() if any((k[0], k[1], k[2], c) not in have for c in cs)]
    n_lake = len(chunk_cells) - len(todo)

    # Write only the cells the request covers, discarding the rest of the decoded strip (see precache_adjacent).
    # Coverage stays consistent: mark_cells is the chunk's WANTED cells, so we never claim to hold what we dropped.
    want_only = None if precache_adjacent() else tuple(sorted(int(c) for c in want_cells))
    reader, fresh_parts = None, []
    if todo:
        reader = RangeReader()
        by_url: dict[str, list] = {}
        for k in todo:
            r = chunk_row[k]; by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
        reader.presign_all([u for u in by_url if not u.startswith("s3://")])
        if on_plan is not None:   # the denominator the progress UI needs, known before any network
            on_plan({"granules": len(by_url), "chunks": len(todo), "cached": n_lake})

        def _keep(mats: dict, dup_cells) -> np.ndarray:
            """The query_points predicate, applied in memory to one chunk's valid points: cell-membership in the wanted
            set (query_points always applies it), + the rectangular bbox unless clip_cells, + quality==0 when
            quality_zero. `dup_cells` are the chunk's cells the lake read already returned — dropping them is what
            keeps a partially cached chunk from contributing its cached cells twice."""
            lon, lat = mats["lon"], mats["lat"]
            if lon.size == 0:
                return np.zeros(0, bool)
            pcell = planner._cells_vectorized(lat, lon, res)
            keep = np.isin(pcell, want_arr)
            if dup_cells:
                keep &= ~np.isin(pcell, np.asarray(sorted(dup_cells), dtype="u8"))
            if not clip_cells:
                w, s, e, n = bbox
                keep &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
            if quality_zero:
                keep &= (mats["quality"] == 0)
            return keep

        def _ingest_granule(url) -> dict:
            """Fetch + decode one granule's missing chunks; return its display points. The Parquet write is queued to
            the background writer, not done here — the caller gets the points from memory."""
            rs = by_url[url]
            ranges, keys = [], []
            for r in rs:
                for ds in ATL06_DATASETS:
                    ranges.append((r[f"{ds}_offset"], r[f"{ds}_size"])); keys.append((r["beam"], r["chunk_index"], ds))
            raws = dict(zip(keys, reader.fetch(url, ranges)))
            writes, out = [], {}                          # queued chunks / granule -> its fresh display points
            for r in rs:
                dec = _decode_chunk(raws, r)
                lat, lon, h, dt, q = dec["latitude"], dec["longitude"], dec["h_li"], dec["delta_time"], dec["atl06_quality_summary"]
                valid = np.isfinite(h) & (h < 3.0e38) & np.isfinite(lat) & np.isfinite(lon)   # data-validity (bbox-independent)
                mats = {"lon": lon[valid].astype("f8"), "lat": lat[valid].astype("f8"), "h": h[valid].astype("f8"),
                        "t": _atlas_epoch_years(dt[valid], r["sdp_epoch"]), "quality": q[valid]}
                k = (r["granule"], r["beam"], r["chunk_index"])
                # mark every wanted cell of the chunk (not only cells that carried valid data) so an all-fill cell is
                # not re-fetched forever; write_point_chunk adds any extra cell its points materialise.
                writes.append(lake.ChunkWrite(r["granule"], r["beam"], r["chunk_index"], mats,
                                              only_cells=want_only, mark_cells=tuple(sorted(chunk_cells[k]))))
                keep = _keep(mats, {c for c in chunk_cells[k] if (k[0], k[1], k[2], c) in have})
                g = out.setdefault(r["granule"], {kk: [] for kk in _EMPTY})
                for kk in _EMPTY:
                    g[kk].append(mats[kk][keep])
            lake.submit_writes(MISSION, res, writes, want_cells, extras=("quality",))
            return {g: {kk: np.concatenate(v) for kk, v in dd.items()} for g, dd in out.items()}

        urls = list(by_url)
        nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                       cpu_bound=False)
        if nw == 1:
            parts = [_ingest_granule(u) for u in urls]
        else:
            with ThreadPoolExecutor(nw) as ex:
                parts = list(ex.map(_ingest_granule, urls))   # ex.map preserves urls order
        for loc in parts:
            for g, dd in loc.items():
                fresh_parts.append(dd)
                if on_granule is not None:
                    on_granule({"granule": g, **{kk: dd[kk] for kk in ("lon", "lat", "h", "t")}})
        if not lake.async_writes_enabled():
            # Kill switch: end the leg with ONE batched coverage mark on this thread, which is what the pre-writer
            # path did. Without it the baseline would defer the mark past the measured wall time and stop being a
            # baseline. No-op when the writer is on — its own threads flush.
            lake.drain_writes(MISSION, want_cells)

    elif on_plan is not None:
        on_plan({"granules": 0, "chunks": 0, "cached": n_lake})   # pure cache hit: nothing to fetch
    arrays = lake.concat_arrays([cached, *fresh_parts], _EMPTY)
    if reader:   # only when the lake grew; off the critical path (single-flight) — it is housekeeping
        lake.enforce_global_limit_async(protect=want_cells, reason="limit (ATL06 fetch)")
    evicted = []
    st = reader.stats.as_dict() if reader else AccessStats().as_dict()
    st.update({"chunks_from_lake": n_lake, "chunks_from_nasa": len(todo), "chunks_fetched": len(todo),
               "cells": len(want_cells), "evicted_for_limit": evicted, "res": res})
    return arrays, st


def _fetch_direct(bbox, window=None, res: int = ATL06_RES, strong_only: bool = True, quality_zero: bool = True) -> tuple[dict, dict]:
    """Reference (pre-lake) path == integration's fetch_bbox: byte-range fetch EVERY matching chunk (all beams when
    strong_only is False), decode, apply the bbox+quality mask, concat — concurrently per granule, no lake. Kept as the
    golden the lake-first path is validated against and for in-region byte-identity checks."""
    from .access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, RangeReader, access_url, pool_size)

    w, s, e, n = bbox
    _want, rows = _index_rows(bbox, window, res, strong_only)
    if not rows:
        return {k: np.array([]) for k in _EMPTY}, {}
    chunk_row = {}
    for r in rows:
        chunk_row.setdefault((r["granule"], r["beam"], r["chunk_index"]), r)
    by_url: dict[str, list] = {}
    for r in chunk_row.values():
        by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
    reader = RangeReader()
    reader.presign_all([u for u in by_url if not u.startswith("s3://")])

    def _fetch_granule(url) -> dict:
        rs = by_url[url]
        ranges, keys = [], []
        for r in rs:
            for ds in ATL06_DATASETS:
                ranges.append((r[f"{ds}_offset"], r[f"{ds}_size"])); keys.append((r["beam"], r["chunk_index"], ds))
        raws = dict(zip(keys, reader.fetch(url, ranges)))
        local = {k: [] for k in _EMPTY}
        for r in rs:
            dec = _decode_chunk(raws, r)
            lat, lon, h, dt, q = dec["latitude"], dec["longitude"], dec["h_li"], dec["delta_time"], dec["atl06_quality_summary"]
            m = np.isfinite(h) & (h < 3.0e38) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
            if quality_zero:
                m &= (q == 0)
            if not m.any():
                continue
            local["lon"].append(lon[m].astype("f8")); local["lat"].append(lat[m].astype("f8")); local["h"].append(h[m].astype("f8"))
            local["t"].append(_atlas_epoch_years(dt[m], r["sdp_epoch"])); local["quality"].append(q[m])
        return local

    urls = list(by_url)
    nw = pool_size(len(urls), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                       cpu_bound=False)
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
