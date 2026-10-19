"""How far granule-level catalogue search misses the data, measured per canonical area and collection.

CMR answers a spatial search with every granule whose FOOTPRINT touches the area; the sub-granule index knows which
granules have POINTS in the area's cells and which bytes hold them. Comparing the two over the demo regions gives the
failure-mode numbers the design document rests on: how many catalogue hits are empty for the box (false positives),
how many bytes a whole-granule retrieval moves against how many the box needs, how much bigger a footprint is than
the box, and how many chunks a granule carries (its along-track extent against a chunk's).

Read-only: CMR is read through coverage.search (24 h on-disk cache; --no-cache refreshes), the index through DuckDB.
Nothing is written except the --out file. Run on the deployed box, where the regional indexes live:

    uv run python scripts/measure_search_modes.py --out docs/notes/search-modes-2026-10.md
    uv run python scripts/measure_search_modes.py --areas summit jakobshavn_margin --collections ATL06 GLAS

Columns of the table (one row per area x collection):
  cmr_hits        granules CMR returns for the area's bbox over the collection's default window
  cmr_gb          their total archive size (CMR DataGranule size), i.e. what "download the granules" moves
  cmr_hull        granules CMR returns for the outline of the area's H3 cells (the same ground the index answers
                  for; a res-5 cell juts up to ~10 km past the box, so this is the like-for-like count)
  idx_cells       granules with at least one index row in the area's H3 cells (the index's answer)
  idx_box         granules with a chunk whose bbox intersects the area's rectangle (a chunk is ~200 km of track,
                  so this is coarse; it is here to show that the chunk bbox alone cannot answer the question)
  false_pos       cmr_hull hits that have no index row in the area's cells, as a fraction of cmr_hull
  missed          index granules the cells' CMR search did not return (footprint misses: stale or coarse polygons)
  need_mb         bytes the index would fetch for the area (every dataset range of every touched chunk)
  bytes_ratio     cmr_gb / need_mb, how many times more a whole-granule retrieval moves
  fp_area         median footprint area / area of the query rectangle (CMR polygon, or its bounding rectangle)
  fp_kind         how CMR locates the granule: polygon, rectangle, line, or orbit (GLAS carries orbit parameters
                  only, so the catalogue back-computes a footprint and fp_area cannot be measured)
  (areas the collection's index claim does not cover are skipped: there is nothing to compare the catalogue with)
  chunks_per_gran median over index granules of (max chunk index + 1): a LOWER bound on a granule's chunk count,
                  because a regional index only carries the chunks that touch the region
  gran_mb         quartiles of the CMR granule size (resolves what "an ATL06 granule is N MB" should say)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import statistics
import sys
from pathlib import Path

log = logging.getLogger("measure_search_modes")

EXTRA_AREAS = {
    # Langtang, Nepal: the HMA site, the only canonical area GEDI can reach (GEDI stops at 51.6 N)
    "langtang": {"bbox": (85.35, 28.20, 85.62, 28.38), "note": "High Mountain Asia; the GEDI/HMA site"},
}
DEFAULT_AREAS = ("summit", "egig_west_flank", "k_transect_upstream", "jakobshavn_margin", "summit_traverse", "langtang")
DEFAULT_COLLECTIONS = ("ATL06", "ATL03", "GLAS", "ICESSN", "GEDI")


def areas(names) -> dict[str, tuple[float, float, float, float]]:
    from aicesat import regions
    table = {**{k: v["bbox"] for k, v in regions.REGIONS.items()}, **{k: v["bbox"] for k, v in EXTRA_AREAS.items()}}
    missing = [n for n in names if n not in table]
    if missing:
        raise SystemExit(f"unknown area(s) {missing}; known: {sorted(table)}")
    return {n: tuple(float(v) for v in table[n]) for n in names}


# --- geometry -----------------------------------------------------------------------------------------------------
def _equal_area_km2(ring, centre) -> float:
    """Area in km^2 of a lon/lat ring, in a Lambert azimuthal equal-area frame centred on the query."""
    from pyproj import Proj
    from shapely.geometry import Polygon
    lon0, lat0 = centre
    p = Proj(proj="laea", lat_0=lat0, lon_0=lon0, datum="WGS84", units="m")
    xy = [p(lon, lat) for lon, lat in ring]
    return Polygon(xy).area / 1e6


def bbox_area_km2(bbox) -> float:
    w, s, e, n = bbox
    return _equal_area_km2([(w, s), (e, s), (e, n), (w, n)], ((w + e) / 2, (s + n) / 2))


def footprint_rings(granule) -> list[list[tuple[float, float]]]:
    """The granule's CMR geometry as lon/lat rings: GPolygons when present, else bounding rectangles."""
    try:
        geo = granule["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
    except (KeyError, TypeError):
        return []
    rings = []
    for gp in geo.get("GPolygons") or []:
        pts = gp.get("Boundary", {}).get("Points") or []
        ring = [(float(p["Longitude"]), float(p["Latitude"])) for p in pts]
        if len(ring) >= 3:
            rings.append(ring)
    for br in geo.get("BoundingRectangles") or []:
        w, s = float(br["WestBoundingCoordinate"]), float(br["SouthBoundingCoordinate"])
        e, n = float(br["EastBoundingCoordinate"]), float(br["NorthBoundingCoordinate"])
        rings.append([(w, s), (e, s), (e, n), (w, n)])
    return rings


def footprint_area_km2(granule, bbox) -> float | None:
    rings = footprint_rings(granule)
    if not rings:
        return None
    w, s, e, n = bbox
    centre = ((w + e) / 2, (s + n) / 2)
    return sum(_equal_area_km2(r, centre) for r in rings)


def granule_mb(granule) -> float | None:
    """Archive size in MB from the UMM record (earthaccess' size() does the same, with a deprecation warning)."""
    try:
        infos = granule["umm"]["DataGranule"]["ArchiveAndDistributionInformation"]
    except (KeyError, TypeError):
        return None
    total = 0.0
    for i in infos:
        if "SizeInBytes" in i:
            total += float(i["SizeInBytes"]) / 1e6
        elif "Size" in i:
            unit = str(i.get("SizeUnit", "MB")).upper()
            total += float(i["Size"]) * {"KB": 1e-3, "MB": 1.0, "GB": 1e3, "TB": 1e6, "NA": 1.0}.get(unit, 1.0)
    return total


# --- the two sides -------------------------------------------------------------------------------------------------
def cmr_side(coll: dict, bbox, window, use_cache: bool, polygon=None) -> dict:
    from aicesat import coverage
    granules = coverage.search(coll["short_name"], coll["version"], bbox, window, use_cache=use_cache, polygon=polygon)
    names = {coverage.granule_name(g) for g in granules}
    sizes = [m for m in (granule_mb(g) for g in granules) if m is not None]
    qa = bbox_area_km2(bbox)
    ratios = [a / qa for a in (footprint_area_km2(g, bbox) for g in granules) if a is not None and qa > 0]
    kinds = {footprint_kind(g) for g in granules}
    return {"names": names, "hits": len(granules), "gb": sum(sizes) / 1e3, "footprint_kind": "/".join(sorted(kinds)),
            "size_quartiles_mb": _quartiles(sizes), "fp_area_median": statistics.median(ratios) if ratios else None}


def footprint_kind(granule) -> str:
    """How CMR describes where the granule is: polygon, rectangle, line, orbit (GLAS: orbit parameters only), none."""
    try:
        hsd = granule["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]
    except (KeyError, TypeError):
        return "none"
    geo = hsd.get("Geometry") or {}
    if geo.get("GPolygons"):
        return "polygon"
    if geo.get("BoundingRectangles"):
        return "rectangle"
    if geo.get("Lines"):
        return "line"
    if hsd.get("Orbit"):
        return "orbit"
    return "none"


def index_side(index_dir: Path, res: int, ym_expr: str, bbox, window, cells=None) -> dict:
    """What the sub-granule index says about the area: granules with rows in its cells / with chunks crossing its
    rectangle, the bytes those chunks occupy, and chunks per granule. `cells` defaults to the area's polyfill."""
    import duckdb

    from aicesat import coverage, planner
    if cells is None:
        cells = planner.cells_for_bbox(bbox, res)
    files = _files_for_cells(index_dir, ym_expr, cells)
    if files is not None and not files:
        return {"names": set(), "box_names": set(), "need_mb": 0.0, "chunks_per_gran": None, "rows": 0}
    src = coverage.read_parquet_src(index_dir, files)
    con = duckdb.connect()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src} LIMIT 0").fetchall()]
        size_cols = [c for c in cols if c.endswith("_size")]
        byte_expr = " + ".join(size_cols) if size_cols else "(byte_end - byte_start)"
        key = [c for c in ("granule", "beam", "chunk_index", "span_index") if c in cols]
        if "chunk_index" not in cols and "span_index" not in cols:
            key.append("byte_start")                       # text indexes: one span per (granule, cell)
        chunk_col = "chunk_index" if "chunk_index" in cols else ("span_index" if "span_index" in cols else None)
        w, s, e, n = bbox
        in_cells = f"h3_cell IN ({','.join(str(int(c)) for c in cells)})"
        in_window = f"AND ({ym_expr}) BETWEEN '{window[0][:7]}' AND '{window[1][:7]}'" if window else ""
        in_box = f"lat_max >= {s} AND lat_min <= {n} AND lon_max >= {w} AND lon_min <= {e}"
        rows = con.execute(f"SELECT count(*) FROM {src} WHERE {in_cells} {in_window}").fetchone()[0]
        names = {r[0] for r in con.execute(f"SELECT DISTINCT granule FROM {src} WHERE {in_cells} {in_window}").fetchall()}
        box_names = {r[0] for r in con.execute(
            f"SELECT DISTINCT granule FROM {src} WHERE {in_cells} AND {in_box} {in_window}").fetchall()}
        need = con.execute(
            f"SELECT coalesce(sum(b), 0) FROM (SELECT {', '.join(key)}, any_value({byte_expr}) AS b FROM {src} "
            f"WHERE {in_cells} AND {in_box} {in_window} GROUP BY {', '.join(key)})").fetchone()[0]
        cpg = None
        if chunk_col:
            per = [r[0] for r in con.execute(
                f"SELECT max({chunk_col}) + 1 FROM {src} WHERE {in_cells} {in_window} GROUP BY granule").fetchall()]
            cpg = statistics.median(per) if per else None
    finally:
        con.close()
    return {"names": names, "box_names": box_names, "need_mb": float(need) / 1e6, "chunks_per_gran": cpg, "rows": rows}


def _files_for_cells(index_dir: Path, ym_expr: str, cells) -> list[str] | None:
    """Per-granule index files touching `cells`, through the directory's coverage manifest (None: scan everything)."""
    import duckdb

    from aicesat import coverage
    manifest = coverage._ensure_manifest(index_dir, ym_expr)
    if manifest is None:
        return None
    con = duckdb.connect()
    try:
        names = [r[0] for r in con.execute(
            f"SELECT DISTINCT granule FROM read_parquet('{manifest}') WHERE h3_cell IN "
            f"({','.join(str(int(c)) for c in cells)})").fetchall()]
    finally:
        con.close()
    return [str(p) for p in ((index_dir / f"{n}.parquet") for n in names) if p.exists()]


def _quartiles(xs) -> tuple[float, float, float] | None:
    if len(xs) < 4:
        return None
    q = statistics.quantiles(xs, n=4)
    return (q[0], q[1], q[2])


# --- one row --------------------------------------------------------------------------------------------------------
def measure(area: str, bbox, coll: dict, use_cache: bool, window=None) -> dict | None:
    from aicesat import coverage
    if not coverage.collection_can_cover(coll["key"], bbox):
        return None
    d, res, ym = coverage._index_for(coll["key"])
    if d is None or not d.exists():
        log.warning("%s: no index dir, skipped", coll["key"])
        return None
    if not coverage.index_covers_area(d, bbox):
        log.warning("%s %s: the index's claim does not cover this area, skipped (nothing to compare against)",
                    area, coll["key"])
        return None
    from aicesat import planner
    window = tuple(window or coll["window"])
    cells = planner.cells_for_bbox(bbox, res)
    c = cmr_side(coll, bbox, window, use_cache)
    hull = cmr_side(coll, bbox, window, use_cache, polygon=planner.search_polygon(cells))
    i = index_side(d, res, ym, bbox, window, cells=cells)
    fp = hull["names"] - i["names"]
    missed = i["names"] - hull["names"]
    need_mb = i["need_mb"]
    return {
        "area": area, "collection": coll["key"], "window": list(window), "bbox": list(bbox),
        "query_km2": round(bbox_area_km2(bbox), 1),
        "cmr_hits": c["hits"], "cmr_gb": round(c["gb"], 2), "cmr_hull": hull["hits"],
        "idx_cells": len(i["names"]), "idx_box": len(i["box_names"]),
        "false_pos": round(len(fp) / hull["hits"], 3) if hull["hits"] else None, "missed": len(missed),
        "need_mb": round(need_mb, 3),
        "bytes_ratio": round(c["gb"] * 1e3 / need_mb, 1) if need_mb else None,
        "fp_area": round(c["fp_area_median"], 1) if c["fp_area_median"] is not None else None,
        "fp_kind": c["footprint_kind"],
        "chunks_per_gran": i["chunks_per_gran"],
        "gran_mb_quartiles": [round(q, 1) for q in c["size_quartiles_mb"]] if c["size_quartiles_mb"] else None,
        "index_rows": i["rows"],
    }


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) >= 10 else f"{v:.3g}"
    if isinstance(v, list):
        return "/".join(_fmt(x) for x in v)
    return f"{v:,}" if isinstance(v, int) else str(v)


