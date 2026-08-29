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
    assert np.allclose(got, np.asarray(doc["series"]["ICESSN"]["positions"], dtype="f4"))


def test_slopes_part_roundtrips_and_pairs_with_positions(tmp_path, monkeypatch):
    doc = _mk_scene(tmp_path, monkeypatch, n=50_000, with_slopes=True)
    sl = _all_values("s1", "slopes:ICESSN")
    assert np.allclose(sl, np.asarray(doc["series"]["ICESSN"]["slopes"], dtype="f4"))
    pos = _all_values("s1", "positions:ICESSN")
    assert sl.size // 2 == pos.size // 3      # one (sn, we) pair per (x, y, z) point — the client indexes them together


def test_chunks_are_stable_prefixes_so_appending_is_valid(tmp_path, monkeypatch):
    """The client keeps chunks [0..k) and fetches from chunk k onward as the array grows. That is only correct if
    chunk i holds the same fixed-size slice regardless of total length — i.e. chunking is a stable prefix split."""
    _mk_scene(tmp_path, monkeypatch, n=50_000)
    d0 = api.scene_part("s1", "positions:ICESSN", chunk=0)
    CHUNK_FLOATS = 96_000 // 4                       # server chunk_bytes / sizeof(float32); the client assumes this
    first = np.frombuffer(base64.b64decode(d0["b64"]), dtype="f4")
    assert first.size == CHUNK_FLOATS                # a full first chunk (array is larger than one chunk)
    full = _all_values("s1", "positions:ICESSN")
    assert np.array_equal(first, full[:CHUNK_FLOATS])   # chunk 0 is exactly the first CHUNK_FLOATS values
    d1 = api.scene_part("s1", "positions:ICESSN", chunk=1)
    second = np.frombuffer(base64.b64decode(d1["b64"]), dtype="f4")
    assert np.array_equal(second, full[CHUNK_FLOATS:CHUNK_FLOATS + second.size])
