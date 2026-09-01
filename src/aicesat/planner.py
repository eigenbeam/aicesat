"""Query planner (spec §4): request -> cells -> chunk refs -> fetch what is missing -> materialize -> hand off to the lake.

Skip logic is exact, not optimistic: the index knows every chunk that touches a cell, and the coverage table records
every chunk already materialized, so "missing" = set difference. A `force` flag re-fetches anyway.
"""
from __future__ import annotations

import logging
import math
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
    (gname, beam), rs, fetch_url, purl, threads = item
    reader = RangeReader(threads=threads)
    if purl:  # out-of-region: seed the presigned HTTPS URL so the worker does no EDL round trip
        reader._presigned[fetch_url] = (purl, time.time())
    ranges, keys = [], []
    for r in rs:
        for d in index.DATASETS:
            ranges.append((r[f"{d}_offset"], r[f"{d}_size"])); keys.append((d, r["chunk_index"]))
    t0 = time.time()
    raws = dict(zip(keys, reader.fetch(fetch_url, ranges)))
    t_fetch = time.time() - t0
    t1 = time.time()
    ph = _materialize(_decode_photons(rs, raws, rs[0]["sdp_epoch"]))
    written = lake.write_photons("ICESAT2", gname, beam, ph)
    chunk_cells = {r["chunk_index"]: sorted({int(c) for c in np.unique(ph["h3_cell"][ph["chunk_index"] == r["chunk_index"]])}) for r in rs}
    st = reader.stats.as_dict()
    st.update({"fetch_seconds": t_fetch, "materialize_seconds": time.time() - t1, "n_photons": int(ph["lon"].size), "cells_written": written})
    return {"granule": gname, "beam": beam, "chunk_cells": chunk_cells, "stats": st, "n_chunks": len(rs)}


def ensure(bbox, window, force: bool = False, threads: int = 8, polygon=None, group_parallel: int = 4) -> dict:
    """Make the lake sufficient for (bbox|polygon, window): index missing granules, fetch missing chunks, materialize."""
    cells = cells_for_bbox(bbox, polygon=polygon)
    return _ensure(cells, bbox, window, force, threads, group_parallel, prune_bbox=bbox,
                   fine_cells=coverage_cells(bbox, polygon))


def ensure_cells(cells, window, force: bool = False, threads: int = 8, group_parallel: int = 4) -> dict:
    """Materialize a set of H3 cells (background loading from the Lake tab): search by the cells' union bbox, prune
    chunks by that bbox, keep only refs for the requested cells."""
    cells = sorted(int(c) for c in cells)
    bbox = cells_bbox(cells)
    return _ensure(cells, bbox, window, force, threads, group_parallel, prune_bbox=bbox,
                   fine_cells=coverage_cells(bbox))


def _in_window(name: str, window) -> bool:
    """Granule inside the time window, decided from its NAME (ATL03_YYYYMMDD...). The index carries the granule name,
    so the window no longer needs a CMR search to resolve."""
    if not window:
        return True
    try:
        start = index.parse_granule_name(name)["start"][:8]
    except ValueError:                      # an unparseable name is KEPT: dropping data silently is the bug class
        log.warning("granule %s: unparseable name, not window-filtered", name)
        return True
    lo, hi = (str(w).replace("-", "")[:8] for w in window)
    return lo <= start <= hi


CLAIM_MAX_CELLS = 60_000   # keep a claim (and the polyfill that tests it) bounded, whatever the area's size


def claim_res(bbox, polygon=None) -> int:
    """The finest claim resolution whose cell count stays under CLAIM_MAX_CELLS for this area.

    Fixed at res 9 a 10x5 deg selection is 1.96M cells and 2.2 s to polyfill — paid per collection on every area
    edit in the Explore panel. Estimated from the area (cheap and deterministic) rather than by polyfilling and
    retrying, so the expensive case is never computed at all. A big region degrades to a coarser claim; even res 7
    (1.4 km) is far finer than the res-5 addressing cell whose 10 km overhang started this."""
    from . import index as atl03_index

    w, s, e, n = bbox
    km2 = abs(e - w) * 111.32 * math.cos(math.radians((s + n) / 2)) * abs(n - s) * 110.57
    for r in range(atl03_index.COVERAGE_RES, 0, -1):
        if km2 / h3.average_hexagon_area(r, unit="km^2") <= CLAIM_MAX_CELLS:
            return r
    return 1


def coverage_cells(bbox, polygon=None, res: int | None = None) -> list:
    """The cells a selection covers — the ground a build CLAIMS, and the shape it searches CMR over."""
    return cells_for_bbox(bbox, res=res if res is not None else claim_res(bbox, polygon), polygon=polygon)


