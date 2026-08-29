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
        row = con.execute(f"SELECT granule, url, s3url FROM read_parquet('{d}/*.parquet') {where} LIMIT 1").fetchone()
        if row is None:
            print("no indexed granule found"); return
        gran, url, s3 = row
        rngs = con.execute(
            f"SELECT h_li_offset, h_li_size FROM read_parquet('{d}/*.parquet') "
            f"WHERE granule = '{gran}' ORDER BY chunk_index LIMIT 200").fetchall()
    finally:
        con.close()
    ranges = [(int(o), int(s)) for o, s in rngs]
    mb = sum(s for _, s in ranges) / 1e6
    target = access_url(url, s3)
    print(f"granule: {gran}\n  {len(ranges)} ranges, {mb:.1f} MB, via {'S3-direct' if target.startswith('s3://') else 'HTTPS'}")

    for threads in (1, 4, 16, 64):
        r = RangeReader(threads=threads)
        if not target.startswith("s3://"):
            r.presign_all([target])
        t = time.time()
        blobs = r.fetch(target, ranges)
        dt = time.time() - t
        got = sum(len(b) for b in blobs) / 1e6
        st = r.stats.as_dict()
        print(f"  threads={threads:3d}  {dt:6.2f}s  {got/dt:6.1f} MB/s  {st.get('requests', 0)/dt:6.1f} GET/s  "
              f"(requests={st.get('requests')}, presigns={st.get('presigns')})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
