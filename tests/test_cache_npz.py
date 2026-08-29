"""cache.save writes a standard .npz at a cheaper deflate level (numpy doesn't expose compresslevel, so the write is
hand-rolled). The format must stay exactly numpy's: round-trippable, and files written by the previous
np.savez_compressed path must still load."""
import numpy as np
import pytest

from aicesat import cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


ARRAYS = {"lon": np.linspace(-50, -49, 100), "lat": np.linspace(69, 70, 100),
          "h": np.linspace(400, 500, 100), "t": np.zeros(100, "datetime64[ms]")}


def test_save_load_roundtrip_preserves_arrays_and_dtypes(cache_dir):
    cache.save("k1", ARRAYS, {"mission": "ATL06", "n": 100})
    arrays, meta = cache.load("k1")
    assert set(arrays) == set(ARRAYS)
    for k, v in ARRAYS.items():
        assert np.array_equal(arrays[k], v), k
        assert arrays[k].dtype == v.dtype, k          # datetime64[ms] must survive, not degrade to int
    assert meta["mission"] == "ATL06"


def test_written_file_is_a_real_npz(cache_dir):
    cache.save("k2", ARRAYS, {})
    with np.load(cache_dir / "k2.npz", allow_pickle=False) as z:   # plain numpy, no aicesat helper
        assert sorted(z.files) == ["h", "lat", "lon", "t"]


def test_loads_legacy_savez_compressed_files(cache_dir):
    """Entries written by the old np.savez_compressed path stay readable — no cache migration needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache_dir / "legacy.npz", **ARRAYS)
    (cache_dir / "legacy.json").write_text('{"mission": "GLAS"}')
    arrays, meta = cache.load("legacy")
    assert np.array_equal(arrays["lon"], ARRAYS["lon"]) and meta["mission"] == "GLAS"


def test_save_is_atomic_no_tmp_left_behind(cache_dir):
    cache.save("k3", ARRAYS, {})
    assert (cache_dir / "k3.npz").exists()
    assert not list(cache_dir.glob(".*tmp*")), "temp file left behind"


def test_overwrite_replaces_previous_entry(cache_dir):
    cache.save("k4", ARRAYS, {"v": 1})
    cache.save("k4", {**ARRAYS, "h": np.full(100, 1.0)}, {"v": 2})
    arrays, meta = cache.load("k4")
    assert meta["v"] == 2 and np.array_equal(arrays["h"], np.full(100, 1.0))
