"""Server-side lake cache for the index-driven missions (ATL06 / GLAS / ICESSN).

These tests stand up a synthetic sub-granule index + an in-memory "granule" blob and mock the byte-range reader, then
prove the lake-first contract without touching NASA:
  * correctness  — the lake-first fetch returns points byte-identical (sorted) to the pre-lake direct path;
  * cache hit    — a second identical fetch issues ZERO NASA GETs and returns identical points;
  * overlap      — a superset bbox fetches only the new chunks, the rest from the lake;
  * eviction     — the disk budget is enforced ACROSS missions, protecting the current scene's cells;
  * resolution   — cells are written at the mission's own H3 resolution (not ATL03's res 6).
"""
import zlib

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import access, index_atl06, index_glas, index_icessn, lake


# ------------------------------------------------------------------ mocked byte-range reader (never hits the network)
BLOBS: dict[str, bytes] = {}


class FakeReader:
    """Serves byte ranges out of an in-memory blob; counts GETs. No auth.login, no sockets."""

    def __init__(self, *a, **k):
        self.stats = access.AccessStats()

    def presign_all(self, urls):
        return {u: u for u in urls}

    def fetch(self, url, ranges):
        blob = BLOBS[url]
        self.stats.requests += len(ranges)
        self.stats.chunks += len(ranges)
        self.stats.bytes += sum(s for _, s in ranges)
        return [blob[o:o + s] for o, s in ranges]


@pytest.fixture(autouse=True)
def _lake_env(tmp_path, monkeypatch):
    """Redirect every lake path to tmp, force out-of-region (HTTPS keys), and mock the reader."""
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "index" / "meta.duckdb")
    monkeypatch.setattr(lake, "SETTINGS_PATH", tmp_path / "index" / "settings.json")
    monkeypatch.setattr(lake, "EVICTION_LOG", tmp_path / "index" / "evictions.jsonl")
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "idx_atl06")
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "idx_glas")
    monkeypatch.setattr(index_icessn, "ICESSN_INDEX_DIR", tmp_path / "idx_icessn")
    monkeypatch.setattr(access, "RangeReader", FakeReader)
    monkeypatch.setenv("AICESAT_S3_DIRECT", "0")
    BLOBS.clear()
    yield
    BLOBS.clear()


def _gz(a, dtype):
    return zlib.compress(np.asarray(a, dtype).tobytes())


# ------------------------------------------------------------------------------------------------------- ATL06 fixture
A_DTYPE = {"latitude": "f8", "longitude": "f8", "h_li": "f4", "delta_time": "f8", "atl06_quality_summary": "i1"}


