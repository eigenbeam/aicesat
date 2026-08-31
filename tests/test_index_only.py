"""The index is a precondition, not an optimisation (issue #24, tier 2).

Discovery is paid once, at index build time. A scene is then assembled from index entries whose H3 cells match the
area of interest, and the data comes from byte-range reads against those entries. There is no second way to get data:
no CMR search at query time, and no whole-granule download — those fallbacks made the same cache key mean two
different datasets depending on which path happened to run, and they hid an unbuilt index behind a slow success.

An unindexed area is now an explicit error telling you to build the index.
"""
import pathlib
import re

import pytest

from aicesat import atl06, coverage, glas, icessn, index, planner, regions

UNINDEXED = (-100.0, -70.0, -99.0, -69.0)      # nothing is ever indexed here


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any CMR search or granule download during extraction is a failure, not a fallback."""
    def _boom(*a, **k):
        raise AssertionError("CMR/network reached from the query path")
    monkeypatch.setattr(coverage, "search", _boom)


@pytest.mark.parametrize("mod, window", [
    (glas, regions.DEFAULT_GLAS_WINDOW),
    (icessn, regions.DEFAULT_ICESSN_WINDOW),
    (atl06, regions.DEFAULT_ATL06_WINDOW),
])
def test_extract_refuses_an_unindexed_area(mod, window, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.cache, "CACHE_DIR", tmp_path / "c")
    with pytest.raises(RuntimeError, match="index"):
        mod.extract(UNINDEXED, window)


def test_planner_refuses_an_unindexed_area(monkeypatch, tmp_path):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx_atl03")
    with pytest.raises(RuntimeError, match="index"):
        planner.ensure(UNINDEXED, regions.DEFAULT_ATL03_WINDOW)


def test_no_cmr_search_and_no_granule_download_in_the_package():
    """coverage.search stays (the index builders in scripts/ call it); nothing under src/ may."""
    src = pathlib.Path(planner.__file__).parent
    offenders = []
    for path in sorted(src.glob("*.py")):
        text = path.read_text()
        for pattern, what in ((r"coverage\.search\(", "CMR search"),
                              (r"earthaccess\.download\(", "whole-granule download"),
                              (r"\bsample_evenly\b", "granule sampling")):
            for m in re.finditer(pattern, text):
                if path.name == "coverage.py" and what == "CMR search":
                    continue                       # the definition itself lives there
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {what}")
    assert not offenders, "query-path fallback reintroduced:\n  " + "\n  ".join(offenders)


def test_no_dead_atl03_extraction_path():
    import inspect
    assert not hasattr(__import__("aicesat.atl03", fromlist=["x"]), "extract_legacy"), \
        "atl03.extract_legacy is unreferenced dead code"
    from aicesat import atl03
    assert "max_photons" not in inspect.signature(atl03.extract).parameters


@pytest.mark.parametrize("mod", [glas, icessn, atl06])
def test_extract_takes_no_granule_cap(mod):
    import inspect
    assert "max_granules" not in inspect.signature(mod.extract).parameters


# --- the manifest that makes "indexed" checkable --------------------------------------------------------------------
def test_build_manifest_widens_and_never_shrinks(tmp_path, monkeypatch):
    """Every builder stamps _build.json; re-indexing a neighbouring box must not un-cover the first one."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    a = (-46.0, 71.0, -45.0, 72.0)
    b = (-45.0, 72.0, -44.0, 73.0)
    d = index.ATL03_INDEX_DIR
    index.write_build_manifest(d, a, index.H3_RES, ("2018-10-01", "2026-01-01"), 3)
    assert coverage._index_covers_bbox(d, a)

    out = index.write_build_manifest(d, b, index.H3_RES, ("2018-10-01", "2026-01-01"), 4)
    assert out["boxes"] == [list(a), list(b)]
    for box in (a, b):
        assert coverage._index_covers_bbox(d, box), f"{box} lost coverage after a second build"
    # ... and the gap BETWEEN two disjoint builds is not claimed
    assert not coverage._index_covers_bbox(d, (-46.0, 71.0, -44.0, 73.0)), \
        "the union of two disjoint builds must not be reported as covered"


