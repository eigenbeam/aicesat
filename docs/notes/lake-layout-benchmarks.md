# Lake file layout: what has been measured (issue #23)

**Collected 2026-09-24** from the commit messages and issue comments where the numbers were first recorded, so
they stop living only in `git log`. Nothing here was re-run for this note.

## Today's layout

One Parquet file per `(cell, granule, beam, chunk)`: `lake/mission=<M>/h3_cell=<cell>/<granule>__<beam>__c<chunk>.parquet`
(`lake.py:94-99`). Written by the decoupled `lake._Writer` (`082cb77`) for ATL06, GLAS, ICESSN and GEDI; ATL03
writes inline via `write_photons`. On the deployed box (2026-08-29) that was 864,448 files / 12 GB, about 14 KB each,
546,284 of them ATL06.

## Local sweeps (laptop NVMe, warm cache)

**Three named layouts** (`e90fad4`): 288 write units × 7 cells × 2,800 rows, ~152 MB.

| layout | files | write, 1 worker | write, 16 workers | read, 7 cells |
|---|---|---|---|---|
| current | 2,016 | 7.15 s | 1.32 s | 0.032 s |
| per_granule | 720 | 2.85 s | 1.01 s | 0.009 s |
| per_cell | 43 | 1.95 s | 0.74 s | 0.005 s |

That comparison confounded file size with layout, so the next run swept the parameters directly.

**Parameter sweep, reads repeated** (`83fa1e7`, superseding the single-sample `c30266c`): 40 cells × 50,000 rows,
54 MB, 7 reps, median read.

| rows/file | KB/file | write s | read 7 cells s | ± range | read 20 cells s |
|---|---|---|---|---|---|
| 2,800 | 76.0 | 2.32 | 0.010 | 0.003 | 0.028 |
| 11,200 | 270.0 | 1.20 | 0.005 | 0.001 | 0.012 |
| 45,000 | 681.2 | 1.04 | 0.004 | 0.001 | 0.010 |
| 180,000 | 1,364.1 | 0.95 | 0.004 | 0.001 | 0.009 |

- **File size is the variable, not layout semantics.** 76 → 270 KB halves both write and read; past ~680 KB it is flat.
- **Row-group size has no effect** (0.004 s from 8,192 to 1,000,000 rows). In the first benchmark every file was a
  single row group, so the knob never engaged. `lake.relayout` only changes row groups, so it buys nothing.
- **Codec is not a lever.** snappy/uncompressed write ~7% faster for 10–13% more bytes (`83fa1e7`); a separate
  micro-benchmark gave zstd 5.06 ms/file, snappy 4.56, uncompressed 4.28 (#23 comment, 2026-08-29).

## On the box (us-west-2, gp3 EBS)

- **Write cost is per file.** ~34 ms per `write_point_chunk` ≈ 7 files × ~5 ms, one file per cell the chunk touches
  (#23 comment, 2026-08-29). In a cold ATL06 leg before the writer was decoupled, write was 34.8 of 88.6
  thread-seconds (39%).
- **Decoupling the writer** (`082cb77`) and not pre-caching adjacent cells (`415de59`) took one cold ATL06 leg from
  77.2 s to 28.7 s (2.7×, recorded in `415de59`). Session notes from a later run the same day give 26.5 s with fetch
  and write roughly balanced (~18 s and ~20 s wall), so neither alone is the bottleneck.
- **Box layout sweep, indicative only.** Session notes from 2026-08-29 record that on the box, with 20k decoy files,
  76 KB → ~300 KB files gave about 26% on writes (not the ~50% seen locally) and about 2.5× on 7-cell reads. That
  run had the three measurement flaws `082cb77` fixed: a no-op rows/file setting, size columns dominated by the
  decoys, and a `--drop-caches` that silently failed, so its reads were warm. The numbers are not in git and have
  not been reproduced.

## Things that turned out not to matter, or were wrong

- **Per-granule batching** (`ac63e38`) was an exact no-op and was deleted (`09d3c40`): production writes
  1.02 chunks per (granule, beam), so there was nothing to batch along the chunk axis. The benchmark's
  "per_granule" layout assumed ~4 chunks per beam; in production it is effectively today's layout. Only merging
  **beams** (ATL06's 6, GEDI's 8) into one file per (cell, granule) cuts the file count.
- **"Fetch workers 4 → 16 does not help"** (#23 comment, 2026-08-29) was retracted in `26f6cf9`: it was measured
  while the fetch threads also did the writes. With writes decoupled, 4 → 16 is 1.9× on fetch wall (34.8 → 18.0 s).

## Still unmeasured

- **Cold reads.** Every read number above is warm-cache; the box's cache-drop failed silently.
- **Concurrent read + write.**
- **Real ATL06 chunk geometry.** The sweeps model 2,800 rows per cell per chunk; real ATL06 chunks are
  10,000 segments over ~7 cells.

## Constraints any new layout must keep

- **The cell is a directory partition.** `lake.query_points` took 145.6 s when it globbed `h3_cell=*`, because
  `union_by_name` read every matched file's schema; `62d054e` fixed it by globbing only the requested cells.
  A layout that moves `h3_cell` into a column loses that pruning.
- **Idempotency.** Re-materializing the same granule must produce the same result.
- **Partial-cell correctness.** Every point lives in its own cell's file, so a later sub-area query is correct.
- **Eviction is per cell.** A file spanning cells breaks the eviction unit.
- **No duplicates after a partial eviction.** A chunk with one cell evicted must not come back twice (the
  `082cb77` regression test: 35 rows where 28 are unique without the guard).
