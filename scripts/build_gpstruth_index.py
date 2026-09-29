"""Build the IS2TGPSSS (Summit GPS traverse) line-offset index over a bbox — resumable and parallel, like
build_icessn_index.py.

The traverse metadata file is read ONCE, here, and every survey's sled geometry is resolved from it and stored on that
survey's index rows (a fetch never sees the metadata file). The collection's RINEX files are never read.

Usage:  uv run python scripts/build_gpstruth_index.py W S E N [res] [workers]
Example (the transect):  uv run python scripts/build_gpstruth_index.py -38.585667 72.578616 -38.474193 72.648222 5 8
"""
import concurrent.futures as cf
import functools
import hashlib
import logging
import statistics
import sys
import time

from aicesat import auth, coverage, gpstruth, index, index_gpstruth


PER_GRANULE_TIMEOUT_S = 240   # a survey that cannot be read in 4 min is a stall, not slow I/O


def _index_one(granule, res, cells, geometry, metadata_sha1):
    try:
        name = coverage.granule_name(granule)
        t = index_gpstruth.build_gpstruth_index(granule, geometry[name], metadata_sha1, res=res, cells=cells)
        return (name, t.num_rows, None)
    except Exception as e:
        try:
            name = coverage.granule_name(granule)
        except Exception:
            name = "?"
        return (name, 0, f"{type(e).__name__}: {e}")


def resolve_geometry(csv_names, key2row, log) -> dict:
    """{csv name: (arp_to_sled_m, track_depth_cm, track_depth_known, rinex_key)}, and a report of what was imputed.

    FALLBACK_TRACK_DEPTH_CM is a fixed constant described in gpstruth.py as the median of the known sinkages; this
    logs the median the metadata file actually gives, so the two can be compared. It changes nothing."""
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


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("build_gpstruth_index")
    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__); sys.exit(2)
    bbox = [float(x) for x in a[:4]]
    res = int(a[4]) if len(a) > 4 else index_gpstruth.GPSTRUTH_RES
    workers = int(a[5]) if len(a) > 5 else 8

    from aicesat import planner
    from aicesat.access import RangeReader, access_url
    fine = planner.coverage_cells(bbox)                  # the ground this build claims, at the claim resolution
    ring = planner.search_polygon(fine)                  # densified convex hull of that ground -> the CMR shape
    cells = planner.addressing_cells(fine, res)          # coarse partition keys the rows are filtered to
    auth.login()
    log.info("enumerating IS2TGPSSS granules over %s (full record) ...", bbox)
    granules = coverage.search(coverage.GPSTRUTH_SHORT_NAME, coverage.GPSTRUTH_VERSION, bbox, None, polygon=ring)
    meta_g, csv_g = gpstruth.split_granules(granules)
    log.info("%d granules: %d survey CSVs, %s metadata file, %d RINEX files skipped",
             len(granules), len(csv_g), "1" if meta_g else "NO", len(granules) - len(csv_g) - (1 if meta_g else 0))
    md = index_gpstruth._index_dir(res); md.mkdir(parents=True, exist_ok=True)
    if not csv_g:
        index.write_build_manifest(md, bbox, res, None, 0, cells=fine)   # records the empty search; claims nothing
        return
    if meta_g is None:
        # Without it every survey would take the default sled geometry: a silent 1-4 cm error in every height.
        raise SystemExit("the IS2TGPSSS traverse-metadata granule is missing from the search result; not building")

    meta_bytes = RangeReader().read_all(access_url(meta_g.data_links()[0], (meta_g.data_links(access="direct") or [""])[0]))
    metadata_sha1 = hashlib.sha1(meta_bytes).hexdigest()[:16]
    key2row = gpstruth.traverse_metadata_from_bytes(meta_bytes)
    names = {coverage.granule_name(g): g for g in csv_g}
    geometry = resolve_geometry(sorted(names), key2row, log)

    done = index_gpstruth.indexed_gpstruth_granules(res, metadata_sha1=metadata_sha1)
    needed = index.unclaimed_cells(md, fine)
    todo = [g for n, g in names.items()
            if n not in done or not index.granule_proves(md / f"{n}.parquet", needed)]
    log.info("res %d: %d surveys found, %d already indexed, %d to build (workers=%d, metadata %s)",
             res, len(names), len(done & set(names)), len(todo), workers, metadata_sha1)
    if not todo:
        index.write_build_manifest(md, bbox, res, None, len(names), cells=fine)
        log.info("nothing to do — index complete")
        log.info("coverage rollup: %s", coverage.build_manifest("GPSTRUTH"))
        return

    t0 = time.time(); ok = err = rows = 0
    budget = PER_GRANULE_TIMEOUT_S * (len(todo) / max(1, workers) + 2)
    work = functools.partial(_index_one, res=res, cells=cells, geometry=geometry, metadata_sha1=metadata_sha1)
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        try:
            for i, (name, nrows, e) in enumerate(ex.map(work, todo, chunksize=1, timeout=budget), 1):
                if e:
                    err += 1; log.warning("FAIL %s: %s", name, e)
                else:
                    ok += 1; rows += nrows
                if i % 25 == 0 or i == len(todo):
                    el = time.time() - t0; rate = i / el if el else 0
                    log.info("%d/%d (%d ok, %d err, %d rows) | %.2f gran/s | elapsed %.1fm",
                             i, len(todo), ok, err, rows, rate, el / 60)
        except cf.TimeoutError:
            log.error("TIMED OUT after %.1f min with %d/%d surveys done — re-run to resume", budget / 60, ok + err, len(todo))
    if err == 0 and ok == len(todo):
        index.write_build_manifest(md, bbox, res, None, len(names), cells=fine)   # every survey landed: claim it
    else:
        log.warning("NOT claiming coverage: %d of %d surveys did not index. Re-run to finish.", len(todo) - ok, len(todo))
    log.info("DONE res %d: %d ok, %d err, %d rows in %.1fm -> %s", res, ok, err, rows, (time.time() - t0) / 60, md)
    log.info("coverage rollup: %s", coverage.build_manifest("GPSTRUTH"))


if __name__ == "__main__":
    main()