def addressing_cells(fine_cells, res: int) -> list:
    """The index/lake partition cells covering a claim's ground, at `res`.

    Usually the claim is FINER than the addressing grid and each claim cell maps to its parent. But claim_res backs
    off with area, so a large enough region claims coarser than something addresses: Greenland claims at res 5 while
    ATL03 addresses at res 6. Asking for a res-6 parent of a res-5 cell is not a thing, and this raised
    H3ResMismatchError on any build big enough to trigger the back-off. Going DOWN means taking the children.

    A cell produced here may be only partly claimed (its far side was never searched). That is fine and it is the
    point: we index what the search found, and claim only the fine ground we actually covered."""
    out: set = set()
    for c in fine_cells:
        s = h3.int_to_str(int(c))
        r = h3.get_resolution(s)
        if r == res:
            out.add(s)
        elif r > res:
            out.add(h3.cell_to_parent(s, res))
        else:
            out.update(h3.cell_to_children(s, res))
    return sorted(h3.str_to_int(x) for x in out)


def _convex_hull(points: list) -> list:
    """Minimum-area convex polygon covering `points`, counter-clockwise (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for q in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper = []
    for q in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


MAX_SEARCH_EDGE_DEG = 0.25   # densify to this, so a great-circle edge bows < ~5 m (see search_polygon)
MAX_SEARCH_VERTICES = 200    # CMR takes the polygon in the query string; a big region blows the URI limit


def search_polygon(cells, max_edge_deg: float = MAX_SEARCH_EDGE_DEG) -> list:
    """A closed CCW [(lon, lat), ...] ring to search CMR with: the convex hull of `cells`, densified.

    Convex, so it is always ONE simple ring with no holes even when the cells are disjoint — the exact union outline
    can be multi-ring, and CMR would need a query per ring. It costs a little over-search (measured: 688 granules vs
    595 for the exact outline over one corridor) and saves the complexity.

    Densified because CMR's polygon edges are GREAT-CIRCLE arcs, not straight lines in lon/lat. At 69N an arc between
    two points at equal latitude bows poleward — INTO the polygon on a southern edge — by ~230 m across a 1.7 deg
    span, which silently excluded 3 of 688 granules when measured. The bow grows with the square of the edge, so
    capping each segment at 0.25 deg brings it under ~5 m. It cannot be avoided by using finer cells: a convex hull's
    long sides span the SELECTION, and refining res 4 -> 7 left the longest edge at 1.4-2.2 deg throughout.
    """
    verts = [(lo, la) for c in cells for la, lo in h3.cell_to_boundary(h3.int_to_str(int(c)))]
    hull = _convex_hull(verts)
    if len(hull) < 3:
        return []
    ring = hull + [hull[0]]
    out = []
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        n = max(1, int(math.ceil(max(abs(x2 - x1), abs(y2 - y1)) / max_edge_deg)))
        for k in range(n):
            out.append((round(x1 + (x2 - x1) * k / n, 5), round(y1 + (y2 - y1) * k / n, 5)))
    out.append(out[0])
    if len(out) > MAX_SEARCH_VERTICES:
        # CMR carries the polygon in the query string. A Greenland-sized hull densified to 0.25 deg is 725 vertices
        # and the request comes back 414 Request-URI Too Large. Coarsening the densification instead would let the
        # great-circle bow eat into the region, and a bow EXCLUDES ground — it could drop granules silently. So give
        # up on the polygon and let the caller search the BOUNDING BOX, which is a strict superset: more granules to
        # parse, never fewer. The polygon is an optimisation; correctness is not negotiable for it.
        log.info("search polygon needs %d vertices (> %d); falling back to a bbox search", len(out), MAX_SEARCH_VERTICES)
        return []
    return out


def cells_bbox(cells) -> tuple:
    """Bounding box of a cell set's OUTER boundary — always >= the bbox the cells were derived from.

    This is the box to search CMR over when building an index for those cells. A hex that intersects the requested
    rectangle also sticks out past it, and a granule can cross that outside portion without ever entering the
    rectangle; searching the rectangle would miss it and leave the boundary cell short of granules while still
    reporting it as built."""
    bs = [h3.cell_to_boundary(h3.int_to_str(int(c))) for c in cells]
    las = [la for b in bs for la, _ in b]; los = [lo for b in bs for _, lo in b]
    return (min(los), min(las), max(los), max(las))


def _ensure(cells, bbox, window, force, threads, group_parallel, prune_bbox, fine_cells=None) -> dict:
    t0 = time.time()
    # The index IS the discovery layer, and it is a precondition: no CMR search and no index build happen here.
    # Discovery is paid once, offline (scripts/build_index.py), and a scene is assembled from the index entries whose
    # H3 cells match the area. An unindexed area is an error, not a slow success via a whole-granule fallback.
    fine = list(fine_cells if fine_cells is not None else coverage_cells(bbox))
    if not index.covers_cells(index.ATL03_INDEX_DIR, fine):
        raise RuntimeError(f"ATL03 not indexed over all {len(fine)} res-{index.COVERAGE_RES} cells this area covers — "
                           f"build the chunk index first "
                           f"(uv run scripts/build_index.py --bbox {' '.join(str(v) for v in bbox)})")
    refs = index.chunk_refs(cells, bbox=prune_bbox, per_cell=True)  # per-chunk boxes prune what the coarse cells let through
    all_rows = refs.to_pylist()
    names_indexed = {r["granule"] for r in all_rows}
    names = sorted(n for n in names_indexed if _in_window(n, window))
    in_window = set(names)
    ref_rows = [r for r in all_rows if r["granule"] in in_window]
    if not ref_rows:
        raise RuntimeError(f"no indexed ATL03 chunks over {bbox} in {list(window) if window else 'any window'} "
                           f"({len(names_indexed)} granules indexed for these cells, none inside the window)")
    # Same granule set, WITHOUT the per-chunk box prune, so chunks_pruned_by_boxes measures the box prune alone
    # rather than the box prune plus the time window.
    refs_cells = index.chunk_refs(cells, granules=names)
    have = set() if force else lake.ingested_chunk_cells("ICESAT2", names)
    # cell-aware: a chunk is fetched if ANY requested cell it touches is not materialized (partial evictions re-fetch)
    todo_keys = {(r["granule"], r["beam"], r["chunk_index"]) for r in ref_rows
                 if (r["granule"], r["beam"], r["chunk_index"], int(r["h3_cell"])) not in have}
    seen = set(); rows = []
    for r in ref_rows:
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
    from .access import access_url, in_region
    reader = RangeReader(threads=threads)
    t_p0 = time.time()
    if in_region():   # S3-direct: workers fetch the s3:// URL with STS creds — no presign round trips
        items = [(gb, rs, access_url(rs[0]["url"], rs[0].get("s3url")), None, threads) for gb, rs in by_gb.items()]
    else:
        presigned = reader.presign_all(sorted({rs[0]["url"] for rs in by_gb.values()})) if by_gb else {}
        items = [(gb, rs, rs[0]["url"], presigned[rs[0]["url"]], threads) for gb, rs in by_gb.items()]
    t_presign = time.time() - t_p0
    n_granules = len({rs[0]["url"] for rs in by_gb.values()})
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
    # one meta.duckdb transaction for the whole batch (per-item opens dominated cold builds — see mark_ingested_many)
    lake.mark_ingested_many("ICESAT2", [(r["granule"], r["beam"], r["chunk_cells"]) for r in results])
    for res in results:
        n_written_cells += len(res["stats"]["cells_written"])
        for k in ("requests", "bytes", "chunks", "spans", "gap_bytes", "seconds", "fetch_seconds", "materialize_seconds"):
            agg[k] += res["stats"].get(k, 0) or 0
        log.info("%s %s: %d chunks -> %d photons -> %d cell files (fetch %.1fs, materialize %.1fs)", res["granule"], res["beam"],
                 res["n_chunks"], res["stats"]["n_photons"], len(res["stats"]["cells_written"]), res["stats"]["fetch_seconds"], res["stats"]["materialize_seconds"])
    evicted = lake.enforce_limit(protect=cells) if results else []
    st = {"requests": agg["requests"], "bytes": agg["bytes"], "seconds": round(agg["seconds"], 2), "chunks": agg["chunks"], "spans": agg["spans"],
          "gap_bytes": agg["gap_bytes"], "presigns": agg["presigns"], "granules_touched": n_granules,
          "hdf5_opens_at_query_time": 0, "structure_parses_at_query_time": 0}
    st.update({"cells": len(cells), "granules": names, "granules_indexed_for_cells": len(names_indexed),
               "chunk_refs": len(rows), "chunks_fetched": len(todo),
               "chunk_refs_by_cells_only": refs_cells.num_rows, "chunks_pruned_by_boxes": refs_cells.num_rows - len(rows),
               "presign_seconds": round(t_presign, 1), "group_phase_seconds": round(t_groups, 1), "group_parallel": group_parallel,
               "fetch_seconds": round(agg["fetch_seconds"], 1), "decode_materialize_seconds": round(agg["materialize_seconds"], 1),
               "chunks_skipped_already_materialized": len(rows) - len(todo), "cell_files_written": n_written_cells,
               "evicted_for_limit": evicted,
               "wall_seconds": round(time.time() - t0, 1), "h3_res": index.H3_RES})
    return {"cells": cells, "granules": names, "stats": st}
