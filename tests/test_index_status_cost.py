"""index_status must not do work proportional to the size of the index.

It is polled every 8 s per collection by the Data Lake view. Reading was already incremental — a per-file mtime cache
means each granule parquet is parsed once — but deciding WHAT to read globbed and stat()ed every file on every call:
~32,060 files for ATL06 on the deployed box, four collections, every 8 s. That competed with the build thread for the
GIL and is the same "work proportional to the store" shape as the 145 s query_points glob.

A directory's mtime changes when an entry is added or replaced, and index files land by rename, so one stat() of the
directory answers "has anything changed?".
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import api, index_atl06


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "atl06")
    api._INDEX_CACHE.clear()
    d = index_atl06._index_dir(index_atl06.ATL06_RES)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _granule(d, name, cells):
    t = pa.table({"h3_cell": pa.array(cells, type=pa.uint64()),
                  "cycle": np.full(len(cells), 7, "i4")})
    tmp = d / f".{name}.tmp"
    pq.write_table(t, tmp)
    tmp.replace(d / f"{name}.parquet")     # atomic rename, as the real index build does


def _count_scans(monkeypatch):
    """Count directory listings of the index dir — the thing that must not happen on every poll."""
    import pathlib
    seen = []
    real = pathlib.Path.glob
    monkeypatch.setattr(pathlib.Path, "glob",
                        lambda self, pat, *a, **k: (seen.append(pat), real(self, pat, *a, **k))[1])
    return seen


def test_a_repeat_poll_does_not_rescan_the_index(idx, monkeypatch):
    _granule(idx, "ATL06_20200115000000_11760601_007_01", [600000000000000000, 600000000000000001])
    first = api.index_status("ATL06")
    assert first["indexed"] and first["granules"] == 1 and first["cells"]

    scans = _count_scans(monkeypatch)
    for _ in range(5):
        again = api.index_status("ATL06")
    assert again == first, "the cached answer differs from the scanned one"
    assert not [p for p in scans if p.endswith(".parquet")], f"re-scanned the index {len(scans)} times"


def test_a_new_granule_is_picked_up(idx, monkeypatch):
    _granule(idx, "ATL06_20200115000000_11760601_007_01", [600000000000000000])
    assert api.index_status("ATL06")["granules"] == 1
    api.index_status("ATL06")                                  # prime the cache
    _granule(idx, "ATL06_20200220000000_11760601_007_01", [600000000000000002])
    after = api.index_status("ATL06")
    assert after["granules"] == 2, "the directory-mtime gate hid a newly indexed granule"
    assert len(after["cells"]) == 2
