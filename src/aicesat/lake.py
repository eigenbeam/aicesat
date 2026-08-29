"""Persistent Parquet lake (spec §7) + coverage metadata (§5.7) + DuckDB queries (§8).

Layout: data/lake/mission=<M>/h3_cell=<int>/<granule>__<beam>.parquet — one file per (cell, granule, beam).
Idempotency is by construction: re-materializing the same (granule, beam) rewrites identical files, and every row
also carries a deterministic surrogate row_id (§7.3) for cross-check. Each photon is written to the cell of its OWN
lat/lon, so a chunk straddling cells never double-counts. Co-registered coordinates are materialized at ingest (§7.4).
"""
from __future__ import annotations

import logging
import os
import queue
import zlib
from datetime import datetime, timezone
from typing import NamedTuple

import threading
import time

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import cache
from .index import INDEX_DIR

log = logging.getLogger(__name__)

LAKE_DIR = cache.DATA_DIR / "lake"
META_DB = INDEX_DIR / "meta.duckdb"
COMMON_EPOCH = 2005.0  # materialized coreg epoch; re-epoching = re-run materialize (§7.4)


def row_ids(granule: str, beam: str, photon_index: np.ndarray) -> np.ndarray:
    """Deterministic surrogate key: crc32(granule/beam) in the high 32 bits, photon index in the low 32 bits."""
    hi = np.uint64(zlib.crc32(f"{granule}/{beam}".encode()) & 0xFFFFFFFF) << np.uint64(32)
    return hi | photon_index.astype("u8")


def cell_dir(mission: str, cell: int):
    return LAKE_DIR / f"mission={mission}" / f"h3_cell={int(cell)}"


def write_photons(mission: str, granule: str, beam: str, ph: dict[str, np.ndarray]) -> list[int]:
    """ph: lon, lat, h, conf, t (datetime64[ms]), photon_index, chunk_index, h3_cell, coreg_lon, coreg_lat.
    Writes one file per cell; returns the cells written."""
    cells = np.unique(ph["h3_cell"])
    for cell in cells:
        m = ph["h3_cell"] == cell
        tbl = pa.table({
            "row_id": pa.array(row_ids(granule, beam, ph["photon_index"][m]), type=pa.uint64()),
            "native_lon": ph["lon"][m], "native_lat": ph["lat"][m], "native_height": ph["h"][m].astype("f8"),
            "height_ref": pa.array(["WGS84 ellipsoid"] * int(m.sum())).dictionary_encode(),
            "native_frame": pa.array(["ITRF2014"] * int(m.sum())).dictionary_encode(),
            "t": pa.array(ph["t"][m].astype("datetime64[ms]")),
            "coreg_lon": ph["coreg_lon"][m], "coreg_lat": ph["coreg_lat"][m],
            "coreg_epoch": pa.array(np.full(int(m.sum()), COMMON_EPOCH)),
            "signal_conf_landice": ph["conf"][m].astype("i1"),
            "source_granule": pa.array([granule] * int(m.sum())).dictionary_encode(),
            "beam": pa.array([beam] * int(m.sum())).dictionary_encode(),
            "photon_index": ph["photon_index"][m].astype("i8"),
            "source_chunk_index": ph["chunk_index"][m].astype("i4"),
        })
        d = cell_dir(mission, int(cell)); d.mkdir(parents=True, exist_ok=True)
        # rows are in photon (along-track) order, so ROW_GROUP_ROWS-row groups carry tight lat/lon min/max statistics
        # and DuckDB skips the groups a bbox predicate cannot touch (spec §5.6 sort order, along-track for v1)
        pq.write_table(tbl, d / f"{granule}__{beam}.parquet", compression="zstd", row_group_size=ROW_GROUP_ROWS)
    return [int(c) for c in cells]


ROW_GROUP_ROWS = 65_536


def _stem(granule: str) -> str:
    """Filename-safe, unambiguous granule stem (drop the extension, no '/' or '__' so cell_stats can split on '__')."""
    return granule.rsplit(".", 1)[0].replace("/", "_").replace("__", "_")


def write_point_chunk(mission: str, granule: str, beam: str, chunk_index: int, arrays: dict, res: int,
                      extras: tuple[str, ...] = (), only_cells=None) -> dict[int, list[int]]:
    """Chunk-aware materialization for the index missions (ATL06/GLAS/ICESSN), the analogue of write_photons.

    `arrays` carries ONE fetched chunk's FULL, pre-bbox-mask points (lon, lat, h, t as datetime64[ms], plus any name
    listed in `extras`, e.g. 'quality'). Each point is written to the file of its OWN H3 cell **at this mission's res**
    (never res 6), so a later query for the same cell but a different sub-bbox re-filters correctly — the partial-cell
    bug is impossible by construction. One Parquet file per cell, `<gstem>__<beam>__c<chunk>.parquet`, carrying a
    `source_chunk_index` column. Returns {chunk_index: [cells written]} suitable for mark_ingested.

    `only_cells` (optional) restricts writing to that set of cells — used by ICESSN, whose per-cell byte spans overlap,
    so a fetched span must materialize ONLY the cells it was fetched for (writing a sibling cell from a partial span
    would reintroduce the partial-cell bug). ATL06/GLAS pass None: a fetched chunk materializes every cell it touches.
    """
    tables = _cell_tables(granule, beam, chunk_index, arrays, res, extras, only_cells)
    beam = beam or "na"; gstem = _stem(granule)
    for cell, tbl in tables.items():
        d = cell_dir(mission, cell); d.mkdir(parents=True, exist_ok=True)
        pq.write_table(tbl, d / f"{gstem}__{beam}__c{int(chunk_index)}.parquet",
                       compression="zstd", row_group_size=ROW_GROUP_ROWS)
    return {int(chunk_index): sorted(tables)}


