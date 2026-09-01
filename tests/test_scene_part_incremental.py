"""The incremental scene transport: the client polls the small `meta` part and appends only the NEW position/slope
chunks, instead of re-fetching the whole doc (millions of floats) on every poll during a build."""
import base64

import numpy as np

from aicesat import api, cache, scene


def _mk_scene(tmp_path, monkeypatch, n=50_000, with_slopes=False):
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    doc = scene.new_scene("s1", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 100.0
    arrays = {"lon": np.full(n, -49.5), "lat": np.full(n, 69.5), "h": np.full(n, 120.0),
              "t": np.zeros(n, "datetime64[ms]")}
    if with_slopes:
        arrays["sn_slope"] = np.full(n, 0.01); arrays["we_slope"] = np.full(n, -0.02)
    scene.add_series(doc, "ICESSN", arrays, {"years": [2010], "granules": []}, "ck")
    cache.save_scene("s1", doc)
    return doc


def _all_values(sid, part):
    """Reassemble a chunked float32 part exactly as the client does."""
    raw, n, c = b"", 1, 0
    while c < n:
        d = api.scene_part(sid, part, chunk=c)
        n = d["n_chunks"]
        raw += base64.b64decode(d["b64"])
        c += 1
    return np.frombuffer(raw, dtype="f4")


def test_meta_excludes_bulk_arrays(tmp_path, monkeypatch):
    _mk_scene(tmp_path, monkeypatch, with_slopes=True)
    meta = api.scene_part("s1", "meta")
    s = meta["series"]["ICESSN"]
    assert "positions" not in s and "slopes" not in s     # the whole point: meta is small enough to poll
    assert s["has_slopes"] is True                        # ...but tells the client a slopes part exists
    assert s["n"] > 0 and "cache_key" in s and "stride" in s   # identity/versioning fields the client needs


def test_meta_marks_missions_without_slopes(tmp_path, monkeypatch):
    _mk_scene(tmp_path, monkeypatch, with_slopes=False)
    meta = api.scene_part("s1", "meta")
    assert meta["series"]["ICESSN"]["has_slopes"] is False


def test_positions_part_roundtrips(tmp_path, monkeypatch):
    doc = _mk_scene(tmp_path, monkeypatch, n=50_000)
    got = _all_values("s1", "positions:ICESSN")
    assert np.allclose(got, cache.scene_array_read("s1", "ICESSN", "positions"))


def test_slopes_part_roundtrips_and_pairs_with_positions(tmp_path, monkeypatch):
    doc = _mk_scene(tmp_path, monkeypatch, n=50_000, with_slopes=True)
    sl = _all_values("s1", "slopes:ICESSN")
    assert np.allclose(sl, cache.scene_array_read("s1", "ICESSN", "slopes"))
    pos = _all_values("s1", "positions:ICESSN")
    assert sl.size // 2 == pos.size // 3      # one (sn, we) pair per (x, y, z) point — the client indexes them together


def test_chunks_are_stable_prefixes_so_appending_is_valid(tmp_path, monkeypatch):
    """The client keeps chunks [0..k) and fetches from chunk k onward as the array grows. That is only correct if
    chunk i holds the same fixed-size slice regardless of total length — i.e. chunking is a stable prefix split.

    The size comes from the SERVER's reported chunk_values, not a constant here: the HTTP route serves ~1 MB chunks
    and an MCP host small ones, and the client learns it from the reply rather than assuming.
    """
    _mk_scene(tmp_path, monkeypatch, n=50_000)
    d0 = api.scene_part("s1", "positions:ICESSN", chunk=0, chunk_bytes=api.MCP_CHUNK_BYTES)
    cv = d0["chunk_values"]
    assert cv == api.MCP_CHUNK_BYTES // 4, d0
    first = np.frombuffer(base64.b64decode(d0["b64"]), dtype="f4")
    assert first.size == cv                          # a full first chunk (array is larger than one chunk)
    full = _all_values("s1", "positions:ICESSN")
    assert np.array_equal(first, full[:cv])          # chunk 0 is exactly the first chunk_values values
    d1 = api.scene_part("s1", "positions:ICESSN", chunk=1, chunk_bytes=api.MCP_CHUNK_BYTES)
    second = np.frombuffer(base64.b64decode(d1["b64"]), dtype="f4")
    assert np.array_equal(second, full[cv:cv + second.size])


def test_http_chunks_are_far_larger_than_mcp_chunks():
    """Rendering a scene issued 38 sequential requests because the browser used the MCP-sized chunks."""
    assert api.HTTP_CHUNK_BYTES >= 10 * api.MCP_CHUNK_BYTES


# --- the doc must not carry bulk arrays any more --------------------------------------------------------------------
def test_scene_doc_size_does_not_scale_with_point_count(tmp_path, monkeypatch):
    """Positions used to be a JSON list of floats inside the doc, so real scenes were 14.7-19.0 MB and every
    scene_part request re-parsed all of it (120 ms) to hand back a 96 KB slice."""
    import json

    sizes = {}
    for n in (5_000, 200_000):
        monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / f"s{n}")
        doc = _mk_scene(tmp_path, monkeypatch, n=n)
        sizes[n] = (cache.SCENE_DIR / "s1.json").stat().st_size
        assert "positions" not in json.dumps(doc["series"]["ICESSN"]), "bulk array leaked back into the doc"
        assert cache.scene_array_len("s1", "ICESSN", "positions") == n * 3
    small, big = sizes[5_000], sizes[200_000]
    assert big < small * 1.5, f"doc grew {small} -> {big} with 40x the points; it should be metadata only"
    assert big < 100_000, f"scene doc is {big} bytes; it should be metadata only"


