"""Run the planner for a region: index granules, fetch missing chunks by byte range, materialize to the lake, query.
usage: uv run scripts/ingest.py [region] [max_granules] [--force]
"""
import json, logging, sys, time
from aicesat import lake, planner, regions
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
if __name__ == "__main__":  # guard: index build spawns worker processes that re-import this file
    for noisy in ("fsspec", "urllib3", "earthaccess"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    region = sys.argv[1] if len(sys.argv) > 1 else regions.DEFAULT_REGION
    maxg = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    bbox = regions.resolve_bbox(region)
    t = time.time()
    plan = planner.ensure(bbox, regions.DEFAULT_ATL03_WINDOW, max_granules=maxg, force="--force" in sys.argv)
    t1 = time.time()
    q = lake.query_photons(bbox, plan["cells"], 3, granules=plan["granules"])
    print(json.dumps({"stats": plan["stats"], "query_seconds": round(time.time() - t1, 2), "photons_conf>=3_in_bbox": int(q["lon"].size),
                      "lake": lake.lake_summary(), "total_seconds": round(time.time() - t, 1)}, indent=1, default=str))
