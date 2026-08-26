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


def test_coalesce_merges_adjacent_and_small_gaps_only():
    r = [(0, 100), (100, 50), (150, 10), (1000, 5), (1_000_000, 10), (1_000_010, 10)]
    assert access.coalesce(r, max_gap=0) == [(0, 160), (1000, 5), (1_000_000, 20)]
    assert access.coalesce(r, max_gap=1000) == [(0, 1005), (1_000_000, 20)]
    assert access.coalesce([], 0) == []
    # order of input does not matter; output is sorted
    assert access.coalesce(list(reversed(r)), max_gap=0) == [(0, 160), (1000, 5), (1_000_000, 20)]


def test_coalesce_respects_max_span():
    r = [(i * 100, 100) for i in range(10)]
    assert access.coalesce(r, max_gap=0, max_span=350) == [(0, 300), (300, 300), (600, 300), (900, 100)]


def test_fetch_slices_spans_back_into_requested_ranges(monkeypatch):
    data = bytes(range(256)) * 40  # 10240 bytes "file"
    rd = access.RangeReader.__new__(access.RangeReader)
    rd.threads, rd.max_gap, rd.stats, rd._presigned = 2, 64, access.AccessStats(), {}
    import threading; rd._lock = threading.Lock()
    rd.presigned = lambda url, refresh=False: url
    calls = []
    def fake_get(purl, off, size):
        calls.append((off, size)); return data[off: off + size]
    rd._get = fake_get
    ranges = [(0, 100), (100, 100), (250, 10), (5000, 20), (5020, 5)]
    out = rd.fetch("u", ranges)
    assert [len(b) for b in out] == [100, 100, 10, 20, 5]
    assert all(out[i] == data[o: o + s] for i, (o, s) in enumerate(ranges))
    assert calls == [(0, 260), (5000, 25)]                       # 5 wanted ranges -> 2 GETs
    assert rd.stats.requests == 2 and rd.stats.gap_bytes == 50 and rd.stats.chunks == 5


def test_chunk_refs_prunes_by_chunk_boxes(tmp_path, monkeypatch):
    """Cells are coarse; the per-chunk segment bounding box must drop chunks that miss the query bbox."""
    import pyarrow as pa, pyarrow.parquet as pq, h3
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path)
    cell = h3.str_to_int(h3.latlng_to_cell(70.0, -44.0, index.H3_RES))
    base = {"granule": "G", "url": "u", "s3url": "", "revision": "1", "sc_orient": 1, "sdp_epoch": 1.0, "beam": "gt1r", "strong": True,
            "cycle": 6, "rgt": 1, "ph_start": 0, "ph_end": 100}
    for d in index.DATASETS:
        base.update({f"{d}_offset": 0, f"{d}_size": 1, f"{d}_filters": "gzip", f"{d}_dtype": "f8", f"{d}_ncols": 1, f"{d}_mask": 0})
    rows = [dict(base, chunk_index=0, h3_cell=cell, lat_min=69.95, lat_max=70.05, lon_min=-44.1, lon_max=-43.9),   # inside
            dict(base, chunk_index=1, h3_cell=cell, lat_min=70.30, lat_max=70.40, lon_min=-44.1, lon_max=-43.9)]   # same cell, north of box
    tbl = pa.table({k: pa.array([r[k] for r in rows], type=pa.uint64() if k == "h3_cell" else None) for k in rows[0]})
    tbl = tbl.replace_schema_metadata({"aicesat_index_version": index.INDEX_SCHEMA_VERSION})
    pq.write_table(tbl, tmp_path / "G.parquet")
    assert index.chunk_refs([cell]).num_rows == 2
    assert index.chunk_refs([cell], bbox=(-45, 69.8, -43, 70.2)).num_rows == 1
    assert index.chunk_refs([cell], bbox=(-45, 69.8, -43, 70.2))["chunk_index"][0].as_py() == 0


def test_vectorized_cells_match_h3py():
    import h3
    from aicesat import planner
    rng = np.random.default_rng(5)
    lat = rng.uniform(60, 82, 2000); lon = rng.uniform(-60, -20, 2000)
    fast = planner._cells_vectorized(lat, lon, index.H3_RES)
    ref = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(b), index.H3_RES)) for a, b in zip(lat, lon)], dtype="u8")
    assert np.array_equal(fast, ref)


def test_coalesce_gap_env_default_is_bdp_aware():
    assert access.MAX_GAP_BYTES >= 256 * 1024  # never below the in-region optimum; larger from remote links


