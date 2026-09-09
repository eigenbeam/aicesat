"""The candidate search is memoised per (scene, params).

Every call re-decompresses each mission's npz and re-runs pyproj over every point, so the cost is proportional to
the scene, not the request: measured 0.99 s then 0.94 s back-to-back on a 264k-point scene. Two things make that
bite. The UI re-fires the search on every slider nudge, and the MCP flow is inherently multi-call -- list the
candidates, then fetch one cell's series. Both paid the full load twice for an identical answer.

The key carries the scene file's mtime+size (the same invalidation _scene_for_read uses), so a build writing
progressive updates is never served a stale result."""
import json

import numpy as np
import pytest

from aicesat import api, cache
from aicesat import scene as scene_mod
from aicesat import timeseries

FRAME = scene_mod.local_frame((-45.5, 71.8, -44.5, 72.2))


@pytest.fixture
def scene_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path)
    (tmp_path / "s1.json").write_text(json.dumps({"frame": FRAME, "z0": 1000.0, "series": {}}))
    return "s1"


@pytest.fixture
def counted_load(monkeypatch):
    """_load_all is the expensive half; count how often it actually runs."""
    import h3
    clat, clon = h3.cell_to_latlng(h3.latlng_to_cell(72.0, -45.0, 9))
    rng = np.random.default_rng(0)
    recs = []
    for mission, years in (("GLAS", [2005.05]), ("ATL06", [2019.5, 2020.5, 2021.5])):
        n = 40 * len(years)
        lat = clat + rng.uniform(-2e-4, 2e-4, n); lon = clon + rng.uniform(-6e-4, 6e-4, n)
        x, y = scene_mod.to_local(FRAME, lon, lat)
        recs.append({"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y,
                     "h": 1000.0 + 0.02 * x + rng.normal(0, 0.02, n),
                     "yr": np.repeat(np.asarray(years, "f8"), 40) + rng.uniform(-0.04, 0.04, n)})
    calls = []
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: (calls.append(1), recs)[1])
    return calls


def test_identical_params_are_served_from_the_memo(scene_on_disk, counted_load):
    a = api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0)
    b = api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0)
    assert a == b
    assert len(counted_load) == 1, f"recomputed {len(counted_load)} times for identical params"


def test_different_params_are_not_confused(scene_on_disk, counted_load):
    api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0)
    api.scene_candidates(scene_on_disk, h3_res=8, delta_t=1.0)
    api.scene_candidates(scene_on_disk, h3_res=9, delta_t=0.5)
    api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0, ref_missions=["ATL06"])
    assert len(counted_load) == 4


def test_a_rewritten_scene_invalidates_the_memo(scene_on_disk, counted_load, tmp_path):
    api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0)
    p = tmp_path / "s1.json"
    doc = json.loads(p.read_text()); doc["z0"] = 1001.0
    p.write_text(json.dumps(doc))
    api.scene_candidates(scene_on_disk, h3_res=9, delta_t=1.0)
    assert len(counted_load) == 2, "a rewritten scene doc must not be answered from the memo"


def test_missing_scene_still_raises_keyerror(scene_on_disk, counted_load):
    with pytest.raises(KeyError):
        api.scene_candidates("nope", h3_res=9, delta_t=1.0)


def test_ref_missions_order_does_not_split_the_memo(scene_on_disk, counted_load):
    api.scene_candidates(scene_on_disk, ref_missions=["GLAS", "ATL06"])
    api.scene_candidates(scene_on_disk, ref_missions=["ATL06", "GLAS"])
    assert len(counted_load) == 1
