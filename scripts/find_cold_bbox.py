"""Find (or make) a bbox that bench_ingest_phases will actually measure cold.

Guessing a bbox by arithmetic wasted a benchmark run earlier: the box landed on already-materialized cells and the
"cold" leg measured nothing. Cold means cold on THREE layers, and the middle one is the trap:

  1. the extract npz cache  — keyed on the exact bbox, so any new bbox misses it;
  2. the LAKE               — keyed on H3 cells, so a *nearby* bbox is warm even though the npz key is new;
  3. the sub-granule index  — must EXIST, or atl06.extract raises before fetching anything.

(2) and (3) pull against each other: the index is only built where scenes were built, which is exactly where the lake
is warm. So the honest search is "indexed AND not marked in coverage_cells", and this reports what it finds rather
than assuming there is anything.

    uv run python scripts/find_cold_bbox.py                       # search for a never-materialized bbox
    uv run python scripts/find_cold_bbox.py --recool <w> <s> <e> <n>        # show what re-cooling one would delete
    uv run python scripts/find_cold_bbox.py --recool <w> <s> <e> <n> --yes  # actually delete it

--recool is the better experiment when the search comes up empty, and it is also the better A/B: run it on the SAME
bbox as a previous measurement and the code is the only variable.
"""
from __future__ import annotations

import argparse
import sys

import duckdb
import h3

from aicesat import atl06, cache, coverage, index_atl06, lake, regions

WINDOW = regions.DEFAULT_ATL06_WINDOW
RES = index_atl06.ATL06_RES
MAX_GRANULES = 20          # atl06.extract's default — part of the npz cache key


def _indexed_cells() -> dict[int, int]:
    """{h3_cell: granules touching it} from the coverage manifest (cheap; no whole-index scan)."""
    d, _res, ym = coverage._index_for("ATL06")
    if d is None or not d.exists():
        sys.exit("no ATL06 index on this machine — nothing can be benchmarked")
    manifest = coverage._ensure_manifest(d, ym)
    if manifest is None:
        sys.exit("the ATL06 index directory holds no granule files")
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT h3_cell, count(DISTINCT granule) FROM read_parquet('{manifest}') GROUP BY 1").fetchall()
    finally:
        con.close()
    return {int(c): int(n) for c, n in rows}


def _marked_cells() -> set[int]:
    """Cells already recorded as materialized. This — not the files — is what makes fetch_bbox skip a chunk."""
    if not lake.META_DB.exists():
        return set()
    with lake.meta_db() as con:
        return {int(c) for (c,) in con.execute(
            "SELECT DISTINCT h3_cell FROM coverage_cells WHERE mission = 'ATL06'").fetchall()}


def _probe(bbox) -> dict | None:
    """Exactly what fetch_bbox would decide, without fetching: which chunks come from NASA, which from the lake, and
    how many bytes the NASA half is. Mirrors atl06.extract's arguments (all six beams)."""
    if not atl06._index_covers(bbox):
        return None
    want_cells, rows = index_atl06._index_rows(bbox, WINDOW, RES, strong_only=False)
    if not rows:
        return None
    names = sorted({r["granule"] for r in rows})
    have = lake.ingested_chunk_cells("ATL06", names)
    chunk_cells, chunk_row = {}, {}
    for r in rows:
        k = (r["granule"], r["beam"], r["chunk_index"])
        chunk_cells.setdefault(k, set()).add(int(r["h3_cell"])); chunk_row.setdefault(k, r)
    todo = [k for k, cs in chunk_cells.items() if any((k[0], k[1], k[2], c) not in have for c in cs)]
    nbytes = sum(sum(chunk_row[k][f"{ds}_size"] for ds in index_atl06.ATL06_DATASETS) for k in todo)
    k_npz = cache.key("atl06", coverage.ATL06_VERSION, bbox, WINDOW, MAX_GRANULES, None)
    return {"bbox": bbox, "cells": len(want_cells), "granules": len(names),
            "from_nasa": len(todo), "from_lake": len(chunk_cells) - len(todo),
            "mb": nbytes / 1e6, "npz_warm": cache.load(k_npz) is not None, "npz_key": k_npz}


def _box(cell: int, dlon: float, dlat: float):
    lat, lon = h3.cell_to_latlng(h3.int_to_str(cell))
    return (round(lon - dlon / 2, 4), round(lat - dlat / 2, 4), round(lon + dlon / 2, 4), round(lat + dlat / 2, 4))


def _build_region():
    """The bbox the ATL06 index was built over. atl06.extract refuses anything outside it, and MOST indexed cells are
    outside it — a granule's index rows follow the whole orbit track, not just the region that triggered the build."""
    import json
    mf = index_atl06._index_dir(RES) / "_build.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text()).get("bbox")
    except Exception:
        return None


