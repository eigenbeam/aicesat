"""Benchmark the interchangeable in-region S3 byte-range fetch mechanisms on a REAL granule workload.

The in-region workload is many small byte-range GETs (a granule's HDF5 chunks: ~10-100 GETs of a few KB..few MB
each), fetched concurrently then decoded. access.py can issue those GETs by several interchangeable mechanisms
(access.S3_FETCH_MECHANISMS); this script times each AVAILABLE one on the IDENTICAL ranges and reports which is
fastest for this workload, so switching the production default is a one-line change.

    ONLY MEANINGFUL IN-REGION (us-west-2). Out-of-region there is no S3-direct path: the script prints the guard,
    lists which mechanisms are importable, and exits without timing anything (the numbers would be your laptop link).

Run it ON THE BOX:

    uv run scripts/bench_fetch_mechanisms.py                     # atl06 workload, all available mechanisms
    uv run scripts/bench_fetch_mechanisms.py --dataset glas      # GLAS GLAH06 instead of ATL06
    uv run scripts/bench_fetch_mechanisms.py --reps 5 --mechanisms s3fs,s3fs_ranges,aiobotocore
    uv pip install boto3 awscrt                                  # OPTIONAL: enable the boto3 / crt mechanisms too

The workload is pulled from the built index (data/index/{atl06,glas}/res5) by intercepting the exact (offset,size)
ranges a fetch_bbox over a small known box issues — a handful of granules, tens of GETs each, tens of MB total, so a
rep is quick and does not contend much with a running build. Two workload shapes are swept (the winner can differ by
shape): "small" (near-raw, many small GETs) and "coalesced" (fewer larger spans, the in-region default gap). Every
mechanism is checked byte-identical before its timings are trusted.

------------------------------------------------------------------------------------------------------------------
HOW TO FLIP THE DEFAULT TO A WINNER (once measured):
  * No code change:  export AICESAT_S3_FETCH=aiobotocore     (the server/index/fetch pick it up on next start)
  * One-line change: in src/aicesat/access.py RangeReader.__init__, change
        self.s3_fetch = os.environ.get("AICESAT_S3_FETCH", "s3fs")
    to the winning name, e.g. ... "aiobotocore").  (env still overrides, so it stays experiment-friendly.)
------------------------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import statistics
import sys
import time

import numpy as np

# small known boxes/windows that hit a handful of granules with modest total bytes (see scripts/bench_index_fetch.py)
DATASETS = {
    "atl06": {"box": [-50.0, 69.0, -49.5, 69.4], "window": ("2019-03-01", "2019-05-31"), "module": "index_atl06"},
    "glas":  {"box": [-50.3, 68.9, -49.2, 69.3], "window": ("2003-02-24", "2003-02-28"), "module": "index_glas"},
}


def capture_workload(dataset: str, bbox, window, max_granules: int, max_mb: float):
    """Pull the REAL per-granule (offset, size) ranges a fetch_bbox issues, by intercepting RangeReader.fetch.
    Returns [(s3url, [(off, size), ...]), ...] capped to at most max_granules / max_mb."""
    import importlib

    from aicesat import access
    mod = importlib.import_module(f"aicesat.{DATASETS[dataset]['module']}")

    captured: list[tuple[str, list]] = []
    orig = access.RangeReader.fetch

    def spy(self, url, ranges):
        captured.append((url, list(ranges)))
        return orig(self, url, ranges)   # let the real (default s3fs) fetch run: it also warms creds/connections

    access.RangeReader.fetch = spy
    try:
        mod.fetch_bbox(bbox, window=window, res=5)
    finally:
        access.RangeReader.fetch = orig

    work, total = [], 0
    for url, ranges in captured:
        if not url.startswith("s3://"):   # only the in-region S3-direct granules are relevant here
            continue
        work.append((url, ranges))
        total += sum(sz for _, sz in ranges)
        if len(work) >= max_granules or total / 1e6 >= max_mb:
            break
    return work


def shape_workload(work, max_gap: int):
    """Re-coalesce each granule's raw ranges at max_gap into the spans actually sent to S3 for this shape."""
    from aicesat import access
    return [(url, access.coalesce(ranges, max_gap=max_gap)) for url, ranges in work]


def digest(blobs_per_granule) -> str:
    h = hashlib.sha256()
    for blobs in blobs_per_granule:
        for b in blobs:
            h.update(b)
    return h.hexdigest()