def _build_atl06(lat, lon, h, q, C=10, granule="ATL06_20200115000000_11760601_007_01.h5", url="https://x/atl06.h5"):
    n = lat.size
    dt = np.linspace(2.0e7, 2.0e7 + n, n)
    data = {"latitude": lat, "longitude": lon, "h_li": h, "delta_time": dt, "atl06_quality_summary": q}
    cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), index_atl06.ATL06_RES)) for a, o in zip(lat, lon)], "u8")
    base = ["granule", "url", "s3url", "sdp_epoch", "beam", "strong", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    rows = {k: [] for k in base}
    for ds in index_atl06.ATL06_DATASETS:
        for suf in ("offset", "size", "dtype", "filters", "mask"):
            rows[f"{ds}_{suf}"] = []
    blob = bytearray()
    for k in range(-(-n // C)):
        s0, s1 = k * C, min((k + 1) * C, n)
        refs = {}
        for ds in index_atl06.ATL06_DATASETS:
            raw = _gz(data[ds][s0:s1], A_DTYPE[ds]); refs[ds] = (len(blob), len(raw)); blob.extend(raw)
        for cell in sorted(set(cells[s0:s1].tolist())):
            rows["granule"].append(granule); rows["url"].append(url); rows["s3url"].append("")
            rows["sdp_epoch"].append(1.0e9); rows["beam"].append("gt1r"); rows["strong"].append(True)
            rows["chunk_index"].append(k); rows["seg_start"].append(s0); rows["seg_end"].append(s1); rows["h3_cell"].append(int(cell))
            for ds in index_atl06.ATL06_DATASETS:
                off, sz = refs[ds]
                rows[f"{ds}_offset"].append(off); rows[f"{ds}_size"].append(sz)
                rows[f"{ds}_dtype"].append(A_DTYPE[ds]); rows[f"{ds}_filters"].append("gzip"); rows[f"{ds}_mask"].append(0)
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    d = index_atl06._index_dir(index_atl06.ATL06_RES); d.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, d / f"{granule}.parquet")
    BLOBS[url] = bytes(blob)


def _atl06_scene():
    n = 30
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0)
    h = np.linspace(2500.0, 2530.0, n).astype("f4"); h[5] = np.float32(3.4e38)   # one fill height (>3e38) -> dropped both paths
    q = np.zeros(n, "i1"); q[12] = 1                                             # one bad-quality segment -> dropped
    _build_atl06(lat, lon, h, q)


def _sortkey(arr):
    order = np.lexsort((arr["h"], arr["lat"], arr["lon"]))
    return {k: np.asarray(v)[order] for k, v in arr.items() if k != "_granules"}


def _same(a, b):
    a, b = _sortkey(a), _sortkey(b)
    assert set(a) == set(b)
    for k in a:
        assert a[k].size == b[k].size, (k, a[k].size, b[k].size)
        if a[k].size:
            assert np.array_equal(a[k], b[k]), k


# ---------------------------------------------------------------------------------------------------- ATL06 behaviours
def test_atl06_lake_first_matches_direct_golden():
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    golden, gst = index_atl06._fetch_direct(bbox)
    got, st = index_atl06.fetch_bbox(bbox)
    assert got["lon"].size == 28                       # 30 segments - 1 fill height - 1 bad quality
    _same(golden, got)
    assert st["chunks_from_nasa"] == 3 and st["chunks_from_lake"] == 0 and st["requests"] > 0


def test_atl06_second_fetch_is_a_pure_cache_hit():
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    first, st1 = index_atl06.fetch_bbox(bbox)
    assert st1["chunks_from_nasa"] == 3 and st1["requests"] > 0
    second, st2 = index_atl06.fetch_bbox(bbox)
    assert st2["chunks_from_nasa"] == 0 and st2.get("requests", 0) == 0    # ZERO NASA GETs on the repeat
    assert st2["chunks_from_lake"] == 3
    _same(first, second)


def test_atl06_overlap_fetches_only_new_chunks():
    _atl06_scene()
    south = (-45.5, 69.5, -44.5, 70.2)                 # hits the southern chunk(s) only
    whole = (-45.5, 69.5, -44.5, 71.5)                 # superset: reuses the south, fetches the rest
    _, st_s = index_atl06.fetch_bbox(south)
    assert st_s["chunks_from_nasa"] >= 1
    got, st_w = index_atl06.fetch_bbox(whole)
    assert st_w["chunks_from_lake"] >= 1 and st_w["chunks_from_nasa"] >= 1   # partial NASA, rest from the lake
    assert st_w["chunks_from_nasa"] < 3
    golden, _ = index_atl06._fetch_direct(whole)
    _same(golden, got)                                 # superset result still byte-identical to a cold direct fetch


def test_atl06_writes_cells_at_mission_resolution_not_res6():
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    _, st = index_atl06.fetch_bbox(bbox)
    written = {int(p.name.split("=")[1]) for p in (lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*")}
    assert written, "no ATL06 cells materialized"
    assert all(h3.get_resolution(h3.int_to_str(c)) == index_atl06.ATL06_RES for c in written)
    assert index_atl06.ATL06_RES == 5 and index_atl06.ATL06_RES != lake_res6()
    # the ingested-set lookup lines up at the same res the fetch used
    hit = {c for (_g, _b, _k, c) in lake.ingested_chunk_cells("ATL06", ["ATL06_20200115000000_11760601_007_01.h5"])}
    assert written <= hit


def lake_res6():
    from aicesat import index
    return index.H3_RES


def test_global_eviction_spans_missions_and_protects_scene_cells():
    """The one disk budget governs ALL collections together; the current scene's cells are protected."""
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    _, _ = index_atl06.fetch_bbox(bbox)
    atl06_cells = set(lake.cell_stats("ATL06"))
    assert atl06_cells
    # add an unrelated ATL03 (ICESAT2) collection to the same lake
    m = 400
    ph = {"lon": np.full(m, -30.0), "lat": np.linspace(60.0, 60.05, m), "h": np.full(m, 100.0), "conf": np.full(m, 4, "i1"),
          "t": np.full(m, np.datetime64("2020-03-12T17:27:45", "ms")), "photon_index": np.arange(m, dtype="i8"),
          "chunk_index": np.zeros(m, "i4"), "coreg_lon": np.full(m, -30.0), "coreg_lat": np.linspace(60.0, 60.05, m)}
    ph["h3_cell"] = np.array([h3.str_to_int(h3.latlng_to_cell(la, lo, lake_res6())) for la, lo in zip(ph["lat"], ph["lon"])], "u8")
    ic_cells = lake.write_photons("ICESAT2", "G_ic", "gt1r", ph)
    lake.mark_ingested("ICESAT2", "G_ic", "gt1r", {0: ic_cells})
    total = sum(s["bytes"] for m in ("ATL06", "ICESAT2") for s in lake.cell_stats(m).values())
    atl06_bytes = sum(s["bytes"] for s in lake.cell_stats("ATL06").values())
    # budget below the total but above the ATL06-only footprint -> only the unprotected ICESAT2 cells can go
    lake.set_settings(max_bytes=int(atl06_bytes + (total - atl06_bytes) * 0.4))
    evicted = lake.enforce_global_limit(protect=atl06_cells)
    assert evicted, "expected an eviction"
    assert all(e["cell"] not in atl06_cells for e in evicted)           # scene cells protected
    assert set(lake.cell_stats("ATL06")) == atl06_cells                 # ATL06 untouched
    assert len(lake.cell_stats("ICESAT2")) < len(ic_cells)              # ICESAT2 LRU-evicted
    assert sum(s["bytes"] for mm in ("ATL06", "ICESAT2") for s in lake.cell_stats(mm).values()) <= int(atl06_bytes + (total - atl06_bytes) * 0.4)


def test_atl06_window_selects_granules_and_readback_follows():
    _atl06_scene()   # single granule dated 2020-01-15
    bbox = (-45.5, 69.5, -44.5, 71.5)
    got, _ = index_atl06.fetch_bbox(bbox, window=("2020-01-01", "2020-12-31"))
    assert got["lon"].size == 28
    # a disjoint window selects no granule in the index -> nothing fetched, nothing read back
    none, st = index_atl06.fetch_bbox(bbox, window=("2010-01-01", "2010-12-31"))
    assert none["lon"].size == 0 and st["chunks_from_nasa"] == 0


def test_atl06_polygon_clip_keeps_only_touched_cells_and_default_is_unchanged():
    """clip_cells + polygon builds from the H3 cells the selection actually touches — a strict subset of the bbox cells,
    read back by cell-membership (no rectangular bbox clip) — while the DEFAULT (no polygon, clip_cells=False) fetch
    stays byte-for-byte the rectangular golden the lake-cache/bench predicate relies on."""
    from aicesat import planner
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    res = index_atl06.ATL06_RES
    poly = [(-45.5, 69.5), (-44.5, 69.5), (-44.5, 70.35), (-45.5, 70.35)]   # southern slab of the scene ([lon, lat] pairs)
    want_full = set(planner.cells_for_bbox(bbox, res=res))
    want_poly = set(planner.cells_for_bbox(bbox, res=res, polygon=poly))
    assert want_poly and want_poly < want_full                             # the polygon touches strictly fewer cells

    # polygon clip on a cold lake: only the touched hexes are addressed; the read keeps points by cell-membership
    clipped, st_c = index_atl06.fetch_bbox(bbox, polygon=poly, clip_cells=True)
    assert clipped["lon"].size > 0
    assert st_c["cells"] == len(want_poly)                                 # addressed the touched-cell set, not the bbox
    got_cells = set(int(c) for c in planner._cells_vectorized(clipped["lat"], clipped["lon"], res))
    assert got_cells <= want_poly                                          # every returned point's res-5 cell is touched

    # default path (no polygon, clip_cells=False): byte-identical to the rectangular direct golden, and it keeps the
    # points in the cells the polygon dropped, so it returns strictly more than the clip.
    golden, _ = index_atl06._fetch_direct(bbox)
    default, _ = index_atl06.fetch_bbox(bbox)
    _same(golden, default)
    assert default["lon"].size > clipped["lon"].size


# --------------------------------------------------------------------------------------------------------- GLAS fixture
G_FLOAT = {"lat": "f8", "lon": "f8", "elev": "f8", "sat_corr": "f8", "delta_ellip": "f8", "time": "f8"}
G_INT = {"elev_use": "i1", "sat_flag": "i1"}


def _build_glas(lat, lon, elev, C=8, granule="GLAH06_634_2103_001_0071_0_01_0001.h5", url="https://x/glah06.h5", gdate="20031015"):
    n = lat.size
    sat = np.zeros(n); dell = np.full(n, 0.7); tsec = np.linspace(1.0e8, 1.0e8 + n, n)
    use = np.zeros(n, "i1"); flag = np.zeros(n, "i1")
    data = {"lat": lat, "lon": lon, "elev": elev, "sat_corr": sat, "delta_ellip": dell, "time": tsec, "elev_use": use, "sat_flag": flag}
    dtype = {**G_FLOAT, **G_INT}
    cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), index_glas.GLAS_RES)) for a, o in zip(lat, lon)], "u8")
    base = ["granule", "url", "s3url", "gdate", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    rows = {k: [] for k in base}
    for key in index_glas.GLAS_KEYS:
        for suf in ("offset", "size", "dtype", "filters", "mask", "fill"):
            rows[f"{key}_{suf}"] = []
    blob = bytearray()
    for k in range(-(-n // C)):
        s0, s1 = k * C, min((k + 1) * C, n)
        refs = {}
        for key in index_glas.GLAS_KEYS:
            raw = _gz(data[key][s0:s1], dtype[key]); refs[key] = (len(blob), len(raw)); blob.extend(raw)
        for cell in sorted(set(cells[s0:s1].tolist())):
            rows["granule"].append(granule); rows["url"].append(url); rows["s3url"].append(""); rows["gdate"].append(gdate)
            rows["chunk_index"].append(k); rows["seg_start"].append(s0); rows["seg_end"].append(s1); rows["h3_cell"].append(int(cell))
            for key in index_glas.GLAS_KEYS:
                off, sz = refs[key]
                rows[f"{key}_offset"].append(off); rows[f"{key}_size"].append(sz); rows[f"{key}_dtype"].append(dtype[key])
                rows[f"{key}_filters"].append("gzip"); rows[f"{key}_mask"].append(0); rows[f"{key}_fill"].append(3.4e38)
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    d = index_glas._index_dir(index_glas.GLAS_RES); d.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, d / f"{granule}.parquet")
    BLOBS[url] = bytes(blob)


def test_glas_lake_first_matches_direct_and_caches():
    n = 24
    _build_glas(np.linspace(69.6, 71.4, n), np.full(n, -45.0), np.linspace(2600.0, 2620.0, n))
    bbox = (-45.5, 69.5, -44.5, 71.5)
    golden, _ = index_glas._fetch_direct(bbox)
    got, st1 = index_glas.fetch_bbox(bbox)
    assert got["lon"].size == n and st1["chunks_from_nasa"] == 3
    _same(golden, got)
    # height reconstruction carried through: h = elev + 0 - 0.7
    assert np.allclose(np.sort(got["h"]), np.sort(np.linspace(2600.0, 2620.0, n) - 0.7))
    again, st2 = index_glas.fetch_bbox(bbox)
    assert st2["chunks_from_nasa"] == 0 and st2.get("requests", 0) == 0
    _same(got, again)
    assert all(h3.get_resolution(h3.int_to_str(int(p.name.split("=")[1]))) == index_glas.GLAS_RES
               for p in (lake.LAKE_DIR / "mission=GLAS").glob("h3_cell=*"))


# ------------------------------------------------------------------------------------------------------ ICESSN fixture
def _icessn_line(sec, lat, lon, elev, rms, track):
    return f"{sec:.1f}, {lat:.5f}, {lon:.5f}, {elev:.2f}, 0.01, 0.01, {rms:.1f}, 500, 0, 0, {int(track)}".encode()


def _build_icessn(lat, lon, elev, rms, track, granule="ILATM2_20120415_120000_smooth_nadir3seg_50pt.csv",
                  url="https://x/ilatm2.csv", gdate="20120415"):
    blob = bytearray(); pos = 0
    starts, ends, lats, lons = [], [], [], []
    blob.extend(b"# header comment\n"); pos = len(blob)
    for i in range(lat.size):
        ln = _icessn_line(43200.0 + i, lat[i], lon[i], elev[i], rms[i], track[i])
        start = pos; blob.extend(ln + b"\n"); pos = len(blob)
        # index every nadir platelet (matches build_icessn_index; the rms cut is re-applied at fetch)
        if track[i] == 0 and np.isfinite(lat[i]) and np.isfinite(lon[i]):
            starts.append(start); ends.append(pos); lats.append(float(lat[i])); lons.append(float(lon[i]))
    lat_a = np.asarray(lats); lon_a = np.asarray(lons); st_a = np.asarray(starts); en_a = np.asarray(ends)
    cells = np.array([h3.str_to_int(h3.latlng_to_cell(a, o, index_icessn.ICESSN_RES)) for a, o in zip(lat_a, lon_a)], "u8")
    rows = {k: [] for k in ("granule", "url", "s3url", "gdate", "h3_cell", "byte_start", "byte_end")}
    for c in np.unique(cells):
        mk = cells == c
        rows["granule"].append(granule); rows["url"].append(url); rows["s3url"].append(""); rows["gdate"].append(gdate)
        rows["h3_cell"].append(int(c)); rows["byte_start"].append(int(st_a[mk].min())); rows["byte_end"].append(int(en_a[mk].max()))
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    d = index_icessn._index_dir(index_icessn.ICESSN_RES); d.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, d / f"{granule}.parquet")
    BLOBS[url] = bytes(blob)


def test_icessn_lake_first_matches_direct_and_caches():
    n = 20
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0); elev = np.linspace(2400.0, 2420.0, n)
    rms = np.full(n, 4.5); rms[7] = 80.0                 # one platelet fails the RMS cut in both paths
    track = np.zeros(n); track[3] = 1                    # one off-nadir platelet, never indexed
    _build_icessn(lat, lon, elev, rms, track)
    bbox = (-45.5, 69.5, -44.5, 71.5)
    golden, _ = index_icessn._fetch_direct(bbox)
    got, st1 = index_icessn.fetch_bbox(bbox)
    assert got["lon"].size == golden["lon"].size and got["lon"].size == n - 2   # minus the RMS reject and the off-nadir
    _same(golden, got)
    again, st2 = index_icessn.fetch_bbox(bbox)
    assert st2["chunks_from_nasa"] == 0 and st2.get("requests", 0) == 0 and st1["chunks_from_nasa"] > 0
    _same(got, again)


