"""A collection that never flew over an area must not be offered there.

IceBridge (ILATM2/ICESSN) exists only at 60..90 N and -90..-53 S — CMR declares exactly those two bounding
rectangles on the collection. Offering it over Nepal produced a coverage error that read like a missing index and
invited the user to build one that would find nothing.
"""
import pytest

from aicesat import coverage

NEPAL = (85.35, 28.20, 85.62, 28.38)
GREENLAND = (-51.0, 69.0, -49.0, 69.4)
ANTARCTICA = (160.0, -80.0, 170.0, -78.0)
STRADDLE_60N = (-45.0, 58.0, -43.0, 62.0)     # half inside IceBridge's northern box


@pytest.mark.parametrize("key,bbox,want", [
    ("ICESSN", NEPAL, False),
    ("ICESSN", GREENLAND, True),
    ("ICESSN", ANTARCTICA, True),
    ("ICESSN", STRADDLE_60N, True),           # OVERLAP, not containment — the northern half really has data
    ("GLAS", NEPAL, True),                    # GLAH06 reaches +-86 and does have 41 granules over the corridor
    ("GLAS", GREENLAND, True),
    ("ATL06", NEPAL, True),
    ("ATL03", NEPAL, True),
])
def test_collection_can_cover(key, bbox, want):
    assert coverage.collection_can_cover(key, bbox) is want


def test_icessn_is_absent_from_the_temperate_latitudes_entirely():
    """The gap between the two boxes is the whole point: 53 S to 60 N has no IceBridge at any longitude."""
    for lat in (-40, -10, 0, 20, 45, 55):
        assert not coverage.collection_can_cover("ICESSN", (0.0, lat, 1.0, lat + 1.0)), f"lat {lat}"


def test_an_unknown_collection_defaults_to_possible():
    """A collection with no declared footprint must not be silently suppressed — unknown means 'do not gate'."""
    assert coverage.collection_can_cover("SOMETHING_NEW", NEPAL) is True


def test_check_coverage_reports_possible_and_skips_the_index_question():
    """`possible` is a different fact from `indexed`/`covered`: those two both mean "have we built it here", and
    neither distinguishes ground nobody flew from ground we have not indexed yet."""
    out = coverage.check_coverage(NEPAL)
    by = {c["key"]: c for c in out["collections"]}
    assert by["ICESSN"]["possible"] is False
    assert by["ICESSN"]["indexed"] is False and by["ICESSN"]["covered"] is False
    assert by["ICESSN"]["n_granules"] == 0
    assert by["GLAS"]["possible"] is True
    assert all("possible" in c for c in out["collections"]), "every collection must carry the flag"


def test_build_scene_skips_an_impossible_leg_rather_than_failing_it(monkeypatch):
    """The UI disables these, but every other caller — MCP tools, scripts, an older UI — needs the guard too."""
    import inspect

    from aicesat import api
    src = inspect.getsource(api.build_scene)
    assert "collection_can_cover" in src, "build_scene must drop legs the instrument never flew"
    i = src.index("collection_can_cover")
    assert "continue" in src[i:i + 400], "an impossible leg is skipped, not raised"


def test_the_ui_disables_rather_than_merely_labels(monkeypatch):
    """A label alone would leave the box checked and the build would still attempt the leg."""
    import pathlib
    src = (pathlib.Path(coverage.__file__).parent / "ui" / "explore.js").read_text()
    i = src.index("c.possible === false")
    window = src[i:i + 420]
    assert "box.checked = false" in window and "box.disabled = true" in window
    assert "not flown here" in window
