"""scripts/measure_search_modes.py: the index side over a synthetic regional index, the CMR side over fake records.

The script's claim is a comparison: granules the catalogue returns for a box against granules the index knows have
points in the box's cells, and the bytes a whole-granule retrieval moves against the bytes the index would fetch.
Both sides are exercised here without network or a real index: two synthetic ATL06-shaped index files (one granule
crossing the box, one that only shares cells but whose chunks miss the rectangle) and three fake CMR granules (the
two indexed ones plus a footprint-only false positive)."""
import importlib.util
import sys
from pathlib import Path

import h3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import coverage

_SPEC = importlib.util.spec_from_file_location("measure_search_modes",
                                               Path(__file__).resolve().parents[1] / "scripts" / "measure_search_modes.py")
msm = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_search_modes"] = msm
_SPEC.loader.exec_module(msm)

BBOX = (-39.5, 72.4, -37.5, 72.8)                 # the summit region
NAME_YM = "substr(granule,7,4) || '-' || substr(granule,11,2)"
RES = 5


def _row(granule, beam, chunk, cell, lat_min, lat_max, lon_min, lon_max, size=1000):
    return {"granule": granule, "beam": beam, "chunk_index": chunk, "h3_cell": cell,
            "lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max,
            "latitude_offset": 10, "latitude_size": size, "h_li_offset": 20, "h_li_size": size // 2}


def _write(d: Path, granule: str, rows: list[dict]):
    pq.write_table(pa.Table.from_pylist(rows), d / f"{granule}.parquet")


@pytest.fixture
def index_dir(tmp_path):
    d = tmp_path / "atl06" / "res5"
    d.mkdir(parents=True)
    cells = {h3.str_to_int(c) for c in h3.h3shape_to_cells(h3.LatLngPoly([(72.4, -39.5), (72.4, -37.5), (72.8, -37.5), (72.8, -39.5)]), RES)}
    inside, outside = min(cells), max(cells)
    # granule A: two beams, chunk 3 crosses the box in both; chunk 4 only in the cell (bbox misses the rectangle)
    a = "ATL06_20200312172457_11760603_007_01.h5"
    _write(d, a, [_row(a, "gt1l", 3, inside, 72.5, 72.6, -38.9, -38.7), _row(a, "gt1l", 3, outside, 72.5, 72.6, -38.9, -38.7),
                  _row(a, "gt1r", 3, inside, 72.5, 72.6, -38.9, -38.7), _row(a, "gt1l", 4, inside, 73.1, 73.3, -38.9, -38.7)])
    # granule B: touches the cell with a chunk whose bbox lies north of the rectangle
    b = "ATL06_20200401000000_00010703_007_01.h5"
    _write(d, b, [_row(b, "gt2l", 7, inside, 72.9, 73.0, -38.0, -37.9, size=4000)])
    return d, cells, a, b


def test_bbox_area_is_equal_area_km2():
    one_deg_equator = msm.bbox_area_km2((0.0, 0.0, 1.0, 1.0))
    assert 12_200 < one_deg_equator < 12_400
    assert msm.bbox_area_km2(BBOX) < one_deg_equator      # 2 x 0.4 deg at 72.6 N is far smaller


def test_index_side_counts_granules_and_bytes_by_distinct_chunk(index_dir):
    d, cells, a, b = index_dir
    r = msm.index_side(d, RES, NAME_YM, BBOX, ("2018-10-01", "2027-01-01"), cells=sorted(cells))
    assert r["names"] == {a, b}, "both granules have rows in the box's cells"
    assert r["box_names"] == {a}, "only A has a chunk whose bbox crosses the rectangle"
    # bytes needed: A's chunk 3 on two beams (1000 + 500 each), counted once per (granule, beam, chunk) not per row
    assert r["need_mb"] == pytest.approx(2 * 1500 / 1e6)
    assert r["chunks_per_gran"] == pytest.approx((5 + 8) / 2)
    assert r["rows"] == 5


def test_index_side_honours_the_time_window(index_dir):
    d, cells, _a, b = index_dir
    r = msm.index_side(d, RES, NAME_YM, BBOX, ("2020-04-01", "2020-04-30"), cells=sorted(cells))
    assert r["names"] == {b}


class _FakeGranule(dict):
    def __init__(self, name, lat0, lat1, lon0, lon1, mb):
        super().__init__()
        self["meta"] = {"native-id": name}
        self["umm"] = {"DataGranule": {"ArchiveAndDistributionInformation": [{"Size": mb, "SizeUnit": "MB"}]},
                       "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"GPolygons": [{"Boundary": {"Points": [
                           {"Longitude": lon0, "Latitude": lat0}, {"Longitude": lon1, "Latitude": lat0},
                           {"Longitude": lon1, "Latitude": lat1}, {"Longitude": lon0, "Latitude": lat1},
                           {"Longitude": lon0, "Latitude": lat0}]}}]}}}}
        self._name = name

    def data_links(self, access=None):
        return [f"https://example.invalid/{self._name}"]


def test_measure_joins_the_two_sides(index_dir, monkeypatch):
    d, _cells, a, b = index_dir
    fake = [_FakeGranule(a, 60, 85, -60, -20, 100.0), _FakeGranule(b, 60, 85, -60, -20, 60.0),
            _FakeGranule("ATL06_20190101000000_00010203_007_01.h5", 60, 85, -60, -20, 80.0)]
    monkeypatch.setattr(coverage, "search", lambda *args, **kw: fake)
    monkeypatch.setattr(coverage, "_index_for", lambda key: (d, RES, NAME_YM))
    monkeypatch.setattr(coverage, "index_covers_area", lambda d_, bbox, polygon=None: True)
    coll = {"key": "ATL06", "short_name": "ATL06", "version": "007", "window": ["2018-10-01", "2027-01-01"]}
    r = msm.measure("summit", BBOX, coll, use_cache=True)
    assert r["cmr_hits"] == 3 and r["cmr_hull"] == 3 and r["idx_cells"] == 2 and r["idx_box"] == 1
    assert r["false_pos"] == pytest.approx(1 / 3, abs=1e-3) and r["missed"] == 0
    assert r["cmr_gb"] == pytest.approx(0.24) and r["need_mb"] == pytest.approx(0.003)
    assert r["bytes_ratio"] == pytest.approx(240 / 0.003, rel=0.05)
    assert r["fp_area"] > 100, "a 25 x 40 degree footprint is hundreds of times the box"
    assert r["gran_mb_quartiles"] is None, "fewer than four sizes: no quartiles"
    assert r["fp_kind"] == "polygon"
    text = msm.render([r], ["--areas", "summit"], {"ATL06": "test"})
    assert "| summit | ATL06 | 3 |" in text and "```json" in text


def test_measure_skips_collections_that_never_flew_there():
    coll = {"key": "GEDI", "short_name": "GEDI02_A", "version": "003", "window": ["2019-04-01", "2027-01-01"]}
    assert msm.measure("summit", BBOX, coll, use_cache=True) is None


def test_measure_skips_areas_the_index_claim_does_not_cover(index_dir, monkeypatch):
    d, _cells, _a, _b = index_dir
    monkeypatch.setattr(coverage, "_index_for", lambda key: (d, RES, NAME_YM))
    monkeypatch.setattr(coverage, "search", lambda *args, **kw: (_ for _ in ()).throw(AssertionError("CMR must not be asked")))
    coll = {"key": "ATL06", "short_name": "ATL06", "version": "007", "window": ["2018-10-01", "2027-01-01"]}
    assert msm.measure("summit", BBOX, coll, use_cache=True) is None, "no _build.json: the claim covers nothing"
