"""Building the ATL06 sub-granule index — deliberately NOT in index_atl06.

index_atl06 is a QUERY-path module: fetch_bbox and plan_bbox are what a scene or a TUI command call, and
tests/test_index_only.py holds the line that nothing on that path may reach CMR. Discovery is paid once, here, at
build time; an unindexed area is an error at query time, never a slow fallback that searches CMR and succeeds.
Keeping the builder in its own module makes that boundary a fact of the file tree rather than a convention.
"""
from __future__ import annotations

import logging
import time

from . import index_atl06

log = logging.getLogger(__name__)


PER_GRANULE_TIMEOUT_S = 240   # a granule that cannot be read in 4 min is a stall, not slow I/O


def _index_one(granule, res, cells):
    """Top-level worker (picklable) -- one granule per process, so the GIL-bound HDF5 b-tree walk truly parallelizes."""
    from . import coverage
    try:
        t = index_atl06.build_atl06_index(granule, res=res, cells=cells)
        return (coverage.granule_name(granule), t.num_rows, None)
    except Exception as e:
        try:
            name = coverage.granule_name(granule)
        except Exception:
            name = "?"
        return (name, 0, f"{type(e).__name__}: {e}")


def build_bbox(bbox, res: int = index_atl06.ATL06_RES, workers: int = 8, window=None, on_plan=None, on_granule=None) -> dict:
    """Build the ATL06 sub-granule index over `bbox` -- resumable and parallel.

    Each granule writes its own Parquet under data/index/atl06/res<R>/, so a re-run skips what finished and retries
    only what is left. The COVERAGE CLAIM is stamped only after every granule lands: stamping it up front (which this
    once did) left an interrupted build claiming ground it never indexed, and scenes over the unbuilt part came back
    quietly short rather than erroring.

    `on_plan(dict)` fires once before any granule is read, with the denominators a progress display needs
    (`granules`, `already_indexed`, `todo`, `new_ground_cells`). `on_granule(name, rows, err)` fires once per granule
    as it completes -- `err` is None on success, else a "Type: message" string.
    """
    import concurrent.futures as cf
    import functools

    from . import auth, coverage, index as index_mod_, index_atl06, planner

    t0 = time.time()
    fine = planner.coverage_cells(bbox)                  # the ground this build claims, at the claim resolution
    ring = planner.search_polygon(fine)                  # densified convex hull of that ground -> the CMR shape
    cells = planner.addressing_cells(fine, res)          # coarse partition keys the rows are filtered to
    auth.login()
    granules = coverage.search("ATL06", "007", bbox, window, polygon=ring)
    names = {coverage.granule_name(g): g for g in granules}
    d = index_atl06._index_dir(res); d.mkdir(parents=True, exist_ok=True)
    done = index_atl06.indexed_atl06_granules(res)
    # Which ground is genuinely NEW? A granule's rows are filtered to the cells its build asked for, so a granule
    # indexed for a smaller bbox holds nothing outside it. Skipping by NAME therefore left the added ring unindexed
    # while the claim was extended over it -- a silent short scene, not an error. So:
    #   * re-running the SAME area -> `needed` is empty -> nothing is re-indexed (the resume path is unchanged)
    #   * ENLARGING it -> only granules that cannot PROVE they cover the new ground are rebuilt
    # Files are never deleted: a rebuild overwrites one granule atomically, so an interrupt leaves the old one intact.
    needed = index_mod_.unclaimed_cells(d, fine)
    todo = [g for n, g in names.items()
            if n not in done or not index_mod_.granule_proves(d / f"{n}.parquet", needed)]
    plan = {"granules": len(names), "already_indexed": len(done & set(names)), "todo": len(todo),
            "new_ground_cells": len(needed), "claim_cells": len(fine), "search_vertices": len(ring),
            "res": res, "workers": workers, "index_dir": str(d)}
    if on_plan is not None:
        on_plan(plan)
    log.info("res %d: %d granules found, %d already indexed, %d to build (workers=%d)",
             res, len(names), plan["already_indexed"], len(todo), workers)

    ok = err = rows = 0
    timed_out = False
    if not todo:
        # already complete: claim it. write_build_manifest claims nothing when the search itself came back empty.
        index_mod_.write_build_manifest(d, bbox, res, window, len(names), cells=fine)
        if names:
            log.info("nothing to do -- index complete")
    else:
        # A wall-clock budget for the run. Without one a single stalled remote read wedges the whole build: an
        # ATL06 rebuild sat at 185/207 for 101 minutes with no output and had to be killed. map()'s timeout is
        # measured from the call, so this is a budget for the batch, sized from a generous per-granule allowance.
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
                # Everything indexed so far is on disk; a re-run skips it and retries only what is left.
                timed_out = True
                log.error("TIMED OUT after %.1f min with %d/%d granules done -- re-run to resume",
                          budget / 60, ok + err, len(todo))
        if err == 0 and ok == len(todo):
            index_mod_.write_build_manifest(d, bbox, res, window, len(names), cells=fine)   # all landed: claim it
        else:
            log.warning("NOT claiming coverage: %d of %d granules did not index. Re-run to finish; the claim is "
                        "only stamped once the ground behind it is complete.", len(todo) - ok, len(todo))
    # The rollup belongs to whoever wrote the index. Doing it here means coverage queries and index_status read one
    # small manifest instead of every granule parquet, and no user request is billed for the re-read.
    rollup = coverage.build_manifest("ATL06")
    return {**plan, "ok": ok, "err": err, "rows": rows, "timed_out": timed_out,
            "claimed": bool(names) and (not todo or (err == 0 and ok == len(todo))),
            "seconds": round(time.time() - t0, 1), "rollup": rollup}
