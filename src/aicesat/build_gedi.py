"""Building the GEDI sub-granule index — deliberately NOT in index_gedi, for the reason build_atl06 gives.

index_gedi is a QUERY-path module: fetch_bbox and plan_bbox are what a scene calls, and tests/test_index_only.py
holds the line that nothing on that path may reach CMR. Discovery is paid once, here, at build time.
"""
from __future__ import annotations

import logging
import time

from . import index_gedi

log = logging.getLogger(__name__)

PER_GRANULE_TIMEOUT_S = 600   # a GEDI granule is a quarter orbit (~170k shots x 8 beams); slower than ATL06


def _index_one(granule, res, cells):
    """Top-level worker (picklable): one granule per process, so the GIL-bound HDF5 b-tree walk parallelizes."""
    from . import coverage
    try:
        t = index_gedi.build_gedi_index(granule, res=res, cells=cells)
        return (coverage.granule_name(granule), t.num_rows, None)
    except Exception as e:
        try:
            name = coverage.granule_name(granule)
        except Exception:
            name = "?"
        return (name, 0, f"{type(e).__name__}: {e}")


def build_bbox(bbox, res: int = index_gedi.GEDI_RES, workers: int = 8, window=None,
               on_plan=None, on_granule=None) -> dict:
    """Build the GEDI index over `bbox` — resumable and parallel, mirroring build_atl06.build_bbox.

    V003 ONLY. V002 and V003 are the same acquisitions (V003 is a reprocessing), so indexing both would double
    every shot in the lake and correlate anything computed from them.
    """
    import concurrent.futures as cf
    import functools

    from . import auth, coverage, index as index_mod_, planner

    t0 = time.time()
    fine = planner.coverage_cells(bbox)
    ring = planner.search_polygon(fine)
    cells = planner.addressing_cells(fine, res)
    auth.login()
    # CMR carries V002 and V003 as separate collection versions of the same acquisitions, so asking for 003 is
    # both the correct filter and the cheap one -- 51 granules over the Langtang box instead of 98, with no
    # client-side filtering and no chance of the two versions of one orbit both reaching the lake.
    granules = coverage.search("GEDI02_A", "003", bbox, window, polygon=ring)
    dropped = 0
    names = {coverage.granule_name(g): g for g in granules}
    d = index_gedi._index_dir(res); d.mkdir(parents=True, exist_ok=True)
    done = index_gedi.indexed_gedi_granules(res)
    needed = index_mod_.unclaimed_cells(d, fine)
    todo = [g for n, g in names.items()
            if n not in done or not index_mod_.granule_proves(d / f"{n}.parquet", needed)]
    plan = {"granules": len(names), "already_indexed": len(done & set(names)),
            "todo": len(todo), "new_ground_cells": len(needed), "claim_cells": len(fine),
            "search_vertices": len(ring), "res": res, "workers": workers, "index_dir": str(d)}
    if on_plan is not None:
        on_plan(plan)
    log.info("res %d: %d V003 granules, %d already indexed, %d to build (workers=%d)",
             res, len(names), plan["already_indexed"], len(todo), workers)

    ok = err = rows = 0
    timed_out = False
    if not todo:
        index_mod_.write_build_manifest(d, bbox, res, window, len(names), cells=fine)
        log.info("nothing to do -- index complete")
    else:
        budget = PER_GRANULE_TIMEOUT_S * (len(todo) / max(1, workers) + 2)
        with cf.ProcessPoolExecutor(max_workers=workers) as ex:
            try:
                for name, nrows, e in ex.map(functools.partial(_index_one, res=res, cells=cells), todo,
                                             chunksize=1, timeout=budget):
                    if e:
                        err += 1; log.warning("FAIL %s: %s", name, e)
                    else:
                        ok += 1; rows += nrows
                    if on_granule is not None:
                        on_granule(name, nrows, e)
            except cf.TimeoutError:
                timed_out = True
                log.error("TIMED OUT after %.1f min with %d/%d granules done -- re-run to resume",
                          budget / 60, ok + err, len(todo))
        if err == 0 and ok == len(todo):
            index_mod_.write_build_manifest(d, bbox, res, window, len(names), cells=fine)
        else:
            log.warning("NOT claiming coverage: %d of %d granules did not index; re-run to finish",
                        len(todo) - ok, len(todo))
    rollup = coverage.build_manifest("GEDI")
    return {**plan, "ok": ok, "err": err, "rows": rows, "timed_out": timed_out,
            "claimed": not todo or (err == 0 and ok == len(todo)),
            "seconds": round(time.time() - t0, 1), "rollup": rollup}
