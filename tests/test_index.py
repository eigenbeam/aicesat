"""Index/access/lake unit tests on a synthetic HDF5 file (no network)."""
import numpy as np
import pytest
import h5py

from aicesat import access, lake, index, planner


@pytest.fixture
def h5(tmp_path):
    p = tmp_path / "t.h5"
    rng = np.random.default_rng(0)
    with h5py.File(p, "w") as f:
        f.create_dataset("f64", data=rng.normal(size=250_000), chunks=(100_000,), compression="gzip", compression_opts=6)
        f.create_dataset("i8x5", data=rng.integers(-1, 5, size=(250_000, 5)).astype("i1"), chunks=(100_000, 5), compression="gzip", shuffle=True)
        f.create_dataset("i64", data=rng.integers(0, 1 << 40, size=25_000), chunks=(10_000,), compression="gzip", shuffle=True)
    return p


def test_decode_matches_h5py_incl_shuffle_and_padded_last_chunk(h5):
    with h5py.File(h5, "r") as f:
        for name, ncols in [("f64", 1), ("i8x5", 5), ("i64", 1)]:
            d = f[name]
            filters = index._filters(d)
            for k in range(d.id.get_num_chunks()):
                ci = d.id.get_chunk_info(k)
                _, raw = d.id.read_direct_chunk(ci.chunk_offset)
                arr = access.decode_chunk(raw, str(d.dtype), filters, ncols, ci.filter_mask)
                sl = tuple(slice(o, o + c) for o, c in zip(ci.chunk_offset, d.chunks))
                ref = d[sl]
                assert np.array_equal(arr[: ref.shape[0]], ref), (name, k, filters)


def test_filters_string(h5):
    with h5py.File(h5, "r") as f:
        assert index._filters(f["f64"]) == "gzip"
        assert index._filters(f["i8x5"]) == "shuffle,gzip"


def test_decode_refuses_unknown_filter():
    with pytest.raises(ValueError):
        access.decode_chunk(b"", "f8", "scaleoffset,gzip")


def test_granule_name_parse():
    i = index.parse_granule_name("ATL03_20200312172457_11760603_007_01.h5")
    assert (i["rgt"], i["cycle"], i["region"], i["version"]) == (1176, 6, 3, 7)


def test_cells_for_bbox_covers_boundary():
    import h3
    bbox = (-45, 69.8, -43, 70.2)
    cells = set(planner.cells_for_bbox(bbox))
    centre_only = {h3.str_to_int(c) for c in h3.h3shape_to_cells(h3.LatLngPoly([(69.8, -45), (69.8, -43), (70.2, -43), (70.2, -45)]), index.H3_RES)}
    assert centre_only <= cells and len(cells) > len(centre_only)
    for lat, lon in [(69.8, -45), (70.2, -43), (69.8, -43.0), (70.2, -45.0), (70.0, -44.0)]:  # corners + centre
        assert h3.str_to_int(h3.latlng_to_cell(lat, lon, index.H3_RES)) in cells


def test_decode_honours_filter_mask():
    raw = np.arange(10, dtype="f8").tobytes()
    arr = access.decode_chunk(raw, "f8", "gzip", filter_mask=0b1)  # deflate skipped for this chunk: stored raw
    assert np.array_equal(arr, np.arange(10.0))


def test_row_ids_deterministic_and_unique():
    a = lake.row_ids("G", "gt1r", np.arange(5))
    b = lake.row_ids("G", "gt1r", np.arange(5))
    c = lake.row_ids("G", "gt2r", np.arange(5))
    assert np.array_equal(a, b) and len(set(a) | set(c)) == 10


def test_lake_write_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "meta.duckdb")
    n = 1000
    ph = {"lon": np.linspace(-44.5, -44.4, n), "lat": np.linspace(69.9, 70.0, n), "h": np.full(n, 2600.0), "conf": np.full(n, 4, "i1"),
          "t": np.full(n, np.datetime64("2020-03-12T17:27:45", "ms")), "photon_index": np.arange(n, dtype="i8"),
          "chunk_index": np.zeros(n, "i4"), "coreg_lon": np.linspace(-44.5, -44.4, n), "coreg_lat": np.linspace(69.9, 70.0, n)}
    import h3
    ph["h3_cell"] = np.array([h3.str_to_int(h3.latlng_to_cell(la, lo, index.H3_RES)) for la, lo in zip(ph["lat"], ph["lon"])], dtype="u8")
    cells = lake.write_photons("ICESAT2", "G1", "gt1r", ph)
    cells2 = lake.write_photons("ICESAT2", "G1", "gt1r", ph)  # again: must not duplicate
    assert cells == cells2
    out = lake.query_photons((-45, 69.8, -43, 70.2), cells, 3, mission="ICESAT2")
    assert out["lon"].size == n
    lake.mark_ingested("ICESAT2", "G1", "gt1r", {0: cells}); lake.mark_ingested("ICESAT2", "G1", "gt1r", {0: cells})
    assert lake.ingested_chunks("ICESAT2", ["G1"]) == {("G1", "gt1r", 0)}
    # bbox predicate is the residual filter: a box that misses the points returns nothing
    assert lake.query_photons((-44.39, 69.8, -43, 70.2), cells, 3)["lon"].size == 0


def test_cell_ids_survive_roundtrip_as_uint64():
    """Regression: mixing int64 and uint64 in np.stack promotes to float64 and corrupts H3 ids."""
    import h3, pyarrow as pa
    c = h3.str_to_int(h3.latlng_to_cell(70.0, -44.0, index.H3_RES))
    ks = np.array([3], dtype="i8"); cs = np.array([c], dtype="u8")
    bad = np.stack([ks, cs], axis=1)
    assert bad.dtype == np.float64 and int(bad[0, 1]) != c  # the trap
    pairs = sorted(set(zip(ks.tolist(), cs.tolist())))
    assert pairs[0][1] == c
    assert pa.array([pairs[0][1]], type=pa.uint64())[0].as_py() == c
