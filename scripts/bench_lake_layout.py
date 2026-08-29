"""Empirical comparison of lake file layouts (issue #23).

Measured on the deployed box, a cold ATL06 leg spends ~39% of its thread-time in `write`, and that cost is
file-count-driven: ~34 ms per write_point_chunk / ~5 ms per parquet file = ~7 files per call, one per cell the chunk
touches. Compression is NOT the lever (zstd 5.06 ms vs uncompressed 4.28 ms per file).

Open question this answers: does the per-file overhead ALSO explain why more fetch workers stopped helping? At 16
workers each op got 4-5x slower and throughput did not improve, which looks like contention on many small file
operations. If so, a coarser layout should raise the parallelism ceiling as well as lower the single-threaded cost —
so every layout is measured at several worker counts, not just serially.

Layouts
  current      one file per (cell, chunk)                      -- today
  per_granule  one file per (cell, granule, beam)              -- accumulate a granule's chunks, write once
  per_cell     one file per cell                               -- upper bound on compaction; ignores incrementality

Reads use the production predicate shape: glob only the requested cells' directories (the fix in 62d054e), so the
comparison reflects how the lake is actually queried.

    uv run python scripts/bench_lake_layout.py [--granules N] [--scale S]
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROWS_PER_CELL = 2800          # rows a chunk contributes to one cell (matches the box's ~74 KB zstd files)
CELLS_PER_CHUNK = 7           # cells a chunk touches (derived from ~34 ms per write / ~5 ms per file)
BEAMS = ("gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r")
CHUNKS_PER_BEAM = 4
CELL0 = 600000000000000000
COMPRESSION = "zstd"
ROW_GROUP = 65536


def _table(rng, n=ROWS_PER_CELL, granule="g", beam="gt1l", chunk=0):
    return pa.table({
        "native_lon": rng.uniform(-39, -38, n), "native_lat": rng.uniform(69, 70, n),
        "native_height": rng.uniform(400, 3000, n), "t": pa.array(np.zeros(n, "datetime64[ms]")),
        "source_granule": pa.array([granule] * n).dictionary_encode(),
        "beam": pa.array([beam] * n).dictionary_encode(),
        "source_chunk_index": np.full(n, chunk, "i4"), "quality": np.zeros(n, "i1"),
    })


def _plan(n_granules):
    """The write units one ingest produces: (granule, beam, chunk, [cells]). Cells overlap across granules, as real
    repeat tracks do, so a cell accumulates many granules' data."""
    out = []
    for g in range(n_granules):
        for b in BEAMS:
            for c in range(CHUNKS_PER_BEAM):
                base = (g * 3 + c) % 40
                out.append((f"ATL06_{g:04d}.h5", b, c, [CELL0 + base + k for k in range(CELLS_PER_CHUNK)]))
    return out


def _write_current(root, units, workers, rng):
    """Today: write_point_chunk is called per chunk and writes one file per cell it touches."""
    def do(u):
        gran, beam, chunk, cells = u
        r = np.random.default_rng(abs(hash(u[:3])) % 2**32)
        for cell in cells:
            d = root / f"h3_cell={cell}"; d.mkdir(parents=True, exist_ok=True)
            pq.write_table(_table(r, granule=gran, beam=beam, chunk=chunk),
                           d / f"{gran}__{beam}__c{chunk}.parquet", compression=COMPRESSION, row_group_size=ROW_GROUP)
    _run(do, units, workers)


def _write_per_granule(root, units, workers, rng):
    """Accumulate a granule+beam's chunks and write ONE file per cell. _ingest_granule already holds every chunk of
    the granule, so this needs no new coordination."""
    by_gb = defaultdict(list)
    for u in units:
        by_gb[(u[0], u[1])].append(u)

    def do(key):
        gran, beam = key
        r = np.random.default_rng(abs(hash(key)) % 2**32)
        per_cell = defaultdict(list)
        for _g, _b, chunk, cells in by_gb[key]:
            for cell in cells:
                per_cell[cell].append(_table(r, granule=gran, beam=beam, chunk=chunk))
        for cell, tabs in per_cell.items():
            d = root / f"h3_cell={cell}"; d.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.concat_tables(tabs), d / f"{gran}__{beam}.parquet",
                           compression=COMPRESSION, row_group_size=ROW_GROUP)
    _run(do, list(by_gb), workers)


def _write_per_cell(root, units, workers, rng):
    """Upper bound: one file per cell for the whole batch. Not incrementally writable in production (a later granule
    would rewrite the file), but it bounds what compaction can buy."""
    per_cell = defaultdict(list)
    for gran, beam, chunk, cells in units:
        r = np.random.default_rng(abs(hash((gran, beam, chunk))) % 2**32)
        for cell in cells:
            per_cell[cell].append(_table(r, granule=gran, beam=beam, chunk=chunk))

    def do(cell):
        d = root / f"h3_cell={cell}"; d.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.concat_tables(per_cell[cell]), d / "all.parquet",
                       compression=COMPRESSION, row_group_size=ROW_GROUP)
    _run(do, list(per_cell), workers)


def _run(fn, items, workers):
    if workers <= 1:
        for i in items:
            fn(i)
    else:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(fn, items))


def _stats(root):
    files = list(root.glob("h3_cell=*/*.parquet"))
    return len(files), sum(f.stat().st_size for f in files) / 1e6


def _read(root, cells):
    """Production read shape: one glob per requested cell (see lake.query_points)."""
    globs = []
    for c in cells:
        d = root / f"h3_cell={c}"
        if d.is_dir() and next(d.glob("*.parquet"), None) is not None:
            globs.append(f"'{d}/*.parquet'")
    if not globs:
        return 0.0, 0
    src = f"read_parquet([{', '.join(globs)}], hive_partitioning=true, union_by_name=true)"
    con = duckdb.connect()
    t = time.time()
    n = con.execute(f"SELECT count(*) FROM {src} WHERE native_height > 500").fetchone()[0]
    dt = time.time() - t
    con.close()
    return dt, n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--granules", type=int, default=20)
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 4, 16])
    args = ap.parse_args()

    base = Path(os.environ.get("TMPDIR", "/tmp")) / "lakelayout"
    shutil.rmtree(base, ignore_errors=True)
    units = _plan(args.granules)
    rng = np.random.default_rng(0)
    query_cells = [CELL0 + k for k in range(CELLS_PER_CHUNK)]
    print(f"{args.granules} granules x {len(BEAMS)} beams x {CHUNKS_PER_BEAM} chunks = {len(units)} write units, "
          f"{CELLS_PER_CHUNK} cells each, {ROWS_PER_CELL} rows/cell\n")
    print(f"{'layout':<13}{'workers':>8}{'write s':>10}{'files':>9}{'MB':>8}{'MB/s':>9}{'read s':>9}{'rows read':>12}")
    print("-" * 78)

    writers = {"current": _write_current, "per_granule": _write_per_granule, "per_cell": _write_per_cell}
    for name, fn in writers.items():
        for w in args.workers:
            root = base / f"{name}_w{w}" / "mission=ATL06"
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True)
            t = time.time(); fn(root, units, w, rng); wt = time.time() - t
            nf, mb = _stats(root)
            rt, rows = _read(root, query_cells)
            print(f"{name:<13}{w:>8}{wt:>10.2f}{nf:>9}{mb:>8.1f}{mb/wt:>9.1f}{rt:>9.3f}{rows:>12,}")
        print()


if __name__ == "__main__":
    main()
