"""The demo's change level and its three-tool MCP surface."""
import h3
import pytest

from aicesat import api, server, survey

STORY = (69.1752, -49.3276)
CANDS = [
    {"h3": "a", "lat": 69.17, "lon": -49.33, "level": "medium", "confidence": 0.5, "span_years": 21.4, "n_bins": 18,
     "trend_cm_yr": -414.0, "why": "w", "components": {"plane_err_max_m": 0.4},
     "series": [{"year": 2005.1, "value_m": 0.0, "missions": ["GLAS"]}, {"year": 2026.5, "value_m": -87.0, "missions": ["ATL06"]}]},
    {"h3": "b", "lat": 69.16, "lon": -49.35, "level": "high", "confidence": 0.8, "span_years": 6.0, "n_bins": 7,
     "trend_cm_yr": -20.0, "why": "w", "components": {"plane_err_max_m": 0.1},
     "series": [{"year": 2020.2, "value_m": 0.0, "missions": ["ATL06"]}, {"year": 2026.2, "value_m": -1.2, "missions": ["ATL06"]}]},
    {"h3": "c", "lat": 69.02, "lon": -49.46, "level": "low", "confidence": 0.34, "span_years": 21.0, "n_bins": 10,
     "trend_cm_yr": -26361.0, "why": "Low confidence — gated", "components": {"plane_err_max_m": 3566.9},
     "series": [{"year": 2004.2, "value_m": 7384.7, "missions": ["GLAS"]}, {"year": 2019.2, "value_m": 0.0, "missions": ["ATL06"]}]},
]
NOTES = "no inter-campaign/inter-sensor bias adjustment and no GIA correction applied"


def test_small_radius_resolves_to_a_res5_hex_and_large_to_res4():
    assert h3.get_resolution(api.region_hex(*STORY, radius_km=10)) == 5
    assert h3.get_resolution(api.region_hex(*STORY, radius_km=30)) == 4


def test_hex_polygon_is_the_hex_boundary_in_lon_lat():
    poly = api.hex_polygon(api.region_hex(*STORY))
    assert len(poly) == 6
    assert min(p[1] for p in poly) < STORY[0] < max(p[1] for p in poly)
    assert min(p[0] for p in poly) < STORY[1] < max(p[0] for p in poly)


def test_region_scene_reuses_a_ready_scene_for_the_same_hex(monkeypatch):
    hx = api.region_hex(*STORY)
    monkeypatch.setattr(api, "scenes", lambda: [{"scene_id": "abc", "question": api.region_question(hx), "status": "ready"}])
    monkeypatch.setattr(api, "start_job", lambda *a, **k: pytest.fail("a ready area must not be rebuilt"))
    assert api.region_scene(hx) == {"scene_id": "abc", "job_id": None, "status": "ready"}


def test_region_scene_starts_exactly_one_build_with_the_region_collections(monkeypatch):
    hx, seen = api.region_hex(*STORY), []
    monkeypatch.setattr(api, "scenes", lambda: [])
    monkeypatch.setattr(api, "_region_jobs", {})
    monkeypatch.setattr(api, "start_job", lambda p, kind="scene": seen.append(p) or {"id": "j9", "scene_id": "new"})
    assert api.region_scene(hx)["scene_id"] == api.region_scene(hx)["scene_id"] == "new"
    assert len(seen) == 1
    assert seen[0]["with_atl03"] is False and seen[0]["with_atl06"] is True and seen[0]["question"] == api.region_question(hx)


@pytest.fixture
def ready(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "region_scene", lambda hx: {"scene_id": "s1", "job_id": None, "status": "ready"})
    monkeypatch.setattr(api, "scene_candidates", lambda sid, **kw: seen.update(kw) or
                        {"params": {"ref_missions": kw.get("ref_missions"), "notes": NOTES}, "candidates": CANDS})
    return seen


def test_low_confidence_cells_are_counted_not_listed(ready):
    out = api.elevation_change(*STORY)
    assert (out["n_cells"], out["n_reliable"], out["n_low_confidence"]) == (3, 2, 1)
    assert [c["h3"] for c in out["cells"]] == ["a", "b"]           # longest record first


def test_each_cell_reports_total_change_its_missions_and_its_slope_removal_error(ready):
    a = api.elevation_change(*STORY)["cells"][0]
    assert a["change_m"] == -87.0 and (a["first_year"], a["last_year"]) == (2005, 2026)
    assert a["missions"] == ["ATL06", "GLAS"] and a["trend_m_per_yr"] == -4.14
    assert a["slope_removal_err_m"] == 0.4


def test_the_change_level_uses_the_single_era_reference(ready):
    api.elevation_change(*STORY)
    assert ready["ref_missions"] == ["ATL06"] and ready["h3_res"] == 8


def test_caveats_travel_with_the_answer(ready):
    assert "GIA" in api.elevation_change(*STORY)["caveats"]


def test_a_build_still_running_says_so_and_how_to_wait(monkeypatch):
    monkeypatch.setattr(api, "region_scene", lambda hx: {"scene_id": "s2", "job_id": "j", "status": "loading"})
    out = api.elevation_change(*STORY, wait_s=0)
    assert out["status"] == "building" and "same arguments" in out["message"]


def test_elevation_change_tool_routes_the_app_to_the_region_level(ready):
    out = server.elevation_change(*STORY)
    assert out["view"] == "scene" and out["query"] == "level=region"
    assert out["open_url"].endswith("/#scene/s1?level=region")


def test_show_timeseries_defaults_to_the_change_levels_search(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "timeseries_cell", lambda sid, h, **kw: seen.update(kw) or {"scene_id": sid, "h3": h})
    out = server.show_timeseries("s1", "a")
    assert seen["h3_res"] == 8 and seen["ref_missions"] == ["ATL06"] and out["res"] == 8


def test_survey_coverage_summarises_per_mission_and_opens_the_globe(monkeypatch):
    monkeypatch.setattr(survey, "area_summary", lambda bbox: {k: {"passes": 3, "year_min": 2004, "year_max": 2026,
                                                                  "n_years": 3, "hexes": 1} for k in survey.MISSIONS})
    out = server.survey_coverage(*STORY, radius_km=20)
    assert out["view"] == "survey" and len(out["bbox"]) == 4
    assert set(out["missions"]) == set(survey.LABELS.values()) and out["indexed"] is True and out["long_record"] is True
