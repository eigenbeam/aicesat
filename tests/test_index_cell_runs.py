"""Index rows carry where each cell sits inside its chunk, so the ingest slices instead of assigning H3 per point.

A build already assigns every positioned point its cell. Recording, per (chunk, cell) row, the run [run_lo, run_hi)
of within-chunk positions that belong to the cell -- and whether that run is exact (no other cell's point inside) --
lets the ingest label a chunk's points by slicing. On the box 99.3% of ATL06 (chunk, cell) runs are exact (12 Summit
granules, 9,422 runs, 2026-10-17); the rest, and any row from an index built before the columns existed, take the
per-point assignment, so the fast path never guesses.
"""
import h3
import h5py
import numpy as np
import pytest

from aicesat import access, auth, index, index_atl06, planner

from test_lake_cache import _build_atl06, _lake_env, _same  # noqa: F401  (autouse env + helpers)


# --- the pure helpers -------------------------------------------------------------------------------------------------
def test_cell_runs_are_exact_when_cells_do_not_interleave():
    pos = np.arange(10)
    cells = np.array([1, 1, 1, 2, 2, 2, 2, 3, 3, 3], "u8")
    assert index.cell_runs(pos, cells) == {1: (0, 3, True), 2: (3, 7, True), 3: (7, 10, True)}


def test_cell_runs_mark_an_interleaved_cell_inexact():
    pos = np.arange(8)
    cells = np.array([1, 1, 2, 1, 2, 2, 3, 3], "u8")                # 1 and 2 interleave; 3 is clean
    runs = index.cell_runs(pos, cells)
    assert runs[1] == (0, 4, False) and runs[2] == (2, 6, False) and runs[3] == (6, 8, True)


def test_cell_runs_ignore_unpositioned_gaps():
    """A segment with no position is simply absent from `pos`; the run spans it and stays exact."""
    pos = np.array([0, 1, 2, 5, 6])                                 # positions 3 and 4 had NaN lat/lon
    cells = np.array([7, 7, 7, 7, 7], "u8")
    assert index.cell_runs(pos, cells) == {7: (0, 7, True)}


def test_cells_from_runs_slices_and_refuses_when_it_cannot():
    runs = {1: (0, 3, True), 2: (3, 7, True), 9: (7, 10, False)}
    out = index.cells_from_runs(10, runs, wanted=[1, 2])
    assert out.tolist() == [1, 1, 1, 2, 2, 2, 2, 0, 0, 0]          # points outside every wanted run are 0: no cell
    assert index.cells_from_runs(10, runs, wanted=[1, 9]) is None, "an inexact wanted run disables the fast path"
    assert index.cells_from_runs(10, runs, wanted=[1, 5]) is None, "a wanted cell without a row disables it"
    assert index.cells_from_runs(10, None, wanted=[1]) is None, "a pre-run index has no runs"
    assert index.cells_from_runs(10, {}, wanted=[1]) is None


# --- the builder records them -------------------------------------------------------------------------------------------
class _Granule(dict):
    def __init__(self, name):
        super().__init__()
        self["meta"] = {"native-id": name, "revision-id": 1}
        self._url = f"https://example.invalid/{name}"

    def data_links(self, access=None):
        return [] if access == "direct" else [self._url]


def _synthetic_atl06(path, lat, lon, C=100):
    n = lat.size
    with h5py.File(path, "w") as f:
        f.create_dataset("orbit_info/sc_orient", data=np.array([0], "i1"))
        f.create_dataset("ancillary_data/atlas_sdp_gps_epoch", data=np.array([1.198800018e9], "f8"))
        g = "gt1l/land_ice_segments"
        for name, arr in (("latitude", lat), ("longitude", lon), ("h_li", np.full(n, 2000.0, "f4")),
                          ("delta_time", np.linspace(0, 10, n)), ("atl06_quality_summary", np.zeros(n, "i1"))):
            f.create_dataset(f"{g}/{name}", data=arr, chunks=(C,), compression="gzip")
    return path


