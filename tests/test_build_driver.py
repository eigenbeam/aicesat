"""One build driver, one pool policy, four strings per granule into the workers.

Five arrangements of the same sequence (build_atl06, build_gedi, and the inline GLAS/ICESSN/GPS scripts) became
build.run_build over pool.run_pool. These tests pin what the consolidation must keep: the spawn context, the per-item
retry, the claim stamped only after every granule landed, and that a worker receives a GranuleRef (picklable, small)
rather than the earthaccess object that made forked workers copy the whole CMR result.
"""
import concurrent.futures as cf
import pickle

import h3
import pyarrow as pa
import pytest

from aicesat import access, auth, build, coverage, index as index_mod, planner, pool


# --- GranuleRef ---------------------------------------------------------------------------------------------------
class _DataGranule(dict):
    """The shape of an earthaccess DataGranule as the builders see it."""
    def __init__(self, name, revision=3, direct=True):
        super().__init__()
        self["meta"] = {"native-id": f"SC:ATL06.007:{name}", "revision-id": revision}
        self._name = name
        self._direct = direct

    def data_links(self, access=None):
        if access == "direct":
            return [f"s3://bucket/{self._name}"] if self._direct else []
        return [f"https://data.example/{self._name}"]


def test_granule_ref_from_a_data_granule_keeps_what_the_builders_read():
    r = build.granule_ref(_DataGranule("ATL06_x.h5"))
    assert r.name == "ATL06_x.h5" and r.url.endswith("/ATL06_x.h5") and r.s3url.startswith("s3://")
    assert r.revision == "3" and r.native_id == "SC:ATL06.007:ATL06_x.h5"
    assert r.data_links()[0] == r.url and r.data_links(access="direct") == [r.s3url]
    assert r["meta"]["revision-id"] == "3" and r["meta"]["native-id"] == r.native_id


def test_granule_ref_tolerates_fakes_without_links_or_meta():
    class _Bare:
        def data_links(self, access=None):
            return [""] if access == "direct" else ["https://x/IS2TGPSSS_ICE12360.2020_v01.csv"]

    r = build.granule_ref(_Bare())
    assert r.name == "IS2TGPSSS_ICE12360.2020_v01.csv" and r.s3url == "" and r.revision == ""
    assert build.granule_ref({"meta": {"native-id": "G1"}}).name == "G1"
    assert build.granule_ref(r) is r


def test_granule_ref_is_small_and_picklable():
    r = build.granule_ref(_DataGranule("ATL06_20181014173630_02460103_007_01.h5"))
    blob = pickle.dumps(r)
    assert len(blob) < 1024 and pickle.loads(blob) == r


# --- pool.run_pool --------------------------------------------------------------------------------------------------
class _Future:
    def __init__(self, fn, args, outcome):
        self._fn, self._args, self._outcome = fn, args, outcome

    def result(self, timeout=None):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._fn(*self._args)


class _Pool:
    """Inline pool that records the constructor kwargs and lets a test script per-item outcomes."""
    contexts: list = []
    submitted: list = []
    outcomes: dict = {}          # item -> exception to raise, consumed once (so a retry succeeds)

    def __init__(self, max_workers=None, mp_context=None):
        _Pool.contexts.append((max_workers, mp_context))

    def submit(self, fn, item, *args):
        _Pool.submitted.append(item)
        exc = _Pool.outcomes.pop(item, None)
        return _Future(fn, (item, *args), exc)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(cf, "ProcessPoolExecutor", _Pool)
    _Pool.contexts, _Pool.submitted, _Pool.outcomes = [], [], {}
    return _Pool


def test_run_pool_spawns_and_sizes_the_pool_to_the_work(fake_pool):
    done, failed = pool.run_pool(lambda x, k: x * k, [1, 2, 3], args=(10,), workers=8, timeout_s=1)
    assert [r for _, r in done] == [10, 20, 30] and failed == []
    (n, ctx), = fake_pool.contexts
    assert n == 3 and ctx.get_start_method() == "spawn"


