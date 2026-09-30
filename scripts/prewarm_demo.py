"""Fetch the demo's change-level areas once, through the same path the globe and elevation_change use (the
"H3 <hex>" scene over the res-5 hex containing each point), so the recording is a lake hit. Run it only after the
ATL06 index over the region has finished: a claim is stamped at the end of a build, and a hex outside every claim
is refused.
usage: AICESAT_DATA_DIR=/abs/path/data-uwg uv run scripts/prewarm_demo.py [lat lon] [lat lon] ...
default: the lower-trunk demo area (69.1752, -49.3276) and Jakobshavn Isbrae's published coordinates (69.1667, -49.8333),
since a model resolving the name "Jakobshavn" will likely pick the latter.
"""
import sys
import time

from aicesat import api

args = [float(a) for a in sys.argv[1:]]
points = list(zip(args[::2], args[1::2])) or [(69.1752, -49.3276), (69.1667, -49.8333)]
for lat, lon in points:
    hx = api.region_hex(lat, lon)
    if any(s.get("question") == api.region_question(hx) and s.get("status") == "ready" for s in api.scenes()):
        print(f"{hx} ({lat}, {lon}): already built")
        continue
    t0 = time.time()
    doc = api.build_scene(polygon=api.hex_polygon(hx), question=api.region_question(hx), wait_for_imagery=True,
                          log_fn=print, **api.REGION_FLAGS)
    print(f"{hx} ({lat}, {lon}): scene {doc['scene_id']} in {time.time() - t0:.0f}s",
          {m: s["n"] for m, s in doc["series"].items()}, flush=True)
