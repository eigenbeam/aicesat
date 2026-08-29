"""Split a cold ATL06 fetch into fetch / decode / lake-write, to see where a cold build's time actually goes.

Context: scripts/bench_range_reads.py measured in-region S3-direct at ~358 MB/s (2000 ranges -> 25 coalesced GETs),
while a real build moved 597 MB at 2.1 MB/s. So the network is ~100x faster than we are using it and the cost is
downstream. This wraps the three phases in place and reports the totals.

    uv run python scripts/bench_ingest_phases.py <w> <s> <e> <n>     # pick an UNCACHED bbox
"""
import sys
import time

from aicesat import atl06, auth, cache, index_atl06, lake, regions
from aicesat.access import RangeReader

PHASES = ("fetch", "decode", "write", "cells", "mark_many",
          "index_rows", "ingested", "query_points", "evict", "cache_save")
TOT = {k: 0.0 for k in PHASES}
CNT = {k: 0 for k in PHASES}


def _timed(bucket, fn):
    def wrapper(*a, **k):
        t = time.time()
        try:
            return fn(*a, **k)
        finally:
            TOT[bucket] += time.time() - t
            CNT[bucket] += 1
    return wrapper


WROTE = {"files": 0, "cells": set()}


def _counted(fn):
    """Tally the files a write actually produced, from its {chunk: [cells]} return value. Globbing the lake instead
    counted every file any previous run had left behind."""
    def wrapper(*a, **k):
        out = fn(*a, **k)
        for cells in out.values():
            WROTE["files"] += len(cells)
            WROTE["cells"].update(cells)
        return out
    return wrapper


def main(bbox) -> None:
    auth.login()
    # _decode_chunk imports access.decode_chunk at call time, so wrap the per-chunk helper itself rather than
    # trying to patch the inner import.
    RangeReader.fetch = _timed("fetch", RangeReader.fetch)
    index_atl06._decode_chunk = _timed("decode", index_atl06._decode_chunk)
    lake.write_point_chunk = _counted(_timed("write", lake.write_point_chunk))
    # everything else on the leg's critical path — the first run left 27.5s of 58.5s unaccounted
    index_atl06._index_rows = _timed("index_rows", index_atl06._index_rows)
    lake.ingested_chunk_cells = _timed("ingested", lake.ingested_chunk_cells)
    lake.mark_ingested_many = _timed("mark_many", lake.mark_ingested_many)
    # H3 assignment is the main untimed CPU step inside the per-granule work. NOTE it is called from BOTH the
    # ingest path and write_point_chunk, so "cells" partly double-counts "write" — read it as "how much H3", not
    # as a disjoint slice.
    from aicesat import planner
    planner._cells_vectorized = _timed("cells", planner._cells_vectorized)
    lake.query_points = _timed("query_points", lake.query_points)
    lake.enforce_global_limit = _timed("evict", lake.enforce_global_limit)
    cache.save = _timed("cache_save", cache.save)

    t = time.time()
    arrays, meta = atl06.extract(bbox, regions.DEFAULT_ATL06_WINDOW)
    wall = time.time() - t
    # The lake write is now queued to a background writer, so WALL is what the user waits for and this tail is what is
    # still outstanding when they get their answer. Report both: the point of the change is to move the write, not to
    # make it disappear, and a growing tail would mean the writer cannot keep up with the fetch.
    t2 = time.time()
    lake.drain_writes()
    tail = time.time() - t2
    st = meta["access"]
    from aicesat.access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, in_region, pool_size)
    # The EFFECTIVE pool, not the constant: AICESAT_FETCH_WORKERS overrides the cap, so printing FETCH_WORKER_CAP
    # left a run ambiguous about whether the override took effect at all.
    nw = pool_size(st.get("granules_touched") or 921, cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES,
                   env=FETCH_WORKER_ENV)
    print(f"\nbbox {bbox}")
    # The config is part of the measurement. presigns>0 means it did NOT take the in-region S3-direct path, which a
    # login shell silently causes: EC2 does not set AWS_REGION, so in_region() is False unless AICESAT_S3_DIRECT=1.
    print(f"  config        s3_direct={in_region()}  async_write={lake.async_writes_enabled()}  "
          f"writers={lake._writer_threads()}  fetch_workers={nw} (cap {FETCH_WORKER_CAP})")
    print(f"  points        {arrays['lon'].size:,}")
    print(f"  chunks        {st.get('chunks_fetched')} from NASA, {st.get('chunks_from_lake')} from the lake")
    gap = st.get("gap_bytes", 0)
    print(f"  bytes         {st.get('bytes', 0)/1e6:.1f} MB in {st.get('requests')} GETs (presigns={st.get('presigns')})")
    print(f"                {(st.get('bytes', 0)-gap)/1e6:.1f} MB WANTED + {gap/1e6:.1f} MB over-fetch from coalescing")
    nch = st.get("chunks_fetched") or 1
    print(f"  lake wrote    {WROTE['files']:,} files in {len(WROTE['cells']):,} cells  "
          f"({WROTE['files']/nch:.1f} files/chunk, {st.get('cells')} cells wanted)")
    print(f"\n  WALL          {wall:7.1f}s   (what the request waits for)")
    print(f"  writer tail   {tail:7.1f}s   (background lake writes still outstanding at that point)")
    for k in PHASES:
        print(f"  {k:13s} {TOT[k]:7.1f}s  (thread-seconds over {CNT[k]} calls)")
    print(f"  unaccounted   {wall - sum(TOT.values()):7.1f}s  (negative = phases ran concurrently)")
    if st.get("bytes"):
        # Two rates, because they differ by ~2.4x and the flattering one is the wrong one to optimise. The raw ceiling
        # (~358 MB/s) was itself measured on bytes READ, so that comparison belongs with the read rate.
        print(f"\n  effective     {(st['bytes']-gap)/1e6/wall:.1f} MB/s of WANTED data")
        print(f"                {st['bytes']/1e6/wall:.1f} MB/s read incl. over-fetch  "
              f"(vs ~358 MB/s measured for the raw transport)")


if __name__ == "__main__":
    a = sys.argv[1:]
    main([float(x) for x in a] if len(a) == 4 else [-38.9, 66.75, -38.2, 67.05])
