"""Pre-build the GEDI L2A sub-granule index over an area, uncapped and in parallel.
usage: uv run scripts/build_gedi_index.py <W> <S> <E> <N> [res] [workers]
"""
import json, logging, sys

if __name__ == "__main__":   # guard: index workers are spawned processes that re-import this file
    from aicesat import build_gedi, index_gedi
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("fsspec", "urllib3", "earthaccess", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__.strip(), file=sys.stderr); sys.exit(2)
    bbox = tuple(float(v) for v in a[:4])
    res = int(a[4]) if len(a) > 4 else index_gedi.GEDI_RES
    workers = int(a[5]) if len(a) > 5 else 8
    out = build_gedi.build_bbox(bbox, res=res, workers=workers,
                                on_granule=lambda n, r, e: print(f"  {'FAIL' if e else 'ok  '} {n} {r} rows"
                                                                 f"{' ' + e if e else ''}", file=sys.stderr))
    print(json.dumps({k: v for k, v in out.items() if k != "rollup"}, indent=1))
    sys.exit(1 if out["err"] else 0)
