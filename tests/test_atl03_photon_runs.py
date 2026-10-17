"""ATL03: photons inside a wanted, exact run take the run's cell; only edge segments and outsiders are assigned.

An ATL03 chunk is 100,000 photons, and the planner assigned every one of them an H3 cell. The index now records, per
(chunk, cell) row, the photon run of the cell's segments inside the chunk plus where the run's first segment ends and
its last begins. A segment's cell is its reference photon's, so the two edge segments may straddle the cell boundary:
their photons are still assigned individually, as are photons outside every wanted run (ATL03 writes every cell a chunk
touches). The result must be byte-identical to assigning them all.
"""
import zlib

import h3
import numpy as np
import pytest

from aicesat import index, planner


# --- the pure helpers -------------------------------------------------------------------------------------------------------
def test_photon_cell_runs_record_run_edges_and_exactness():
    # three segments per cell, 10 photons each, cell 1 then cell 2; a third cell's segment interleaves nothing
    rel_s = np.array([0, 10, 20, 30, 40, 50]); rel_e = rel_s + 10
    cells = np.array([1, 1, 1, 2, 2, 2], "u8")
    runs = index.photon_cell_runs(rel_s, rel_e, cells)
    assert runs[1] == (0, 30, True, 10, 20) and runs[2] == (30, 60, True, 40, 50)
    cells = np.array([1, 1, 2, 1, 2, 2], "u8")                          # cell 2's segment inside cell 1's run
    runs = index.photon_cell_runs(rel_s, rel_e, cells)
    assert runs[1][2] is False and runs[2][2] is False


def test_labels_from_photon_runs_mark_edges_and_outsiders_for_assignment():
    runs = {1: (0, 30, True, 10, 20), 2: (30, 60, True, 40, 50)}
    labels, assign = index.labels_from_photon_runs(80, runs, wanted=[1, 2])
    assert labels[:30].tolist() == [1] * 30 and labels[30:60].tolist() == [2] * 30 and labels[60:].tolist() == [0] * 20
    assert assign[:10].all() and not assign[10:20].any() and assign[20:40].all() and not assign[40:50].any() and assign[50:].all()
    assert index.labels_from_photon_runs(80, runs, wanted=[1, 3]) is None
    assert index.labels_from_photon_runs(80, {1: (0, 30, False, 10, 20)}, wanted=[1]) is None
    assert index.labels_from_photon_runs(80, {}, wanted=[1]) is None


# --- decode + materialize agree with assigning every photon ---------------------------------------------------------------
def _chunk(lat, lon):
    n = lat.size
    arrays = {"lat_ph": lat.astype("f8"), "lon_ph": lon.astype("f8"), "h_ph": np.full(n, 2000.0, "f4"),
              "delta_time": np.linspace(0, 1, n), "signal_conf_ph": np.zeros((n, 5), "i1")}
    raws = {(d, 0): zlib.compress(a.tobytes()) for d, a in arrays.items()}
    row = {"chunk_index": 0, "ph_start": 0, "ph_end": n, "sdp_epoch": 1.198800018e9, "signal_conf_ph_ncols": 5}
    for d, a in arrays.items():
        row[f"{d}_dtype"] = str(a.dtype); row[f"{d}_filters"] = "gzip"; row[f"{d}_mask"] = 0
    return row, raws


def _segments(lat, lon, per):
    """Segments of `per` photons, each labelled by its first (reference) photon's cell, as the builder sees them."""
    n = lat.size
    s = np.arange(0, n, per); e = np.minimum(s + per, n)
    cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(lat[i]), float(lon[i]), index.H3_RES)) for i in s], "u8")
    return s, e, cells


