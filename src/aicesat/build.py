"""Building the sub-granule indexes: ONE driver for every collection.

The sequence is the same for all of them -- plan the ground, log in, search CMR, decide which granules still need
building, run a pool, claim the ground only when every granule landed, roll up the coverage manifest -- and it used to
be written five times (build_atl06.build_bbox, build_gedi.build_bbox, and inline in the GLAS, ICESSN and GPS scripts),
each copy forking its workers with the CMR result in the parent. It is written once here, parameterised by a
`BuildCollection` value, and the pool is `pool.run_pool` (spawn, per-future monotonic deadline, retry).

What crosses into a worker is a `GranuleRef`: four strings instead of the earthaccess DataGranule object, so a worker
receives only its task whatever the start method. The ref keeps `data_links()` and `["meta"]` so the per-granule
builders (`index_*.build_*_index`) and the test fakes need no change.

This module is a BUILDER: it is the one place besides coverage.py allowed to reach CMR (tests/test_index_only.py
`_MAY_SEARCH_CMR`). Query-path modules -- index_*.py, the extract wrappers -- must never import it.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import coverage, index as index_mod, planner, pool

log = logging.getLogger(__name__)


class BuildAborted(Exception):
    """A build that must not proceed (e.g. GPS without its traverse-metadata granule). Nothing is claimed."""


@dataclass(frozen=True)
class GranuleRef:
    """What a worker needs to build one granule's index file: strings, not the earthaccess object.

    `data_links()` and `["meta"]` mirror the DataGranule interface so the builders and their test fakes are unchanged;
    `["name"]` is for the build tests' dict granules."""
    name: str
    url: str = ""
    s3url: str = ""
    revision: str = ""
    native_id: str = ""

    def data_links(self, access=None) -> list[str]:
        if access == "direct":
            return [self.s3url] if self.s3url else []
        return [self.url] if self.url else []

    def __getitem__(self, key):
        if key == "meta":
            return {"native-id": self.native_id or self.name, "revision-id": self.revision}
        if key == "name":
            return self.name
        raise KeyError(key)

    def size(self):
        raise AttributeError("GranuleRef carries no size")


def granule_ref(g) -> GranuleRef:
    """A GranuleRef from an earthaccess DataGranule, a dict-shaped fake, or a GranuleRef (identity).

    The name comes from coverage.granule_name (the index file's name on disk; tests patch it). A granule with no
    links or no `meta` (the ICESSN/GPS test fakes) yields empty strings, never an error."""
    if isinstance(g, GranuleRef):
        return g
    name = coverage.granule_name(g)
    try:
        links = g.data_links() or []
    except Exception:
        links = []
    try:
        direct = g.data_links(access="direct") or []
    except Exception:
        direct = []
    try:
        meta = g["meta"] or {}
    except Exception:
        meta = {}
    return GranuleRef(name=name, url=links[0] if links else "", s3url=direct[0] if direct else "",
                      revision=str(meta.get("revision-id", "")), native_id=str(meta.get("native-id", name)))


@dataclass(frozen=True)
class BuildCollection:
    """The per-collection parameters of the one build sequence.

    `indexed(res, extra)` is the resume set (granule names on disk with the current schema); `index_dir(res)` the
    directory the files land in; `prepare(granules) -> (granules, extra)` runs once in the parent before the pool
    (GPS reads its traverse-metadata granule here) and may raise BuildAborted; `extra` rides into every job.
    Function-valued fields look their targets up at CALL time so tests can monkeypatch the index modules."""
    key: str
    short_name: str
    version: str
    default_res: int
    index_dir: Callable[[int], Path]
    indexed: Callable[[int, dict], set]
    timeout_s: int = 240            # a granule that cannot be read in 4 min is a stall, not slow I/O
    default_workers: int = 8        # 16 where the build is latency-bound (CSV collections); the HDF5 builders are
                                    # re-measured after the whole-object read (scripts/bench_build_io.py)
    prepare: Callable | None = None


