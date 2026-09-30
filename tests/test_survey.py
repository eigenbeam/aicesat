"""Level 1 of the demo ladder: per-hex mission coverage from the index manifests (no NASA, no lake)."""
import h3
import pytest

from aicesat import survey

C5 = h3.latlng_to_cell(69.175, -49.328, 5)          # the story area
C5B = h3.latlng_to_cell(69.60, -48.20, 5)           # a different res-5 hex
I = h3.str_to_int
ROWS = {
    "GLAS": [(I(C5), "GLAH06_a", "2004-03"), (I(C5), "GLAH06_b", "2008-10"), (I(C5B), "GLAH06_a", "2004-03")],
    # two ~4-minute slices of one flight: two granules, one day
    "ICESSN": [(I(C5), "ILATM2_20110505_134446_smooth_nadir3seg_50pt.csv", "2011-05"),
               (I(C5), "ILATM2_20110505_134841_smooth_nadir3seg_50pt.csv", "2011-05")],
    "ATL06": [(I(C5), "ATL06_20190401010101_02770103_007_01.h5", "2019-04"),
              (I(C5), "ATL06_20260101010101_02790106_007_01.h5", "2026-01"),
              (I(C5), "ATL06_20260101010101_02790106_007_01.h5", "2026-01")],
}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(survey, "_rows", lambda key: ROWS[key])
    real = survey._day_of   # GLAH06 names carry no date: the index's gdate stands in, faked here
    monkeypatch.setattr(survey, "_day_of", lambda key: {"GLAH06_a": "20040301", "GLAH06_b": "20081001"}.get
                        if key == "GLAS" else real(key))
    # GLAS and ATL06 claim exactly C5; IceBridge claims C5 through a coarse ancestor (the claim set is compacted)
    monkeypatch.setattr(survey, "_packed", lambda key: {h3.cell_to_parent(C5, 3)} if key == "ICESSN" else {C5})


def _by(out):
    return {h["h3"]: h for h in out["hexes"]}


def test_counts_distinct_passes_and_years_per_mission(fake):
    hx = _by(survey.coverage_hexes(5))[C5]
    assert hx["missions"]["GLAS"] == {"passes": 2, "days": 2, "year_min": 2004, "year_max": 2008, "n_years": 2,
                                      "claimed": True}
    assert hx["missions"]["ATL06"]["passes"] == 2               # a duplicated manifest row is one pass
    assert hx["n_missions"] == 3 and hx["claimed"] is True


def test_days_count_acquisition_dates_not_granules(fake):
    m = _by(survey.coverage_hexes(5))[C5]["missions"]
    assert (m["ICESSN"]["passes"], m["ICESSN"]["days"]) == (2, 1)      # one flight, two granule slices
    assert (m["ATL06"]["passes"], m["ATL06"]["days"]) == (2, 2)


def test_claim_is_per_mission_and_a_hex_builds_only_if_every_mission_is_claimed(fake):
    hx = _by(survey.coverage_hexes(5))[C5B]
    assert hx["missions"]["GLAS"]["claimed"] is False
    assert hx["claimed"] is False


def test_coarser_res_rolls_children_up_without_double_counting(fake):
    hx = _by(survey.coverage_hexes(3))[h3.cell_to_parent(C5, 3)]
    assert hx["missions"]["GLAS"]["passes"] == 2                # GLAH06_a crosses both children: counted once


def test_finer_than_the_index_is_refused(fake):
    with pytest.raises(ValueError):
        survey.coverage_hexes(6)


def test_bbox_keeps_hexes_by_centre(fake):
    lat, lon = h3.cell_to_latlng(C5)
    out = survey.coverage_hexes(5, bbox=[lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01])
    assert [h["h3"] for h in out["hexes"]] == [C5]


def test_area_summary_counts_distinct_passes_over_the_area(fake):
    lat, lon = h3.cell_to_latlng(C5)
    s = survey.area_summary(survey.area_bbox(lat, lon, 5))
    assert s["GLAS"]["passes"] == 2 and s["ICESSN"]["year_min"] == 2011 and s["ATL06"]["year_max"] == 2026
