"""Small HDF5 granules are read whole at build time, and their chunk maps sliced from the same bytes.

h5py walking a granule's structure through the fsspec block cache was 2.0 s of a 3.3 s ATL06 build: ~66 serial 1 MiB
reads per granule. An ATL06 granule is 32-128 MB; read whole through the CRT in 8 MiB spans it takes 0.2 s, and h5py
over the in-memory bytes walks the same structure in milliseconds. These tests pin the pieces that make it safe: the
slicing is exact, the size comes from the first span's Content-Range, the cap is honoured, nothing happens
out-of-region, and a granule built from a buffer is byte-identical to one built from the file.
"""
import io
import threading

import h5py
import numpy as np
import pytest

from aicesat import access, index


# --- slice_ranges ------------------------------------------------------------------------------------------------------
def test_slice_ranges_returns_exactly_the_wanted_bytes_in_order():
    buf = bytes(range(256)) * 4
    out = access.slice_ranges(buf, [(10, 5), (0, 3), (1020, 4)])
    assert out == [buf[10:15], buf[0:3], buf[1020:1024]]
    assert all(isinstance(b, bytes) for b in out)


def test_slice_ranges_refuses_a_range_off_the_end():
    with pytest.raises(IOError, match="runs off the end"):
        access.slice_ranges(b"0123456789", [(8, 4)])


# --- read_whole_object against a fake CRT client ----------------------------------------------------------------------
class _FakeCrt:
    """Serves one object; records the spans asked for."""
    def __init__(self, data, content_range=True):
        self.data, self.content_range, self.calls = data, content_range, []

    def get_range_with_headers(self, bucket, key, off, size):
        self.calls.append(("first", off, size))
        end = min(off + size, len(self.data))
        hdrs = {"content-range": f"bytes {off}-{end - 1}/{len(self.data)}"} if self.content_range else {}
        return self.data[off:end], hdrs

    def get_ranges(self, bucket, key, spans, timings=None):
        self.calls.append(("rest", list(spans)))
        return [self.data[o:o + s] for o, s in spans]


def _reader(crt):
    r = access.RangeReader.__new__(access.RangeReader)
    r.stats, r._lock, r.s3_fetch = access.AccessStats(), threading.Lock(), "crt"
    r._crt_client = lambda: crt
    r._s3_with_refresh = lambda fn: fn()
    return r


def test_read_whole_object_sizes_from_content_range_and_fetches_the_rest_in_one_submission(monkeypatch):
    monkeypatch.setattr(access, "WHOLE_OBJECT_SPAN", 1000)
    data = bytes(np.random.default_rng(0).integers(0, 256, 3500, dtype="u1"))
    crt = _FakeCrt(data)
    r = _reader(crt)
    out = access.read_whole_object(r, "s3://b/k", max_bytes=10_000)
    assert out == data
    assert crt.calls[0] == ("first", 0, 1000)
    assert crt.calls[1] == ("rest", [(1000, 1000), (2000, 1000), (3000, 500)])
    assert r.stats.requests == 4 and r.stats.bytes == 3500 and r.stats.granules_touched == {"s3://b/k"}


def test_read_whole_object_returns_none_over_the_cap_after_one_span(monkeypatch):
    monkeypatch.setattr(access, "WHOLE_OBJECT_SPAN", 1000)
    crt = _FakeCrt(bytes(5000))
    r = _reader(crt)
    assert access.read_whole_object(r, "s3://b/k", max_bytes=4000) is None
    assert crt.calls == [("first", 0, 1000)], "the first span is the only cost of an object over the cap"
    assert r.stats.requests == 1 and r.stats.bytes == 1000


def test_read_whole_object_handles_an_object_smaller_than_one_span(monkeypatch):
    monkeypatch.setattr(access, "WHOLE_OBJECT_SPAN", 1000)
    crt = _FakeCrt(b"x" * 300, content_range=False)        # no Content-Range, but the object fit in the first span
    assert access.read_whole_object(_reader(crt), "s3://b/k", max_bytes=10_000) == b"x" * 300


