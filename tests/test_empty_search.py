"""An empty granule search must not be recorded as indexed ground (#30).

Every builder used to treat "the search returned no granules" as "nothing left to do" and stamp the coverage claim,
so the area read as indexed and a scene there reported "no data" rather than "the search found nothing". An empty
search can be real (no pass ever crossed the ground) or a search that quietly matched nothing, and one search cannot
tell the two apart, so it is recorded as what it is and reported, never claimed.

Every test here points the index directories at tmp_path: the builders' indexed_*_granules() helpers delete
stale-schema files, so running them against the real data directory is not an option.
"""
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

from aicesat import auth, cache, coverage, index, planner

BBOX = (-50.0, 69.0, -49.97, 69.015)          # small: a few dozen res-9 claim cells, pure h3 arithmetic
OTHER = (-50.2, 69.2, -50.17, 69.215)
WINDOW = ("2019-01-01", "2020-01-01")
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _manifest(d):
    return json.loads((Path(d) / "_build.json").read_text())


# ---- the choke point every builder stamps through --------------------------------------------------------------
def test_zero_granules_records_the_search_and_claims_nothing(tmp_path):
    d = tmp_path / "idx"
    fine = planner.coverage_cells(BBOX)
    doc = index.write_build_manifest(d, BBOX, 5, WINDOW, 0, cells=fine)
    assert index.manifest_cells(d) == set() and not index.covers_cells(d, fine)
    assert doc["cells"] == []
    rec = index.empty_search_covering(d, fine)
    assert rec is not None and rec["window"] == list(WINDOW) and rec["requested"] == list(BBOX)


def test_an_empty_search_leaves_an_existing_claim_alone(tmp_path):
    d = tmp_path / "idx"
    a, b = planner.coverage_cells(BBOX), planner.coverage_cells(OTHER)
    index.write_build_manifest(d, BBOX, 5, WINDOW, 3, cells=a)
    claim = index.manifest_cells(d)
    index.write_build_manifest(d, OTHER, 5, WINDOW, 0, cells=b)
    assert index.manifest_cells(d) == claim and index.covers_cells(d, a) and not index.covers_cells(d, b)
    assert index.empty_search_covering(d, b) is not None and index.empty_search_covering(d, a) is None
    index.write_build_manifest(d, BBOX, 5, WINDOW, 3, cells=a)           # a later real claim keeps the history
    assert len(_manifest(d)["empty_searches"]) == 1


def test_coverage_gap_says_the_search_found_nothing(tmp_path):
    d = tmp_path / "idx"
    index.write_build_manifest(d, OTHER, 5, WINDOW, 0, cells=planner.coverage_cells(OTHER))
    msg = coverage.coverage_gap(d, OTHER)
    assert msg and "found no granules" in msg and "2019-01-01" in msg
    assert coverage.searched_empty(d, OTHER)["window"] == list(WINDOW)
    assert coverage.searched_empty(d, BBOX) is None                      # a different area was not searched


def test_check_coverage_reports_searched_empty(tmp_path, monkeypatch):
    d = tmp_path / "glas"
    index.write_build_manifest(d, OTHER, 5, WINDOW, 0, cells=planner.coverage_cells(OTHER))
    monkeypatch.setattr(coverage, "_index_for", lambda key: (d, 5, "substr(gdate,1,4)") if key == "GLAS" else (None, None, None))
    monkeypatch.setattr(coverage, "collection_can_cover", lambda key, bbox: True)
    rows = {r["key"]: r for r in coverage.check_coverage(OTHER)["collections"]}
    assert rows["GLAS"]["indexed"] is False and rows["GLAS"]["covered"] is False
    assert rows["GLAS"]["searched_empty"]["window"] == list(WINDOW)
    assert all(r["searched_empty"] is None for k, r in rows.items() if k != "GLAS")


