"""Parameter sweep for the lake's on-disk layout (issue #23).

The first pass (bench_lake_layout.py) compared three NAMED layouts, which confounded the thing that actually varies —
file size — with layout semantics, and fixed several parameters without testing them. This sweeps them properly:

  * rows per file (i.e. FILE SIZE) as the independent variable, decoupled from layout
  * row_group_size — untested before: files held 2.8k-11k rows against a 65536 row-group, so every file was ONE row
    group and the knob never engaged
  * compression codec, measured through the whole write+read pipeline rather than in isolation
  * number of cells per query (reads are not one shape)
  * DECOY files in unqueried cells, to reproduce the box's scale (864k files) — today's 145 s bug only appeared at
    ~500k files, so a benchmark at 2k files can miss scale effects entirely

Caveat stated up front: reads here are warm-cache unless --drop-caches is passed (which needs root). Warm numbers
compare layouts fairly but understate cold-read cost on the box.

    uv run python scripts/bench_lake_params.py --total-rows 4000000
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

CELL0 = 600000000000000000


def _table(rng, n):
    return pa.table({
        "native_lon": rng.uniform(-39, -38, n), "native_lat": rng.uniform(69, 70, n),
        "native_height": rng.uniform(400, 3000, n), "t": pa.array(np.zeros(n, "datetime64[ms]")),
        "source_granule": pa.array([f"ATL06_{i % 97:04d}.h5" for i in range(n)]).dictionary_encode(),
        "beam": pa.array(["gt1l"] * n).dictionary_encode(),
        "source_chunk_index": np.arange(n, dtype="i4") // 2800, "quality": np.zeros(n, "i1"),
    })


def _build(root, n_cells, rows_per_cell, rows_per_file, row_group, comp, rng):
    root.mkdir(parents=True, exist_ok=True)
    for c in range(n_cells):
        d = root / f"h3_cell={CELL0 + c}"; d.mkdir(parents=True, exist_ok=True)
        left, part = rows_per_cell, 0
        while left > 0:
            n = min(rows_per_file, left)
            pq.write_table(_table(rng, n), d / f"p{part:05d}.parquet",
                           compression=comp, row_group_size=row_group)
            left -= n; part += 1


def _decoys(root, n_files, rows, rng, comp):
    """Files in cells the queries never touch — reproduces a big lake so scale effects are visible."""
    if n_files <= 0:
        return
    per_cell = 200
    for i in range(n_files):
        d = root / f"h3_cell={CELL0 + 10_000 + i // per_cell}"
        d.mkdir(parents=True, exist_ok=True)
        pq.write_table(_table(rng, rows), d / f"d{i:06d}.parquet", compression=comp, row_group_size=65536)


def _drop_caches():
    try:
        subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                       check=True, capture_output=True, timeout=30)
        return True
    except Exception:
        return False


def _read(root, cells, drop=False):
    globs = []
    for c in cells:
        d = root / f"h3_cell={c}"
        if d.is_dir() and next(d.glob("*.parquet"), None) is not None:
            globs.append(f"'{d}/*.parquet'")
    if not globs:
        return 0.0, 0
    if drop:
        _drop_caches()
    src = f"read_parquet([{', '.join(globs)}], hive_partitioning=true, union_by_name=true)"
    con = duckdb.connect()
    t = time.time()
    n = con.execute(f"SELECT count(*), avg(native_height) FROM {src} "
                    f"WHERE native_height > 500 AND native_lat BETWEEN 69.2 AND 69.8").fetchone()[0]
    dt = time.time() - t
    con.close()
    return dt, n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total-rows", type=int, default=4_000_000, help="rows across the queried cells")
    ap.add_argument("--cells", type=int, default=40)
    ap.add_argument("--decoys", type=int, default=0, help="extra files in unqueried cells (lake-scale effect)")
    ap.add_argument("--drop-caches", action="store_true", help="cold reads (needs passwordless sudo)")
    args = ap.parse_args()

    base = Path(os.environ.get("TMPDIR", "/tmp")) / "lakeparams"
    shutil.rmtree(base, ignore_errors=True)
    rng = np.random.default_rng(0)
    rows_per_cell = args.total_rows // args.cells
    q7 = [CELL0 + c for c in range(7)]
    q20 = [CELL0 + c for c in range(20)]

    print(f"{args.cells} cells x {rows_per_cell:,} rows = {args.total_rows:,} rows"
          f"{f' + {args.decoys} decoy files' if args.decoys else ''}")
    print(f"cold reads: {'yes' if args.drop_caches else 'NO (warm cache — understates cold cost)'}\n")

    # --- 1. file size sweep ---------------------------------------------------------------------------------
    print("=== file size (rows/file), zstd, row_group=65536 ===")
    print(f"{'rows/file':>10}{'files':>8}{'MB':>8}{'KB/file':>9}{'write s':>9}{'read7 s':>9}{'read20 s':>10}")
    for rpf in (2_800, 11_200, 45_000, 180_000, 720_000):
        root = base / f"rpf{rpf}" / "mission=ATL06"
        t = time.time(); _build(root, args.cells, rows_per_cell, rpf, 65536, "zstd", rng); wt = time.time() - t
        if args.decoys:
            _decoys(root, args.decoys, 2800, rng, "zstd")
        files = list(root.glob("h3_cell=*/*.parquet")); mb = sum(f.stat().st_size for f in files) / 1e6
        r7, _ = _read(root, q7, args.drop_caches)
        r20, _ = _read(root, q20, args.drop_caches)
        print(f"{rpf:>10,}{len(files):>8}{mb:>8.1f}{mb*1000/max(len(files),1):>9.1f}{wt:>9.2f}{r7:>9.3f}{r20:>10.3f}")

    # --- 2. row group sweep at a mid file size --------------------------------------------------------------
    print("\n=== row_group_size (rows/file = 180,000, zstd) ===")
    print(f"{'row_group':>10}{'MB':>8}{'write s':>9}{'read7 s':>9}{'read20 s':>10}")
    for rg in (8_192, 65_536, 262_144, 1_000_000):
        root = base / f"rg{rg}" / "mission=ATL06"
        t = time.time(); _build(root, args.cells, rows_per_cell, 180_000, rg, "zstd", rng); wt = time.time() - t
        files = list(root.glob("h3_cell=*/*.parquet")); mb = sum(f.stat().st_size for f in files) / 1e6
        r7, _ = _read(root, q7, args.drop_caches)
        r20, _ = _read(root, q20, args.drop_caches)
        print(f"{rg:>10,}{mb:>8.1f}{wt:>9.2f}{r7:>9.3f}{r20:>10.3f}")

    # --- 3. codec, through the whole pipeline ---------------------------------------------------------------
    print("\n=== compression (rows/file = 180,000, row_group=65536) ===")
    print(f"{'codec':>10}{'MB':>8}{'write s':>9}{'read7 s':>9}{'read20 s':>10}")
    for comp in ("zstd", "snappy", None):
        root = base / f"c{comp}" / "mission=ATL06"
        t = time.time(); _build(root, args.cells, rows_per_cell, 180_000, 65536, comp, rng); wt = time.time() - t
        files = list(root.glob("h3_cell=*/*.parquet")); mb = sum(f.stat().st_size for f in files) / 1e6
        r7, _ = _read(root, q7, args.drop_caches)
        r20, _ = _read(root, q20, args.drop_caches)
        print(f"{str(comp):>10}{mb:>8.1f}{wt:>9.2f}{r7:>9.3f}{r20:>10.3f}")


if __name__ == "__main__":
    main()
