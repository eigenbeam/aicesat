"""Query planner (spec §4): request -> cells -> chunk refs -> fetch what is missing -> materialize -> hand off to the lake.

Skip logic is exact, not optimistic: the index knows every chunk that touches a cell, and the coverage table records
every chunk already materialized, so "missing" = set difference. A `force` flag re-fetches anyway.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import h3
import numpy as np

from . import coreg, coverage, index, lake
from .access import RangeReader, decode_chunk

log = logging.getLogger(__name__)
GPS_EPOCH_MS = np.datetime64(datetime(1980, 1, 6) - timedelta(seconds=18), "ms")  # GPS->UTC, 18 leap s (2017+)
LAND_ICE_COL = 3


def cells_for_bbox(bbox, res: int = index.H3_RES, dilate: int = 1, polygon=None) -> list[int]:
    """Every cell that overlaps the area (h3 'overlap' containment when available, else centre-containment + k-ring)."""
    w, s, e, n = bbox
    poly = h3.LatLngPoly([(la, lo) for lo, la in polygon]) if polygon else h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
    try:
        cells = set(h3.h3shape_to_cells_experimental(poly, res, contain="overlap"))
    except Exception:
        cells = set(h3.h3shape_to_cells(poly, res))
        for _ in range(dilate):
            cells = {c for c0 in list(cells) for c in h3.grid_disk(c0, 1)}
    return sorted(h3.str_to_int(c) for c in cells)


def _decode_photons(refs_rows: list[dict], raws: dict[tuple[str, int], bytes], sdp_epoch: float) -> dict:
    """Decode one beam's fetched chunks into photon arrays (all photons of each chunk; partitioned by cell later)."""
    parts = {k: [] for k in ("lon", "lat", "h", "conf", "delta_time", "photon_index", "chunk_index")}
    for r in refs_rows:
        k = r["chunk_index"]
        dec = lambda d, ncols=1: decode_chunk(raws[(d, k)], r[f"{d}_dtype"], r[f"{d}_filters"], ncols, r[f"{d}_mask"])
        lat, lon, hp, dt = dec("lat_ph"), dec("lon_ph"), dec("h_ph"), dec("delta_time")
        conf = dec("signal_conf_ph", r["signal_conf_ph_ncols"])[:, LAND_ICE_COL]
        n = r["ph_end"] - r["ph_start"]  # last chunk of a dataset is padded to the full chunk size
        parts["lon"].append(lon[:n].astype("f8")); parts["lat"].append(lat[:n].astype("f8")); parts["h"].append(hp[:n].astype("f8"))
        parts["conf"].append(conf[:n].astype("i1")); parts["delta_time"].append(dt[:n].astype("f8"))
        parts["photon_index"].append(np.arange(r["ph_start"], r["ph_end"], dtype="i8")); parts["chunk_index"].append(np.full(n, k, dtype="i4"))
    out = {k: np.concatenate(v) for k, v in parts.items()}
    out["t"] = GPS_EPOCH_MS + ((out["delta_time"] + sdp_epoch) * 1000).astype("timedelta64[ms]")
    return out


def _cells_vectorized(lat: np.ndarray, lon: np.ndarray, res: int) -> np.ndarray:
    try:
        from h3ronpy.vector import coordinates_to_cells
        return np.asarray(coordinates_to_cells(lat, lon, res), dtype="u8")
    except Exception:  # h3ronpy unavailable: exact scalar fallback
        return np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), res)) for la, lo in zip(lat, lon)], dtype="u8")