def test_run_pool_retries_once_and_reports_the_last_error(fake_pool):
    fake_pool.outcomes = {"b": RuntimeError("503 Service Unavailable")}      # fails once, then succeeds
    seen = []
    done, failed = pool.run_pool(lambda x: x.upper(), ["a", "b", "c"], workers=2, timeout_s=1,
                                 on_each=lambda it, r: seen.append(it))
    assert [it for it, _ in done] == ["a", "c", "b"] and failed == []
    assert seen == ["a", "c", "b"], "on_each fires in submission order, retries after"
    assert fake_pool.submitted.count("b") == 2

    class _Always(_Pool):
        def submit(self, fn, item, *args):
            _Pool.submitted.append(item)
            return _Future(fn, (item, *args), RuntimeError("boom") if item == "x" else None)

    cf.ProcessPoolExecutor = _Always
    done, failed = pool.run_pool(lambda x: x, ["x", "y"], workers=2, timeout_s=1)
    assert [it for it, _ in done] == ["y"]
    assert failed == [("x", "RuntimeError: boom")]


def test_run_pool_treats_a_timeout_as_a_retryable_failure(fake_pool):
    fake_pool.outcomes = {"slow": cf.TimeoutError()}
    done, failed = pool.run_pool(lambda x: x, ["slow"], workers=1, timeout_s=7)
    assert done == [("slow", "slow")] and failed == []                        # the retry succeeded

    class _Stuck(_Pool):
        def submit(self, fn, item, *args):
            return _Future(fn, (item, *args), cf.TimeoutError())

    cf.ProcessPoolExecutor = _Stuck
    done, failed = pool.run_pool(lambda x: x, ["slow"], workers=1, timeout_s=7)
    assert done == [] and failed[0][0] == "slow" and "timed out" in failed[0][1]


# --- run_build ------------------------------------------------------------------------------------------------------
def _patch_common(monkeypatch, tmp_path, granules):
    monkeypatch.setattr(planner, "coverage_cells", lambda *a, **k: [1, 2])
    monkeypatch.setattr(planner, "search_polygon", lambda *a, **k: [(0, 0)] * 4)
    monkeypatch.setattr(planner, "addressing_cells", lambda *a, **k: [10])
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(coverage, "search", lambda *a, **k: granules)
    monkeypatch.setattr(coverage, "build_manifest", lambda c: {"rollup": c})
    monkeypatch.setattr(index_mod, "unclaimed_cells", lambda d, cells: [])
    monkeypatch.setattr(index_mod, "granule_proves", lambda p, needed: True)
    stamped = []
    monkeypatch.setattr(index_mod, "write_build_manifest",
                        lambda d, bbox, res, window, n, cells=None: stamped.append(n) or {})
    return stamped


def test_run_build_hands_the_pool_granule_refs_not_granules(monkeypatch, tmp_path, fake_pool):
    from aicesat import index_glas
    gs = [_DataGranule("GLAH06_a.H5"), _DataGranule("GLAH06_b.H5")]
    stamped = _patch_common(monkeypatch, tmp_path, gs)
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "glas")
    monkeypatch.setattr(index_glas, "indexed_glas_granules", lambda res: set())
    seen = []
    monkeypatch.setattr(index_glas, "build_glas_index", lambda g, res, cells: seen.append(g) or pa.table({"x": [1, 2]}))
    events = []
    out = build.run_build(build.collection("GLAS"), (0, 0, 1, 1), on_granule=lambda n, r, e: events.append((n, r, e)))
    assert all(isinstance(g, build.GranuleRef) for g in seen) and [g.name for g in seen] == ["GLAH06_a.H5", "GLAH06_b.H5"]
    assert all(isinstance(it, build.GranuleRef) for it in fake_pool.submitted)
    assert events == [("GLAH06_a.H5", 2, None), ("GLAH06_b.H5", 2, None)]
    assert out["ok"] == 2 and out["err"] == 0 and out["rows"] == 4 and out["claimed"] and stamped == [2]
    assert fake_pool.contexts[0][1].get_start_method() == "spawn"


def test_run_build_withholds_the_claim_and_reports_the_failure(monkeypatch, tmp_path, fake_pool):
    from aicesat import index_icessn
    gs = [_DataGranule("ILATM2_ok.csv"), _DataGranule("ILATM2_bad.csv")]
    stamped = _patch_common(monkeypatch, tmp_path, gs)
    monkeypatch.setattr(index_icessn, "ICESSN_INDEX_DIR", tmp_path / "icessn")
    monkeypatch.setattr(index_icessn, "indexed_icessn_granules", lambda res: set())

    def _build(g, res, cells):
        if g.name == "ILATM2_bad.csv":
            raise RuntimeError("boom")
        return pa.table({"x": [1]})

    monkeypatch.setattr(index_icessn, "build_icessn_index", _build)
    errs = []
    out = build.run_build(build.collection("ICESSN"), (0, 0, 1, 1), on_granule=lambda n, r, e: errs.append((n, e)))
    assert out["ok"] == 1 and out["err"] == 1 and out["claimed"] is False and stamped == []
    assert out["failed"] == ["ILATM2_bad.csv"]
    assert fake_pool.submitted.count(build.granule_ref(gs[1])) == 2, "a failure is retried once"
    assert ("ILATM2_bad.csv", "RuntimeError: boom") in errs


