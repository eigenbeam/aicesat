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
    from aicesat import planner
    fine = planner.coverage_cells(bbox)                  # the ground this build claims, at the claim resolution
    ring = planner.search_polygon(fine)                  # densified convex hull of that ground -> the CMR shape
    cells = planner.addressing_cells(fine, index.H3_RES)  # coarse partition keys the rows are filtered to
    granules = coverage.search(coverage.ATL03_SHORT_NAME, coverage.ATL03_VERSION, bbox, window, polygon=ring)
    t0 = time.time()
    out = index.ensure_index(granules, workers=a.workers, cells=fine)
    # The query path refuses an area whose index has no manifest covering it (planner._ensure).
    cov = index.write_build_manifest(index.ATL03_INDEX_DIR, bbox, index.H3_RES, window, len(granules), cells=fine)
    print(json.dumps({"bbox": list(bbox), "claim_cells_compacted": len(cov["cells"]), "search_vertices": len(ring), "window": list(window), "granules": len(granules), **out,
                      "wall_seconds": round(time.time() - t0, 1)}, indent=1))
    if out["failed"]:
        print(f"\n{len(out['failed'])} granule(s) did not index; re-run this command to retry just those.", file=sys.stderr)
        sys.exit(1)