def test_an_empty_search_result_is_not_cached(tmp_path, monkeypatch):
    import earthaccess

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    answers = [[], [{"meta": {"native-id": "G1"}}]]

    class _Q:
        def get_all(self):
            return answers.pop(0)

    monkeypatch.setattr(coverage, "_cloud_query", lambda **kw: _Q())
    monkeypatch.setattr(earthaccess, "search_data", lambda **kw: [])
    monkeypatch.setattr(coverage, "dedup_granules", lambda g: list(g))
    assert coverage.search("GLAH06", "034", BBOX, WINDOW) == []
    assert not list(tmp_path.glob("cmr_*.pkl"))
    assert coverage.search("GLAH06", "034", BBOX, WINDOW) == [{"meta": {"native-id": "G1"}}]


# ---- every builder, end to end against a real (temporary) _build.json ------------------------------------------
@pytest.fixture
def empty_search(tmp_path, monkeypatch):
    from aicesat import index_atl06, index_gedi, index_glas, index_icessn

    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "atl06")
    monkeypatch.setattr(index_gedi, "GEDI_INDEX_DIR", tmp_path / "gedi")
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "glas")
    monkeypatch.setattr(index_icessn, "ICESSN_INDEX_DIR", tmp_path / "icessn")
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "atl03")
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(coverage, "search", lambda *a, **k: [])
    monkeypatch.setattr(coverage, "build_manifest", lambda c: None)
    return tmp_path


def _run_script(name, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [name] + argv)
    try:
        runpy.run_path(str(SCRIPTS / name), run_name="__main__")
    except SystemExit as e:
        assert e.code in (None, 0), f"{name} exited {e.code}"


@pytest.mark.parametrize("builder", ["ATL06", "GEDI", "GLAS", "ICESSN", "ATL03"])
def test_no_builder_claims_an_area_its_search_found_empty(builder, empty_search, monkeypatch):
    from aicesat import build_atl06, build_gedi, index_atl06, index_gedi, index_glas, index_icessn

    args = [str(v) for v in BBOX]
    if builder == "ATL06":
        out = build_atl06.build_bbox(BBOX, window=WINDOW)
        d = index_atl06._index_dir(index_atl06.ATL06_RES)
        assert out["claimed"] is False
    elif builder == "GEDI":
        out = build_gedi.build_bbox(BBOX, window=WINDOW)
        d = index_gedi._index_dir(index_gedi.GEDI_RES)
        assert out["claimed"] is False
    elif builder == "GLAS":
        _run_script("build_glas_index.py", args, monkeypatch)
        d = index_glas._index_dir(index_glas.GLAS_RES)
    elif builder == "ICESSN":
        _run_script("build_icessn_index.py", args, monkeypatch)
        d = index_icessn._index_dir(index_icessn.ICESSN_RES)
    else:
        _run_script("build_index.py", ["--bbox", *args, "--window", *WINDOW], monkeypatch)
        d = index.ATL03_INDEX_DIR
    doc = _manifest(d)
    assert not doc.get("cells"), f"{builder}: an empty search stamped a coverage claim"
    assert doc.get("empty_searches"), f"{builder}: the empty search was not recorded"
    assert Path(d).resolve().is_relative_to(empty_search.resolve())


# ---- live: the searches the builders depend on still return something ------------------------------------------
@pytest.mark.skipif(os.environ.get("AICESAT_NET_TESTS") != "1", reason="live CMR search; set AICESAT_NET_TESTS=1")
@pytest.mark.parametrize("short_name,version", [(coverage.GLAS_SHORT_NAME, coverage.GLAS_VERSION),
                                                (coverage.ICESSN_SHORT_NAME, coverage.ICESSN_VERSION)])
def test_live_search_returns_granules_over_egig(short_name, version):
    from aicesat import regions

    bbox = regions.resolve_bbox("egig_west_flank")
    assert len(coverage.search(short_name, version, bbox, None, use_cache=False)) > 0