def _materialize(out: dict) -> dict:
    """Assign each photon its own H3 cell and materialize coreg coordinates at the common epoch (§7.4)."""
    good_ll = np.isfinite(out["lat"]) & np.isfinite(out["lon"])
    out["h3_cell"] = np.zeros(out["lat"].size, dtype="u8")
    out["h3_cell"][good_ll] = _cells_vectorized(out["lat"][good_ll], out["lon"][good_ll], index.H3_RES)
    good = good_ll & np.isfinite(out["h"])
    out["coreg_lon"], out["coreg_lat"] = np.full_like(out["lon"], np.nan), np.full_like(out["lat"], np.nan)
    if good.any():
        ty = coreg.decimal_year(out["t"][good])
        clon, clat, _ = coreg.propagate(out["lon"][good], out["lat"][good], out["h"][good], ty, lake.COMMON_EPOCH, "ITRF2014")
        out["coreg_lon"][good], out["coreg_lat"][good] = clon, clat
    return out


def _process_group(item) -> dict:
    """Worker: fetch one (granule, beam) group's chunks (presigned URL supplied), decode, materialize, write cell files.
    Returns access stats + the chunk->cells map; the coverage table is updated by the parent (single DuckDB writer)."""
    (gname, beam), rs, purl, threads = item
    reader = RangeReader(threads=threads)
    reader._presigned[rs[0]["url"]] = (purl, time.time())  # no EDL round trip in the worker
    ranges, keys = [], []
    for r in rs:
        for d in index.DATASETS:
            ranges.append((r[f"{d}_offset"], r[f"{d}_size"])); keys.append((d, r["chunk_index"]))
    t0 = time.time()
    raws = dict(zip(keys, reader.fetch(rs[0]["url"], ranges)))
    t_fetch = time.time() - t0
    t1 = time.time()
    ph = _materialize(_decode_photons(rs, raws, rs[0]["sdp_epoch"]))
    written = lake.write_photons("ICESAT2", gname, beam, ph)
    chunk_cells = {r["chunk_index"]: sorted({int(c) for c in np.unique(ph["h3_cell"][ph["chunk_index"] == r["chunk_index"]])}) for r in rs}
    st = reader.stats.as_dict()
    st.update({"fetch_seconds": t_fetch, "materialize_seconds": time.time() - t1, "n_photons": int(ph["lon"].size), "cells_written": written})
    return {"granule": gname, "beam": beam, "chunk_cells": chunk_cells, "stats": st, "n_chunks": len(rs)}


def ensure(bbox, window, max_granules: int = 8, force: bool = False, threads: int = 8, polygon=None, group_parallel: int = 4) -> dict:
    """Make the lake sufficient for (bbox|polygon, window): index missing granules, fetch missing chunks, materialize."""
    cells = cells_for_bbox(bbox, polygon=polygon)
    return _ensure(cells, bbox, window, max_granules, force, threads, group_parallel, prune_bbox=bbox)


def ensure_cells(cells, window, max_granules: int = 40, force: bool = False, threads: int = 8, group_parallel: int = 4) -> dict:
    """Materialize a set of H3 cells (background loading from the Lake tab): search by the cells' union bbox, prune
    chunks by that bbox, keep only refs for the requested cells."""
    cells = sorted(int(c) for c in cells)
    boundaries = [h3.cell_to_boundary(h3.int_to_str(c)) for c in cells]
    lats = [la for b in boundaries for la, _ in b]; lons = [lo for b in boundaries for _, lo in b]
    bbox = (min(lons), min(lats), max(lons), max(lats))
    return _ensure(cells, bbox, window, max_granules, force, threads, group_parallel, prune_bbox=bbox)