def test_icessn_overlapping_span_does_not_partially_rewrite_a_cached_cell():
    """A cell fetched via a neighbour's overlapping byte span must keep its FULL platelet set (no partial-cell bug)."""
    n = 24
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0); elev = np.linspace(2400.0, 2430.0, n)
    rms = np.full(n, 4.5); track = np.zeros(n)
    _build_icessn(lat, lon, elev, rms, track)
    south = (-45.5, 69.5, -44.5, 70.3)
    whole = (-45.5, 69.5, -44.5, 71.5)
    index_icessn.fetch_bbox(south)                       # warms some cells; their spans overlap the northern cells' bytes
    got, _ = index_icessn.fetch_bbox(whole)
    golden, _ = index_icessn._fetch_direct(whole)
    _same(golden, got)                                   # every cell still complete after the overlapping second fetch


# ------------------------------------------------------ ATL06 weak-beams (integration behaviour, preserved lake-first)
def _build_atl06_beams(beams, C=10, granule="ATL06_20200115000000_11760601_007_01.h5", url="https://x/atl06b.h5"):
    """beams: list of (beam_name, strong, lat, lon, h, q). One granule, several beams sharing one blob."""
    base = ["granule", "url", "s3url", "sdp_epoch", "beam", "strong", "chunk_index", "seg_start", "seg_end", "h3_cell"]
    rows = {k: [] for k in base}
    for ds in index_atl06.ATL06_DATASETS:
        for suf in ("offset", "size", "dtype", "filters", "mask"):
            rows[f"{ds}_{suf}"] = []
    blob = bytearray()
    for beam, strong, lat, lon, h, q in beams:
        n = lat.size; dt = np.linspace(2.0e7, 2.0e7 + n, n)
        data = {"latitude": lat, "longitude": lon, "h_li": h, "delta_time": dt, "atl06_quality_summary": q}
        cells = np.array([h3.str_to_int(h3.latlng_to_cell(float(a), float(o), index_atl06.ATL06_RES)) for a, o in zip(lat, lon)], "u8")
        for k in range(-(-n // C)):
            s0, s1 = k * C, min((k + 1) * C, n); refs = {}
            for ds in index_atl06.ATL06_DATASETS:
                raw = _gz(data[ds][s0:s1], A_DTYPE[ds]); refs[ds] = (len(blob), len(raw)); blob.extend(raw)
            for cell in sorted(set(cells[s0:s1].tolist())):
                rows["granule"].append(granule); rows["url"].append(url); rows["s3url"].append(""); rows["sdp_epoch"].append(1.0e9)
                rows["beam"].append(beam); rows["strong"].append(strong); rows["chunk_index"].append(k)
                rows["seg_start"].append(s0); rows["seg_end"].append(s1); rows["h3_cell"].append(int(cell))
                for ds in index_atl06.ATL06_DATASETS:
                    off, sz = refs[ds]
                    rows[f"{ds}_offset"].append(off); rows[f"{ds}_size"].append(sz)
                    rows[f"{ds}_dtype"].append(A_DTYPE[ds]); rows[f"{ds}_filters"].append("gzip"); rows[f"{ds}_mask"].append(0)
    tbl = pa.table({k: (pa.array(v, type=pa.uint64()) if k == "h3_cell" else pa.array(v)) for k, v in rows.items()})
    d = index_atl06._index_dir(index_atl06.ATL06_RES); d.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, d / f"{granule}.parquet")
    BLOBS[url] = bytes(blob)


def test_atl06_weak_beams_preserved_through_the_lake():
    """Integration fetches all six beams (strong_only=False); the lake-first path must carry weak beams through, and a
    strong-only read must NOT pick up weak-beam points that an all-beam query left in the same cell."""
    n = 20
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0)
    hs = np.linspace(2500.0, 2519.0, n).astype("f4"); hw = np.linspace(3000.0, 3019.0, n).astype("f4")
    q = np.zeros(n, "i1")
    _build_atl06_beams([("gt1r", True, lat, lon, hs, q), ("gt1l", False, lat, lon, hw, q)])
    bbox = (-45.5, 69.5, -44.5, 71.5)
    # strong_only=False (how atl06.py calls it): BOTH beams, byte-identical to the all-beam direct golden
    allbeam, st = index_atl06.fetch_bbox(bbox, strong_only=False)
    golden, _ = index_atl06._fetch_direct(bbox, strong_only=False)
    assert allbeam["h"].size == 2 * n
    _same(golden, allbeam)
    # now the lake holds BOTH beams; a strong-only query must return ONLY the strong beam (~2500s), not the weak (~3000s)
    strong, _ = index_atl06.fetch_bbox(bbox)      # strong_only=True default
    assert strong["h"].size == n and strong["h"].max() < 2600
    assert st["chunks_from_nasa"] >= 1


