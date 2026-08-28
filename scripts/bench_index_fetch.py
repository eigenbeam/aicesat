"""Benchmark index-driven fetch across missions: time fetch_bbox and report the access stats that matter —
GETs, bytes, over-fetch (gap %), presigns, and hdf5_opens_at_query_time (0, the whole point of the index).

Small units by default so it runs in a few minutes on a constrained link. On us-west-2 the wall-clock is the
REAL perf number (S3-direct is auto-selected via AWS_REGION); out-of-region the wall-clock is just your local
link, but the STRUCTURAL metrics (GETs / bytes / over-fetch / opens) are valid anywhere. Add --sweep for larger
units (intended to run in-region). GETs=spans, so GETs count round trips after coalescing; presigns are the EDL
round trips (0 in-region). ATL03 has its own dedicated method comparison: scripts/bench_access.py.

usage: uv run scripts/bench_index_fetch.py [--reps N] [--sweep]
"""
import argparse
import statistics
import time

from aicesat import auth, access

JAK = [-50.3, 68.9, -49.2, 69.3]          # the built Jakobshavn index extent
ATL06_BOX = [-50.0, 69.0, -49.5, 69.4]    # a small sub-box of the SW-Greenland ATL06 index


def units(sweep: bool):
    """(mission, fetch(bbox, window)->(arrays, stats), bbox, window, label). Small first; --sweep appends larger."""
    from aicesat import index_atl06, index_glas, index_icessn
    U = [
        ("GLAS",   lambda b, w: index_glas.fetch_bbox(b, window=w, res=5),   JAK,       ("2003-02-24", "2003-02-28"), "~1 pass"),
        ("ICESSN", lambda b, w: index_icessn.fetch_bbox(b, window=w, res=5), JAK,       ("2011-03-01", "2011-03-31"), "1 flight month"),
        ("ATL06",  lambda b, w: index_atl06.fetch_bbox(b, window=w, res=5),  ATL06_BOX, ("2019-03-01", "2019-05-31"), "small box, 1 season"),
    ]
    if sweep:
        U += [
            ("GLAS",   lambda b, w: index_glas.fetch_bbox(b, window=w, res=5),   JAK, ("2003-02-01", "2003-04-01"), "1 campaign"),
            ("ICESSN", lambda b, w: index_icessn.fetch_bbox(b, window=w, res=5), JAK, ("2011-01-01", "2011-12-31"), "1 year"),
            ("ATL06",  lambda b, w: index_atl06.fetch_bbox(b, window=w, res=5),  JAK, ("2019-01-01", "2020-12-31"), "full box, 2 yrs"),
        ]
    return U


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2, help="repetitions per unit (for variance)")
    ap.add_argument("--sweep", action="store_true", help="add larger units (run these in-region)")
    a = ap.parse_args()

    auth.login()
    reg = access.in_region()
    print(f"in_region={reg}  |  " + ("S3-direct: wall-clock is the real perf number." if reg else
          "OUT-OF-REGION: wall-clock = your local link, NOT the system; trust the structural columns "
          "(GETs / MB / gap% / opens). Real timings come from us-west-2."))
    hdr = (f"{'mission':7} {'unit':20} {'pts':>7} {'gran':>4} {'GETs':>4} {'MB':>6} {'gap%':>5} "
           f"{'presign':>7} {'opens':>5} {'t_med':>7} {'t_min':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, fn, bbox, win, label in units(a.sweep):
        times, last = [], None
        for _ in range(a.reps):
            t0 = time.time()
            try:
                arr, st = fn(bbox, win)
            except Exception as e:
                print(f"{name:7} {label:20} ERROR: {type(e).__name__}: {e}")
                last = None
                break
            times.append(time.time() - t0)
            last = (arr, st)
        if not last:
            continue
        arr, st = last
        mb = st.get("bytes", 0) / 1e6
        gap = 100 * st.get("gap_bytes", 0) / max(1, st.get("bytes", 1))
        print(f"{name:7} {label:20} {arr['h'].size:7d} {st.get('granules_touched', 0):4d} "
              f"{st.get('spans', 0):4d} {mb:6.1f} {gap:5.0f} {st.get('presigns', 0):7d} "
              f"{st.get('hdf5_opens_at_query_time', 0):5d} {statistics.median(times):7.1f} {min(times):7.1f}")
    print("\nGETs=coalesced range requests; opens=HDF5 files opened at query time (0 — addressing came from the index).")
    print("ATL03 index-vs-legacy-vs-download: scripts/bench_access.py")


if __name__ == "__main__":
    main()