def test_gps_prepare_runs_in_the_parent_and_aborts_without_metadata(monkeypatch, tmp_path, fake_pool):
    from aicesat import index_gpstruth

    class _Granule:
        def __init__(self, name):
            self._name = name
            self["meta"] = None  # noqa: never read; mirrors the dict fakes' lack of meta

        def __setitem__(self, k, v):
            pass

        def data_links(self, access=None):
            return [""] if access == "direct" else [f"https://x/{self._name}"]

    meta, survey = _Granule("IS2TGPSSS_TraverseMetadata_v01.txt"), _Granule("IS2TGPSSS_ICE12360.2020_v01.csv")
    stamped = _patch_common(monkeypatch, tmp_path, [meta, survey])
    monkeypatch.setattr(index_gpstruth, "GPSTRUTH_INDEX_DIR", tmp_path / "gps")
    monkeypatch.setattr(index_gpstruth, "indexed_gpstruth_granules", lambda res, metadata_sha1=None: set())

    class _Reader:
        def read_all(self, url):
            return b"header\n"

    monkeypatch.setattr(access, "RangeReader", lambda *a, **k: _Reader())
    calls = []
    monkeypatch.setattr(index_gpstruth, "build_gpstruth_index",
                        lambda g, geometry, sha, res, cells: calls.append((g.name, geometry, sha)) or pa.table({"x": [1]}))
    out = build.run_build(build.collection("GPSTRUTH"), (0, 0, 1, 1))
    assert out["ok"] == 1 and out["claimed"] and stamped == [1]
    (name, geometry, sha), = calls
    assert name == "IS2TGPSSS_ICE12360.2020_v01.csv" and len(sha) == 16 and len(geometry) == 4

    # Without the metadata granule the build must refuse rather than default every survey's sled geometry.
    monkeypatch.setattr(coverage, "search", lambda *a, **k: [survey])
    out = build.run_build(build.collection("GPSTRUTH"), (0, 0, 1, 1))
    assert out["claimed"] is False and out["aborted"] and out["ok"] == 0 and stamped == [1]


# --- the resolution guard in every builder --------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["ATL06", "GLAS", "ICESSN", "GPSTRUTH"])
def test_every_builder_refuses_a_filter_at_the_wrong_resolution(key, monkeypatch, tmp_path):
    """cells_filter(cells, res) raises when the filter is at another resolution than the rows; without `res` a
    tight-box build once indexed zero rows per granule and reported success. ATL03 and GEDI already passed it."""
    from aicesat import index_atl06, index_glas, index_gpstruth, index_icessn
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)

    class _Reader:
        def read_all(self, url):
            return b"header\n"

    monkeypatch.setattr(access, "RangeReader", lambda *a, **k: _Reader())
    wrong = [h3.str_to_int(h3.latlng_to_cell(72.5, -38.5, 9))]          # a claim-resolution cell
    name = {"ATL06": "ATL06_20181014173630_02460103_007_01.h5", "GLAS": "GLAH06_634_2131_002_0045_1_01_0001.H5",
            "ICESSN": "ILATM2_20110501_123456_smooth_nadir3seg_50pt.csv", "GPSTRUTH": "IS2TGPSSS_ICE12360.2020_v01.csv"}[key]
    g = build.GranuleRef(name, url=f"https://x/{name}")
    with pytest.raises(ValueError, match="nothing could match"):
        if key == "ATL06":
            index_atl06.build_atl06_index(g, cells=wrong)
        elif key == "GLAS":
            index_glas.build_glas_index(g, cells=wrong)
        elif key == "ICESSN":
            index_icessn.build_icessn_index(g, cells=wrong)
        else:
            index_gpstruth.build_gpstruth_index(g, (1.8, 3.0, True, ""), "meta", cells=wrong)


def test_the_atl03_ingest_pool_spawns():
    """planner._ensure runs its decode pool inside the web server beside live CRT/aio clients and writer threads;
    a forked child inherits their locks. Pinned on the source because reaching the pool needs a real index."""
    import inspect
    src = inspect.getsource(planner._ensure)
    assert 'get_context("spawn")' in src
