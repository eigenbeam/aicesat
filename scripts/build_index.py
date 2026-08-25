"""Offline index pre-build (spec §6.1): index every ATL03 granule touching an area and time window, uncapped, in parallel.
Run ahead of time so no query ever waits on an index build. Idempotent: current-schema index files are skipped.
usage: uv run scripts/build_index.py [--region NAME | --bbox W S E N] [--window START END] [--workers 8]
"""
import argparse, json, logging, sys, time

if __name__ == "__main__":  # guard: index workers are spawned processes that re-import this file
    from aicesat import coverage, index, regions
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("fsspec", "urllib3", "earthaccess"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=None, choices=list(regions.REGIONS))
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    ap.add_argument("--window", nargs=2, metavar=("START", "END"), default=None)
    ap.add_argument("--workers", type=int, default=index.INDEX_WORKERS)
    a = ap.parse_args()
    bbox = regions.resolve_bbox(a.region, tuple(a.bbox) if a.bbox else None)
    window = tuple(a.window) if a.window else regions.DEFAULT_ATL03_WINDOW
    granules = coverage.search(coverage.ATL03_SHORT_NAME, coverage.ATL03_VERSION, bbox, window)
    t0 = time.time()
    out = index.ensure_index(granules, workers=a.workers)
    print(json.dumps({"bbox": list(bbox), "window": list(window), "granules": len(granules), **out, "wall_seconds": round(time.time() - t0, 1)}, indent=1))
