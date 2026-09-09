"""The model-facing projection of the candidate search.

timeseries.candidates() returns EVERY qualifying cell with its full series and hex geometry -- 42 KB / 36 cells on
one real scene, 159 KB / 133 cells on another. That is the right answer for the UI and the wrong one for a model's
context window. So the response is slimmed here, at the transport edge, and never in the search itself
(tests/test_no_caps.py guards that; a max_candidates=60 default was retired for silently dropping cells).

The rule this file pins: slimming must SAY what it dropped. n_candidates_total is always the true count."""
import json

import numpy as np
import pytest

from aicesat import api, cache
from aicesat import scene as scene_mod
from aicesat import timeseries

FRAME = scene_mod.local_frame((-45.5, 71.8, -44.5, 72.2))


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """A scene doc on disk plus 12 synthetic cells, each observed at four epochs."""
    import h3
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path)
    (tmp_path / "s1.json").write_text(json.dumps({"frame": FRAME, "z0": 1000.0, "series": {}}))
    api._CAND_MEMO.clear()

    centre = h3.latlng_to_cell(72.0, -45.0, 9)
    cells = [centre] + list(h3.grid_disk(centre, 2))[:11]
    rng = np.random.default_rng(0)
    recs = []
    for mission, years in (("GLAS", [2005.05]), ("ATL06", [2019.5, 2020.5, 2021.5])):
        lats, lons, yrs = [], [], []
        for cell in cells:
            clat, clon = h3.cell_to_latlng(cell)
            for y in years:
                lats.append(clat + rng.uniform(-2e-4, 2e-4, 40))
                lons.append(clon + rng.uniform(-6e-4, 6e-4, 40))
                yrs.append(np.full(40, y) + rng.uniform(-0.04, 0.04, 40))
        lat = np.concatenate(lats); lon = np.concatenate(lons); yr = np.concatenate(yrs)
        x, y = scene_mod.to_local(FRAME, lon, lat)
        recs.append({"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y,
                     "h": 1000.0 + 0.02 * x + rng.normal(0, 0.02, x.size), "yr": yr})
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    yield "s1"
    api._CAND_MEMO.clear()


def test_summary_drops_the_bulk_and_keeps_the_verdict(scene):
    out = api.timeseries_candidates(scene, limit=5)
    row = out["candidates"][0]
    assert not ({"series", "xy", "components", "center"} & set(row)), sorted(row)
    for k in ("rank", "h3", "lat", "lon", "level", "confidence", "n_bins", "span_years", "trend_cm_yr", "why"):
        assert k in row, k


def test_truncation_reports_what_it_dropped(scene):
    full = api.scene_candidates(scene)
    out = api.timeseries_candidates(scene, limit=5)
    assert len(full["candidates"]) > 5, "fixture must produce more cells than the limit"
    assert out["n_candidates_total"] == len(full["candidates"])
    assert out["returned"] == len(out["candidates"]) == 5
    assert out["n_candidates_total"] > out["returned"]


def test_summary_is_small_enough_for_a_context_window(scene):
    out = api.timeseries_candidates(scene, limit=10)
    assert len(json.dumps(out)) < 6000, len(json.dumps(out))


def test_ranks_are_the_search_order_and_are_one_based(scene):
    full = api.scene_candidates(scene)["candidates"]
    out = api.timeseries_candidates(scene, limit=5)
    assert [c["rank"] for c in out["candidates"]] == [1, 2, 3, 4, 5]
    assert [c["h3"] for c in out["candidates"]] == [c["h3"] for c in full[:5]]


def test_limit_none_returns_every_cell(scene):
    out = api.timeseries_candidates(scene, limit=None)
    assert out["returned"] == out["n_candidates_total"]


def test_the_caveat_travels_with_the_answer(scene):
    out = api.timeseries_candidates(scene, limit=3)
    assert "GIA" in out["params"]["notes"] and "bias" in out["params"]["notes"]


def test_cell_lookup_returns_the_series_and_the_breakdown(scene):
    top = api.timeseries_candidates(scene, limit=1)["candidates"][0]
    cell = api.timeseries_cell(scene, top["h3"])
    assert cell["h3"] == top["h3"] and cell["rank"] == 1
    assert cell["series"] and all({"year", "value_m", "mad_m", "n", "missions"} <= set(p) for p in cell["series"])
    assert "scores" in cell["components"]
    assert cell["trend_cm_yr"] == top["trend_cm_yr"]
    assert "xy" not in cell, "scene-local render geometry is for the viewer, not the model"


def test_unknown_cell_names_the_params_that_would_have_produced_it(scene):
    with pytest.raises(ValueError) as e:
        api.timeseries_cell(scene, "890689b5433ffff", h3_res=9, delta_t=1.0)
    msg = str(e.value)
    assert "890689b5433ffff" in msg and "h3_res" in msg and "delta_t" in msg


def test_unknown_scene_raises_keyerror(scene):
    with pytest.raises(KeyError):
        api.timeseries_candidates("nope")


# --- the MCP edge: an anticipated failure must reach the model with its message ---------------------------------
def test_tool_errors_carry_their_message_to_the_model(scene, monkeypatch):
    """The SDK treats any exception other than ToolError as a crash and WITHHOLDS its text -- the model reads only
    "Error executing tool <name>". Both of these failures are ones the caller can fix from the message (a stale
    cell id, a wrong scene id), so they are useless generic and must stay ToolError."""
    from mcp.server.mcpserver.exceptions import ToolError
    from aicesat import server

    monkeypatch.setattr(server, "_widget_url", lambda sid: "", raising=False)

    with pytest.raises(ToolError) as e:
        server.show_timeseries(scene, "8900000000bffff")
    assert "8900000000bffff" in str(e.value) and "h3_res" in str(e.value)

    with pytest.raises(ToolError) as e:
        server.show_timeseries("nosuchscene", "8900000000bffff")
    assert "nosuchscene" in str(e.value) and "list_scenes" in str(e.value)

    with pytest.raises(ToolError) as e:
        server.find_timeseries_candidates("nosuchscene")
    assert "nosuchscene" in str(e.value)


def test_tools_route_the_app_to_the_timeseries_view(scene):
    """structuredContent.view is what app.js routes on. Without it the bare scene_id lands on the 3-D scene view,
    whose point cloud cannot travel the MCP App transport at all."""
    from aicesat import server

    d = server.find_timeseries_candidates(scene, limit=2)
    assert d["view"] == "ts" and d["url"].endswith("/#ts/" + scene)

    h3 = d["candidates"][0]["h3"]
    c = server.show_timeseries(scene, h3)
    assert c["view"] == "ts" and c["select"] == h3 and c["url"].endswith("?sel=" + h3)