def run_mechanism(reader, name: str, shaped, reps: int):
    """Warm once (discarded), then time `reps` measured passes over all granules. Returns
    (median_wall_s, per_get_seconds[list], byte_digest, n_spans, n_bytes)."""
    from aicesat import access
    fn = access.S3_FETCH_MECHANISMS[name]
    per_get: list[float] = []
    walls: list[float] = []
    dig = None
    for rep in range(reps + 1):
        collect = None if rep == 0 else per_get     # per-GET latency only on measured reps
        t0 = time.perf_counter()
        out = [fn(reader, url, spans, collect) for url, spans in shaped]
        walls.append(time.perf_counter() - t0)
        if rep == 0:
            dig = digest(out)
    walls = walls[1:]   # drop warmup
    n_spans = sum(len(s) for _, s in shaped)
    n_bytes = sum(sz for _, spans in shaped for _, sz in spans)
    return statistics.median(walls), per_get, dig, n_spans, n_bytes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=sorted(DATASETS), default="atl06")
    ap.add_argument("--mechanisms", default="", help="comma list; default = every available mechanism")
    ap.add_argument("--reps", type=int, default=3, help="measured passes per mechanism (median reported)")
    ap.add_argument("--granules", type=int, default=6, help="cap granules in the workload")
    ap.add_argument("--max-mb", type=float, default=40.0, help="cap total workload bytes (MB)")
    ap.add_argument("--shapes", default="small,coalesced", help="comma list of: small,coalesced")
    a = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    from aicesat import access

    reg = access.in_region()
    print(f"in_region={reg}")

    # availability of each mechanism (importable deps), independent of region
    avail, skipped = [], []
    for name in access.S3_FETCH_MECHANISMS:
        ok, why = access.s3_mechanism_available(name)
        (avail if ok else skipped).append(name if ok else f"{name} ({why})")
    print("mechanisms available:", ", ".join(avail) or "(none)")
    if skipped:
        print("mechanisms skipped:  ", ", ".join(skipped))

    if not reg:
        print("\nOUT-OF-REGION: no S3-direct path here, so the S3 fetch mechanisms are SKIPPED. These timings are only\n"
              "meaningful on the us-west-2 box (in_region()==True), where reads go straight to NSIDC S3 with STS creds.\n"
              "Run this script there:  uv run scripts/bench_fetch_mechanisms.py")
        return

    want = [m.strip() for m in a.mechanisms.split(",") if m.strip()] or list(access.S3_FETCH_MECHANISMS)
    run_names = [m for m in want if access.s3_mechanism_available(m)[0]]
    if not run_names:
        print("no requested mechanism is available; nothing to time.")
        return

    from aicesat import auth
    auth.login()
    access.s3_credentials()   # warm STS creds so no mechanism eats the first-cred latency

    d = DATASETS[a.dataset]
    print(f"\ndataset={a.dataset}  box={d['box']}  window={d['window']}  reps={a.reps}")
    work = capture_workload(a.dataset, d["box"], d["window"], a.granules, a.max_mb)
    if not work:
        print(f"no S3 workload captured for {a.dataset} over {d['box']} {d['window']} — is the index built "
              f"(data/index/{a.dataset}/res5) and are there granules in this box/window?")
        return
    raw_gets = sum(len(r) for _, r in work)
    raw_mb = sum(sz for _, r in work for _, sz in r) / 1e6
    print(f"workload: {len(work)} granules, {raw_gets} raw ranges, {raw_mb:.1f} MB wanted")

    shape_gaps = {"small": 0, "coalesced": (256 << 10)}   # small = near-raw (many small GETs); coalesced = in-region default gap
    reader = access.RangeReader()   # one reader: creds cached, each mechanism caches its own client on it
    print(f"reader.threads={reader.threads} (thread-pool width == aiobotocore concurrency cap, for a fair comparison)\n")
    try:
        for shape in [s.strip() for s in a.shapes.split(",") if s.strip()]:
            if shape not in shape_gaps:
                print(f"unknown shape {shape!r}; skipping"); continue
            shaped = shape_workload(work, shape_gaps[shape])
            n_spans = sum(len(s) for _, s in shaped)
            n_mb = sum(sz for _, spans in shaped for _, sz in spans) / 1e6
            print(f"=== shape '{shape}' (max_gap={shape_gaps[shape]} B): {len(shaped)} granules, {n_spans} GETs, "
                  f"{n_mb:.1f} MB fetched ===")
            hdr = f"  {'mechanism':13}{'wall_s':>9}{'MB/s':>8}{'GETs/s':>9}{'p50_ms':>9}{'p95_ms':>9}  identity"
            print(hdr)
            print("  " + "-" * (len(hdr) - 2))
            digests: dict[str, str] = {}
            ref = None
            for name in run_names:
                try:
                    med, per_get, dig, ns, nb = run_mechanism(reader, name, shaped, a.reps)
                except Exception as e:
                    print(f"  {name:13}   ERROR: {type(e).__name__}: {str(e)[:70]}")
                    continue
                digests[name] = dig
                ref = ref if ref is not None else dig
                ok = "ok" if dig == ref else "*** DIFFERS ***"
                mbps = nb / 1e6 / med if med > 0 else 0.0
                getsps = ns / med if med > 0 else 0.0
                if per_get:
                    p50 = np.percentile(per_get, 50) * 1e3
                    p95 = np.percentile(per_get, 95) * 1e3
                    p50s, p95s = f"{p50:8.1f}", f"{p95:8.1f}"
                else:
                    p50s = p95s = f"{'-':>8}"   # batched cat_ranges: per-GET latency not observable
                print(f"  {name:13}{med:9.3f}{mbps:8.1f}{getsps:9.1f}{p50s:>9}{p95s:>9}  {ok}")
            uniq = set(digests.values())
            if len(uniq) > 1:
                print("  WARNING: mechanisms returned DIFFERENT bytes — timings above are NOT comparable. Investigate.")
            else:
                print("  (all mechanisms byte-identical)")
            print()
    finally:
        reader.close()

    print("Notes: wall_s = median over reps of fetching every granule sequentially (spans within a granule concurrent), "
          "mirroring fetch_bbox. MB/s and GETs/s are over that wall. p50/p95 are single-GET latency where observable "
          "('-' for the batched cat_ranges). Flip the default with AICESAT_S3_FETCH=<winner> (see the file header).")


if __name__ == "__main__":
    main()
