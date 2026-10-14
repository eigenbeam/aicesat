"""The ATL03 index pool hands the parent a row count per granule, not the granule's index table.

build_granule_index returns the table it writes, and ensure_index kept every finished future -- so every table --
until the whole build ended. The parent only ever read the granule's name. Measured on the all-Greenland build
(48,838 granules): the parent grew ~28 MB per 30 s while the workers stayed flat, on course to run a 15 GiB box out
of memory about an hour in."""
from aicesat import index


class _Done:
    def __init__(self, v):
        self.v = v

    def result(self, timeout=None):
        return self.v


class _FakePool:
    """Runs submissions inline and records what was submitted."""
    submitted = []
    contexts = []

    def __init__(self, max_workers=None, mp_context=None):
        _FakePool.contexts.append(mp_context)

    def submit(self, fn, *args):
        _FakePool.submitted.append(fn)
        return _Done(fn(*args))

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class _Table:
    num_rows = 42


def test_the_pool_returns_a_count_not_the_table(monkeypatch):
    import concurrent.futures as cf
    monkeypatch.setattr(cf, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(index, "indexed_granules", lambda: set())
    monkeypatch.setattr(index, "build_granule_index", lambda g, res, cells: _Table())
    _FakePool.submitted.clear()
    out = index.ensure_index([{"meta": {"native-id": "G1"}}, {"meta": {"native-id": "G2"}}], workers=2)
    assert out["built"] == ["G1", "G2"] and out["failed"] == []
    assert _FakePool.submitted and all(fn is index._index_granule_job for fn in _FakePool.submitted)
    assert index._index_granule_job({"meta": {"native-id": "G3"}}, 6, None) == 42


def test_workers_are_spawned_not_forked(monkeypatch):
    """Forked workers start as copies of the parent, which holds the build's whole CMR result (~3 GB for all of
    Greenland). Reference counting touches those pages, copy-on-write un-shares them, and used memory climbs towards
    (workers + 1) x 3 GB: measured 12 -> 13.5 GB in five minutes on a 15 GB box. Spawned workers receive only their
    task. (macOS spawns by default, which is why a laptop build never showed it; Linux forks.)"""
    import concurrent.futures as cf
    monkeypatch.setattr(cf, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(index, "indexed_granules", lambda: set())
    monkeypatch.setattr(index, "build_granule_index", lambda g, res, cells: _Table())
    _FakePool.contexts.clear()
    index.ensure_index([{"meta": {"native-id": "G1"}}], workers=1)
    assert _FakePool.contexts and _FakePool.contexts[0].get_start_method() == "spawn"