def test_a_chunk_request_reads_only_that_chunk(tmp_path, monkeypatch):
    """The sidecar is memory-mapped so serving chunk k touches chunk k, not the whole array."""
    _mk_scene(tmp_path, monkeypatch, n=200_000)
    total = cache.scene_array_len("s1", "ICESSN", "positions")
    d = api.scene_part("s1", "positions:ICESSN", chunk=1, chunk_bytes=api.MCP_CHUNK_BYTES)
    got = np.frombuffer(base64.b64decode(d["b64"]), dtype="f4")
    per = api.MCP_CHUNK_BYTES // 4
    assert d["n_values"] == total and d["chunk_values"] == per
    whole = cache.scene_array_read("s1", "ICESSN", "positions")
    assert np.array_equal(got, whole[per:2 * per])


def test_science_paths_read_full_arrays_not_display_positions():
    """The display stride is only safe because co-registration and the time series re-read the FULL cached arrays via
    cache_key. If either ever reached for series[m] positions instead, the stride would silently become a data cut."""
    import inspect

    from aicesat import coreg, timeseries
    for fn in (coreg._reload_arrays, coreg.coregister_scene, timeseries._load_all):
        src = inspect.getsource(fn)
        assert '"positions"' not in src and "'positions'" not in src, \
            f"{fn.__qualname__} reads display positions; it must use cache_key and the full arrays"
    assert 'cache.load(series["cache_key"])' in inspect.getsource(coreg._reload_arrays)


def test_a_pre_sidecar_scene_is_migrated_on_first_read(tmp_path, monkeypatch):
    """Scenes written before the sidecar carry positions as a JSON list; they must keep working, once."""
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "old")
    doc = scene.new_scene("s1", [-50.0, 69.0, -49.0, 70.0])
    vals = [float(i) for i in range(30)]
    doc["series"]["ICESSN"] = {"mission": "ICESSN", "n": 10, "n_extracted": 10, "stride": 1, "cache_key": "ck",
                               "positions": vals, "slopes": [0.5] * 20, "meta": {}, "granules": []}
    cache.save_scene("s1", doc)
    assert cache.scene_array_len("s1", "ICESSN", "positions") == 0      # nothing in the sidecar yet

    got = _all_values("s1", "positions:ICESSN")
    assert np.allclose(got, np.asarray(vals, dtype="f4"))
    assert cache.scene_array_len("s1", "ICESSN", "positions") == 30     # ...moved out on that first read
    after = cache.load_scene("s1")
    assert "positions" not in after["series"]["ICESSN"], "the doc should no longer carry the array"
    assert after["series"]["ICESSN"]["has_slopes"] is True
    assert np.allclose(_all_values("s1", "slopes:ICESSN"), np.full(20, 0.5, dtype="f4"))


# --- a prefix of the stored points must be a fair sample of all of them ----------------------------------------------
def test_any_prefix_of_the_stored_series_is_a_spatial_sample(tmp_path, monkeypatch):
    """The display cap moved to the client, which simply stops fetching. That is only correct because the points are
    stored SHUFFLED — a prefix of track-ordered points would be one corner of the scene."""
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "sample")
    n = 60_000
    doc = scene.new_scene("s1", [-50.0, 69.0, -49.0, 70.0])
    doc["z0"] = 0.0
    arrays = {"lon": np.linspace(-50.0, -49.0, n),       # a long track: order matters enormously
              "lat": np.linspace(69.0, 70.0, n),
              "h": np.linspace(0.0, 1000.0, n),
              "t": np.zeros(n, "datetime64[ms]")}
    scene.add_series(doc, "ATL06", arrays, {"granules": []}, "ck")

    xyz = cache.scene_array_read("s1", "ATL06", "positions").reshape(-1, 3)
    assert xyz.shape[0] == n, "every extracted point must be stored, not a stride of them"
    for frac in (0.02, 0.1, 0.5):
        pre = xyz[: int(n * frac)]
        # the prefix must span the same ground as the whole, not a slice of it
        assert pre[:, 0].min() < xyz[:, 0].min() + 0.05 * np.ptp(xyz[:, 0])
        assert pre[:, 0].max() > xyz[:, 0].max() - 0.05 * np.ptp(xyz[:, 0])
        assert abs(pre[:, 2].mean() - xyz[:, 2].mean()) < 0.02 * np.ptp(xyz[:, 2]), \
            f"prefix of {frac:.0%} is biased in height — the order is not a fair sample"


def test_the_shuffle_is_deterministic(tmp_path, monkeypatch):
    """A rebuild of the same scene must produce byte-identical arrays, or every cache key downstream is a lie."""
    n = 5_000
    arrays = {"lon": np.linspace(-50.0, -49.0, n), "lat": np.linspace(69.0, 70.0, n),
              "h": np.linspace(0.0, 100.0, n), "t": np.zeros(n, "datetime64[ms]")}
    out = []
    for i in (1, 2):
        monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / f"run{i}")
        doc = scene.new_scene("s1", [-50.0, 69.0, -49.0, 70.0]); doc["z0"] = 0.0
        scene.add_series(doc, "ATL06", arrays, {"granules": []}, "ck")
        out.append(cache.scene_array_read("s1", "ATL06", "positions"))
    assert np.array_equal(out[0], out[1])
