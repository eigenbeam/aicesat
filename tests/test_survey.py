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


GLAS_DAYS = {"GLAH06_a": "20040301", "GLAH06_b": "20081001"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """The manifests as coverage_hexes reads them (one parquet per mission, DuckDB aggregates it), and the same rows
    as area_summary reads them (_rows)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    paths = {}
    for key, rows in ROWS.items():
        paths[key] = tmp_path / f"{key}.parquet"
        pq.write_table(pa.table({"h3_cell": pa.array([r[0] for r in rows], pa.uint64()),
                                 "granule": [r[1] for r in rows], "ym": [r[2] for r in rows]}), paths[key])
    monkeypatch.setattr(survey, "_manifest", lambda key: paths[key])
    monkeypatch.setattr(survey, "_rows", lambda key: ROWS[key])
    real = survey._day_of   # GLAH06 names carry no date: the index's gdate stands in, faked here
    monkeypatch.setattr(survey, "_day_of", lambda key: GLAS_DAYS.get if key == "GLAS" else real(key))
    monkeypatch.setattr(survey, "_glas_days", lambda: GLAS_DAYS)
    # GLAS and ATL06 claim exactly C5; IceBridge claims C5 through a coarse ancestor (the claim set is compacted)
    monkeypatch.setattr(survey, "_packed", lambda key: {h3.cell_to_parent(C5, 3)} if key == "ICESSN" else {C5})
    survey._HEX_MEMO.clear()
    return paths


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


def test_the_sql_parent_is_h3s_parent():
    """The roll-up runs in DuckDB on the integer ids: the bit arithmetic must equal h3.cell_to_parent at every res."""
    import duckdb
    cells = [h3.latlng_to_cell(la, lo, 5) for la, lo in ((69.17, -49.33), (72.58, -38.5), (60.1, -45.0), (-12.0, 130.0))]
    for res in range(0, 6):
        got = duckdb.sql(f"SELECT {survey._parent_sql(res)} FROM (SELECT unnest(?::UBIGINT[]) AS h3_cell)",
                         params=[[h3.str_to_int(c) for c in cells]]).fetchall()
        assert [h3.int_to_str(int(r[0])) for r in got] == [h3.cell_to_parent(c, res) for c in cells], res


def test_a_repeat_call_is_served_from_the_memo_until_a_manifest_changes(fake, monkeypatch):
    calls = []
    real = survey._aggregate
    monkeypatch.setattr(survey, "_aggregate", lambda key, res: calls.append((key, res)) or real(key, res))
    first = survey.coverage_hexes(5)
    assert survey.coverage_hexes(5) == first and len(calls) == 3
    import os, time
    t = time.time() + 5
    os.utime(fake["ATL06"], (t, t))                            # the ATL06 manifest was rebuilt
    survey.coverage_hexes(5)
    assert len(calls) == 6


def test_a_building_index_does_not_rebuild_its_manifest_on_every_call(tmp_path, monkeypatch):
    """During a build the index dir changes every second; coverage readers may take a manifest up to max_stale_s old."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from aicesat import coverage
    d = tmp_path / "idx"; d.mkdir()
    pq.write_table(pa.table({"h3_cell": pa.array([I(C5)], pa.uint64()), "granule": ["g1"], "gdate": ["20040301"]}),
                   d / "g1.parquet")
    ym = "substr(gdate,1,4) || '-' || substr(gdate,5,2)"
    m = coverage._ensure_manifest(d, ym)
    built = m.stat().st_mtime_ns
    pq.write_table(pa.table({"h3_cell": pa.array([I(C5B)], pa.uint64()), "granule": ["g2"], "gdate": ["20050301"]}),
                   d / "g2.parquet")                         # the build lands another granule
    assert coverage._ensure_manifest(d, ym, max_stale_s=300).stat().st_mtime_ns == built
    assert coverage._ensure_manifest(d, ym).stat().st_mtime_ns != built   # strict readers still rebuild
