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


def _materialize(out: dict) -> dict:
    """Assign each photon its own H3 cell and materialize coreg coordinates at the common epoch (§7.4)."""
    out["h3_cell"] = np.array([h3.str_to_int(h3.latlng_to_cell(float(la), float(lo), index.H3_RES)) for la, lo in zip(out["lat"], out["lon"])], dtype="u8")
    good = np.isfinite(out["lat"]) & np.isfinite(out["lon"]) & np.isfinite(out["h"])
    out["coreg_lon"], out["coreg_lat"] = np.full_like(out["lon"], np.nan), np.full_like(out["lat"], np.nan)
    if good.any():
        ty = coreg.decimal_year(out["t"][good])
        clon, clat, _ = coreg.propagate(out["lon"][good], out["lat"][good], out["h"][good], ty, lake.COMMON_EPOCH, "ITRF2014")
        out["coreg_lon"][good], out["coreg_lat"][good] = clon, clat
    return out


def ensure(bbox, window, max_granules: int = 8, force: bool = False, threads: int = 8, polygon=None, group_parallel: int = 4) -> dict:
    """Make the lake sufficient for (bbox|polygon, window): index missing granules, fetch missing chunks, materialize."""
    t0 = time.time()
    cells = cells_for_bbox(bbox, polygon=polygon)
    granules = coverage.search(coverage.ATL03_SHORT_NAME, coverage.ATL03_VERSION, bbox, window)[:max_granules]
    names = [g["meta"]["native-id"] for g in granules]
    idx = index.ensure_index(granules)
    refs_cells = index.chunk_refs(cells, granules=names)
    refs = index.chunk_refs(cells, granules=names, bbox=bbox)  # per-chunk boxes prune what the coarse cells let through
    rows = refs.to_pylist()
    have = set() if force else lake.ingested_chunks("ICESAT2", names)
    todo = [r for r in rows if (r["granule"], r["beam"], r["chunk_index"]) not in have]
    reader = RangeReader(threads=threads)
    by_gb: dict[tuple[str, str], list[dict]] = {}
    for r in todo:
        by_gb.setdefault((r["granule"], r["beam"]), []).append(r)
    n_written_cells = 0

    def fetch_group(item):
        (gname, beam), rs = item
        ranges, keys = [], []
        for r in rs:
            for d in index.DATASETS:
                ranges.append((r[f"{d}_offset"], r[f"{d}_size"])); keys.append((d, r["chunk_index"]))
        return (gname, beam), rs, dict(zip(keys, reader.fetch(rs[0]["url"], ranges)))

    # Fetch several (granule, beam) groups at once: after coalescing a group has only a handful of spans, so
    # per-group parallelism alone would leave most connections idle. Decode/materialize stays sequential (CPU).
    from concurrent.futures import ThreadPoolExecutor
    t_f0 = time.time()
    with ThreadPoolExecutor(group_parallel) as ex:
        fetched = list(ex.map(fetch_group, by_gb.items()))
    t_fetch = time.time() - t_f0
    t_m0 = time.time()
    for (gname, beam), rs, raws in fetched:
        ph = _materialize(_decode_photons(rs, raws, rs[0]["sdp_epoch"]))
        written = lake.write_photons("ICESAT2", gname, beam, ph)
        n_written_cells += len(written)
        chunk_cells = {r["chunk_index"]: sorted({int(c) for c in np.unique(ph["h3_cell"][ph["chunk_index"] == r["chunk_index"]])}) for r in rs}
        lake.mark_ingested("ICESAT2", gname, beam, chunk_cells)
        log.info("%s %s: %d chunks -> %d photons -> %d cell files", gname, beam, len(rs), ph["lon"].size, len(written))
    st = reader.stats.as_dict()
    st.update({"cells": len(cells), "granules": names, "index": idx, "chunk_refs": len(rows), "chunks_fetched": len(todo),
               "chunk_refs_by_cells_only": refs_cells.num_rows, "chunks_pruned_by_boxes": refs_cells.num_rows - len(rows),
               "fetch_seconds": round(t_fetch, 1), "decode_materialize_seconds": round(time.time() - t_m0, 1),
               "chunks_skipped_already_materialized": len(rows) - len(todo), "cell_files_written": n_written_cells,
               "wall_seconds": round(time.time() - t0, 1), "h3_res": index.H3_RES})
    return {"cells": cells, "granules": names, "stats": st}