def _gps_prepare(granules):
    """GPS: the traverse-metadata granule is read ONCE, in the parent, and every survey's sled geometry resolved from
    it; the metadata sha1 is stamped into each survey's index rows so a metadata change invalidates them."""
    import hashlib

    from . import gpstruth
    from .access import RangeReader, access_url

    meta_g, csv_g = gpstruth.split_granules(granules)
    log.info("%d granules: %d survey CSVs, %s metadata file, %d RINEX files skipped",
             len(granules), len(csv_g), "1" if meta_g else "NO", len(granules) - len(csv_g) - (1 if meta_g else 0))
    if not csv_g:
        return [], {}                                   # an empty search is recorded by run_build, claimed by nobody
    if meta_g is None:
        # Without it every survey would take the default sled geometry: a silent 1-4 cm error in every height.
        raise BuildAborted("the IS2TGPSSS traverse-metadata granule is missing from the search result; not building")
    meta_bytes = RangeReader().read_all(access_url(meta_g.data_links()[0], (meta_g.data_links(access="direct") or [""])[0]))
    metadata_sha1 = hashlib.sha1(meta_bytes).hexdigest()[:16]
    key2row = gpstruth.traverse_metadata_from_bytes(meta_bytes)
    names = sorted(coverage.granule_name(g) for g in csv_g)
    return csv_g, {"geometry": resolve_geometry(names, key2row), "metadata_sha1": metadata_sha1}


def resolve_geometry(csv_names, key2row) -> dict:
    """{csv name: (arp_to_sled_m, track_depth_cm, track_depth_known, rinex_key)}, logging what had to be imputed.

    FALLBACK_TRACK_DEPTH_CM is a fixed constant described in gpstruth.py as the median of the known sinkages; this
    logs the median the metadata file actually gives, so the two can be compared. It changes nothing."""
    import statistics

    from . import gpstruth

    geometry, imputed, unmatched, known_depths = {}, 0, [], []
    for name in csv_names:
        row, rkey = gpstruth.survey_row(key2row, name)
        if row is None:
            unmatched.append(name)
        arp, depth, known = gpstruth._sled_geometry(row)
        geometry[name] = (arp, depth, known, rkey or "")
        if known:
            known_depths.append(depth)
        else:
            imputed += 1
    if unmatched:
        log.warning("%d survey(s) have no traverse-metadata row; default sled geometry used: %s",
                    len(unmatched), ", ".join(unmatched[:5]) + (" ..." if len(unmatched) > 5 else ""))
    if known_depths:
        log.info("track depth: measured on %d surveys, median %.2f cm; imputed on %d (fallback constant %.2f cm)",
                 len(known_depths), statistics.median(known_depths), imputed, gpstruth.FALLBACK_TRACK_DEPTH_CM)
    return geometry


def _collections() -> dict[str, BuildCollection]:
    from . import index_atl06, index_gedi, index_glas, index_gpstruth, index_icessn
    return {
        "ATL06": BuildCollection("ATL06", "ATL06", "007", index_atl06.ATL06_RES,
                                 lambda res: index_atl06._index_dir(res),
                                 lambda res, extra: index_atl06.indexed_atl06_granules(res)),
        "GEDI": BuildCollection("GEDI", "GEDI02_A", "003", index_gedi.GEDI_RES,
                                lambda res: index_gedi._index_dir(res),
                                lambda res, extra: index_gedi.indexed_gedi_granules(res),
                                timeout_s=600),   # a GEDI granule is a quarter orbit (~170k shots x 8 beams)
        "GLAS": BuildCollection("GLAS", coverage.GLAS_SHORT_NAME, coverage.GLAS_VERSION, index_glas.GLAS_RES,
                                lambda res: index_glas._index_dir(res),
                                lambda res, extra: index_glas.indexed_glas_granules(res)),
        "ICESSN": BuildCollection("ICESSN", coverage.ICESSN_SHORT_NAME, coverage.ICESSN_VERSION, index_icessn.ICESSN_RES,
                                  lambda res: index_icessn._index_dir(res),
                                  lambda res, extra: index_icessn.indexed_icessn_granules(res),
                                  default_workers=16),
        "GPSTRUTH": BuildCollection("GPSTRUTH", coverage.GPSTRUTH_SHORT_NAME, coverage.GPSTRUTH_VERSION,
                                    index_gpstruth.GPSTRUTH_RES,
                                    lambda res: index_gpstruth._index_dir(res),
                                    lambda res, extra: index_gpstruth.indexed_gpstruth_granules(
                                        res, metadata_sha1=extra.get("metadata_sha1")),
                                    default_workers=16, prepare=_gps_prepare),
    }


def collection(key: str) -> BuildCollection:
    return _collections()[key]


