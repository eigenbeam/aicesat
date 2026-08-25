"""Smoke test: extract GLAH06 shots over a region (all campaigns) and print a summary."""
import json, logging, sys, time
from aicesat import glas, regions
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("fsspec").setLevel(logging.WARNING)
region = sys.argv[1] if len(sys.argv) > 1 else regions.DEFAULT_REGION
t = time.time()
arrays, meta = glas.extract(regions.resolve_bbox(region), regions.DEFAULT_GLAS_WINDOW)
meta["elapsed_s"] = round(time.time() - t, 1)
meta["h_range"] = [float(arrays["h"].min()), float(arrays["h"].max())]
meta["granules"] = meta["granules"][:5]
print(json.dumps(meta, indent=1))
