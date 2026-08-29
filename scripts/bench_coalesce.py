"""How much is the byte-range coalescing gap worth in-region?

RangeReader defaults to a 256 KB gap in-region, on the reasoning that "the round trip is ~10-30 ms and egress is
free, so a SMALL gap wins (less over-fetch)". The ingest measurements contradict the premise: a real leg moved
1,415 MB in 5,741 GETs — 246 KB each at ~25 ms each, i.e. ~9.7 MB/s per stream, almost entirely time-to-first-byte.
If a GET costs the same whether it carries 246 KB or 1.5 MB, then a round trip is EXPENSIVE relative to over-fetch
and the gap should be large.

This sweeps the gap over the real range geometry of a bbox — same granules, same byte offsets the ingest would use —
and fetches only (no decode, no lake write), so the transport is the only variable. It reports the over-fetch each
gap costs (gap_bytes: bytes pulled solely because they sat between two wanted ranges) next to what it buys.

    uv run python scripts/bench_coalesce.py <w> <s> <e> <n> [--gaps 0.25 1 4 16] [--reps 2]
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from aicesat import auth, index_atl06, regions
from aicesat.access import (FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, RangeReader, access_url,
                            coalesce, in_region, pool_size)


def _plan(bbox, window):
    """The exact (url -> [(offset, size)]) the ingest would fetch for this bbox."""
    _want, rows = index_atl06._index_rows(bbox, window, index_atl06.ATL06_RES, strong_only=False)
    if not rows:
        raise SystemExit(f"no ATL06 index rows over {bbox}")
    chunk_row = {}
    for r in rows:
        chunk_row.setdefault((r["granule"], r["beam"], r["chunk_index"]), r)
    by_url: dict[str, list] = {}
    for r in chunk_row.values():
        by_url.setdefault(access_url(r["url"], r["s3url"]), []).append(r)
    plan = {}
    for url, rs in by_url.items():
        ranges = []
        for r in rs:
            for ds in index_atl06.ATL06_DATASETS:
                ranges.append((r[f"{ds}_offset"], r[f"{ds}_size"]))
        plan[url] = sorted(ranges)
    return plan


def _run(plan, gap, nw):
    reader = RangeReader(max_gap=gap)
    reader.presign_all([u for u in plan if not u.startswith("s3://")])
    t = time.time()
    if nw == 1:
        for u, ranges in plan.items():
            reader.fetch(u, ranges)
    else:
        with ThreadPoolExecutor(nw) as ex:
            list(ex.map(lambda u: reader.fetch(u, plan[u]), list(plan)))
    return time.time() - t, reader.stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"))
    ap.add_argument("--gaps", type=float, nargs="+", default=[0.25, 1, 4, 16, 64],
                    help="coalescing gaps in MB (0.25 = the in-region default)")
    ap.add_argument("--reps", type=int, default=2, help="timed repetitions; the table reports the best")
    args = ap.parse_args()

    auth.login()
    plan = _plan(tuple(args.bbox), regions.DEFAULT_ATL06_WINDOW)
    wanted = sum(s for rs in plan.values() for _o, s in rs)
    nw = pool_size(len(plan), cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                   cpu_bound=False)
    print(f"{len(plan)} granules, {sum(len(r) for r in plan.values()):,} ranges, {wanted/1e6:.0f} MB wanted, "
          f"{nw} workers, s3_direct={in_region()}")
    print(f"\n{'gap MB':>8}{'spans':>9}{'MB read':>10}{'over-fetch':>12}{'best s':>9}{'MB/s':>9}{'ms/GET':>9}")
    for g in args.gaps:
        gap = int(g * (1 << 20))
        spans = sum(len(coalesce(rs, gap)) for rs in plan.values())   # predicted, before any network call
        best, st = None, None
        for _ in range(args.reps):
            dt, s = _run(plan, gap, nw)
            if best is None or dt < best:
                best, st = dt, s
        over = st.bytes - wanted
        print(f"{g:>8.2f}{spans:>9,}{st.bytes/1e6:>10.0f}{over/1e6:>11.0f}M{best:>9.1f}"
              f"{st.bytes/1e6/best:>9.1f}{best*1000*nw/max(spans,1):>9.1f}")
    print("\nover-fetch is bytes pulled only because they sat between two wanted ranges; in-region egress is free, so\n"
          "it costs time only, and a gap pays whenever it removes more round-trip latency than it adds transfer.")


if __name__ == "__main__":
    main()
