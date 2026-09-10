"""Markers and the scene's coordinate helpers. The JS half runs under node via this shim, like test_tool_routing."""
import shutil
import subprocess

import pytest

from aicesat import scene


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scene_coordinate_helpers_js():
    r = subprocess.run(["node", "tests/test_scene_markers.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_normalize_markers_keeps_the_good_and_drops_the_bad():
    """A pin in the wrong place is worse than no pin — the viewer reads it as a location — so a marker that cannot
    be trusted is dropped, never coerced."""
    out = scene.normalize_markers([
        {"lon": 85.52515462, "lat": 28.28531746, "label": "collapse source"},
        {"lon": "85.5", "lat": "28.2"},                 # numeric strings are fine
        {"lon": 85.5},                                  # no lat
        {"lat": 28.2},                                  # no lon
        {"lon": "east", "lat": 28.2},                   # unparseable
        {"lon": 999.0, "lat": 0.0},                     # out of range
        {"lon": 0.0, "lat": 91.0},                      # out of range
        None,
    ])
    assert out == [{"lon": 85.52515462, "lat": 28.28531746, "label": "collapse source"},
                   {"lon": 85.5, "lat": 28.2, "label": ""}]


def test_normalize_markers_caps_the_label():
    out = scene.normalize_markers([{"lon": 0, "lat": 0, "label": "x" * 200}])
    assert len(out[0]["label"]) == 60


def test_normalize_markers_accepts_nothing():
    assert scene.normalize_markers(None) == [] and scene.normalize_markers([]) == []


def test_new_scene_carries_markers():
    doc = scene.new_scene("abc", (85.44, 28.21, 85.62, 28.37),
                          markers=[{"lon": 85.525, "lat": 28.285, "label": "source"}])
    assert doc["markers"] == [{"lon": 85.525, "lat": 28.285, "label": "source"}]
    assert scene.new_scene("abc", (85.44, 28.21, 85.62, 28.37))["markers"] == []


def test_scene_part_ships_markers_to_the_widget():
    """The widget replaces its whole scene object from each poll, so a field missing from scene_part is a field the
    renderer never sees — which is how this would silently do nothing."""
    import pathlib
    src = pathlib.Path(scene.__file__).parent / "api.py"
    text = src.read_text()
    i = text.index("def scene_part")
    assert '"markers"' in text[i:i + 2500], "scene_part must include markers or the renderer never gets them"
