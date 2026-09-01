"""Build the ATL06 sub-granule H3 index over a bbox — resumable and parallel. Re-run to resume (each granule
writes its own Parquet under data/index/atl06/res<R>/, so finished granules are skipped on a re-run).

Usage:  uv run python scripts/build_atl06_index.py W S E N [res] [workers]
Example (SW Greenland incl. Jakobshavn + K-transect):
        uv run python scripts/build_atl06_index.py -52 62 -44 70 5 8
"""
import concurrent.futures as cf
import functools
import logging
import sys
import time

from aicesat import auth, coverage, index, index_atl06


PER_GRANULE_TIMEOUT_S = 240   # a granule that cannot be read in 4 min is a stall, not slow I/O


def _index_one(granule, res, cells):
    """Top-level worker (picklable) — one granule per process, so the GIL-bound HDF5 b-tree walk truly parallelizes."""
    try:
        t = index_atl06.build_atl06_index(granule, res=res, cells=cells)
        return (coverage.granule_name(granule), t.num_rows, None)
    except Exception as e:
        try:
            name = coverage.granule_name(granule)
        except Exception:
            name = "?"
        return (name, 0, f"{type(e).__name__}: {e}")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("build_atl06_index")
    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__); sys.exit(2)
    bbox = [float(x) for x in a[:4]]
    res = int(a[4]) if len(a) > 4 else index_atl06.ATL06_RES
    workers = int(a[5]) if len(a) > 5 else 8

    from aicesat import planner
    fine = planner.coverage_cells(bbox)                  # the ground this build claims, at the claim resolution
    ring = planner.search_polygon(fine)                  # densified convex hull of that ground -> the CMR shape
    cells = planner.addressing_cells(fine, res)          # coarse partition keys the rows are filtered to
    auth.login()
    log.info("enumerating ATL06 granules over %s (full record) ...", bbox)
    granules = coverage.search("ATL06", "007", bbox, None, polygon=ring)
    names = {coverage.granule_name(g): g for g in granules}
    done = index_atl06.indexed_atl06_granules(res)
    todo = [g for n, g in names.items() if n not in done]
    log.info("res %d: %d granules found, %d already indexed, %d to build (workers=%d)",
             res, len(names), len(done & set(names)), len(todo), workers)
    md = index_atl06._index_dir(res); md.mkdir(parents=True, exist_ok=True)
    # The claim is stamped only AFTER the ground is actually indexed — see the end of this function. Stamping it
    # here (which this did) meant an interrupted build left a claim covering granules it never got to, so coverage
    # reported the whole region indexed and scenes over the unbuilt part came back quietly short.
    if not todo:
        index.write_build_manifest(md, bbox, res, None, len(names), cells=fine)   # already complete: claim it
        log.info("nothing to do — index complete")
        log.info("coverage rollup: %s", coverage.build_manifest("ATL06"))
        return

    t0 = time.time(); ok = err = rows = 0
    # A wall-clock budget for the run. Without one a single stalled remote read wedges the whole build: an
    # ATL06 rebuild sat at 185/207 for 101 minutes with no output and had to be killed. map()'s timeout is
    # measured from the call, so this is a budget for the batch, sized from a generous per-granule allowance.
    budget = PER_GRANULE_TIMEOUT_S * (len(todo) / max(1, workers) + 2)
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        try:
            for i, (name, nrows, e) in enumerate(ex.map(functools.partial(_index_one, res=res, cells=cells), todo, chunksize=1, timeout=budget), 1):
                if e:
                    err += 1; log.warning("FAIL %s: %s", name, e)
                else:
                    ok += 1; rows += nrows
                if i % 25 == 0 or i == len(todo):
                    el = time.time() - t0; rate = i / el if el else 0
                    log.info("%d/%d (%d ok, %d err, %d rows) | %.2f gran/s | elapsed %.1fm | ETA %.1fm",
                             i, len(todo), ok, err, rows, rate, el / 60, (len(todo) - i) / rate / 60 if rate else 0)
        except cf.TimeoutError:
            # Everything indexed so far is on disk; a re-run skips it and retries only what is left.
            log.error("TIMED OUT after %.1f min with %d/%d granules done — re-run to resume", budget / 60, ok + err, len(todo))
    if err == 0 and ok == len(todo):
        index.write_build_manifest(md, bbox, res, None, len(names), cells=fine)   # every granule landed: claim it
    else:
        log.warning("NOT claiming coverage: %d of %d granules did not index. Re-run to finish; the claim is only "
                    "stamped once the ground behind it is complete.", len(todo) - ok, len(todo))
    log.info("DONE res %d: %d ok, %d err, %d rows in %.1fm -> %s",
             res, ok, err, rows, (time.time() - t0) / 60, index_atl06._index_dir(res))
    # The rollup belongs to whoever wrote the index. Doing it here means coverage queries and index_status
    # read one small manifest instead of every granule parquet, and no user request is billed for the re-read.
    log.info("coverage rollup: %s", coverage.build_manifest("ATL06"))


if __name__ == "__main__":
    main()