def _cell_tables(granule: str, beam: str, chunk_index: int, arrays: dict, res: int,
                 extras: tuple[str, ...], only_cells) -> dict[int, "pa.Table"]:
    """One chunk's points split into {cell: Arrow table}. Shared by the per-chunk and per-granule write paths so both
    produce byte-identical column layouts."""
    from . import planner

    lon = np.asarray(arrays["lon"], "f8"); lat = np.asarray(arrays["lat"], "f8")
    h = np.asarray(arrays["h"], "f8"); t = np.asarray(arrays["t"]).astype("datetime64[ms]")
    ok = np.isfinite(lon) & np.isfinite(lat)          # a point with no valid position cannot be placed in a cell
    if not ok.any():
        return {}
    cells = np.zeros(lon.size, "u8")
    cells[ok] = planner._cells_vectorized(lat[ok], lon[ok], res)
    keep_cells = None if only_cells is None else {int(c) for c in only_cells}
    out: dict[int, pa.Table] = {}
    for cell in np.unique(cells[ok]):
        if keep_cells is not None and int(cell) not in keep_cells:
            continue
        m = ok & (cells == cell)
        nn = int(m.sum())
        cols = {"native_lon": lon[m], "native_lat": lat[m], "native_height": h[m], "t": pa.array(t[m]),
                "source_granule": pa.array([granule] * nn).dictionary_encode(),
                "beam": pa.array([beam or "na"] * nn).dictionary_encode(),
                "source_chunk_index": np.full(nn, int(chunk_index), "i4")}
        for ex in extras:
            cols[ex] = np.asarray(arrays[ex])[m]
        out[int(cell)] = pa.table(cols)
    return out


BATCH_WRITE_ENV = "AICESAT_LAKE_BATCH_WRITES"
BATCH_SUFFIX = "cb"      # the accumulating per-(cell, granule, beam) file; still matches query_points' `*__c*` glob


def batch_writes_enabled() -> bool:
    """EXPERIMENTAL. Set AICESAT_LAKE_BATCH_WRITES=1 to write one Parquet file per (cell, granule, beam) instead of
    one per (cell, chunk). Off by default until the win is measured on the box — see write_point_chunks."""
    return os.environ.get(BATCH_WRITE_ENV, "0").lower() in ("1", "true", "yes")


def write_point_chunks(mission: str, chunks, res: int, extras: tuple[str, ...] = ()) -> dict[int, list[int]]:
    """Materialize a whole granule's chunks with ONE file per (cell, granule, beam) instead of one per (cell, chunk).

    Motivation, measured on the box: a 4,566-chunk leg spent 115.9 write thread-seconds over ~32k files, ~3.6 ms each,
    and more writer threads made it worse — a fixed serialized resource, so the lever is fewer files.

    The file name is DETERMINISTIC, which is the whole design. A first attempt named each file by the chunk SET it
    held, which meant discovering what already existed with `d.glob(...)` per cell per job: a directory scan whose
    cost grows with the LAKE rather than the request — the same shape as the 145 s query_points bug — and it made a
    batched build 36% SLOWER than per-chunk (84.9 s vs 62.5 s). A fixed name turns that into one O(1) exists().

    A re-fetch therefore MERGES: read the existing file, drop the rows for the chunks being rewritten, concatenate the
    new ones. That is strictly better than the set-naming scheme it replaces — no data loss on a partial re-fetch and
    no duplication — and it costs one small read only when the file is already there. The write is tmp+replace, so a
    concurrent query_points never sees a half-rewritten accumulation.
    """
    per_cell: dict[int, list] = {}                     # cell -> [(chunk_index, table)]
    written: dict[int, list[int]] = {int(c.chunk_index): [] for c in chunks}
    for cw in chunks:
        for cell, tbl in _cell_tables(cw.granule, cw.beam, cw.chunk_index, cw.arrays, res, extras, cw.only_cells).items():
            per_cell.setdefault(cell, []).append((int(cw.chunk_index), tbl))
    if not per_cell:
        return written
    granule, beam = chunks[0].granule, (chunks[0].beam or "na")
    gstem = _stem(granule)
    for cell, items in per_cell.items():
        d = cell_dir(mission, cell); d.mkdir(parents=True, exist_ok=True)
        new = sorted({ci for ci, _ in items})
        out = d / f"{gstem}__{beam}__{BATCH_SUFFIX}.parquet"
        tabs = [t for _ci, t in items]
        if out.exists():                               # O(1) — never a directory listing
            old = pq.read_table(out)
            m = ~np.isin(np.asarray(old.column("source_chunk_index")), np.asarray(new, "i4"))
            if bool(m.any()):
                tabs.insert(0, old.filter(pa.array(m)))
        tmp = d / f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        pq.write_table(pa.concat_tables(tabs) if len(tabs) > 1 else tabs[0], tmp,
                       compression="zstd", row_group_size=ROW_GROUP_ROWS)
        os.replace(tmp, out)                           # atomic: a reader sees the old file or the new one, never both
        for ci in new:                                 # supersede any per-chunk file for exactly these chunks (O(1))
            (d / f"{gstem}__{beam}__c{ci}.parquet").unlink(missing_ok=True)
            written[ci].append(cell)
    return {ci: sorted(cs) for ci, cs in written.items()}


STREAM_BATCHES = 6   # cell groups per streamed read: enough to look continuous, few enough not to add query overhead