def _index_one(ref: GranuleRef, key: str, res: int, cells, extra: dict) -> tuple[str, int]:
    """Top-level worker (picklable under spawn): build one granule's index file, return (name, rows).

    The builder is looked up on its module at call time, so a test that monkeypatches `index_atl06.build_atl06_index`
    is honoured. Exceptions propagate: the pool retries them (pool.run_pool) and reports the last one."""
    from . import index_atl06, index_gedi, index_glas, index_gpstruth, index_icessn
    if key == "ATL06":
        t = index_atl06.build_atl06_index(ref, res=res, cells=cells)
    elif key == "GEDI":
        t = index_gedi.build_gedi_index(ref, res=res, cells=cells)
    elif key == "GLAS":
        t = index_glas.build_glas_index(ref, res=res, cells=cells)
    elif key == "ICESSN":
        t = index_icessn.build_icessn_index(ref, res=res, cells=cells)
    elif key == "GPSTRUTH":
        t = index_gpstruth.build_gpstruth_index(ref, extra["geometry"][ref.name], extra["metadata_sha1"], res=res, cells=cells)
    else:
        raise ValueError(f"no builder for collection {key}")
    return ref.name, t.num_rows


def run_build(col: BuildCollection, bbox, res: int | None = None, workers: int | None = None, window=None,
              on_plan=None, on_granule=None) -> dict:
    """Build `col`'s index over `bbox` -- resumable and parallel.

    Each granule writes its own Parquet under `col.index_dir(res)`, so a re-run skips what finished and retries only
    what is left. The COVERAGE CLAIM is stamped only after every granule lands: stamping it up front left an
    interrupted build claiming ground it never indexed, and scenes over the unbuilt part came back quietly short.

    `on_plan(dict)` fires once before any granule is read, with the denominators a progress display needs
    (`granules`, `already_indexed`, `todo`, `new_ground_cells`). `on_granule(name, rows, err)` fires once per granule
    as it completes, in submission order -- `err` is None on success, else a "Type: message" string.
    """
    from . import auth

    res = col.default_res if res is None else res
    workers = col.default_workers if workers is None else workers
    t0 = time.time()
    fine = planner.coverage_cells(bbox)                  # the ground this build claims, at the claim resolution
    ring = planner.search_polygon(fine)                  # densified convex hull of that ground -> the CMR shape
    cells = planner.addressing_cells(fine, res)          # coarse partition keys the rows are filtered to
    auth.login()
    granules = coverage.search(col.short_name, col.version, bbox, window, polygon=ring)
    extra: dict = {}
    d = col.index_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    if col.prepare is not None:
        try:
            granules, extra = col.prepare(granules)
        except BuildAborted as e:
            log.error("%s: %s", col.key, e)
            return {"granules": len(granules), "already_indexed": 0, "todo": 0, "new_ground_cells": 0,
                    "claim_cells": len(fine), "search_vertices": len(ring), "res": res, "workers": workers,
                    "index_dir": str(d), "ok": 0, "err": 0, "rows": 0, "claimed": False, "aborted": str(e),
                    "seconds": round(time.time() - t0, 1), "rollup": None, "failed": []}
    refs = [granule_ref(g) for g in granules]
    del granules                                         # nothing below holds the earthaccess objects
    names = {r.name: r for r in refs}
    done = col.indexed(res, extra)
    # Which ground is genuinely NEW? A granule's rows are filtered to the cells its build asked for, so a granule
    # indexed for a smaller bbox holds nothing outside it. Skipping by NAME therefore left the added ring unindexed
    # while the claim was extended over it -- a silent short scene, not an error. So:
    #   * re-running the SAME area -> `needed` is empty -> nothing is re-indexed (the resume path is unchanged)
    #   * ENLARGING it -> only granules that cannot PROVE they cover the new ground are rebuilt
    # Files are never deleted: a rebuild overwrites one granule atomically, so an interrupt leaves the old one intact.
    needed = index_mod.unclaimed_cells(d, fine)
    todo = [r for n, r in names.items()
            if n not in done or not index_mod.granule_proves(d / f"{n}.parquet", needed)]
    plan = {"granules": len(names), "already_indexed": len(done & set(names)), "todo": len(todo),
            "new_ground_cells": len(needed), "claim_cells": len(fine), "search_vertices": len(ring),
            "res": res, "workers": workers, "index_dir": str(d)}
    if on_plan is not None:
        on_plan(plan)
    log.info("%s res %d: %d granules found, %d already indexed, %d to build (workers=%d)",
             col.key, res, len(names), plan["already_indexed"], len(todo), workers)

    ok = err = rows = 0
    failed_names: list[str] = []
    if not todo:
        # already complete: claim it. write_build_manifest claims nothing when the search itself came back empty.
        index_mod.write_build_manifest(d, bbox, res, window, len(names), cells=fine)
        if names:
            log.info("nothing to do -- index complete")
    else:
        def _each(ref, result):
            nonlocal ok, rows
            ok += 1
            rows += result[1]
            if on_granule is not None:
                on_granule(ref.name, result[1], None)

        built, failed = pool.run_pool(_index_one, todo, args=(col.key, res, cells, extra), workers=workers,
                                      timeout_s=col.timeout_s, name_of=lambda r: r.name, on_each=_each)
        for ref, e in failed:
            err += 1
            failed_names.append(ref.name)
            log.warning("FAIL %s: %s", ref.name, e)
            if on_granule is not None:
                on_granule(ref.name, 0, e)
        if err == 0 and ok == len(todo):
            index_mod.write_build_manifest(d, bbox, res, window, len(names), cells=fine)   # all landed: claim it
        else:
            log.warning("NOT claiming coverage: %d of %d granules did not index. Re-run to finish; the claim is "
                        "only stamped once the ground behind it is complete.", len(todo) - ok, len(todo))
    # The rollup belongs to whoever wrote the index. Doing it here means coverage queries and index_status read one
    # small manifest instead of every granule parquet, and no user request is billed for the re-read.
    rollup = coverage.build_manifest(col.key)
    return {**plan, "ok": ok, "err": err, "rows": rows,
            "claimed": bool(names) and (not todo or (err == 0 and ok == len(todo))),
            "seconds": round(time.time() - t0, 1), "rollup": rollup, "failed": failed_names}


