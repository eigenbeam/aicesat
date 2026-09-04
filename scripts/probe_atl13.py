"""Is ATL13 (inland water surface height) indexable by the existing sub-granule machinery?

    uv run python scripts/probe_atl13.py [W S E N]      # defaults to the Imja Tsho box

Indexing a collection the way ATL06/GLAS/ICESSN are indexed needs four things, and any one of them missing means
either extra work or a different approach entirely. This prints a verdict on each rather than a guess:

  1. CMR has granules over the area                      (else nothing to index)
  2. per-beam datasets giving lat / lon / height / time   (the columns every index row needs)
  3. those datasets are CHUNKED                           (a contiguous dataset has no byte ranges to address)
  4. their filter pipelines are within SUPPORTED_FILTERS  ({gzip, shuffle}) — access.decode_chunk REFUSES to guess
     at anything else, so scaleoffset or fletcher32 would need a decoder before any of this works

It also checks the property ATL06 relies on: that every dataset in a beam shares one chunking, so a single chunk
index addresses them all together. ATL13 is a segment/water-body product rather than a fixed-rate one, so that is
not a given and is the thing most likely to differ.

Read-only: opens the granule over byte ranges (no full download) and prints structure.
"""
from __future__ import annotations

import sys

import h5py

from aicesat import access, auth, coverage, index

IMJA = (86.864, 27.8341, 86.9744, 27.9888)          # the scene under study
WANT = ("lat", "lon", "height", "time")


def main() -> int:
    a = sys.argv[1:]
    bbox = tuple(float(x) for x in a[:4]) if len(a) >= 4 else IMJA
    auth.login()

    print(f"=== 1. CMR: ATL13 over {bbox} ===")
    granules = None
    for ver in ("006", "005", "004"):
        try:
            granules = coverage.search("ATL13", ver, bbox, None)
        except Exception as e:
            print(f"  version {ver}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if granules:
            print(f"  version {ver}: {len(granules)} granules")
            break
    if not granules:
        print("  VERDICT: no ATL13 granules found over this area. Nothing to index.")
        return 1
    g = granules[0]
    name = coverage.granule_name(g)
    print(f"  first granule: {name}  ({g.size():.1f} MB)")

    print("\n=== 2/3/4. structure, chunking, filters ===")
    url = g.data_links()[0]
    s3 = (g.data_links(access="direct") or [""])[0]
    with h5py.File(access.cloud_hdf5_file(url, s3 or None), "r") as f:
        beams = [k for k in f if k.startswith("gt")]
        print(f"  beam groups: {beams or '(none — not a per-beam layout)'}")
        top = [k for k in f if not k.startswith("gt")]
        print(f"  other top-level groups: {top}")
        if not beams:
            print("  VERDICT: no gt* beams; the per-beam indexers do not apply as written.")
            return 1

        b = beams[0]
        print(f"\n  --- datasets under {b} (name, shape, dtype, chunks, filters) ---")
        rows = []

        def walk(nm, obj):
            if isinstance(obj, h5py.Dataset):
                rows.append((nm, obj))
        f[b].visititems(walk)
        print(f"  {len(rows)} datasets; showing those that look like lat/lon/height/time plus a sample\n")

        def interesting(nm):
            n = nm.lower()
            return any(k in n for k in ("lat", "lon", "ht_", "height", "delta_time", "water", "segment_id"))

        shown = [r for r in rows if interesting(r[0])][:18] or rows[:18]
        unsupported, unchunked, chunkings = [], [], {}
        for nm, ds in shown:
            fl = index._filters(ds) or "(none)"
            ch = ds.chunks
            print(f"    {nm:38} {str(ds.shape):>14} {str(ds.dtype):>10} chunks={str(ch):>12} filters={fl}")
            if ch is None:
                unchunked.append(nm)
            else:
                chunkings.setdefault(ch[0], []).append(nm)
            for step in (fl.split(",") if fl != "(none)" else []):
                if step and step not in access.SUPPORTED_FILTERS:
                    unsupported.append((nm, step))

        print("\n=== VERDICT ===")
        ok = True
        if unchunked:
            ok = False
            print(f"  (3) NOT CHUNKED: {len(unchunked)} of the inspected datasets are contiguous, e.g. {unchunked[:3]}")
            print("      A contiguous dataset has no per-chunk byte ranges, so there is nothing to address")
            print("      sub-granule. Those columns would have to be read whole, or read another way.")
        else:
            print("  (3) chunked: every inspected dataset has a chunk layout")
        if unsupported:
            ok = False
            names = sorted({s for _n, s in unsupported})
            print(f"  (4) UNSUPPORTED FILTERS: {names} — access.decode_chunk refuses to guess (spec 6.3).")
            print("      A decoder for each would be needed before any ATL13 index could be read back.")
        else:
            print(f"  (4) filters are all within {sorted(access.SUPPORTED_FILTERS)}")
        if len(chunkings) > 1:
            print(f"  (2) MIXED CHUNKING: {len(chunkings)} distinct chunk sizes among the inspected datasets "
                  f"({sorted(chunkings)}).")
            print("      ATL06 asserts one chunking per beam so a single chunk index addresses every column at once.")
            print("      Mixed chunking does not block indexing, but that assumption has to be dropped.")
        elif chunkings:
            print(f"  (2) single chunking across the inspected datasets: {list(chunkings)[0]} rows per chunk")
        print(f"\n  {'LOOKS INDEXABLE' if ok else 'NOT INDEXABLE AS-IS'} with the existing machinery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