def _synthetic_lake(tmp_path, monkeypatch, n_cells=3):
    import h3
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "index" / "meta.duckdb")
    monkeypatch.setattr(lake, "SETTINGS_PATH", tmp_path / "index" / "settings.json")
    monkeypatch.setattr(lake, "EVICTION_LOG", tmp_path / "index" / "evictions.jsonl")
    cells = []
    for i in range(n_cells):
        n = 500 * (i + 1)
        lat0 = 69.9 + i * 0.05
        ph = {"lon": np.full(n, -44.5), "lat": np.linspace(lat0, lat0 + 0.001, n), "h": np.full(n, 2600.0), "conf": np.full(n, 4, "i1"),
              "t": np.full(n, np.datetime64("2020-03-12T17:27:45", "ms")), "photon_index": np.arange(n, dtype="i8"),
              "chunk_index": np.zeros(n, "i4"), "coreg_lon": np.full(n, -44.5), "coreg_lat": np.linspace(lat0, lat0 + 0.001, n)}
        ph["h3_cell"] = np.array([h3.str_to_int(h3.latlng_to_cell(la, lo, index.H3_RES)) for la, lo in zip(ph["lat"], ph["lon"])], dtype="u8")
        written = lake.write_photons("ICESAT2", f"G{i}", "gt1r", ph)
        lake.mark_ingested("ICESAT2", f"G{i}", "gt1r", {0: written})
        cells.extend(written)
    return sorted(set(cells))


def test_cell_stats_and_summary(tmp_path, monkeypatch):
    cells = _synthetic_lake(tmp_path, monkeypatch)
    st = lake.cell_stats()
    assert set(st) == set(cells) and all(s["rows"] > 0 and s["bytes"] > 0 and s["chunks"] >= 1 and s["last_ingested"] for s in st.values())
    summ = lake.lake_summary()
    assert summ["cells"] == len(cells) and summ["rows"] == sum(s["rows"] for s in st.values()) and summ["max_bytes"] == lake.DEFAULT_MAX_BYTES


def test_evict_cells_removes_files_and_coverage_rows_only(tmp_path, monkeypatch):
    cells = _synthetic_lake(tmp_path, monkeypatch)
    victim = cells[0]
    ev = lake.evict_cells([victim], reason="test")
    assert len(ev) == 1 and ev[0]["cell"] == victim and ev[0]["reason"] == "test"
    assert not lake.cell_dir("ICESAT2", victim).exists()
    assert victim not in lake.cell_stats()
    assert all(c != victim for (_, _, _, c) in lake.ingested_chunk_cells("ICESAT2", [f"G{i}" for i in range(3)]))
    assert lake.recent_evictions()[-1]["cell"] == victim


def test_enforce_limit_evicts_oldest_first_and_protects(tmp_path, monkeypatch):
    cells = _synthetic_lake(tmp_path, monkeypatch)
    st = lake.cell_stats()
    total = sum(s["bytes"] for s in st.values())
    # make G0's cell the oldest
    con = lake.meta_conn(); con.execute("UPDATE coverage_cells SET ingested_at = TIMESTAMP '2020-01-01' WHERE granule = 'G0'"); con.close()
    oldest = [c for c, s in st.items() if "G0" in s["granules"]][0]
    lake.set_settings(max_bytes=int(total * 0.8))
    ev = lake.enforce_limit(protect=[oldest])       # oldest is protected -> a different cell goes
    assert ev and all(e["cell"] != oldest for e in ev)
    lake.set_settings(max_bytes=1)
    ev2 = lake.enforce_limit(protect=[])
    assert oldest in {e["cell"] for e in ev2} and not lake.cell_stats()


def test_settings_roundtrip(tmp_path, monkeypatch):
    _synthetic_lake(tmp_path, monkeypatch, n_cells=1)
    assert lake.get_settings()["max_bytes"] == lake.DEFAULT_MAX_BYTES
    assert lake.set_settings(max_bytes=123)["max_bytes"] == 123 and lake.get_settings()["max_bytes"] == 123


def test_dem_tile_addressing_and_window_read(tmp_path, monkeypatch):
    from aicesat import dem
    import rasterio
    from rasterio.transform import from_origin
    # STAC item 24_70 has proj:bbox [2899904, -1700096, 3000096, -1599904]: its centre must map to (24, 70)
    assert dem.tile_index(2_950_000, -1_650_000) == (24, 70)
    assert dem.tiles_for_extent(2_950_000, -1_650_000, 3_050_000, -1_550_000) == [(24, 70), (24, 71), (25, 70), (25, 71)]
    # synthetic COG-like GeoTIFF: a plane z = 0.01 * x_m, nodata outside; read it back onto a coarse grid
    path = tmp_path / "t.tif"
    w, h, res = 200, 150, 32.0
    left, top = 2_900_000.0, -1_600_000.0
    xs = left + (np.arange(w) + 0.5) * res
    data = np.tile(0.01 * (xs - left), (h, 1)).astype("f4")
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", crs="EPSG:3413",
                       transform=from_origin(left, top, res, res), nodata=-9999.0) as dst:
        dst.write(data, 1)
    bounds = (left + 640, top - 3200, left + 3200, top - 640)  # inside the tile
    arr = dem._read_tile_window(str(path), bounds, (20, 20))
    assert arr is not None and np.isfinite(arr).all()
    # values increase with x by 0.01 m per m: across 2560 m -> 25.6 m
    assert abs((arr[0, -1] - arr[0, 0]) - 0.01 * (2560 - 128)) < 0.5
    assert abs(arr[0, 0] - arr[-1, 0]) < 1e-3  # no y dependence
