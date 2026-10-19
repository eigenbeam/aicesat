"""Smoke test: extract ATL03 photons over a region and print a summary.

usage: uv run python scripts/extract_atl03.py [region]

The planner runs its decode pool with spawned workers, and a spawned worker re-imports the main script, so the work
must sit under a main guard or every worker re-runs the extraction.
"""
import json
import logging
import sys
import time

from aicesat import atl03, regions


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    region = sys.argv[1] if len(sys.argv) > 1 else regions.DEFAULT_REGION
    t = time.time()
    arrays, meta = atl03.extract(regions.resolve_bbox(region), regions.DEFAULT_ATL03_WINDOW)
    meta["elapsed_s"] = round(time.time() - t, 1)
    meta["h_range"] = [float(arrays["h"].min()), float(arrays["h"].max())]
    meta["t_range"] = [str(arrays["t"].min()), str(arrays["t"].max())]
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
