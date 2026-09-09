"""Build the ATL06 sub-granule H3 index over a bbox — resumable and parallel. Re-run to resume (each granule
writes its own Parquet under data/index/atl06/res<R>/, so finished granules are skipped on a re-run).

Usage:  uv run python scripts/build_atl06_index.py W S E N [res] [workers]
Example (SW Greenland incl. Jakobshavn + K-transect):
        uv run python scripts/build_atl06_index.py -52 62 -44 70 5 8

The build itself lives in aicesat.build_atl06.build_bbox — this is a logging driver over it, so the TUI and this script
run the same sequence rather than two copies of it.
"""
import logging
import sys
import time


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("build_atl06_index")
    from aicesat import build_atl06, index_atl06

    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__); sys.exit(2)
    bbox = [float(x) for x in a[:4]]
    res = int(a[4]) if len(a) > 4 else index_atl06.ATL06_RES
    workers = int(a[5]) if len(a) > 5 else 8

    state = {"n": 0, "todo": 0, "ok": 0, "err": 0, "rows": 0, "t0": time.time()}

    def on_plan(p):
        state["todo"] = p["todo"]
        if p["new_ground_cells"]:
            log.info("new ground: %d of %d coverage cells are not yet claimed", p["new_ground_cells"], p["claim_cells"])

    def on_granule(name, nrows, err):
        state["n"] += 1
        state["err" if err else "ok"] += 1
        state["rows"] += nrows
        i, todo = state["n"], state["todo"]
        if i % 25 == 0 or i == todo:
            el = time.time() - state["t0"]; rate = i / el if el else 0
            log.info("%d/%d (%d ok, %d err, %d rows) | %.2f gran/s | elapsed %.1fm | ETA %.1fm",
                     i, todo, state["ok"], state["err"], state["rows"], rate, el / 60,
                     (todo - i) / rate / 60 if rate else 0)

    out = build_atl06.build_bbox(bbox, res=res, workers=workers, on_plan=on_plan, on_granule=on_granule)
    log.info("DONE res %d: %d ok, %d err, %d rows in %.1fm -> %s",
             res, out["ok"], out["err"], out["rows"], out["seconds"] / 60, out["index_dir"])
    log.info("coverage rollup: %s", out["rollup"])
    sys.exit(0 if out["claimed"] else 1)


if __name__ == "__main__":
    main()