def find(args) -> None:
    indexed, marked = _indexed_cells(), _marked_cells()
    region = _build_region()
    if region is None:
        sys.exit("no _build.json beside the ATL06 index — atl06.extract cannot confirm coverage for any bbox")
    rw, rs, re_, rn = region
    cold = {c: n for c, n in indexed.items() if c not in marked}
    # Keep only candidates whose whole candidate box fits inside the build region, or _index_covers rejects it.
    fits = {}
    for c, n in cold.items():
        w, s, e, nn = _box(c, args.dlon, args.dlat)
        if rw <= w and rs <= s and e <= re_ and nn <= rn:
            fits[c] = n
    print(f"ATL06 index: {len(indexed):,} cells   lake: {len(marked):,} marked   "
          f"never materialized: {len(cold):,}   inside the build region {region}: {len(fits):,}")
    if not cold:
        print("\nNo indexed cell is unmaterialized — every area you have indexed is already in the lake.\n"
              "Use --recool on a previously benchmarked bbox instead; that also makes the A/B exact.")
        return
    if not fits:
        print(f"\n{len(cold):,} cells are unmaterialized but every one sits OUTSIDE the index build region, where\n"
              f"atl06.extract raises before fetching. Either build the index over a new area, or use --recool.")
        return

    # Rank by granule count: a cell with more granules gives a leg with real bytes rather than a trivial one.
    out = []
    for cell, _n in sorted(fits.items(), key=lambda kv: -kv[1])[: args.scan]:
        p = _probe(_box(cell, args.dlon, args.dlat))
        if p and p["from_nasa"]:
            out.append(p)
    if not out:
        print("\nCandidates were in range but none resolved to fetchable index rows. Try --recool.")
        return

    # Prefer genuinely all-cold boxes, then the one closest to the target size, so the run is comparable to prior ones.
    out.sort(key=lambda p: (p["from_lake"] > 0, p["npz_warm"], abs(p["mb"] - args.target_mb)))
    print(f"\n{'w':>9}{'s':>9}{'e':>9}{'n':>9}{'cells':>7}{'gran':>6}{'NASA':>7}{'lake':>6}{'MB':>8}  npz")
    for p in out[: args.top]:
        w, s, e, n = p["bbox"]
        print(f"{w:>9.3f}{s:>9.3f}{e:>9.3f}{n:>9.3f}{p['cells']:>7}{p['granules']:>6}"
              f"{p['from_nasa']:>7}{p['from_lake']:>6}{p['mb']:>8.1f}  {'WARM' if p['npz_warm'] else 'cold'}")
    best = out[0]
    w, s, e, n = best["bbox"]
    if best["from_lake"]:
        print(f"\nNOTE: the best candidate still serves {best['from_lake']} chunks from the lake — the box overlaps "
              f"warm cells. Shrink it with --dlon/--dlat, or use --recool for a fully cold run.")
    print(f"\n  uv run python scripts/bench_ingest_phases.py {w} {s} {e} {n}")


def recool(args) -> None:
    """Delete a bbox's lake cells and extract-cache entry so it fetches cold again. Re-fetchable, but it IS deletion."""
    bbox = tuple(args.recool)
    before = _probe(bbox)
    if before is None:
        sys.exit(f"{bbox} is not covered by the ATL06 index — atl06.extract would raise before fetching")
    cells = index_atl06._index_rows(bbox, WINDOW, RES, strong_only=False)[0]
    stats = lake.cell_stats("ATL06", with_rows=False)
    victims = [c for c in cells if c in stats]
    nbytes = sum(stats[c]["bytes"] for c in victims)
    npz = cache.CACHE_DIR / f"{before['npz_key']}.npz"
    print(f"bbox {bbox}")
    print(f"  currently: {before['from_nasa']} chunks from NASA, {before['from_lake']} from the lake, "
          f"npz {'WARM' if before['npz_warm'] else 'cold'}")
    print(f"  would DELETE {len(victims)} lake cell(s), {nbytes/1e6:.1f} MB, and the npz entry if present")
    print(f"  cold run would then fetch ~{before['mb'] + nbytes/1e6:.0f} MB")   # rough: lake chunks return to NASA
    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to do it.")
        return
    lake.evict_cells(victims, mission="ATL06", reason="bench re-cool", stats=stats)
    for p in (npz, cache.CACHE_DIR / f"{before['npz_key']}.json"):
        if p.exists():
            p.unlink()
    after = _probe(bbox) or {"from_nasa": "?", "from_lake": "?", "npz_warm": False}
    print(f"\ndeleted. now: {after['from_nasa']} chunks from NASA, {after['from_lake']} from the lake, "
          f"npz {'WARM' if after['npz_warm'] else 'cold'}")
    w, s, e, n = bbox
    print(f"\n  uv run python scripts/bench_ingest_phases.py {w} {s} {e} {n}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recool", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                    help="make this bbox cold again by evicting its lake cells (shows the plan unless --yes)")
    ap.add_argument("--yes", action="store_true", help="actually delete, for --recool")
    ap.add_argument("--dlon", type=float, default=0.7, help="candidate box width in degrees")
    ap.add_argument("--dlat", type=float, default=0.3, help="candidate box height in degrees")
    ap.add_argument("--scan", type=int, default=40, help="cold cells to probe (each probe is an index query)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--target-mb", type=float, default=600.0, help="prefer a box near this fetch size")
    args = ap.parse_args()
    recool(args) if args.recool else find(args)


if __name__ == "__main__":
    main()
