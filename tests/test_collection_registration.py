"""Every collection must be registered in every place a collection has to be registered.

Adding one touches a dozen files — coverage.collections(), the declared footprint, a scene colour, a lake product
name, a time-series label, a build_scene flag, an index directory, a default window, and the Explore panel's own
flag map. Miss one and the failure is quiet and late: the collection builds but paints black, or shows up unlabelled
in the Lake view, or silently never appears as a build option.

These iterate coverage.collections() rather than naming collections, so the NEXT one added is checked for free.
"""
import inspect
import pathlib
import re

import pytest

from aicesat import api, coverage, lake, regions, scene, timeseries

COLLECTIONS = coverage.collections()
KEYS = [c["key"] for c in COLLECTIONS]
UI = pathlib.Path(scene.__file__).parent / "ui"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_has_a_scene_colour(c):
    """No colour means the points render black-on-black and the legend swatch is blank."""
    assert c["mission"] in scene.COLORS, f"{c['key']}: scene.COLORS has no entry for mission {c['mission']!r}"
    rgb = scene.COLORS[c["mission"]]
    assert len(rgb) == 3 and all(0 <= v <= 255 for v in rgb)


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_has_a_lake_product_name(c):
    """lake.PRODUCTS is what the Lake view labels a mission partition with."""
    assert c["mission"] in lake.PRODUCTS, f"{c['key']}: lake.PRODUCTS has no entry for {c['mission']!r}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_has_a_time_series_label(c):
    assert c["mission"] in timeseries.MISSION_LABEL, f"{c['key']}: timeseries.MISSION_LABEL has no {c['mission']!r}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_flag_is_a_build_scene_parameter(c):
    """`flag` is the keyword Explore and the MCP tool pass; if build_scene has no such parameter it is ignored."""
    params = inspect.signature(api.build_scene).parameters
    assert c["flag"] in params, f"{c['key']}: build_scene has no {c['flag']!r} parameter"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_resolves_to_an_index_directory(c):
    """coverage._index_for drives the coverage check, index_status and the manifest rollup."""
    d, res, ym = coverage._index_for(c["key"])
    assert d is not None, f"{c['key']}: coverage._index_for returned no index dir"
    assert isinstance(res, int) and res > 0
    assert ym, f"{c['key']}: no year-month SQL expression, so by_month coverage would be empty"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_declares_where_its_instrument_flew(c):
    """Without a FOOTPRINTS entry collection_can_cover defaults to global, and the app offers a leg that cannot
    succeed — which is what 'do not offer a collection where the instrument never flew' exists to prevent."""
    assert c["key"] in coverage.FOOTPRINTS, f"{c['key']}: no declared footprint"
    for w, s, e, n in coverage.FOOTPRINTS[c["key"]]:
        assert -180 <= w < e <= 180 and -90 <= s < n <= 90


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_has_a_default_window_that_matches_its_epoch(c):
    start, end = c["window"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)
    assert start < end
    epoch_year = c["epoch"][:4]
    assert start[:4] <= epoch_year or epoch_year <= start[:4], c["epoch"]


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_is_offered_by_the_explore_panel(c):
    """explore.js keeps its OWN key->flag map. A collection missing from it can never be built from the UI."""
    src = (UI / "explore.js").read_text()
    m = re.search(r"const flagOf = \{([^}]*)\}", src)
    assert m, "explore.js no longer has a flagOf map; this test needs updating"
    assert f"{c['key']}:" in m.group(1), f"{c['key']}: absent from explore.js flagOf"
    assert f"'{c['flag']}'" in m.group(1), f"{c['key']}: explore.js does not map it to {c['flag']!r}"


def test_the_footprint_gate_actually_excludes_somewhere():
    """A sanity check on the mechanism itself: GEDI flies on the ISS (51.6 deg), so it must be refused over an ice
    sheet, and ICESSN only ever flew the poles, so it must be refused over Nepal."""
    greenland, nepal = (-45.0, 70.0, -44.9, 70.05), (85.46, 28.24, 85.57, 28.35)
    assert not coverage.collection_can_cover("GEDI", greenland)
    assert coverage.collection_can_cover("GEDI", nepal)
    assert not coverage.collection_can_cover("ICESSN", nepal)
    assert coverage.collection_can_cover("ICESSN", greenland)
