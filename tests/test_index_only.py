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
    index.write_build_manifest(a, ("2018-10-01", "2026-01-01"), 3)
    assert coverage._index_covers_bbox(index.ATL03_INDEX_DIR, a)

    out = index.write_build_manifest(b, ("2018-10-01", "2026-01-01"), 4)
    assert out["bbox"] == [-46.0, 71.0, -44.0, 73.0]
    for box in (a, b):
        assert coverage._index_covers_bbox(index.ATL03_INDEX_DIR, box), f"{box} lost coverage after a second build"


def test_planner_accepts_an_area_once_its_manifest_exists(tmp_path, monkeypatch):
    """The precondition is exactly the manifest — with one present the planner proceeds past the coverage gate."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    bbox = (-45.5, 71.8, -44.5, 72.2)
    index.write_build_manifest((-50.0, 70.0, -40.0, 75.0), None, 1)
    monkeypatch.setattr(index, "chunk_refs", lambda cells, **kw: _EmptyRefs())
    with pytest.raises(RuntimeError, match="no indexed ATL03 chunks"):
        planner.ensure(bbox, regions.DEFAULT_ATL03_WINDOW)


class _EmptyRefs:
    num_rows = 0

    def to_pylist(self):
        return []