COLUMNS = ("area", "collection", "cmr_hits", "cmr_gb", "cmr_hull", "idx_cells", "idx_box", "false_pos", "missed", "need_mb",
           "bytes_ratio", "fp_area", "fp_kind", "chunks_per_gran", "gran_mb_quartiles")


def render(rows: list[dict], argv: list[str], builds: dict) -> str:
    out = [f"# Catalogue search vs the sub-granule index, measured {dt.date.today().isoformat()}", "",
           "Produced by `uv run python scripts/measure_search_modes.py " + " ".join(argv) + "` on the deployed box.",
           "Column meanings are in the script's docstring. `gran_mb_quartiles` are Q1/median/Q3 of the CMR granule size.",
           "", "Index builds measured against:", ""]
    for k, v in sorted(builds.items()):
        out.append(f"- {k}: {v}")
    out += ["", "| " + " | ".join(COLUMNS) + " |", "|" + "|".join("---" for _ in COLUMNS) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_fmt(r[c]) for c in COLUMNS) + " |")
    out += ["", "```json", json.dumps(rows, indent=1), "```", ""]
    return "\n".join(out)


def build_stamps(keys) -> dict:
    from aicesat import coverage
    stamps = {}
    for k in keys:
        d, _res, _ym = coverage._index_for(k)
        mf = d / "_build.json" if d is not None else None
        if mf is not None and mf.exists():
            try:
                b = json.loads(mf.read_text())
                n = sum(1 for _ in d.glob("*.parquet"))
                stamps[k] = f"{n:,} granule files, target {b.get('target')}, window {b.get('window')}, at {b.get('at')}"
            except (OSError, ValueError) as e:
                stamps[k] = f"unreadable _build.json: {e}"
        else:
            stamps[k] = "no index"
    return stamps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--areas", nargs="+", default=list(DEFAULT_AREAS))
    ap.add_argument("--collections", nargs="+", default=list(DEFAULT_COLLECTIONS))
    ap.add_argument("--window", nargs=2, metavar=("START", "END"), default=None, help="override every collection's window")
    ap.add_argument("--no-cache", action="store_true", help="bypass the 24 h CMR cache")
    ap.add_argument("--out", type=Path, default=None, help="write the markdown table (and JSON) here")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    from aicesat import coverage
    colls = {c["key"]: c for c in coverage.collections()}
    unknown = [k for k in a.collections if k not in colls]
    if unknown:
        raise SystemExit(f"unknown collection(s) {unknown}; known: {sorted(colls)}")
    rows = []
    for area, bbox in areas(a.areas).items():
        for key in a.collections:
            r = measure(area, bbox, colls[key], use_cache=not a.no_cache, window=a.window)
            if r is None:
                continue
            rows.append(r)
            log.info("%s %s: cmr %d (%.2f GB) idx %d/%d fp %s need %.1f MB", area, key, r["cmr_hits"], r["cmr_gb"],
                     r["idx_cells"], r["idx_box"], r["false_pos"], r["need_mb"])
    text = render(rows, sys.argv[1:] if argv is None else list(argv), build_stamps(a.collections))
    print(text)
    if a.out:
        a.out.write_text(text)
        log.info("wrote %s", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