@pytest.mark.parametrize("wanted_all", [True, False])
def test_materialize_from_runs_is_photon_exact(wanted_all):
    n, per = 4000, 20
    lat = np.linspace(72.50, 72.62, n); lon = np.full(n, -38.5)            # crosses two or three res-6 cells
    s, e, seg_cells = _segments(lat, lon, per)
    runs = index.photon_cell_runs(s, e, seg_cells)
    assert all(r[2] for r in runs.values()), "a straight track gives exact runs"
    row, raws = _chunk(lat, lon)
    wanted = sorted(runs) if wanted_all else sorted(runs)[:1]
    row.update(_runs=runs, _wanted=wanted)
    ph = planner._materialize(planner._decode_photons([row], raws, row["sdp_epoch"]))
    truth = planner._cells_vectorized(lat, lon, index.H3_RES)
    assert np.array_equal(ph["h3_cell"], truth), "slicing by runs must give exactly the per-photon cells"
    assert 0 < ph["_assigned"] < n, f"only edges and outsiders are assigned, got {ph['_assigned']} of {n}"
    if not wanted_all:
        assert ph["_assigned"] > n * 0.3, "photons outside the wanted run are all assigned"


def test_a_row_without_runs_assigns_every_photon():
    n = 1000
    lat = np.linspace(72.50, 72.56, n); lon = np.full(n, -38.5)
    row, raws = _chunk(lat, lon)
    ph = planner._materialize(planner._decode_photons([row], raws, row["sdp_epoch"]))
    assert ph["_assigned"] == n
    assert np.array_equal(ph["h3_cell"], planner._cells_vectorized(lat, lon, index.H3_RES))


def test_an_inexact_run_assigns_every_photon():
    n, per = 2000, 20
    a = h3.latlng_to_cell(72.5, -38.5, index.H3_RES); b = h3.grid_disk(a, 1)[1]
    (la, lo), (lb, lob) = h3.cell_to_latlng(a), h3.cell_to_latlng(b)
    lat = np.repeat(np.array([la, lb] * (n // per // 2)), per); lon = np.repeat(np.array([lo, lob] * (n // per // 2)), per)
    s, e, seg_cells = _segments(lat, lon, per)
    runs = index.photon_cell_runs(s, e, seg_cells)
    assert not any(r[2] for r in runs.values())
    row, raws = _chunk(lat, lon)
    row.update(_runs=runs, _wanted=sorted(runs))
    ph = planner._materialize(planner._decode_photons([row], raws, row["sdp_epoch"]))
    assert ph["_assigned"] == n
    assert np.array_equal(ph["h3_cell"], planner._cells_vectorized(lat, lon, index.H3_RES))


# --- the builder records them -----------------------------------------------------------------------------------------------
def test_the_atl03_builder_records_photon_runs(tmp_path, monkeypatch):
    from aicesat import access, auth
    from test_build_provenance import _Granule, _LocalReader, _synthetic_atl03, _two_cells

    lat, lon, x, y = _two_cells(index.H3_RES)
    h5 = _synthetic_atl03(tmp_path / "ATL03_20200312172457_11760603_007_01.h5", lat, lon)
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(access, "in_region", lambda: False)
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    monkeypatch.setattr(access, "RangeReader", lambda *a, **k: _LocalReader(h5))
    monkeypatch.setattr(access, "cloud_hdf5_file", lambda url, s3=None, **k: str(h5))
    tbl = index.build_granule_index(_Granule(h5.name), cells=[x, y])
    rows = tbl.to_pylist()
    assert rows and {r["h3_cell"] for r in rows} == {x, y}
    assert all(r["run_exact"] for r in rows), "a straight track: every (chunk, cell) run is exact"
    for r in rows:   # photon runs live inside the chunk and the edges inside the run
        assert 0 <= r["run_lo"] < r["run_hi"] <= 1000
        assert r["run_lo"] <= r["edge_lo_end"] <= r["run_hi"] and r["run_lo"] <= r["edge_hi_start"] <= r["run_hi"]
    assert tbl.schema.field("edge_lo_end").type == index.pa.int64()