def _build(tmp_path, monkeypatch, lat, lon, cells):
    h5 = _synthetic_atl06(tmp_path / "ATL06_20181014173630_02460103_007_01.h5", lat, lon)
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(access, "in_region", lambda: False)
    monkeypatch.setattr(access, "open_granule", lambda url, s3=None, **k: access.GranuleSource(url, s3, buf=h5.read_bytes()))
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "idx")
    return index_atl06.build_atl06_index(_Granule(h5.name), cells=cells)


def test_the_builder_records_exact_runs_for_a_straight_track(tmp_path, monkeypatch):
    lat = np.linspace(72.5, 72.9, 400)
    lon = np.full(400, -38.5)
    cells = sorted({h3.str_to_int(h3.latlng_to_cell(float(a), -38.5, 5)) for a in lat})
    tbl = _build(tmp_path, monkeypatch, lat, lon, cells)
    rows = tbl.to_pylist()
    assert rows and all(r["run_exact"] for r in rows), "a straight track meets each hexagon in one run"
    for r in rows:
        k, C = r["chunk_index"], 100
        seg = np.arange(k * C, min((k + 1) * C, 400))
        in_cell = np.array([h3.str_to_int(h3.latlng_to_cell(float(lat[s]), -38.5, 5)) == r["h3_cell"] for s in seg])
        idx = np.flatnonzero(in_cell)
        assert (r["run_lo"], r["run_hi"]) == (int(idx[0]), int(idx[-1]) + 1)
    assert tbl.schema.field("run_lo").type == index.pa.int64() and tbl.schema.field("run_exact").type == index.pa.bool_()


def test_the_builder_marks_a_zigzag_across_a_boundary_inexact(tmp_path, monkeypatch):
    a = h3.latlng_to_cell(72.5, -38.5, 5)
    b = h3.grid_disk(a, 1)[1]
    (la, lo), (lb, lob) = h3.cell_to_latlng(a), h3.cell_to_latlng(b)
    lat = np.array([la, lb] * 50); lon = np.array([lo, lob] * 50)         # alternating between two cells' centres
    tbl = _build(tmp_path, monkeypatch, lat, lon, [h3.str_to_int(a), h3.str_to_int(b)])
    rows = tbl.to_pylist()
    assert len(rows) == 2 and not any(r["run_exact"] for r in rows)


# --- the ingest uses them -----------------------------------------------------------------------------------------------
def _scene(**kw):
    n = 30
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0)
    h = np.linspace(2500.0, 2530.0, n).astype("f4"); h[5] = np.float32(3.4e38)
    q = np.zeros(n, "i1"); q[12] = 1
    _build_atl06(lat, lon, h, q, **kw)


BBOX = (-45.5, 69.5, -44.5, 71.5)


def test_the_ingest_slices_by_runs_and_never_assigns(monkeypatch):
    _scene()
    monkeypatch.setattr(planner, "_cells_vectorized", lambda *a, **k: pytest.fail("the ingest assigned H3 per point"))
    got, st = index_atl06.fetch_bbox(BBOX)
    assert st["chunks_fetched"] == 3 and st["chunks_cells_from_runs"] == 3
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)
    assert got["lon"].size == 28


def test_a_pre_run_index_still_takes_the_per_point_path(monkeypatch):
    _scene(runs=False)
    calls = []
    real = planner._cells_vectorized
    monkeypatch.setattr(planner, "_cells_vectorized", lambda lat, lon, res: calls.append(1) or real(lat, lon, res))
    got, st = index_atl06.fetch_bbox(BBOX)
    assert st["chunks_cells_from_runs"] == 0 and len(calls) == 3
    _same(index_atl06._fetch_direct(BBOX)[0], got)


def test_precaching_keeps_the_per_point_path(monkeypatch):
    _scene()
    monkeypatch.setenv(index_atl06.PRECACHE_ENV, "1")
    calls = []
    real = planner._cells_vectorized
    monkeypatch.setattr(planner, "_cells_vectorized", lambda lat, lon, res: calls.append(1) or real(lat, lon, res))
    got, st = index_atl06.fetch_bbox(BBOX)
    assert st["chunks_cells_from_runs"] == 0 and calls, "runs cover only the wanted cells; precaching needs every cell"
    _same(index_atl06._fetch_direct(BBOX)[0], got)
