#!/usr/bin/env python
"""'X not indexed over <bbox>' but you DID build the index — this says which of the four reasons it is.

    uv run python scripts/why_not_covered.py 86.8426 27.7978 87.0067 28.0153

The gate (coverage.index_covers_area) demands CONTAINMENT, not overlap: every coverage cell the selection touches
must be in the built claim. So an index that covers 95% of your area still refuses it, and the error message —
"not indexed over <bbox> — build the index first" — reads as if nothing was built at all. The four cases:

  1. no _build.json          the build never stamped a claim (never ran here, or was killed before stamping)
  2. outside the index       the selection reaches past the claim piece nearest it (km per side, and the box
                             Explore's "Fit to indexed area" would give), or overlaps no piece at all
  3. bounds ok, cells short  the extent contains it but specific cells were never built — the usual case, and the
                             one the error message hides. Reported as "missing N of M cells".
  4. covered                 this collection is fine; something else failed. Check the job log.

Read-only. Complements check_index.py, which catches the OPPOSITE fault (a claim the granule files do not back).
"""
from __future__ import annotations

import argparse
import json
import sys


def report(name: str, d, bbox, polygon=None) -> bool:
    from aicesat import index as atl03_index
    from aicesat import planner

    print(f"\n=== {name} ===")
    print(f"  index dir: {d}")
    if not d.exists():
        print("  VERDICT (1): the index directory does not exist. Nothing was built here.")
        return False
    mf = d / "_build.json"
    if not mf.exists():
        print("  VERDICT (1): no _build.json — the build never stamped a coverage claim.")
        print("     A build killed before stamping leaves granule files with no claim; re-run the build script.")
        return False
    try:
        doc = json.loads(mf.read_text())
    except Exception as e:
        print(f"  VERDICT (1): _build.json is unreadable ({type(e).__name__}). Re-run the build script.")
        return False

    from aicesat import coverage

    res = doc.get("coverage_res") or atl03_index.COVERAGE_RES
    pieces = coverage.claim_regions(d)
    rb = lambda p: [round(v, 4) for v in p["bbox"]]
    print(f"  claim: {len(pieces)} piece(s) {[rb(p) for p in pieces]} coverage_res={res} target={doc.get('target')}")
    # Measured against the claim piece this selection is about, as coverage_gap does. The claim's overall extent
    # (`bounds`) spans every build ever stamped, so with one in Nepal and one in Greenland it hides an overhang.
    piece = coverage.claim_piece_for(pieces, bbox)
    if piece is None:
        print("  VERDICT (1): the claim is EMPTY — its rows were dropped (a schema change invalidates the claim).")
        print("     Re-run the build script.")
        return False
    over = coverage.overhang_km(bbox, piece["bbox"])
    if not coverage._overlap(bbox, piece["bbox"]) or over:
        print(f"  VERDICT (2): the selection is NOT inside the indexed area.")
        print(f"     selection {list(bbox)}")
        print(f"     nearest indexed piece {rb(piece)}")
        if not coverage._overlap(bbox, piece["bbox"]):
            print(f"     no overlap: {coverage._km_apart(bbox, piece['bbox']):,.0f} km apart")
        else:
            print(f"     overshoots: {', '.join(f'{side} by {km:.2f} km' for side, km in over.items())}")
            fit = coverage.fit_to_coverage(bbox, [name])
            if fit["bbox"]:
                print(f"     fitted box (what Explore's 'Fit to indexed area' gives): {fit['bbox']}")
        print("     Fix: draw the scene inside the indexed area, or rebuild the index over a bbox that CONTAINS it.")
        return False

    want = planner.coverage_cells(bbox, polygon, res=res)
    ok = atl03_index.covers_cells(d, want)
    if ok:
        print(f"  VERDICT (4): COVERED — all {len(want)} coverage cells are claimed. This collection is not the blocker.")
        return True
    # Which cells are missing? covers_cells walks up from each wanted cell, so test them one at a time.
    missing = [c for c in want if not atl03_index.covers_cells(d, [c])]
    print(f"  VERDICT (3): bounds contain the selection, but {len(missing)} of {len(want)} coverage cells "
          f"(res {res}) are NOT claimed.")
    print("     This is the case the error message hides: the index exists and overlaps, but the gate needs EVERY")
    print("     cell. A build that stopped early, or one whose bbox clipped a corner, lands here.")
    if missing[:6]:
        print(f"     missing cells (first few): {[hex(c) if isinstance(c, int) else c for c in missing[:6]]}")
    print("     Fix: re-run the build over a bbox that fully contains the scene, then re-check.")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    a = ap.parse_args()
    bbox = tuple(a.bbox)

    from aicesat import coverage

    print(f"selection: {bbox}")
    results = {c["key"]: report(c["key"], coverage._index_for(c["key"])[0], bbox) for c in coverage.collections()}
    good = [k for k, v in results.items() if v]
    print("\n" + "=" * 70)
    if good:
        print(f"COVERED: {', '.join(good)} — a build over this area should return data from those.")
    else:
        print("NO collection covers this selection, which is exactly what")
        print("'no collection returned data over this area' means. It is NOT an auth failure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