def test_read_whole_object_gives_up_when_it_cannot_size_the_object(monkeypatch):
    monkeypatch.setattr(access, "WHOLE_OBJECT_SPAN", 1000)
    crt = _FakeCrt(bytes(5000), content_range=False)
    assert access.read_whole_object(_reader(crt), "s3://b/k", max_bytes=10_000) is None


def test_the_cap_env_var_and_zero_disable(monkeypatch):
    monkeypatch.delenv(access.WHOLE_OBJECT_MAX_ENV, raising=False)
    assert access.whole_object_max_bytes() == access.WHOLE_OBJECT_MAX_BYTES
    monkeypatch.setenv(access.WHOLE_OBJECT_MAX_ENV, "0")
    assert access.whole_object_max_bytes() == 0
    assert access.read_whole_object(_reader(_FakeCrt(b"abc")), "s3://b/k") is None
    monkeypatch.setenv(access.WHOLE_OBJECT_MAX_ENV, "12345")
    assert access.whole_object_max_bytes() == 12345


# --- open_granule ------------------------------------------------------------------------------------------------------
def test_open_granule_out_of_region_is_the_block_cache_path(monkeypatch):
    monkeypatch.setattr(access, "in_region", lambda: False)
    monkeypatch.setattr(access, "RangeReader", lambda *a, **k: pytest.fail("no reader should be built out of region"))
    src = access.open_granule("https://x/g.h5", "s3://b/g.h5", whole_object=True)
    assert not src.whole_object and src.reader is None
    opened = []
    monkeypatch.setattr(access, "cloud_hdf5_file", lambda url, s3url=None, reader=None, **k: opened.append((url, s3url, k.get("block_size"))) or "f")
    assert src.open() == "f" and opened == [("https://x/g.h5", "s3://b/g.h5", 1 << 20)]
    big = access.open_granule("https://x/g.h5", "s3://b/g.h5", block_size=4 << 20)
    assert big.open() == "f" and opened[-1][2] == 4 << 20


def test_open_granule_in_region_reads_whole_through_the_reader_it_was_given(monkeypatch):
    monkeypatch.setattr(access, "in_region", lambda: True)
    crt = _FakeCrt(b"HDF\x00" * 100)
    r = _reader(crt)
    src = access.open_granule("https://x/g.h5", "s3://b/g.h5", whole_object=True, reader=r)
    assert src.whole_object and src.reader is r
    assert src.fetch([(4, 4)]) == [b"HDF\x00"] and src.stats.from_buffer == 1
    assert isinstance(src.open(), io.BytesIO)


def test_open_granule_honours_whole_object_false(monkeypatch):
    monkeypatch.setattr(access, "in_region", lambda: True)
    r = _reader(_FakeCrt(b"x" * 10))
    src = access.open_granule("https://x/g.h5", "s3://b/g.h5", whole_object=False, reader=r)
    assert not src.whole_object and r.stats.requests == 0


# --- the structure walk and the chunk map agree between the file and the buffer ---------------------------------------
def test_chunks_sliced_from_the_buffer_decode_to_the_same_arrays_as_h5py(tmp_path):
    p = tmp_path / "t.h5"
    rng = np.random.default_rng(1)
    with h5py.File(p, "w") as f:
        f.create_dataset("a/lat", data=rng.normal(size=25_000), chunks=(4_000,), compression="gzip")
        f.create_dataset("a/flag", data=rng.integers(0, 3, 25_000).astype("i1"), chunks=(4_000,), compression="gzip", shuffle=True)
    buf = p.read_bytes()
    src = access.GranuleSource("https://x/t.h5", "s3://b/t.h5", buf=buf)
    with h5py.File(src.open(), "r") as f:
        for name in ("a/lat", "a/flag"):
            got = index.read_via_chunks(f[name], src)
            assert np.array_equal(got, f[name][:]), name
        first = index.read_via_chunks(f["a/lat"], src, first_n=3)
        assert np.array_equal(first, f["a/lat"][:3])
    assert src.stats.from_buffer == 7 + 7 + 1, "chunk maps were sliced, never fetched"