def _ensure(cells, bbox, window, max_granules, force, threads, group_parallel, prune_bbox) -> dict:
    t0 = time.time()
    granules = coverage.search(coverage.ATL03_SHORT_NAME, coverage.ATL03_VERSION, bbox, window)[:max_granules]
    names = [g["meta"]["native-id"] for g in granules]
    idx = index.ensure_index(granules)
    refs_cells = index.chunk_refs(cells, granules=names)
    refs = index.chunk_refs(cells, granules=names, bbox=prune_bbox, per_cell=True)  # per-chunk boxes prune what the coarse cells let through
    have = set() if force else lake.ingested_chunk_cells("ICESAT2", names)
    # cell-aware: a chunk is fetched if ANY requested cell it touches is not materialized (partial evictions re-fetch)
    todo_keys = {(r["granule"], r["beam"], r["chunk_index"]) for r in refs.to_pylist()
                 if (r["granule"], r["beam"], r["chunk_index"], int(r["h3_cell"])) not in have}
    seen = set(); rows = []
    for r in refs.to_pylist():
        k = (r["granule"], r["beam"], r["chunk_index"])
        if k not in seen:
            seen.add(k); rows.append({kk: v for kk, v in r.items() if kk != "h3_cell"})
    todo = [r for r in rows if (r["granule"], r["beam"], r["chunk_index"]) in todo_keys]
    by_gb: dict[tuple[str, str], list[dict]] = {}
    for r in todo:
        by_gb.setdefault((r["granule"], r["beam"]), []).append(r)
    # Presign every touched granule concurrently in the parent (1-2 s each from outside the region), then hand
    # (group, presigned URL) to worker PROCESSES: decode + cell assignment + co-registration + Parquet writes are CPU
    # and the parent's GIL would serialize them. Coverage marks happen here (DuckDB is single-writer).
    reader = RangeReader(threads=threads)
    t_p0 = time.time()
    presigned = reader.presign_all(sorted({rs[0]["url"] for rs in by_gb.values()})) if by_gb else {}
    t_presign = time.time() - t_p0
    items = [(gb, rs, presigned[rs[0]["url"]], threads) for gb, rs in by_gb.items()]
    from concurrent.futures import ProcessPoolExecutor
    t_f0 = time.time()
    results = []
    if items:
        with ProcessPoolExecutor(max_workers=min(group_parallel, len(items))) as ex:
            results = list(ex.map(_process_group, items))
    t_groups = time.time() - t_f0
    n_written_cells = 0
    agg = {"requests": reader.stats.requests, "bytes": 0, "chunks": 0, "spans": 0, "gap_bytes": 0, "presigns": reader.stats.presigns,
           "seconds": 0.0, "fetch_seconds": 0.0, "materialize_seconds": 0.0}
    for res in results:
        lake.mark_ingested("ICESAT2", res["granule"], res["beam"], res["chunk_cells"])
        n_written_cells += len(res["stats"]["cells_written"])
        for k in ("requests", "bytes", "chunks", "spans", "gap_bytes", "seconds", "fetch_seconds", "materialize_seconds"):
            agg[k] += res["stats"].get(k, 0) or 0
        log.info("%s %s: %d chunks -> %d photons -> %d cell files (fetch %.1fs, materialize %.1fs)", res["granule"], res["beam"],
                 res["n_chunks"], res["stats"]["n_photons"], len(res["stats"]["cells_written"]), res["stats"]["fetch_seconds"], res["stats"]["materialize_seconds"])
    evicted = lake.enforce_limit(protect=cells) if results else []
    st = {"requests": agg["requests"], "bytes": agg["bytes"], "seconds": round(agg["seconds"], 2), "chunks": agg["chunks"], "spans": agg["spans"],
          "gap_bytes": agg["gap_bytes"], "presigns": agg["presigns"], "granules_touched": len(presigned),
          "hdf5_opens_at_query_time": 0, "structure_parses_at_query_time": 0}
    st.update({"cells": len(cells), "granules": names, "index": idx, "chunk_refs": len(rows), "chunks_fetched": len(todo),
               "chunk_refs_by_cells_only": refs_cells.num_rows, "chunks_pruned_by_boxes": refs_cells.num_rows - len(rows),
               "presign_seconds": round(t_presign, 1), "group_phase_seconds": round(t_groups, 1), "group_parallel": group_parallel,
               "fetch_seconds": round(agg["fetch_seconds"], 1), "decode_materialize_seconds": round(agg["materialize_seconds"], 1),
               "chunks_skipped_already_materialized": len(rows) - len(todo), "cell_files_written": n_written_cells,
               "evicted_for_limit": evicted,
               "wall_seconds": round(time.time() - t0, 1), "h3_res": index.H3_RES})
    return {"cells": cells, "granules": names, "stats": st}