def test_build_manifest_absorbs_a_contained_box(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx2")
    d = index.ATL03_INDEX_DIR
    index.write_build_manifest(d, (-50.0, 60.0, -40.0, 70.0), index.H3_RES)
    out = index.write_build_manifest(d, (-46.0, 62.0, -44.0, 64.0), index.H3_RES)
    assert out["boxes"] == [[-50.0, 60.0, -40.0, 70.0]], "a contained box should not add an entry"


def test_legacy_single_bbox_manifest_is_still_read(tmp_path, monkeypatch):
    """Deployments stamped before the list format must keep working."""
    import json
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx3")
    d = index.ATL03_INDEX_DIR
    d.mkdir(parents=True)
    (d / "_build.json").write_text(json.dumps({"bbox": [-52, 62, -44, 70], "res": 5, "target": 9}))
    assert coverage._index_covers_bbox(d, (-50.0, 64.0, -46.0, 68.0))
    out = index.write_build_manifest(d, (-60.0, 60.0, -55.0, 65.0), 5)      # upgraded in place, old box kept
    assert out["boxes"] == [[-52.0, 62.0, -44.0, 70.0], [-60.0, 60.0, -55.0, 65.0]]


def test_planner_accepts_an_area_once_its_manifest_exists(tmp_path, monkeypatch):
    """The precondition is exactly the manifest — with one present the planner proceeds past the coverage gate."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    bbox = (-45.5, 71.8, -44.5, 72.2)
    index.write_build_manifest(index.ATL03_INDEX_DIR, (-50.0, 70.0, -40.0, 75.0), index.H3_RES)
    monkeypatch.setattr(index, "chunk_refs", lambda cells, **kw: _EmptyRefs())
    with pytest.raises(RuntimeError, match="no indexed ATL03 chunks"):
        planner.ensure(bbox, regions.DEFAULT_ATL03_WINDOW)


class _EmptyRefs:
    num_rows = 0

    def to_pylist(self):
        return []


# --- one flaky granule must not discard the rest of the build --------------------------------------------------------
class _ImmediateFuture:
    def __init__(self, exc=None):
        self._exc = exc

    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return None


class _FakeExecutor:
    """Runs nothing; hands back a pre-set outcome per granule so the retry logic can be exercised in-process."""
    outcomes: dict = {}
    submitted: list = []

    def __init__(self, max_workers=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, g):
        name = g["meta"]["native-id"]
        _FakeExecutor.submitted.append(name)
        return _ImmediateFuture(_FakeExecutor.outcomes.get(name))

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _granule(name):
    return {"meta": {"native-id": name}}


def test_one_unpicklable_worker_error_does_not_abort_the_build(monkeypatch):
    """A transient CDN error killed a 120-granule build at 101 because only FutTimeout was caught, and it surfaced
    as 'can't pickle CIMultiDictProxy' rather than the 503 it was."""
    import concurrent.futures

    names = [f"g{i}" for i in range(5)]
    _FakeExecutor.submitted = []
    _FakeExecutor.outcomes = {"g2": TypeError("can't pickle multidict._multidict.CIMultiDictProxy objects")}
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(index, "indexed_granules", lambda: set())

    out = index.ensure_index([_granule(n) for n in names])

    assert sorted(out["built"]) == ["g0", "g1", "g3", "g4"], "healthy granules must still be indexed"
    assert out["failed"] == ["g2"], out
    assert _FakeExecutor.submitted.count("g2") == 2, "the failed granule should be retried once"


def test_a_transient_failure_that_clears_on_retry_is_reported_as_built(monkeypatch):
    import concurrent.futures

    class _OnceFailing(_FakeExecutor):
        seen: set = set()

        def submit(self, fn, g):
            name = g["meta"]["native-id"]
            _FakeExecutor.submitted.append(name)
            if name == "g1" and name not in _OnceFailing.seen:
                _OnceFailing.seen.add(name)
                return _ImmediateFuture(RuntimeError("503 Service Unavailable"))
            return _ImmediateFuture(None)

    _FakeExecutor.submitted = []
    _OnceFailing.seen = set()
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _OnceFailing)
    monkeypatch.setattr(index, "indexed_granules", lambda: set())

    out = index.ensure_index([_granule(n) for n in ("g0", "g1", "g2")])
    assert out["failed"] == []
    assert sorted(out["built"]) == ["g0", "g1", "g2"]
