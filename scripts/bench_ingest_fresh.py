"""Run scripts/bench_ingest_phases.py on a box the lake has never seen, then forget what it wrote.

bench_ingest_phases measures a COLD ingest, so each run needs a box no previous run touched: atl06.extract keeps a
scene cache keyed by the exact bbox, and the lake keeps the cells. This shifts a base box by `--shift` (degrees; use a
fresh multiple of 1e-4 each run) so the scene cache misses, runs the bench in a subprocess with the service's
environment, and afterwards returns the box's ATL06 cells to never-fetched with lake.forget_cells, so the next run is
cold too and the deployed lake is left as it was found.

    uv run python scripts/bench_ingest_fresh.py --shift 8e-4 [--base -40.5 74.6 -39.8 74.9] [--writers 2]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", nargs=4, type=float, default=[-40.5, 74.6, -39.8, 74.9], metavar=("W", "S", "E", "N"))
    ap.add_argument("--shift", type=float, required=True, help="degrees added to every edge; a fresh 1e-4 multiple per run")
    ap.add_argument("--writers", type=int, default=None, help="AICESAT_LAKE_WRITERS for this run")
    ap.add_argument("--keep", action="store_true", help="do not forget the cells afterwards")
    a = ap.parse_args()
    bbox = [round(v + a.shift, 6) for v in a.base]
    env = dict(os.environ)
    if a.writers is not None:
        env["AICESAT_LAKE_WRITERS"] = str(a.writers)
    here = Path(__file__).resolve().parent
    out = subprocess.run([sys.executable, str(here / "bench_ingest_phases.py"), *map(str, bbox)], env=env,
                         capture_output=True, text=True, check=False)
    print(out.stdout.strip())
    if out.returncode:
        print(out.stderr.strip()[-2000:], file=sys.stderr)
    if not a.keep:
        from aicesat import index_atl06, lake, planner
        cells = planner.cells_for_bbox(bbox, res=index_atl06.ATL06_RES)
        gone = lake.forget_cells("ATL06", cells, f"bench_ingest_fresh shift={a.shift}")
        print(json.dumps({"forgot": gone}))
    return out.returncode


if __name__ == "__main__":
    sys.exit(main())