# ------------------------------------------------ per-granule progressive streaming (on_granule) on a cache MISS only
def _row_multiset(a):
    """Multiset of (lon, lat, h, t) point rows — exact, since streamed and lake-read floats are bit-identical."""
    from collections import Counter
    return Counter(zip(np.asarray(a["lon"], "f8").tolist(), np.asarray(a["lat"], "f8").tolist(),
                       np.asarray(a["h"], "f8").tolist(), np.asarray(a["t"]).astype("i8").tolist()))


def _is_subset(sub, full):
    cs, cf = _row_multiset(sub), _row_multiset(full)
    return all(cf[k] >= v for k, v in cs.items())


def test_atl06_on_granule_streams_subset_per_granule_and_is_silent_on_a_cache_hit():
    """A cache-MISS fetch emits each pass's display points ONCE, as a strict subset of the authoritative arrays; a
    later cache HIT (nothing to fetch) emits nothing and returns byte-identical arrays."""
    lat = np.linspace(69.6, 71.4, 30); lon = np.full(30, -45.0); q = np.zeros(30, "i1")
    _build_atl06(lat, lon, np.linspace(2500.0, 2530.0, 30).astype("f4"), q,
                 granule="ATL06_20200115000000_11760601_007_01.h5", url="https://x/atl06A.h5")
    _build_atl06(lat, lon, np.linspace(2700.0, 2730.0, 30).astype("f4"), q,     # a second pass over the same cells
                 granule="ATL06_20200220000000_11760601_007_01.h5", url="https://x/atl06B.h5")
    bbox = (-45.5, 69.5, -44.5, 71.5)

    fires = []
    got, st = index_atl06.fetch_bbox(bbox, on_granule=fires.append)
    assert st["chunks_from_nasa"] > 0                                    # cold lake -> a real fetch
    assert len(fires) == 2                                               # ONE emission per granule (pass)
    assert {f["granule"] for f in fires} == {"ATL06_20200115000000_11760601_007_01.h5",
                                             "ATL06_20200220000000_11760601_007_01.h5"}
    streamed = {k: np.concatenate([f[k] for f in fires]) for k in ("lon", "lat", "h", "t")}
    assert _is_subset(streamed, got)                                    # never a superset -> the cloud won't shrink
    assert streamed["lon"].size == got["lon"].size                      # a full cold build: preview == final set

    fires2 = []
    got2, st2 = index_atl06.fetch_bbox(bbox, on_granule=fires2.append)
    assert st2["chunks_from_nasa"] == 0 and fires2 == []                 # pure cache hit: nothing streamed
    _same(got, got2)                                                     # authoritative arrays unchanged


