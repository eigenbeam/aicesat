#!/usr/bin/env python
"""'X not indexed over <bbox>' but you DID build the index — this says which of the four reasons it is.

    uv run python scripts/why_not_covered.py 86.8426 27.7978 87.0067 28.0153

The gate (coverage.index_covers_area) demands CONTAINMENT, not overlap: every coverage cell the selection touches
must be in the built claim. So an index that covers 95% of your area still refuses it, and the error message —
"not indexed over <bbox> — build the index first" — reads as if nothing was built at all. The four cases:

  1. no _build.json          the build never stamped a claim (never ran here, or was killed before stamping)
  2. bbox outside bounds     the claim's own extent does not contain the selection — cheap reject, before any cells
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

    b, res = doc.get("bounds"), doc.get("coverage_res") or atl03_index.COVERAGE_RES
    print(f"  claim: bounds={b} coverage_res={res} granules={doc.get('granules')} target={doc.get('target')}")
    w, s, e, n = bbox
    if b and not (b[0] <= w and b[1] <= s and e <= b[2] and n <= b[3]):
        print(f"  VERDICT (2): the selection is NOT inside the claimed extent.")
        print(f"     selection {list(bbox)}")
        print(f"     claimed   {b}")
        over = [f"{side} by {abs(v):.4f}deg" for side, v in
                (("west", b[0] - w), ("south", b[1] - s), ("east", e - b[2]), ("north", n - b[3])) if v > 0]
        print(f"     overshoots: {', '.join(over)}")
        print("     Fix: rebuild the index over a bbox that CONTAINS the scene, or draw the scene inside the claim.")
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

    from aicesat import index_atl06, index_glas, index_icessn

    print(f"selection: {bbox}")
    results = {
        "ATL06": report("ATL06", index_atl06._index_dir(index_atl06.ATL06_RES), bbox),
        "GLAS": report("GLAS", index_glas._index_dir(index_glas.GLAS_RES), bbox),
        "ICESSN": report("ICESSN", index_icessn._index_dir(index_icessn.ICESSN_RES), bbox),
    }
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
