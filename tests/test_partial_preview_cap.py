"""append_partial must cap the streamed preview so the scene doc can't balloon to the full point count — otherwise
cache.save_scene re-serialises an ever-growing doc on every granule (O(N^2)), the dominant cost of a large build."""
import numpy as np

from aicesat import scene


def _batch(n, lon=-49.5, lat=69.5, h=100.0):
    return {"lon": np.full(n, lon), "lat": np.full(n, lat), "h": np.full(n, h)}


def test_append_partial_stays_under_the_cap():
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    # offer far more than the cap, in 50k batches (as ATL06 would across ~200 granules)
    for _ in range(10):                       # 500k points offered
        scene.append_partial(doc, "ATL06", _batch(50_000))
    n = doc["series"]["ATL06"]["n"]
    assert n <= scene.PARTIAL_PREVIEW_CAP     # bounded
    assert len(doc["series"]["ATL06"]["positions"]) // 3 == n


def test_late_granules_still_reach_the_preview():
    """The cap must THIN, not truncate. The first design dropped granules once full, so a long build's preview showed
    only the granules that arrived first — the scene looked like it was missing data and progress appeared to stall."""
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    # each granule sits at its own longitude, so we can tell which ones made it into the preview
    for i in range(12):
        scene.append_partial(doc, "ATL06", _batch(40_000, lon=-49.9 + i * 0.05))
    pos = np.asarray(doc["series"]["ATL06"]["positions"], dtype="f4").reshape(-1, 3)
    assert pos.shape[0] <= scene.PARTIAL_PREVIEW_CAP
    # points from the LAST granule must be present (they were dropped entirely under the old cap)
    xs = np.unique(np.round(pos[:, 0], 1))
    assert len(xs) >= 10, f"only {len(xs)} distinct granule positions survived — late granules were dropped"


def test_preview_thinning_is_bounded_across_many_granules():
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    for i in range(40):                       # 2M points offered, well past the cap
        scene.append_partial(doc, "ATL06", _batch(50_000, lon=-49.9 + i * 0.02))
        assert doc["series"]["ATL06"]["n"] <= scene.PARTIAL_PREVIEW_CAP   # never exceeds, at any point


def test_append_partial_below_cap_keeps_everything():
    doc = scene.new_scene("s", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    scene.append_partial(doc, "GLAS", _batch(1000))
    scene.append_partial(doc, "GLAS", _batch(500))
    assert doc["series"]["GLAS"]["n"] == 1500   # under the cap: nothing dropped