def test_glas_on_granule_streams_subset_on_miss_and_silent_on_hit():
    n = 24
    _build_glas(np.linspace(69.6, 71.4, n), np.full(n, -45.0), np.linspace(2600.0, 2620.0, n))
    bbox = (-45.5, 69.5, -44.5, 71.5)
    fires = []
    got, st = index_glas.fetch_bbox(bbox, on_granule=fires.append)
    assert st["chunks_from_nasa"] > 0 and len(fires) == 1
    assert fires[0]["granule"] == "GLAH06_634_2103_001_0071_0_01_0001.h5"
    streamed = {k: np.concatenate([f[k] for f in fires]) for k in ("lon", "lat", "h", "t")}
    assert _is_subset(streamed, got) and streamed["lon"].size == got["lon"].size
    fires2 = []
    got2, st2 = index_glas.fetch_bbox(bbox, on_granule=fires2.append)
    assert st2["chunks_from_nasa"] == 0 and fires2 == []
    _same(got, got2)


def test_icessn_on_granule_streams_subset_on_miss_and_silent_on_hit():
    n = 20
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0); elev = np.linspace(2400.0, 2420.0, n)
    rms = np.full(n, 4.5); rms[7] = 80.0                    # one platelet fails the RMS cut (dropped both paths)
    track = np.zeros(n); track[3] = 1                       # one off-nadir platelet (never indexed)
    _build_icessn(lat, lon, elev, rms, track)
    bbox = (-45.5, 69.5, -44.5, 71.5)
    fires = []
    got, st = index_icessn.fetch_bbox(bbox, on_granule=fires.append)
    assert st["chunks_from_nasa"] > 0 and len(fires) == 1
    streamed = {k: np.concatenate([f[k] for f in fires]) for k in ("lon", "lat", "h", "t")}
    assert _is_subset(streamed, got) and streamed["lon"].size == got["lon"].size
    fires2 = []
    got2, st2 = index_icessn.fetch_bbox(bbox, on_granule=fires2.append)
    assert st2["chunks_from_nasa"] == 0 and fires2 == []
    _same(got, got2)


def test_on_granule_none_is_the_unchanged_default_path():
    """Belt-and-braces: with on_granule=None the fetch is byte-identical to the direct golden (the benchmark and the
    lake-cache goldens all ride on this)."""
    _atl06_scene()
    bbox = (-45.5, 69.5, -44.5, 71.5)
    golden, _ = index_atl06._fetch_direct(bbox)
    got, _ = index_atl06.fetch_bbox(bbox, on_granule=None)
    _same(golden, got)
