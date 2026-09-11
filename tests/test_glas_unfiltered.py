"""GLAS reaches the scene as delivered — no mission is filtered at finalize.

add_series used to run a neighbourhood-median cloud gate on GLAS and nothing else, which over Langtang deleted 45.7%
of the shots, 1,174 of them good (the measurement and the rejected replacements are recorded above add_series). These
pin the contract that removal established, because it is load-bearing for the stream: append_partial's buffer and the
finalized array are now the same points, so a `reset` frame announces a new inode and never a changed point set.
"""
import numpy as np

from aicesat import scene

BBOX = (85.3319, 28.1766, 85.6589, 28.3904)


def _arrays(h):
    n = len(h)
    return {"lon": np.linspace(85.40, 85.60, n), "lat": np.linspace(28.20, 28.35, n),
            "h": np.asarray(h, "f8"), "t": np.array(["2005-01-01"] * n, dtype="datetime64[ms]")}


def test_glas_shots_are_not_dropped_even_when_absurd(tmp_path, monkeypatch):
    """A 3.3 km cloud return is a real ICESat-1 measurement; the scene draws it rather than hiding it."""
    monkeypatch.setattr(scene.cache, "SCENE_DIR", tmp_path)
    doc = scene.new_scene("t", BBOX)
    h = [4000.0, 4010.0, 4020.0, 7300.0, 4030.0]      # one shot 3.3 km above the terrain
    scene.add_series(doc, "GLAS", _arrays(h), {"mission": "GLAS"}, "k")
    assert doc["series"]["GLAS"]["n"] == len(h)
    assert doc["series"]["GLAS"]["n_extracted"] == len(h)


def test_no_mission_is_filtered_at_finalize(tmp_path, monkeypatch):
    """Every mission's series carries exactly the points it was handed — the property the stream's reset relies on."""
    monkeypatch.setattr(scene.cache, "SCENE_DIR", tmp_path)
    doc = scene.new_scene("t", BBOX)
    for mission in ("GLAS", "ATL06", "ICESSN", "ICESAT2"):
        a = _arrays([4000.0, 9999.0, 4020.0])
        scene.add_series(doc, mission, a, {"mission": mission}, f"k-{mission}")
        assert doc["series"][mission]["n"] == 3, f"{mission} lost points at finalize"


def test_the_cache_key_is_the_extract_key_not_a_cleaned_variant(tmp_path, monkeypatch):
    """GLAS used to be re-saved under `<key>-clean` and its series pointed at that. With no filter there is one
    array and one key, so the series' provenance points straight at the extract the user can re-fetch."""
    monkeypatch.setattr(scene.cache, "SCENE_DIR", tmp_path)
    doc = scene.new_scene("t", BBOX)
    scene.add_series(doc, "GLAS", _arrays([4000.0, 4010.0]), {"mission": "GLAS"}, "abc123")
    assert doc["series"]["GLAS"]["cache_key"] == "abc123"


def test_no_outlier_bookkeeping_is_reported_any_more(tmp_path, monkeypatch):
    """Provenance must not claim a filter that no longer runs."""
    monkeypatch.setattr(scene.cache, "SCENE_DIR", tmp_path)
    doc = scene.new_scene("t", BBOX)
    scene.add_series(doc, "GLAS", _arrays([4000.0, 9999.0]), {"mission": "GLAS"}, "k")
    m = doc["series"]["GLAS"]["meta"]
    for gone in ("n_outliers_dropped", "n_outlier_judged", "outlier_rule", "outlier_reference"):
        assert gone not in m, f"{gone} still advertised"
