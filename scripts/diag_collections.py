"""Why did a collection not appear in a scene? Run each leg standalone and show the real exception.

build_scene catches every leg's failure and logs one line ("{name} unavailable: {e}") so a partial scene still
builds. That is right for the server and useless for diagnosis: the traceback is gone, and "unavailable" covers a
missing index, an auth failure, an empty region and a bug in the leg equally.

This runs the same extract() calls the build does, one collection at a time, and prints for each: whether the
sub-granule index covers the bbox (and over what region it was built), then the point count or the FULL traceback.

    uv run python scripts/diag_collections.py <w> <s> <e> <n>
    uv run python scripts/diag_collections.py --scene <scene_id>     # use that scene's own bbox
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

from aicesat import cache, regions

# collection -> (module name, extract window, the module attr holding its index-coverage predicate)
LEGS = (
    ("GLAS",   "glas",   "DEFAULT_GLAS_WINDOW"),
    ("ICESSN", "icessn", "DEFAULT_ICESSN_WINDOW"),
    ("ATL06",  "atl06",  "DEFAULT_ATL06_WINDOW"),
)


def _index_report(mod, name):
    """What the leg's index-coverage check will decide, and why — _index_covers returning False silently diverts to
    the slow CMR + whole-granule fallback, which fails in completely different ways."""
    import json
    try:
        idx = {"GLAS": "index_glas", "ICESSN": "index_icessn", "ATL06": "index_atl06"}[name]
        m = __import__(f"aicesat.{idx}", fromlist=["x"])
        res = getattr(m, {"GLAS": "GLAS_RES", "ICESSN": "ICESSN_RES", "ATL06": "ATL06_RES"}[name])
        d = m._index_dir(res)
        n = len(list(d.glob("*.parquet"))) if d.exists() else 0
        mf = d / "_build.json"
        built = json.loads(mf.read_text()) if mf.exists() else None
        return d, res, n, built
    except Exception as e:                                    # noqa: BLE001
        return None, None, 0, {"error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bbox", type=float, nargs="*", metavar="W S E N")
    ap.add_argument("--scene", help="take the bbox from a built scene instead")
    args = ap.parse_args()

    if args.scene:
        doc = cache.load_scene(args.scene)
        if doc is None:
            raise SystemExit(f"no scene {args.scene}")
        bbox = tuple(doc["bbox"])
        print(f"scene {args.scene}: bbox={bbox}  series already in it: {sorted(doc.get('series', {}))}")
    elif len(args.bbox) == 4:
        bbox = tuple(args.bbox)
    else:
        raise SystemExit("give W S E N, or --scene <id>")

    for name, modname, winattr in LEGS:
        print("\n" + "=" * 100)
        mod = __import__(f"aicesat.{modname}", fromlist=["x"])
        window = getattr(regions, winattr)
        d, res, nfiles, built = _index_report(mod, name)
        covers = mod._index_covers(bbox)
        print(f"{name}: window={window}")
        print(f"  index dir      {d}  ({nfiles:,} granule parquets)")
        print(f"  built over     {built}")
        print(f"  _index_covers  {covers}"
              + ("" if covers else "   <-- FALSE: this leg takes the slow CMR + whole-granule fallback, not the index"))
        t0 = time.time()
        try:
            arr, meta = mod.extract(bbox, window)
            print(f"  RESULT         {meta.get('n', arr['lon'].size):,} points in {time.time() - t0:.1f}s "
                  f"(cache_key={meta.get('cache_key')})")
            st = meta.get("access") or {}
            if st:
                print(f"  access         {st.get('chunks_from_nasa', '?')} chunks from NASA, "
                      f"{st.get('chunks_from_lake', '?')} from the lake, {st.get('requests', '?')} GETs, "
                      f"{(st.get('bytes') or 0)/1e6:.1f} MB")
        except Exception:                                     # noqa: BLE001
            print(f"  FAILED after {time.time() - t0:.1f}s:")
            traceback.print_exc(file=sys.stdout)   # same stream as the header, so piping keeps the order


if __name__ == "__main__":
    main()