def cli_main(col: BuildCollection, argv: list[str]) -> int:
    """The shared script body: `W S E N [res] [workers]`, INFO logging, progress every 25 granules.

    Exit status is 0 unless a granule failed or the build aborted; an empty search exits 0 (it is recorded, not
    claimed). The claim being withheld because of failures is the error case, and a re-run resumes."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("fsspec", "urllib3", "earthaccess", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if len(argv) < 4:
        print(f"usage: build_{col.key.lower()}_index.py W S E N [res={col.default_res}] [workers={col.default_workers}]",
              file=sys.stderr)
        return 2
    bbox = [float(x) for x in argv[:4]]
    res = int(argv[4]) if len(argv) > 4 else col.default_res
    workers = int(argv[5]) if len(argv) > 5 else col.default_workers
    state = {"n": 0, "todo": 0, "ok": 0, "err": 0, "rows": 0, "t0": time.time()}

    def on_plan(p):
        state["todo"] = p["todo"]
        if p["new_ground_cells"]:
            log.info("new ground: %d of %d coverage cells are not yet claimed", p["new_ground_cells"], p["claim_cells"])

    def on_granule(name, nrows, e):
        state["n"] += 1
        state["err" if e else "ok"] += 1
        state["rows"] += nrows
        if e:
            log.warning("FAIL %s: %s", name, e)
        i, todo = state["n"], state["todo"]
        if i % 25 == 0 or i == todo:
            el = time.time() - state["t0"]
            rate = i / el if el else 0
            log.info("%d/%d (%d ok, %d err, %d rows) | %.2f gran/s | elapsed %.1fm | ETA %.1fm",
                     i, todo, state["ok"], state["err"], state["rows"], rate, el / 60,
                     (todo - i) / rate / 60 if rate else 0)

    out = run_build(col, bbox, res=res, workers=workers, on_plan=on_plan, on_granule=on_granule)
    if out.get("aborted"):
        return 1
    log.info("DONE %s res %d: %d ok, %d err, %d rows in %.1fm -> %s",
             col.key, res, out["ok"], out["err"], out["rows"], out["seconds"] / 60, out["index_dir"])
    log.info("coverage rollup: %s", out["rollup"])
    if not out["claimed"] and out["granules"]:
        log.warning("coverage NOT claimed; re-run this command to finish")
    return 1 if out["err"] else 0
