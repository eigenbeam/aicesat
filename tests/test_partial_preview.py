"""append_partial keeps every point it is given, and every granule reaches the viewer.

This file used to pin a cap (PARTIAL_PREVIEW_CAP) and a power-of-2 thinning loop. Both existed for one reason: the
client fetched a bounded PREFIX of this buffer, and a prefix of append-ordered data is the granules that happened to
arrive first, not a sample of all of them. So the buffer had to be thinned to stay both bounded and representative.

The push transport delivers every point as it lands. There is no prefix, so there is nothing to keep representative,
so there is no cap. What must still hold is the property the cap was protecting in the first place: a long build's
preview keeps growing and late granules are visible.
"""
import numpy as np
import pytest

from aicesat import cache, scene


@pytest.fixture(autouse=True)
def _scene_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")


def _batch(n, lon=-49.5, lat=69.5, h=100.0):
    return {"lon": np.full(n, lon), "lat": np.full(n, lat), "h": np.full(n, h)}


def test_append_partial_keeps_every_point():
    doc = scene.new_scene("cap", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    for _ in range(10):
        scene.append_partial(doc, "ATL06", _batch(50_000))
    assert doc["series"]["ATL06"]["n"] == 500_000, "nothing may be thinned away any more"
    assert cache.scene_array_len(doc["scene_id"], "ATL06", "positions") // 3 == 500_000


def test_every_granule_reaches_the_preview():
    """The property the old cap existed to protect. Under truncation, late granules vanished and the build looked
    stalled; under thinning they survived at reduced density; now they simply all arrive."""
    doc = scene.new_scene("late", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    for i in range(12):
        scene.append_partial(doc, "ATL06", _batch(40_000, lon=-49.9 + i * 0.05))
    pos = cache.scene_array_read(doc["scene_id"], "ATL06", "positions").reshape(-1, 3)
    assert pos.shape[0] == 12 * 40_000
    assert len(np.unique(np.round(pos[:, 0], 1))) == 12, "every granule's ground position is present"


def test_the_preview_grows_monotonically():
    """It must never shrink mid-build: the stream client appends what arrives, and a buffer that shrank under it was
    exactly the case the `reset` control frame had to be invented for."""
    doc = scene.new_scene("grow", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    seen = []
    for i in range(40):
        scene.append_partial(doc, "ATL06", _batch(50_000, lon=-49.9 + i * 0.02))
        seen.append(doc["series"]["ATL06"]["n"])
    assert seen == sorted(seen) and len(set(seen)) == 40
    assert seen[-1] == 40 * 50_000


def test_append_partial_below_any_old_cap_still_keeps_everything():
    doc = scene.new_scene("under", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    scene.append_partial(doc, "GLAS", _batch(1000))
    scene.append_partial(doc, "GLAS", _batch(500))
    assert doc["series"]["GLAS"]["n"] == 1500
