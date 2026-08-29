"""append_partial must cap the streamed preview so the scene doc can't balloon to the full point count — otherwise
cache.save_scene re-serialises an ever-growing doc on every granule (O(N^2)), the dominant cost of a large build."""
import numpy as np

from aicesat import scene


def _batch(n, lon=-49.5, lat=69.5, h=100.0):
    return {"lon": np.full(n, lon), "lat": np.full(n, lat), "h": np.full(n, h)}


def test_append_partial_caps_the_preview():
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    # offer far more than the cap, in 50k batches (as ATL06 would across ~200 granules)
    for _ in range(10):                       # 500k points offered
        scene.append_partial(doc, "ATL06", _batch(50_000))
    n = doc["series"]["ATL06"]["n"]
    assert scene.PARTIAL_PREVIEW_CAP <= n < scene.PARTIAL_PREVIEW_CAP + 50_000   # grew to the cap, then stopped
    # the positions buffer is bounded (3 floats/point), not the full 500k
    assert len(doc["series"]["ATL06"]["positions"]) // 3 == n


def test_append_partial_below_cap_keeps_everything():
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    scene.append_partial(doc, "GLAS", _batch(1000))
    scene.append_partial(doc, "GLAS", _batch(500))
    assert doc["series"]["GLAS"]["n"] == 1500   # under the cap: nothing dropped
