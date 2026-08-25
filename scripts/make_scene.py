"""Build (or rebuild) a scene from cached/extracted data without the MCP transport.
usage: uv run scripts/make_scene.py [region] [--glas] [--coreg] [--id SCENE_ID]
"""
import argparse, json, logging, sys
from aicesat import atl03, cache, regions, scene
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
if __name__ == "__main__":  # guard: index build spawns worker processes that re-import this file
    logging.getLogger("fsspec").setLevel(logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("region", nargs="?", default=regions.DEFAULT_REGION)
    p.add_argument("--id", default=None)
    p.add_argument("--glas", action="store_true")
    p.add_argument("--coreg", action="store_true")
    p.add_argument("--max-granules", type=int, default=1)
    p.add_argument("--no-imagery", action="store_true")
    a = p.parse_args()
    bbox = regions.resolve_bbox(a.region)
    sid = a.id or a.region
    arrays, meta = atl03.extract(bbox, regions.DEFAULT_ATL03_WINDOW, max_granules=a.max_granules)
    doc = scene.new_scene(sid, bbox, f"show me ICESat-2 photons over {a.region}")
    scene.add_series(doc, "ICESAT2", arrays, meta, meta["cache_key"])
    if "--no-imagery" not in sys.argv:
        scene.add_imagery(doc)
    if a.glas:
        from aicesat import glas
        g_arrays, g_meta = glas.extract(bbox, regions.DEFAULT_GLAS_WINDOW)
        scene.add_series(doc, "GLAS", g_arrays, g_meta, g_meta["cache_key"])
    cache.save_scene(sid, doc)
    if a.coreg:
        from aicesat.server import run_coregister
        out = run_coregister(sid)
        print(json.dumps({k: v for k, v in out.items() if k in ("stats", "comparability", "displacement_m", "years_apart", "compute_seconds")}, indent=1, default=str))
    print(json.dumps({"scene_id": sid, "series": {m: s["n"] for m, s in doc["series"].items()}, "path": str(cache.scene_path(sid))}))
