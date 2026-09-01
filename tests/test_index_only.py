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


# --- the manifest that makes "indexed" checkable ---------------------------------------------------------------------
def test_build_manifest_unions_cells_and_never_drops_them(tmp_path, monkeypatch):
    """Indexing a second region adds cells; it must never retract the first region's."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    d = index.ATL03_INDEX_DIR
    a = (-46.0, 71.0, -45.0, 72.0)
    b = (-42.0, 66.0, -41.0, 67.0)                       # disjoint from a
    ca = set(planner.cells_for_bbox(a, res=index.H3_RES))
    cb = set(planner.cells_for_bbox(b, res=index.H3_RES))
    index.write_build_manifest(d, a, index.H3_RES, None, 3, cells=ca)
    assert coverage.index_covers_area(d, a, index.H3_RES)

    out = index.write_build_manifest(d, b, index.H3_RES, None, 4, cells=cb)
    assert set(out["cells"]) == ca | cb
    for box in (a, b):
        assert coverage.index_covers_area(d, box, index.H3_RES), f"{box} lost coverage after a second build"


def test_coverage_is_exact_set_membership_not_a_bounding_box(tmp_path, monkeypatch):
    """The gap between two disjoint builds must not be claimed — the union of their boxes would have claimed it."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx2")
    d = index.ATL03_INDEX_DIR
    a = (-46.0, 71.0, -45.0, 72.0)
    b = (-42.0, 66.0, -41.0, 67.0)
    index.write_build_manifest(d, a, index.H3_RES, cells=planner.cells_for_bbox(a, res=index.H3_RES))
    index.write_build_manifest(d, b, index.H3_RES, cells=planner.cells_for_bbox(b, res=index.H3_RES))
    between = (-44.5, 68.5, -43.5, 69.5)
    assert not coverage.index_covers_area(d, between, index.H3_RES), \
        "an area between two disjoint builds touches cells nobody indexed"


def test_an_area_poking_past_the_built_box_is_refused_cell_by_cell(tmp_path, monkeypatch):
    """The point of cells: coverage no longer depends on the SHAPE that was built, only on which cells exist."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx3")
    d = index.ATL03_INDEX_DIR
    built = (-46.0, 71.0, -45.0, 72.0)
    index.write_build_manifest(d, built, index.H3_RES, cells=planner.cells_for_bbox(built, res=index.H3_RES))
    inner = (-45.6, 71.3, -45.4, 71.6)
    assert coverage.index_covers_area(d, inner, index.H3_RES)
    assert not coverage.index_covers_area(d, (-47.0, 71.0, -45.0, 72.0), index.H3_RES)


def test_planner_refuses_when_only_some_cells_are_built(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx4")
    d = index.ATL03_INDEX_DIR
    bbox = (-45.5, 71.8, -44.5, 72.2)
    cells = planner.cells_for_bbox(bbox, res=index.H3_RES)
    index.write_build_manifest(d, bbox, index.H3_RES, cells=list(cells)[:-1])   # one cell short
    with pytest.raises(RuntimeError, match="not indexed over 1 of"):
        planner.ensure(bbox, regions.DEFAULT_ATL03_WINDOW)


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

    def submit(self, fn, g, *a):
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

        def submit(self, fn, g, *a):
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


# --- an empty granule's parquet must be readable alongside a full one ------------------------------------------------
@pytest.mark.parametrize("rows", [
    # (column, one sample value) per builder, mirroring what each assembles
    {"granule": "g.h5", "url": "u", "s3url": "s", "revision": "1", "sc_orient": 0, "sdp_epoch": 1.5, "beam": "gt1l",
     "strong": True, "cycle": 3, "rgt": 12, "chunk_index": 0, "ph_start": 0, "ph_end": 10, "h3_cell": 123,
     "lat_min": 1.0, "lat_max": 2.0, "lon_min": 3.0, "lon_max": 4.0,
     "lat_ph_offset": 5, "lat_ph_size": 6, "lat_ph_filters": "gzip", "lat_ph_dtype": "f8", "lat_ph_ncols": 1,
     "lat_ph_mask": 0},
    # ICESSN: the shape whose byte_start/byte_end/n_lines used to fall through to the string default
    {"granule": "g.csv", "url": "u", "s3url": "s", "gdate": "20120415", "h3_cell": 123,
     "byte_start": 0, "byte_end": 99, "n_lines": 7, "lat_min": 1.0, "lat_max": 2.0, "lon_min": 3.0, "lon_max": 4.0},
    # GLAS
    {"granule": "g.H5", "url": "u", "s3url": "s", "revision": "1", "gdate": "20051021", "chunk_index": 0,
     "seg_start": 0, "seg_end": 10, "h3_cell": 123, "lat_min": 1.0, "lat_max": 2.0, "lon_min": 3.0, "lon_max": 4.0,
     "lat_offset": 1, "lat_size": 2, "lat_filters": "gzip", "lat_dtype": "f8", "lat_mask": 0, "lat_fill": 3.4e38},
])
def test_typed_table_empty_schema_matches_full(rows):
    """A cell filter makes zero-row granules routine; their parquet must not differ in schema from a full one, or
    DuckDB's union_by_name reconciles across files and the read fails."""
    full = index.typed_table({k: [v] for k, v in rows.items()})
    empty = index.typed_table({k: [] for k in rows})
    assert full.schema == empty.schema, f"schema drift:\n full={full.schema}\nempty={empty.schema}"


def test_polygon_selection_is_tested_by_its_own_cells_not_its_bounding_box(tmp_path, monkeypatch):
    """A drawn shape touches fewer cells than its bounding box. Testing the box refused areas that are fully built."""
    from aicesat import index_glas

    d = tmp_path / "glas" / "res5"
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "glas")
    res = index_glas.GLAS_RES
    # a thin diagonal sliver: its bbox spans a wide square, the shape itself only a corridor
    poly = [[-50.4, 69.05], [-50.35, 69.05], [-49.6, 69.55], [-49.65, 69.55]]
    bbox = (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
    poly_cells = set(planner.cells_for_bbox(bbox, res=res, polygon=poly))
    box_cells = set(planner.cells_for_bbox(bbox, res=res))
    assert poly_cells < box_cells, "fixture must have a shape narrower than its bounding box"

    index.write_build_manifest(d, bbox, res, cells=poly_cells)      # only the shape's own cells are built
    assert glas._index_covers(bbox, poly) is True, "the drawn shape's cells are all built -> it must be accepted"
    assert glas._index_covers(bbox) is False, "without the polygon it falls back to the box and is refused"
