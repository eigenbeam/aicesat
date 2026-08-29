"""Measure the raw byte-range read ceiling for ONE granule, with all scene/lake orchestration removed.

A real build showed ~2 MB/s and ~12 GETs/s from in-region S3-direct (presigns=0), unchanged by 4x more granule
workers — so the limit is not our granule-level fan-out. This isolates the transport: the same ranges, one granule,
at several thread counts. If throughput is flat in `threads`, the ceiling is per-connection/per-request latency
(client config, credentials, or the S3 path itself); if it scales, our orchestration is not exploiting it.

    uv run python scripts/bench_range_reads.py [granule_substring]
"""
import sys
import time

import duckdb

from aicesat import auth, index_atl06
from aicesat.access import RangeReader, access_url, in_region


def main(match: str | None = None) -> None:
    auth.login()
    print("in_region:", in_region())
    d = index_atl06._index_dir(index_atl06.ATL06_RES)
    where = f"WHERE granule LIKE '%{match}%'" if match else ""
    con = duckdb.connect()
    try:
        # the BIGGEST granule, not the first: an edge-of-orbit granule has a handful of tiny chunks that coalesce
        # into one request and measure nothing. Pull every dataset's ranges, as a real fetch does.
        cols = ", ".join(f"sum({ds}_size)" for ds in index_atl06.ATL06_DATASETS)
        row = con.execute(
            f"SELECT granule, any_value(url), any_value(s3url), ({cols}) AS total "
            f"FROM read_parquet('{d}/*.parquet') {where} GROUP BY granule ORDER BY total DESC LIMIT 1").fetchone()
        if row is None:
            print("no indexed granule found"); return
        gran, url, s3, _tot = row
        sel = ", ".join(f"{ds}_offset, {ds}_size" for ds in index_atl06.ATL06_DATASETS)
        rows = con.execute(f"SELECT {sel} FROM read_parquet('{d}/*.parquet') "
                           f"WHERE granule = '{gran}' ORDER BY chunk_index, beam LIMIT 400").fetchall()
    finally:
        con.close()
    ranges = [(int(r[i]), int(r[i + 1])) for r in rows for i in range(0, len(r), 2) if int(r[i + 1]) > 0]
    mb = sum(s for _, s in ranges) / 1e6
    target = access_url(url, s3)
    print(f"granule: {gran}\n  {len(ranges)} ranges, {mb:.1f} MB, via {'S3-direct' if target.startswith('s3://') else 'HTTPS'}")
    if mb < 1:
        print("  (still tiny — the index may hold only edge granules; results will not be meaningful)")

    RangeReader(threads=1).fetch(target, ranges[:1])   # warm the STS creds so run 1 isn't charged for them
    for threads in (1, 4, 16, 64):
        r = RangeReader(threads=threads)
        if not target.startswith("s3://"):
            r.presign_all([target])
        t = time.time()
        blobs = r.fetch(target, ranges)
        dt = time.time() - t
        got = sum(len(b) for b in blobs) / 1e6
        st = r.stats.as_dict()
        print(f"  threads={threads:3d}  {dt:6.2f}s  {got/dt:7.1f} MB/s  {st.get('requests', 0)/dt:6.1f} GET/s  "
              f"(requests={st.get('requests')}, presigns={st.get('presigns')})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