def query_points(bbox, cells: list[int], mission: str, granules: list[str] | None = None,
                 beams: list[str] | None = None, extra_cols: tuple[str, ...] = (), quality_zero: bool = False,
                 clip_cells: bool = False, on_batch=None) -> dict:
    """Read materialized index-mission points back, optionally emitting them progressively.

    `on_batch` (opt-in): read the cells in groups and hand each group's points to the callback as they land, then
    return the same concatenated result. Data served from the LAKE otherwise arrives in one blocking call at the end
    of a leg, so a build with a warm lake showed no progress at all and then the whole cloud appeared at once. Cells
    partition the points (every point lives in exactly one cell's files), so the concatenation is the same set the
    single-query path returns. Default (None) is byte-for-byte the previous behaviour.
    """
    if on_batch is not None and len(cells) > 1:
        groups, per = [], max(1, -(-len(cells) // STREAM_BATCHES))
        for i in range(0, len(cells), per):
            groups.append(cells[i:i + per])
        parts = []
        for g in groups:
            r = query_points(bbox, g, mission, granules=granules, beams=beams, extra_cols=extra_cols,
                             quality_zero=quality_zero, clip_cells=clip_cells)
            if r["lon"].size:
                on_batch(r)
            parts.append(r)
        keys = parts[0].keys() if parts else ("lon", "lat", "h", "t")
        return {k: (np.concatenate([p[k] for p in parts]) if parts else np.array([])) for k in keys}
    return _query_points(bbox, cells, mission, granules, beams, extra_cols, quality_zero, clip_cells)


def _query_points(bbox, cells: list[int], mission: str, granules: list[str] | None = None,
                  beams: list[str] | None = None, extra_cols: tuple[str, ...] = (), quality_zero: bool = False,
                  clip_cells: bool = False) -> dict:
    """Read materialized index-mission points back (the analogue of query_photons): DuckDB over the mission's cell
    files with cell + bbox (+ granule, + beam, + optional quality) predicate pushdown. Returns the SAME dict shape the
    mission's fetch_bbox returns: {'lon','lat','h','t'} plus each name in `extra_cols`.

    The glob targets only chunk files (`*__c*.parquet`), so it never picks up ATL03 photon files or a legacy lossy
    `__pts.parquet`. `granules` restricts the read to the granules the caller's window selected — trimming an
    accumulated lake (wider granule set from a prior, broader query) to exactly this request, byte-identically to a
    direct fetch. `beams` likewise restricts to the beams the query selected, so a strong-only ATL06 query never picks
    up weak-beam points a prior all-beam query may have materialized in the same cell.

    `clip_cells` (opt-in) drops the rectangular native_lat/native_lon bbox predicate and keeps ONLY the cell-membership
    predicate (`h3_cell IN cells`). The lake partitions every point into the file of its OWN res-`res` cell (computed
    at ingest with planner._cells_vectorized — see write_point_chunk), so `h3_cell IN cells` is exactly "keep points
    whose H3 cell at this mission's resolution is in the touched-cell set" — the hex-membership clip the caller wants
    for a polygon / hex-aligned box. Default (False) keeps today's rectangular-bbox behaviour byte-for-byte (the golden
    the lake-cache tests and bench_vs_h5coro rely on)."""
    base = {"lon": np.array([]), "lat": np.array([]), "h": np.array([]), "t": np.array([]),
            **{c: np.array([]) for c in extra_cols}}
    if not cells or not LAKE_DIR.exists():
        return base
    # Address ONLY the requested cells' directories. A single `h3_cell=*` glob makes DuckDB stat and read the schema of
    # every chunk file in the mission (measured: ~500k files for ATL06 on the deployed box -> 145 s) before the
    # h3_cell predicate can prune it, because union_by_name must reconcile schemas across the whole file list first.
    # One glob per requested cell keeps that proportional to the query (~1.7k files) instead of the lake.
    globs = []
    for c in cells:
        d = cell_dir(mission, int(c))
        if next(d.glob("*__c*.parquet"), None) is not None:   # skip empty/absent cells: DuckDB errors on a glob that matches nothing
            globs.append(f"{d}/*__c*.parquet")
    if not globs:
        return base
    glob = "[" + ", ".join("'" + g + "'" for g in globs) + "]"   # list-of-globs form
    w, s, e, n = bbox
    cond = [f"h3_cell IN ({','.join(str(int(c)) for c in cells)})"]
    if not clip_cells:
        cond += [f"native_lat BETWEEN {s} AND {n}", f"native_lon BETWEEN {w} AND {e}"]
    if granules is not None:
        cond.append("source_granule IN (" + ",".join("'" + g + "'" for g in granules) + ")")
    if beams is not None:
        cond.append("beam IN (" + ",".join("'" + b + "'" for b in beams) + ")")
    src = f"read_parquet({glob}, hive_partitioning = true, union_by_name = true)"
    con = duckdb.connect()
    try:
        # Schema-tolerant extra columns: a column added to write_point_chunk's `extras` after some chunks were already
        # ingested (e.g. ICESSN platelet slopes) is absent from those older files. union_by_name backfills a column
        # NULL only if it exists in SOME file; if it exists in NONE, selecting it is a binder error. So select only the
        # columns actually present and return the rest as NaN — old chunks degrade gracefully (flat platelets) until
        # they are re-ingested, rather than crashing the whole query.
        have = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()} if extra_cols else set()
        present = [c for c in extra_cols if c in have]
        absent = [c for c in extra_cols if c not in have]
        if quality_zero and "quality" in present:
            cond.append("quality = 0")
        sel = "native_lon, native_lat, native_height, t" + "".join(f", {c}" for c in present)
        r = con.execute(f"SELECT {sel} FROM {src} WHERE {' AND '.join(cond)}").fetchnumpy()
    finally:
        con.close()
    out = {"lon": np.asarray(r["native_lon"], "f8"), "lat": np.asarray(r["native_lat"], "f8"),
           "h": np.asarray(r["native_height"], "f8"), "t": np.asarray(r["t"]).astype("datetime64[ms]")}
    n = out["lon"].size
    for c in present:
        out[c] = np.asarray(r[c])
    for c in absent:
        out[c] = np.full(n, np.nan)
    return out


def write_points(mission: str, arrays: dict, meta: dict) -> list[int]:
    """Materialize a per-scene point collection (GLAS / ATL06 / ICESSN) into the lake so it appears in the Lake view
    as its own collection. Minimal schema (native lon/lat/height/t + provenance) written per H3 res-6 cell, one file
    per (granule, cell) named `<granule>__pts.parquet` — cell_stats reads only Parquet metadata + the filename, so
    this is enough to show cells, rows, bytes and granules. Returns the cells written."""
    from . import index, planner

    lon = np.asarray(arrays["lon"], "f8"); lat = np.asarray(arrays["lat"], "f8")
    h = np.asarray(arrays["h"], "f8"); t = np.asarray(arrays["t"]).astype("datetime64[ms]")
    gidx = np.asarray(arrays.get("granule_idx", np.zeros(lon.size, "i2")))
    names = [g["granule"] for g in meta.get("granules", [])] or ["points"]
    ok = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(h)
    if not ok.any():
        return []
    cells = np.zeros(lon.size, "u8")
    cells[ok] = planner._cells_vectorized(lat[ok], lon[ok], index.H3_RES)
    ref = meta.get("height_ref", "WGS84 ellipsoid")
    written = set()
    for gi in np.unique(gidx[ok]):
        gname = names[int(gi)] if int(gi) < len(names) else f"g{int(gi)}"
        gname = gname.rsplit(".", 1)[0].replace("/", "_").replace("__", "_")   # safe, unambiguous filename stem
        gm = ok & (gidx == gi)
        for cell in np.unique(cells[gm]):
            m = gm & (cells == cell)
            nn = int(m.sum())
            tbl = pa.table({"native_lon": lon[m], "native_lat": lat[m], "native_height": h[m],
                            "t": pa.array(t[m]), "height_ref": pa.array([ref] * nn).dictionary_encode(),
                            "source_granule": pa.array([gname] * nn).dictionary_encode()})
            d = cell_dir(mission, int(cell)); d.mkdir(parents=True, exist_ok=True)
            pq.write_table(tbl, d / f"{gname}__pts.parquet", compression="zstd", row_group_size=ROW_GROUP_ROWS)
            written.add(int(cell))
    log.info("%s: materialized %d points into %d lake cells", mission, int(ok.sum()), len(written))
    return sorted(written)


def relayout(mission: str = "ICESAT2") -> dict:
    """Rewrite existing lake files with ROW_GROUP_ROWS-row groups (no network). Idempotent."""
    import time

    t0, n_files, n_rows = time.time(), 0, 0
    for f in LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet"):
        md = pq.read_metadata(f)
        if md.num_row_groups >= max(1, md.num_rows // ROW_GROUP_ROWS):
            continue
        tbl = pq.read_table(f)
        tmp = f.with_suffix(".tmp")
        pq.write_table(tbl, tmp, compression="zstd", row_group_size=ROW_GROUP_ROWS)
        tmp.replace(f)
        n_files += 1; n_rows += tbl.num_rows
    return {"files_rewritten": n_files, "rows": n_rows, "seconds": round(time.time() - t0, 1)}


SETTINGS_PATH = INDEX_DIR / "settings.json"
EVICTION_LOG = INDEX_DIR / "evictions.jsonl"
DEFAULT_MAX_BYTES = 5 << 30  # 5 GB


_META_LOCK = threading.RLock()  # DuckDB takes an exclusive file lock; serialise all opens in this process


class _Meta:
    """Context manager: hold the process lock for the whole open->query->close, with a short retry if another
    process (a stray CLI, a second server) still holds the OS lock."""

    def __enter__(self) -> duckdb.DuckDBPyConnection:
        _META_LOCK.acquire()
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        for attempt in range(20):
            try:
                self.con = _open_meta()
                return self.con
            except duckdb.IOException:
                if attempt == 19:
                    _META_LOCK.release(); raise
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            self.con.close()
        finally:
            _META_LOCK.release()


def meta_db() -> "_Meta":
    return _Meta()


def _open_meta() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(META_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS coverage_cells (
        mission VARCHAR, granule VARCHAR, beam VARCHAR, chunk_index INTEGER, h3_cell UBIGINT, ingested_at TIMESTAMP,
        PRIMARY KEY (mission, granule, beam, chunk_index, h3_cell))""")
    # one-time migration from the list-valued table
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'coverage'").fetchone()[0]:
        con.execute("""INSERT OR IGNORE INTO coverage_cells
                       SELECT mission, granule, beam, chunk_index, unnest(h3_cells), ingested_at FROM coverage""")
        con.execute("DROP TABLE coverage")
    return con


def meta_conn() -> duckdb.DuckDBPyConnection:  # legacy: unlocked open (tests/CLI); server paths use meta_db()
    return _open_meta()


def mark_ingested(mission: str, granule: str, beam: str, chunk_cells: dict[int, list[int]]) -> None:
    mark_ingested_many(mission, [(granule, beam, chunk_cells)])


def mark_ingested_many(mission: str, items) -> None:
    """Record many (granule, beam, {chunk: cells}) ingests in ONE meta.duckdb transaction.

    Opening meta.duckdb is expensive (file lock + connect + close) and every open is serialised process-wide by
    _META_LOCK, so calling this per granule made it the single largest cost of a cold build: measured 44.5 s over 301
    calls (~148 ms each) on a leg whose whole wall time was 63.6 s — more than the network fetch. One open for the
    whole batch makes it negligible.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    gran, beams, chunks, cells_, = [], [], [], []
    for granule, beam, chunk_cells in items:
        for k, cells in (chunk_cells or {}).items():
            for c in cells:
                gran.append(granule); beams.append(beam); chunks.append(int(k)); cells_.append(int(c))
    if not gran:
        return
    # Bulk-insert from Arrow, NOT executemany. DuckDB is columnar: a parameterised executemany walks row by row and
    # every row probes the 5-column composite PRIMARY KEY, which measured 31.7 s for one batch on the deployed lake
    # (and 44.5 s when it was additionally called per granule). Registering the batch and inserting with one statement
    # is vectorised and does the constraint work in bulk.
    batch = pa.table({"mission": pa.array([mission] * len(gran)), "granule": pa.array(gran),
                      "beam": pa.array(beams), "chunk_index": pa.array(chunks, type=pa.int32()),
                      "h3_cell": pa.array(cells_, type=pa.uint64()),
                      "ingested_at": pa.array([now] * len(gran), type=pa.timestamp("us"))})
    with meta_db() as con:
        con.register("_ingest_batch", batch)
        try:
            con.execute("INSERT OR REPLACE INTO coverage_cells SELECT * FROM _ingest_batch")
        finally:
            con.unregister("_ingest_batch")


def ingested_chunk_cells(mission: str, granules: list[str]) -> set[tuple[str, str, int, int]]:
    """(granule, beam, chunk_index, h3_cell) tuples already materialized."""
    if not META_DB.exists() or not granules:
        return set()
    with meta_db() as con:
        rows = con.execute("SELECT granule, beam, chunk_index, h3_cell FROM coverage_cells WHERE mission = ? AND granule IN (" +
                           ",".join("?" * len(granules)) + ")", [mission, *granules]).fetchall()
    return {(g, b, int(k), int(c)) for g, b, k, c in rows}


def ingested_chunks(mission: str, granules: list[str]) -> set[tuple[str, str, int]]:
    """Chunks with at least one materialized cell (kept for callers that only need chunk identity)."""
    return {(g, b, k) for g, b, k, _ in ingested_chunk_cells(mission, granules)}


# ------------------------------------------------------------------------ background writer (ingest off the response)

WRITE_QUEUE_MAX = 8        # granule-jobs in flight. Each holds one granule's decoded arrays (~10 MB for ATL06), so this
                           # bounds writer memory at ~80 MB and blocks the fetch thread past it: backpressure, not growth.
MARK_ROWS_PER_FLUSH = 4096  # coverage rows to accumulate before opening meta.duckdb (see _Writer._flush)
ASYNC_WRITE_ENV = "AICESAT_LAKE_ASYNC_WRITE"
WRITER_THREADS_ENV = "AICESAT_LAKE_WRITERS"
WRITER_THREADS = 2


def async_writes_enabled() -> bool:
    """Kill switch. Set AICESAT_LAKE_ASYNC_WRITE=0 to write inline on the calling thread (the pre-writer behaviour)."""
    return os.environ.get(ASYNC_WRITE_ENV, "1").lower() not in ("0", "false", "no")


def _writer_threads() -> int:
    """Writer pool size, AICESAT_LAKE_WRITERS-overridable. Read at first use, not at import, so a benchmark can sweep
    it. Two is not obviously right: a 4,145 MB leg spent 126.3 write thread-seconds, which is ~63 s of wall on two
    threads against ~42 s of fetch wall on four — i.e. the WRITER, not the network, was the slower stage."""
    try:
        return max(1, int(os.environ.get(WRITER_THREADS_ENV, "") or WRITER_THREADS))
    except ValueError:
        return WRITER_THREADS


class ChunkWrite(NamedTuple):
    """One decoded chunk queued for materialization.

    `only_cells` restricts which cells are written (ICESSN's byte spans overlap, so a fetched span must materialize
    ONLY the cells it was fetched for). `mark_cells` are cells to record as ingested even if they carried no valid
    points, so an all-fill cell is not re-fetched forever.
    """
    granule: str
    beam: str
    chunk_index: int
    arrays: dict
    only_cells: tuple | None = None
    mark_cells: tuple = ()


class _Writer:
    """Drains lake Parquet writes off the request thread.

    Measured motivation: a cold ATL06 leg spent 34.8 of 88.6 pool thread-seconds (39%) inside write_point_chunk. The
    request does not need those files — it needs the POINTS, and the fetch already holds them in memory. The lake is a
    cache for LATER requests, so filling it is housekeeping, like eviction (enforce_global_limit_async).

    Two ordering guarantees the callers' correctness rests on:
      * coverage is marked only AFTER the file is on disk, so a crash or a failed write loses cache, never truth — an
        unmarked chunk is simply re-fetched next time;
      * `drain(mission, cells)` blocks until every queued job touching those cells is written AND marked, so a second
        build over the same area never reads a half-written lake or a stale ingested_chunk_cells.
    """

    def __init__(self, workers: int | None = None, maxsize: int = WRITE_QUEUE_MAX):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._pending: dict[tuple[str, int], int] = {}   # (mission, cell) -> unsettled jobs touching it
        self._done: list[tuple] = []                     # finished jobs awaiting one batched coverage mark
        self._flushing = threading.Lock()
        self._workers, self._started = workers, False
        self._inflight = 0               # jobs a worker is currently writing (see _loop's flush condition)
        self.jobs_written = 0
        self.chunk_errors = 0

    # -- producer ---------------------------------------------------------------------------------------------------
    def submit(self, mission: str, res: int, chunks, cells, extras: tuple[str, ...] = ()) -> None:
        """Queue one granule's chunks. `cells` are the cells this job settles — what a concurrent drain() waits for."""
        chunks = [c for c in chunks if c.arrays["lon"].size or c.mark_cells]
        if not chunks:
            return
        cells = tuple(int(c) for c in cells)
        with self._lock:
            for c in cells:
                self._pending[(mission, c)] = self._pending.get((mission, c), 0) + 1
        job = (mission, res, chunks, cells, tuple(extras))
        if not async_writes_enabled():
            # Inline, and WITHOUT a forced coverage flush: the queue is always empty in this mode, so forcing would
            # open meta.duckdb once per granule — the ~150 ms/open cost mark_ingested_many exists to avoid. The caller
            # ends the leg with drain_writes(), which flushes the accumulated marks in one transaction, matching the
            # single batched mark the pre-writer path did.
            self._run_job(job)
            self._flush(force=False)      # row-batched only; the leg's drain does the final one
            return
        self._ensure_workers()
        self._q.put(job)          # blocks past WRITE_QUEUE_MAX — the fetch pool throttles to the writer's rate

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            n = self._workers if self._workers is not None else _writer_threads()
        for i in range(n):
            threading.Thread(target=self._loop, name=f"lake-writer-{i}", daemon=True).start()

    # -- consumer ---------------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = self._q.get()
            with self._lock:
                self._inflight += 1
            try:
                self._run_job(job)
            finally:
                with self._lock:
                    self._inflight -= 1
                    idle = self._inflight == 0
                # Force a coverage flush only when the writer has genuinely nothing left, not merely because the queue
                # momentarily drained: with N threads finishing the last N jobs, "queue is empty" is true for each of
                # them, so that test opened meta.duckdb once per job — the ~150 ms/open cost mark_ingested_many
                # exists to remove. A long leg keeps the queue full and never noticed; a short one paid it every time.
                self._flush(force=idle and self._q.empty())
                self._q.task_done()

    def _run_job(self, job) -> None:
        mission, res, chunks, cells, extras = job
        per_gb: dict[tuple[str, str], dict] = {}
        # A job carries one granule but possibly several beams; the batch path writes one file per cell per
        # (granule, beam), so group before calling it.
        by_gb: dict[tuple[str, str], list] = {}
        for cw in chunks:
            by_gb.setdefault((cw.granule, cw.beam), []).append(cw)
        for (granule, beam), group in by_gb.items():
            if batch_writes_enabled():
                try:
                    cc = write_point_chunks(mission, group, res, extras=extras)
                except Exception:   # a failed batch loses only this (granule, beam); it stays unmarked, so re-fetched
                    self.chunk_errors += len(group)
                    log.exception("lake writer: %s %s/%s batch of %d chunks failed to write; it will be re-fetched",
                                  mission, granule, beam, len(group))
                    continue
                for cw in group:
                    per_gb.setdefault((granule, beam), {})[cw.chunk_index] = sorted(set(cc[cw.chunk_index]) | set(cw.mark_cells))
                continue
            for cw in group:
                try:
                    cc = write_point_chunk(mission, cw.granule, cw.beam, cw.chunk_index, cw.arrays, res,
                                           extras=extras, only_cells=cw.only_cells)
                except Exception:   # one bad chunk must not lose the granule's other chunks, nor mark itself ingested
                    self.chunk_errors += 1
                    log.exception("lake writer: %s %s/%s chunk %s failed to write; it will be re-fetched",
                                  mission, cw.granule, cw.beam, cw.chunk_index)
                    continue
                per_gb.setdefault((cw.granule, cw.beam), {})[cw.chunk_index] = sorted(set(cc[cw.chunk_index]) | set(cw.mark_cells))
        with self._lock:
            self._done.append((mission, [(g, b, cm) for (g, b), cm in per_gb.items()], cells))
            self.jobs_written += 1

    def _flush(self, force: bool = False) -> None:
        """Mark every completed job's coverage in ONE meta.duckdb transaction, then release the cells it pinned.

        Opening meta.duckdb costs ~150 ms and is serialised process-wide by _META_LOCK, so marking per job would put
        back exactly the cost mark_ingested_many removed — on a background thread, but still holding a lock the next
        foreground ingested_chunk_cells needs.
        """
        if not self._flushing.acquire(blocking=False):
            return                      # another worker is flushing; it takes whatever is in _done, and drain() re-tries
        try:
            with self._lock:
                rows = sum(len(cm) for _m, items, _c in self._done for _g, _b, cm in items)
                if not self._done or not (force or rows >= MARK_ROWS_PER_FLUSH):
                    return
                batch, self._done = self._done, []
            by_mission: dict[str, list] = {}
            for mission, items, _cells in batch:
                by_mission.setdefault(mission, []).extend(items)
            try:
                for mission, items in by_mission.items():
                    mark_ingested_many(mission, items)
            finally:
                self._release(batch)    # release the cells even if the mark failed: the files are on disk either way
        finally:
            self._flushing.release()

    def _release(self, batch) -> None:
        with self._lock:
            for mission, _items, cells in batch:
                for c in cells:
                    k = (mission, c)
                    n = self._pending.get(k, 0) - 1
                    if n > 0:
                        self._pending[k] = n
                    else:
                        self._pending.pop(k, None)
            self._settled.notify_all()

    # -- consistency barrier ----------------------------------------------------------------------------------------
    def drain(self, mission: str | None = None, cells=None, timeout: float = 300.0) -> bool:
        """Block until every queued write touching `cells` is on disk and marked (all cells when `cells` is None).

        Immediate when nothing is in flight, which is the common case — this only costs anything when a second build
        overlaps the first over the same cells, and that is exactly when reading a partially written lake would be
        wrong.
        """
        want = None if cells is None else {(mission, int(c)) for c in cells}
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                busy = bool(self._pending) if want is None else any(k in self._pending for k in want)
                if not busy:
                    return True
                self._settled.wait(0.25)
            self._flush(force=True)     # a finished job can sit in _done if another worker held the flush lock
            if time.monotonic() > deadline:
                log.warning("lake writer: drain timed out with %d cell(s) still pending", len(self._pending))
                return False


_WRITER = _Writer()


def submit_writes(mission: str, res: int, chunks, cells, extras: tuple[str, ...] = ()) -> None:
    """Hand one granule's decoded chunks to the background writer. See _Writer for the ordering contract."""
    _WRITER.submit(mission, res, chunks, cells, extras=extras)


def drain_writes(mission: str | None = None, cells=None, timeout: float = 300.0) -> bool:
    """Barrier: wait until background writes over `cells` are on disk and marked. Call before reading the lake."""
    return _WRITER.drain(mission, cells, timeout)


def concat_arrays(parts, keys) -> dict:
    """Concatenate result dicts over `keys`, dropping empty parts.

    query_points returns a float64 empty array for EVERY key when a cell set is unmaterialized, including `t`, which
    would silently demote a datetime64 concatenation to float. Dropping empties keeps the dtypes the fetch produced.
    """
    parts = [p for p in parts if p is not None and np.asarray(p["lon"]).size]
    if not parts:
        return {k: np.array([]) for k in keys}
    if len(parts) == 1:
        return {k: np.asarray(parts[0][k]) for k in keys}
    return {k: np.concatenate([np.asarray(p[k]) for p in parts]) for k in keys}


# ----------------------------------------------------------------------------- per-cell stats, settings, eviction

def cell_stats(mission: str = "ICESAT2", with_rows: bool = True) -> dict[int, dict]:
    """Per materialized cell: granules, beams, chunks, rows, bytes, first/last ingested. Files are the source of truth
    for bytes/rows (Parquet footers), the coverage table for provenance and age.

    `with_rows=False` skips the per-file Parquet footer read and reports rows=0. Row counts are the ONLY thing here
    that needs to open files at all — bytes come from stat() — and they cost a footer read per file across the whole
    mission. On the deployed lake (864k files) that was ~300 s, and eviction, which needs only bytes and age, paid it
    on every build that fetched anything. Callers that just size the lake must pass with_rows=False.
    """
    out: dict[int, dict] = {}
    if not LAKE_DIR.exists():
        return out
    for cdir in LAKE_DIR.glob(f"mission={mission}/h3_cell=*"):
        cell = int(cdir.name.split("=")[1])
        files = list(cdir.glob("*.parquet"))
        if not files:
            continue
        rows = 0
        if with_rows:
            for f in files:
                try:
                    rows += pq.read_metadata(f).num_rows
                except Exception:
                    pass
        out[cell] = {"cell": cell, "files": len(files), "rows": rows, "bytes": sum(f.stat().st_size for f in files),
                     "granules": sorted({f.name.split("__")[0] for f in files}), "beams": sorted({f.stem.split("__")[1] for f in files}),
                     "chunks": 0, "first_ingested": None, "last_ingested": None}
    if META_DB.exists() and out:
        with meta_db() as con:
            grp = con.execute("SELECT h3_cell, count(*), min(ingested_at), max(ingested_at) FROM coverage_cells "
                              "WHERE mission = ? GROUP BY h3_cell", [mission]).fetchall()
        for cell, n, first, last in grp:
            if int(cell) in out:
                out[int(cell)].update(chunks=int(n), first_ingested=first.isoformat() if first else None,
                                      last_ingested=last.isoformat() if last else None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for st in out.values():
        st["age_s"] = (now - datetime.fromisoformat(st["last_ingested"])).total_seconds() if st["last_ingested"] else None
    return out


def get_settings() -> dict:
    import json
    d = {"max_bytes": DEFAULT_MAX_BYTES}
    if SETTINGS_PATH.exists():
        try:
            d.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:
            pass
    return d


def set_settings(**kw) -> dict:
    import json
    d = get_settings(); d.update({k: v for k, v in kw.items() if v is not None})
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d))
    return d


def evict_cells(cells, mission: str = "ICESAT2", reason: str = "manual", stats: dict | None = None) -> list[dict]:
    """Delete the cells' Parquet files and coverage rows (the index is untouched); returns what was evicted.

    `stats` lets a caller that already walked the mission pass its cell_stats in. The walk stats() every file in the
    mission, so an enforce_*_limit that computed it and then called this re-walked the whole lake a second time."""
    import json
    import shutil

    if stats is None:
        stats = cell_stats(mission, with_rows=False)   # eviction needs bytes + age, never row counts (footer reads)
    evicted = []
    with meta_db() as con:
        for c in cells:
            c = int(c)
            st = stats.get(c)
            if st is None:
                continue
            shutil.rmtree(cell_dir(mission, c), ignore_errors=True)
            con.execute("DELETE FROM coverage_cells WHERE mission = ? AND h3_cell = ?", [mission, c])
            evicted.append({"cell": c, "bytes": st["bytes"], "rows": st["rows"], "last_ingested": st["last_ingested"], "reason": reason,
                            "evicted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if evicted:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with EVICTION_LOG.open("a") as f:
            for rec in evicted:
                f.write(json.dumps(rec) + "\n")
    return evicted


def enforce_limit(protect=(), mission: str = "ICESAT2") -> list[dict]:
    """Evict least-recently-ingested cells until the lake is under max_bytes; never touches `protect` cells."""
    limit = int(get_settings()["max_bytes"])
    stats = cell_stats(mission, with_rows=False)   # sizing only — skip the per-file footer reads
    total = sum(s["bytes"] for s in stats.values())
    if total <= limit:
        return []
    protect = {int(c) for c in protect}
    victims = sorted((s for c, s in stats.items() if c not in protect), key=lambda s: (s["last_ingested"] or "", s["cell"]))
    chosen = []
    for s in victims:
        if total <= limit:
            break
        chosen.append(s["cell"]); total -= s["bytes"]
    return evict_cells(chosen, mission, reason=f"limit {limit} bytes", stats=stats) if chosen else []


def _lake_missions() -> list[str]:
    return [d.name.split("=", 1)[1] for d in LAKE_DIR.glob("mission=*")] if LAKE_DIR.exists() else []


def enforce_global_limit(protect=(), reason: str = "limit") -> list[dict]:
    """Evict least-recently-ingested cells across ALL missions until the WHOLE lake is under max_bytes (the one disk
    budget the Lake UI sets governs every collection together, not each in isolation). `protect` is a flat set of H3
    cell ids never evicted — the current scene's cells. H3 ids encode their resolution, so protecting the union of a
    scene's cells across missions (res 6 ATL03 + res 5 index missions) is unambiguous."""
    limit = int(get_settings()["max_bytes"])
    protect = {int(c) for c in protect}
    items = []  # (mission, cell, stats)
    for m in _lake_missions():
        for c, st in cell_stats(m, with_rows=False).items():   # sizing only — skip the per-file footer reads
            items.append((m, c, st))
    total = sum(st["bytes"] for _, _, st in items)
    if total <= limit:
        return []
    victims = sorted((x for x in items if x[1] not in protect),
                     key=lambda x: (x[2]["last_ingested"] or "", x[0], x[1]))
    chosen: dict[str, list[int]] = {}
    for m, c, st in victims:
        if total <= limit:
            break
        chosen.setdefault(m, []).append(c); total -= st["bytes"]
    evicted = []
    by_mission = {}
    for m, c, st in items:            # reuse the walk we already did, per mission
        by_mission.setdefault(m, {})[c] = st
    for m, cells in chosen.items():
        evicted += evict_cells(cells, m, reason=f"{reason} ({limit} bytes)", stats=by_mission.get(m))
    return evicted


_EVICT_RUNNING = threading.Event()


def enforce_global_limit_async(protect=(), reason: str = "limit") -> None:
    """Run the disk-budget eviction OFF the caller's critical path, at most one at a time.

    Eviction walks every cell directory in the lake (a stat() per file) and measured 7.5 s on the deployed box — 38%
    of a 19.5 s scene build — for pure housekeeping the request does not depend on. It is also duplicated today: each
    index mission enforced the limit inline AND api.build_scene spawns its own background enforcement, so one build
    could pay for it several times. The single-flight guard collapses those: a walk already in progress is enough,
    since each run evicts until the whole lake is under budget.

    Trade-off: the freed bytes are not reflected in the response's stats. That is fine — the caller reports what it
    fetched, not what housekeeping reclaimed, and a scene already holds the points it read.
    """
    if _EVICT_RUNNING.is_set():
        return
    protect = list(protect)

    def _run():
        _EVICT_RUNNING.set()
        try:
            ev = enforce_global_limit(protect=protect, reason=reason)
            if ev:
                log.info("lake eviction freed %d cells (%.1f MB)", len(ev), sum(e["bytes"] for e in ev) / 1e6)
        except Exception as e:
            log.warning("lake eviction failed: %s", e)
        finally:
            _EVICT_RUNNING.clear()

    threading.Thread(target=_run, name="lake-evict", daemon=True).start()


def recent_evictions(n: int = 50) -> list[dict]:
    import json
    if not EVICTION_LOG.exists():
        return []
    lines = EVICTION_LOG.read_text().splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


def query_photons(bbox, cells: list[int], min_conf: int, granules: list[str] | None = None, mission: str = "ICESAT2") -> dict:
    """The query path (§8): DuckDB over the hive-partitioned lake with cell + bbox predicate pushdown."""
    w, s, e, n = bbox
    if not any(LAKE_DIR.glob(f"mission={mission}/h3_cell=*/*.parquet")):
        raise RuntimeError("lake is empty for " + mission)
    # one glob per requested cell, not `h3_cell=*` over the whole mission — see the note in query_points
    globs = []
    for c in cells:
        d = cell_dir(mission, int(c))
        if next(d.glob("*.parquet"), None) is not None:
            globs.append(f"{d}/*.parquet")
    if not globs:
        return {k: np.array([]) for k in ("lon", "lat", "h", "conf", "t", "ph_index", "granule_idx", "beam_idx",
                                          "coreg_lon", "coreg_lat")} | {"_granules": []}
    glob = "[" + ", ".join("'" + g + "'" for g in globs) + "]"
    con = duckdb.connect()
    cond = [f"h3_cell IN ({','.join(str(int(c)) for c in cells)})",
            f"native_lat BETWEEN {s} AND {n}", f"native_lon BETWEEN {w} AND {e}", f"signal_conf_landice >= {min_conf}"]
    if granules:
        cond.append("source_granule IN (" + ",".join("'" + g + "'" for g in granules) + ")")
    src = f"read_parquet({glob}, hive_partitioning = true)"
    where = " AND ".join(cond)
    # Integer codes are computed in SQL and the result comes back as numpy directly: string columns crossing into
    # Python cost ~1.5 s per 6M rows (measured); this path is ~0.8 s for the same rows.
    glist = [r[0] for r in con.execute(f"SELECT DISTINCT source_granule FROM {src} WHERE {where} ORDER BY 1").fetchall()]
    q = f"""SELECT native_lon, native_lat, native_height, signal_conf_landice, t, photon_index,
                   dense_rank() OVER (ORDER BY source_granule) - 1 AS granule_idx, CAST(beam[3] AS INTEGER) - 1 AS beam_idx,
                   coreg_lon, coreg_lat
            FROM {src} WHERE {where}"""
    r = con.execute(q).fetchnumpy()
    return {"lon": np.asarray(r["native_lon"]), "lat": np.asarray(r["native_lat"]), "h": np.asarray(r["native_height"]),
            "conf": np.asarray(r["signal_conf_landice"]), "t": np.asarray(r["t"]).astype("datetime64[ms]"),
            "ph_index": np.asarray(r["photon_index"]),
            "granule_idx": np.asarray(r["granule_idx"]).astype("i2"), "beam_idx": np.asarray(r["beam_idx"]).astype("i1"),
            "coreg_lon": np.asarray(r["coreg_lon"]), "coreg_lat": np.asarray(r["coreg_lat"]),
            "_granules": glist}


PRODUCTS = {"ICESAT2": "ICESat-2 ATL03", "ATL06": "ICESat-2 ATL06", "GLAS": "ICESat/GLAS GLAH06", "ICESSN": "IceBridge ATM ICESSN"}


def missions() -> list[dict]:
    """Collections currently materialized in the lake (lightweight: cell dirs + file sizes, no parquet-footer reads)."""
    if not LAKE_DIR.exists():
        return []
    out = []
    for d in sorted(LAKE_DIR.glob("mission=*")):
        m = d.name.split("=", 1)[1]
        cells = [c for c in d.glob("h3_cell=*") if any(c.glob("*.parquet"))]
        if not cells:
            continue
        b = sum(f.stat().st_size for c in cells for f in c.glob("*.parquet"))
        out.append({"mission": m, "product": PRODUCTS.get(m, m), "cells": len(cells), "bytes": int(b)})
    return out


def lake_summary(mission: str = "ICESAT2") -> dict:
    stats = cell_stats(mission)
    total = sum(s["bytes"] for s in stats.values())
    settings = get_settings()
    return {"mission": mission, "product": PRODUCTS.get(mission, mission), "missions": missions(),
            "files": sum(s["files"] for s in stats.values()), "rows": sum(s["rows"] for s in stats.values()), "cells": len(stats),
            "bytes": total, "max_bytes": int(settings["max_bytes"]), "usage": (total / settings["max_bytes"]) if settings["max_bytes"] else None,
            "granules": len({g for s in stats.values() for g in s["granules"]}),
            "oldest_ingested": min((s["last_ingested"] for s in stats.values() if s["last_ingested"]), default=None),
            "newest_ingested": max((s["last_ingested"] for s in stats.values() if s["last_ingested"]), default=None),
            "evictions_recent": recent_evictions(10)}
