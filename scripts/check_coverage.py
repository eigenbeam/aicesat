"""Region-validation helper: how many ATL03 / GLAH06 granules touch a bbox, by cycle / campaign.

usage: uv run scripts/check_coverage.py [--region NAME | --bbox W S E N] [--atl03 START END] [--glas START END]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from aicesat import regions
from aicesat.coverage import check_coverage

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

p = argparse.ArgumentParser()
p.add_argument("--region", default=None, choices=list(regions.REGIONS))
p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
p.add_argument("--atl03", nargs=2, metavar=("START", "END"), default=None)
p.add_argument("--glas", nargs=2, metavar=("START", "END"), default=None)
a = p.parse_args()

bbox = regions.resolve_bbox(a.region, tuple(a.bbox) if a.bbox else None)
out = check_coverage(bbox, atl03_window=a.atl03, glas_window=a.glas)
print(json.dumps(out, indent=2))
