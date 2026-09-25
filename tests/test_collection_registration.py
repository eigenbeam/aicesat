"""Every collection must be registered in every place a collection has to be registered.

Adding one touches a dozen files — coverage.collections(), the declared footprint, a scene colour, a lake product
name, a time-series label, a build_scene flag, an index directory, a default window, and the Explore panel's own
flag map. Miss one and the failure is quiet and late: the collection builds but paints black, or shows up unlabelled
in the Lake view, or silently never appears as a build option.

These iterate coverage.collections() rather than naming collections, so the NEXT one added is checked for free.
Where a list could be derived from collections() instead (the scripts, the lake-limit resolutions, the job's flag
pass-through), it has been, so there is nothing left there to forget. What remains is checked here three ways: by
membership, by the naming convention a collection's index follows (coverage.index_module / index_version /
build_script), and by running build_scene with one collection at a time, since its internals cannot be inspected.
"""
import importlib
import inspect
import pathlib
import re
import time

import numpy as np
import pytest

from aicesat import api, cache, coverage, lake, regions, scene, timeseries

COLLECTIONS = coverage.collections()
KEYS = [c["key"] for c in COLLECTIONS]
UI = pathlib.Path(scene.__file__).parent / "ui"
ROOT = pathlib.Path(__file__).resolve().parents[1]


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
    assert start[:4] == c["epoch"][:4], f"{c['key']}: window starts {start}, epoch says {c['epoch']}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_is_offered_by_the_explore_panel(c):
    """explore.js keeps its OWN key->flag map. A collection missing from it can never be built from the UI."""
    src = (UI / "explore.js").read_text()
    m = re.search(r"const flagOf = \{([^}]*)\}", src)
    assert m, "explore.js no longer has a flagOf map; this test needs updating"
    assert f"{c['key']}:" in m.group(1), f"{c['key']}: absent from explore.js flagOf"
    assert f"'{c['flag']}'" in m.group(1), f"{c['key']}: explore.js does not map it to {c['flag']!r}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_build_progress_reads_the_plan_for_every_collection(c):
    """hasPlan decides whether the build card trusts the submitted plan or falls back to sniffing log lines. A
    collection its list leaves out makes a build of only that collection take the log-sniffing path."""
    src = (UI / "explore.js").read_text()
    m = re.search(r"const hasPlan = ([^;]*);", src)
    assert m, "explore.js no longer has a hasPlan expression; this test needs updating"
    assert "flagOf" in m.group(1) or f"'{c['flag']}'" in m.group(1), f"{c['key']}: absent from explore.js hasPlan"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_logs_reach_the_lake_activity_log(c):
    """The Lake page's running log only captures the loggers logbuf lists by name."""
    from aicesat import logbuf
    assert f"aicesat.{c['key'].lower()}" in logbuf._LOGGERS, f"{c['key']}: its logger is not in logbuf._LOGGERS"


def test_the_footprint_gate_actually_excludes_somewhere():
    """A sanity check on the mechanism itself: GEDI flies on the ISS (51.6 deg), so it must be refused over an ice
    sheet, and ICESSN only ever flew the poles, so it must be refused over Nepal."""
    greenland, nepal = (-45.0, 70.0, -44.9, 70.05), (85.46, 28.24, 85.57, 28.35)
    assert not coverage.collection_can_cover("GEDI", greenland)
    assert coverage.collection_can_cover("GEDI", nepal)
    assert not coverage.collection_can_cover("ICESSN", nepal)
    assert coverage.collection_can_cover("ICESSN", greenland)


# ---- the naming convention: what a collection's key promises -----------------------------------------------------
@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_window_is_its_regions_default(c):
    assert tuple(c["window"]) == getattr(regions, f"DEFAULT_{c['key']}_WINDOW", None), \
        f"{c['key']}: regions.DEFAULT_{c['key']}_WINDOW is missing or differs from the collection's window"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_collection_index_follows_the_naming_convention(c):
    """Scripts find a collection's index module, schema version and build script from its key alone."""
    mod = coverage.index_module(c["key"])
    version, schema_key = coverage.index_version(c["key"])
    assert version and schema_key.startswith(b"aicesat_")
    d, res, _ym = coverage._index_for(c["key"])
    if c["key"] != "ATL03":                     # ATL03's index predates the convention (aicesat.index, H3_RES)
        assert getattr(mod, f"{c['key']}_RES") == res and mod._index_dir(res) == d
    assert (ROOT / coverage.build_script(c["key"])).is_file(), f"{c['key']}: no {coverage.build_script(c['key'])}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_api_index_source_agrees_with_coverage(c):
    want = coverage._index_for(c["key"])[:2]
    assert api._index_source(c["key"]) == want and api._index_source(c["mission"]) == want


