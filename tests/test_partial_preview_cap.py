"""append_partial must keep the streamed preview bounded AND representative of every granule seen so far.

The original reason was doc size (positions were JSON in the doc, re-serialised per granule). That is gone — they
append to a binary sidecar now. The reason it still matters: the finalized series is stored shuffled so any prefix is
a fair sample, but the preview is appended as granules land, so its prefix is the FIRST granules. The client fetches
a prefix, so the thinning is what stops a long build's preview from showing only where it started."""
import numpy as np
import pytest

from aicesat import cache, scene


@pytest.fixture(autouse=True)
def _scene_dir(tmp_path, monkeypatch):
    """append_partial writes a binary sidecar per scene; keep it out of the real data directory. Without this the
    files accumulate across tests AND across runs, so a second run sees the first one's points."""
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")


def _batch(n, lon=-49.5, lat=69.5, h=100.0):
    return {"lon": np.full(n, lon), "lat": np.full(n, lat), "h": np.full(n, h)}


def test_append_partial_stays_under_the_cap():
    doc = scene.new_scene("cap", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    # offer far more than the cap, in 50k batches (as ATL06 would across ~200 granules)
    for _ in range(10):                       # 500k points offered
        scene.append_partial(doc, "ATL06", _batch(50_000))
    n = doc["series"]["ATL06"]["n"]
    assert n <= scene.PARTIAL_PREVIEW_CAP     # bounded
    assert cache.scene_array_len(doc["scene_id"], "ATL06", "positions") // 3 == n


def test_late_granules_still_reach_the_preview():
    """The cap must THIN, not truncate. The first design dropped granules once full, so a long build's preview showed
    only the granules that arrived first — the scene looked like it was missing data and progress appeared to stall."""
    doc = scene.new_scene("late", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    # each granule sits at its own longitude, so we can tell which ones made it into the preview
    for i in range(12):
        scene.append_partial(doc, "ATL06", _batch(40_000, lon=-49.9 + i * 0.05))
    pos = cache.scene_array_read(doc["scene_id"], "ATL06", "positions").reshape(-1, 3)
    assert pos.shape[0] <= scene.PARTIAL_PREVIEW_CAP
    # points from the LAST granule must be present (they were dropped entirely under the old cap)
    xs = np.unique(np.round(pos[:, 0], 1))
    assert len(xs) >= 10, f"only {len(xs)} distinct granule positions survived — late granules were dropped"


def test_preview_thinning_is_bounded_across_many_granules():
    doc = scene.new_scene("bounded", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    for i in range(40):                       # 2M points offered, well past the cap
        scene.append_partial(doc, "ATL06", _batch(50_000, lon=-49.9 + i * 0.02))
        assert doc["series"]["ATL06"]["n"] <= scene.PARTIAL_PREVIEW_CAP   # never exceeds, at any point


def test_append_partial_below_cap_keeps_everything():
    doc = scene.new_scene("under", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    scene.append_partial(doc, "GLAS", _batch(1000))
    scene.append_partial(doc, "GLAS", _batch(500))
    assert doc["series"]["GLAS"]["n"] == 1500   # under the cap: nothing dropped
