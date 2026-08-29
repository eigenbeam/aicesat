"""Build the GLAS/GLAH06 sub-granule H3 index over a bbox — resumable and parallel (see build_atl06_index.py).

Usage:  uv run python scripts/build_glas_index.py W S E N [res] [workers]
Example (SW Greenland):  uv run python scripts/build_glas_index.py -52 62 -44 70 5 8
"""
import concurrent.futures as cf
import functools
import json
import logging
import sys
import time

from aicesat import auth, coverage, index_glas


def _index_one(granule, res, bbox):
    try:
        t = index_glas.build_glas_index(granule, res=res, bbox=bbox)
        return (coverage.granule_name(granule), t.num_rows, None)
    except Exception as e:
        try:
            name = coverage.granule_name(granule)
        except Exception:
            name = "?"
        return (name, 0, f"{type(e).__name__}: {e}")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("build_glas_index")
    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__); sys.exit(2)
    bbox = [float(x) for x in a[:4]]
    res = int(a[4]) if len(a) > 4 else index_glas.GLAS_RES
    workers = int(a[5]) if len(a) > 5 else 8

    auth.login()
    log.info("enumerating GLAH06 granules over %s (full record) ...", bbox)
    granules = coverage.search(coverage.GLAS_SHORT_NAME, coverage.GLAS_VERSION, bbox, None)
    names = {coverage.granule_name(g): g for g in granules}
    done = index_glas.indexed_glas_granules(res)
    todo = [g for n, g in names.items() if n not in done]
    log.info("res %d: %d granules found, %d already indexed, %d to build (workers=%d)",
             res, len(names), len(done & set(names)), len(todo), workers)
    md = index_glas._index_dir(res); md.mkdir(parents=True, exist_ok=True)
    (md / "_build.json").write_text(json.dumps({"bbox": bbox, "res": res, "target": len(names), "started": time.time()}))
    if not todo:
        log.info("nothing to do — index complete")
        log.info("coverage rollup: %s", coverage.build_manifest("GLAS"))
        return

    t0 = time.time(); ok = err = rows = 0
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (name, nrows, e) in enumerate(ex.map(functools.partial(_index_one, res=res, bbox=bbox), todo, chunksize=1), 1):
            if e:
                err += 1; log.warning("FAIL %s: %s", name, e)
            else:
                ok += 1; rows += nrows
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0; rate = i / el if el else 0
                log.info("%d/%d (%d ok, %d err, %d rows) | %.2f gran/s | elapsed %.1fm | ETA %.1fm",
                         i, len(todo), ok, err, rows, rate, el / 60, (len(todo) - i) / rate / 60 if rate else 0)
    log.info("DONE res %d: %d ok, %d err, %d rows in %.1fm -> %s",
             res, ok, err, rows, (time.time() - t0) / 60, index_glas._index_dir(res))
    # The rollup belongs to whoever wrote the index. Doing it here means coverage queries and index_status
    # read one small manifest instead of every granule parquet, and no user request is billed for the re-read.
    log.info("coverage rollup: %s", coverage.build_manifest("GLAS"))


if __name__ == "__main__":
    main()