# ---- the UI's own per-mission maps ------------------------------------------------------------------------------
def _literal(src: str, name: str) -> str:
    """The body of a `const <name> = {...};` or `[...];` literal."""
    m = re.search(rf"const {name} = ([\[{{].*?[\]}}]);", src, re.S)
    assert m, f"no `const {name}` literal found; this test needs updating"
    return m.group(1)


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_scene_view_has_a_footprint_for_the_mission(c):
    assert re.search(rf"\b{c['mission']}:", _literal((UI / "scene.js").read_text(), "FOOTPRINT_M")), \
        f"{c['key']}: scene.js FOOTPRINT_M has no {c['mission']}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_time_series_panel_knows_the_mission(c):
    src = (UI / "tspanel.js").read_text()
    for name in ("MISSIONS", "MISSION_ORDER", "MISSION_COLORS"):
        assert re.search(rf"\b{c['mission']}\b", _literal(src, name)), f"{c['key']}: tspanel.js {name} has no {c['mission']}"
    rgb = re.search(rf"\b{c['mission']}:\s*\[([^\]]*)\]", _literal(src, "MISSION_COLORS"))
    assert [int(v) for v in rgb.group(1).split(",")] == list(scene.COLORS[c["mission"]]), \
        f"{c['key']}: tspanel.js MISSION_COLORS no longer mirrors scene.COLORS"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_build_progress_has_a_row_for_the_collection(c):
    assert f"['{c['key']}'," in _literal((UI / "explore.js").read_text(), "ALL"), \
        f"{c['key']}: explore.js progress list ALL has no row, so its build step never shows"


# ---- the flag reaches build_scene from both entry points ---------------------------------------------------------
@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_ui_extract_passes_the_flag_to_the_job(c, monkeypatch):
    from aicesat import server
    assert c["flag"] in inspect.signature(server.ui_extract).parameters, f"{c['key']}: ui_extract has no {c['flag']}"
    seen = {}
    monkeypatch.setattr(api, "start_job", lambda params, kind="scene": seen.update(params) or {"id": "j", "scene_id": "s"})
    server.ui_extract(bbox=[85.46, 28.24, 85.57, 28.35], **{c["flag"]: True})
    assert seen.get(c["flag"]) is True, f"{c['key']}: ui_extract does not pass {c['flag']} to the job"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_a_build_job_passes_the_flag_to_build_scene(c, monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "build_scene", lambda *a, **k: seen.update(k) or {"scene_id": "s"})
    monkeypatch.setattr(api, "_widget_url", lambda sid: "", raising=False)
    job = api.start_job({"bbox": [85.46, 28.24, 85.57, 28.35], c["flag"]: True})
    deadline = time.time() + 5
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert job["status"] == "done", job.get("error")
    assert seen.get(c["flag"]) is True, f"{c['key']}: start_job does not pass {c['flag']} to build_scene"


# ---- build_scene, one collection at a time (its LEGS table and error/series ordering are internal) ----------------
SURFACE = {"x0": 0.0, "y0": 0.0, "cell": 100.0, "nx": 2, "ny": 2, "z": [1.0, 2.0, 3.0, 4.0], "source": "MockDEM",
           "attribution": "mock", "is_dem": True, "n_cells_observed": 4, "n_cells_extrapolated": 0, "nodata_cells": 0,
           "note": "mock surface"}


def _inside_footprint(key):
    w, s, e, n = coverage.FOOTPRINTS[key][0]
    lon, lat = (w + e) / 2, (s + n) / 2
    return [lon, lat, lon + 0.05, lat + 0.03]


@pytest.fixture
def one_leg(monkeypatch, tmp_path):
    """build_scene with exactly one collection enabled and its extract stubbed; every other leg, the DEM, the lake
    and the scene registry are redirected or stubbed so nothing touches the network or the real data directory."""
    from aicesat import dem
    (tmp_path / "cache").mkdir(); (tmp_path / "scenes").mkdir()
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    monkeypatch.setattr(api, "REGISTRY", tmp_path / "scenes" / "registry.json")
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "meta.duckdb")
    monkeypatch.setattr(dem, "surface_for_frame", lambda *a, **k: dict(SURFACE))

    def run(c, fail=False):
        bbox = _inside_footprint(c["key"])

        def _extract(*a, **k):
            if fail:
                raise RuntimeError(f"{c['key']} stub failure")
            rng = np.random.default_rng(0)
            arrays = {"lon": bbox[0] + rng.uniform(0, 0.05, 12), "lat": bbox[1] + rng.uniform(0, 0.03, 12),
                      "h": 1000.0 + rng.normal(0, 1, 12)}
            return arrays, {"cache_key": f"{c['key']}-k", "n": 12, "campaigns": ["L2A"], "years": [2011], "access": {}}

        monkeypatch.setattr(importlib.import_module(f"aicesat.{c['key'].lower()}"), "extract", _extract)
        flags = {x["flag"]: x["key"] == c["key"] for x in COLLECTIONS}
        return api.build_scene(bbox, None, "registration test", with_imagery=False, **flags)
    return run


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_build_scene_carries_the_collection_into_the_scene(c, one_leg):
    doc = one_leg(c)
    assert list(doc["series"]) == [c["mission"]], f"{c['key']}: build_scene produced {list(doc['series'])}"


@pytest.mark.parametrize("c", COLLECTIONS, ids=KEYS)
def test_build_scene_names_the_collection_when_its_leg_fails(c, one_leg):
    with pytest.raises(RuntimeError) as e:
        one_leg(c, fail=True)
    assert f"{c['mission']}: RuntimeError: {c['key']} stub failure" in str(e.value), str(e.value)
