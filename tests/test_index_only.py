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

from aicesat import atl06, cache, coverage, glas, icessn, index, planner, regions

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


# --- the manifest: a compacted claim at the FINE resolution --------------------------------------------------------
def _claim(d, bbox, polygon=None):
    """Stamp the manifest for exactly the ground a build over this selection would have searched."""
    return index.write_build_manifest(d, bbox, 5, cells=planner.coverage_cells(bbox, polygon))


def test_claim_is_stored_compacted_and_unions_across_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    d = index.ATL03_INDEX_DIR
    a = (-46.0, 71.0, -45.9, 71.1)
    b = (-42.0, 66.0, -41.9, 66.1)                       # disjoint from a
    fine_a = planner.coverage_cells(a)
    doc = _claim(d, a)
    assert len(doc["cells"]) < len(fine_a), "a solid region must compact"
    assert coverage.index_covers_area(d, a)

    _claim(d, b)
    for box in (a, b):
        assert coverage.index_covers_area(d, box), f"{box} lost coverage after a second build"


def test_the_gap_between_two_disjoint_builds_is_not_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx2")
    d = index.ATL03_INDEX_DIR
    _claim(d, (-46.0, 71.0, -45.9, 71.1))
    _claim(d, (-42.0, 66.0, -41.9, 66.1))
    assert not coverage.index_covers_area(d, (-44.5, 68.5, -44.4, 68.6))


def test_claim_matches_the_selection_not_the_addressing_cell(tmp_path, monkeypatch):
    """The point of claiming fine: a coarse addressing cell juts far past the drawn shape, and nothing searched there.

    A selection is buildable, but a box nudged just outside it — still inside the SAME res-5 addressing cell — is not.
    """
    import h3
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx3")
    d = index.ATL03_INDEX_DIR
    built = (-46.00, 71.00, -45.98, 71.01)
    _claim(d, built)
    assert coverage.index_covers_area(d, built)

    # somewhere in the same res-5 cell as the built area, but outside the built ground itself
    parent = h3.cell_to_parent(h3.latlng_to_cell(71.005, -45.99, index.COVERAGE_RES), 5)
    far = [c for c in h3.cell_to_children(parent, index.COVERAGE_RES)
           if not index.covers_cells(d, [c])]
    assert far, "fixture needs an uncovered child of the same addressing cell"
    la, lo = h3.cell_to_latlng(far[0])
    assert not coverage.index_covers_area(d, (lo - 1e-4, la - 1e-4, lo + 1e-4, la + 1e-4)), \
        "ground inside the addressing cell but outside the build must NOT be claimed"


def test_planner_refuses_when_the_claim_does_not_cover_the_area(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx4")
    bbox = (-45.5, 71.8, -45.4, 71.9)
    _claim(index.ATL03_INDEX_DIR, (-46.5, 70.8, -46.4, 70.9))     # a claim somewhere else entirely
    with pytest.raises(RuntimeError, match="not indexed over all"):
        planner.ensure(bbox, regions.DEFAULT_ATL03_WINDOW)


def test_polygon_selection_is_claimed_by_its_own_ground_not_its_bounding_box(tmp_path, monkeypatch):
    """A drawn shape covers far less ground than its bounding box; building it must not require the box."""
    from aicesat import index_glas
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "glas")
    d = index_glas._index_dir(index_glas.GLAS_RES)
    poly = [[-50.40, 69.05], [-50.38, 69.05], [-49.90, 69.35], [-49.92, 69.35]]
    bbox = (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
    assert len(planner.coverage_cells(bbox, poly)) < len(planner.coverage_cells(bbox)), "shape must be thinner than its box"

    index.write_build_manifest(d, bbox, index_glas.GLAS_RES, cells=planner.coverage_cells(bbox, poly))
    assert glas._index_covers(bbox, poly) is True, "the drawn shape's own ground is built -> accept it"
    assert glas._index_covers(bbox) is False, "the full bounding box was never built -> refuse it"


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




# --- the claim must stay bounded, and mixed resolutions must still answer correctly ----------------------------------
def test_claim_resolution_backs_off_with_area():
    """Fixed at res 9 a 10x5 deg selection is ~2M cells and 2.2 s to polyfill — paid per collection per area edit."""
    small = planner.claim_res((-50.05, 69.10, -49.80, 69.20))
    wide = planner.claim_res((-51.5, 68.7, -48.5, 69.8))
    huge = planner.claim_res((-55.0, 66.0, -45.0, 71.0))
    assert small > wide > huge, (small, wide, huge)
    for bbox in ((-50.05, 69.10, -49.80, 69.20), (-51.5, 68.7, -48.5, 69.8), (-55.0, 66.0, -45.0, 71.0)):
        assert len(planner.coverage_cells(bbox)) <= planner.CLAIM_MAX_CELLS


def test_a_coarse_claim_still_covers_a_fine_query(tmp_path, monkeypatch):
    """Builds at different scales land different resolutions in one manifest; membership matches by ancestry."""
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    d = index.ATL03_INDEX_DIR
    big = (-52.0, 68.0, -48.0, 70.0)
    index.write_build_manifest(d, big, 5, cells=planner.coverage_cells(big),
                               coverage_res=planner.claim_res(big))
    inner = (-50.05, 69.10, -49.80, 69.20)          # a small area inside it, which claims at a FINER resolution
    assert planner.claim_res(inner) > planner.claim_res(big)
    assert coverage.index_covers_area(d, inner), "a coarse claim must still cover ground inside it"


def test_area_outside_the_claim_bounds_is_rejected_without_a_polyfill(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx2")
    d = index.ATL03_INDEX_DIR
    built = (-50.05, 69.10, -49.80, 69.20)
    index.write_build_manifest(d, built, 5, cells=planner.coverage_cells(built))
    assert coverage.index_covers_area(d, built)
    assert not coverage.index_covers_area(d, (10.0, 10.0, 10.1, 10.1))
    # and an area that OVERLAPS the claim but extends past it is still refused
    assert not coverage.index_covers_area(d, (-50.30, 69.10, -49.80, 69.20))


def test_a_sleeping_laptop_does_not_time_out_a_healthy_build(monkeypatch):
    """A 95-minute lid-close looked exactly like a hung build; a wall-clock deadline would have failed it on wake.

    time.time() jumps by the whole nap, time.monotonic() does not, so the deadline must come from the latter.
    """
    import concurrent.futures
    import time as time_mod

    seen_timeouts = []

    class _Recording(_FakeExecutor):
        def submit(self, fn, g, *a):
            _FakeExecutor.submitted.append(g["meta"]["native-id"])
            fut = _ImmediateFuture()
            real = fut.result

            def result(timeout=None):
                seen_timeouts.append(timeout)
                return real()
            fut.result = result
            return fut

    _FakeExecutor.submitted = []
    _FakeExecutor.outcomes = {}
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _Recording)
    monkeypatch.setattr(index, "indexed_granules", lambda: set())
    # every consultation of the wall clock reports another 10 hours gone, as a long sleep would
    clock = [time_mod.time()]

    def jumping():
        clock[0] += 36_000
        return clock[0]
    monkeypatch.setattr(index.time, "time", jumping)

    out = index.ensure_index([_granule(f"g{i}") for i in range(3)])
    assert out["failed"] == [], "a wall-clock deadline would have failed every granule here"
    assert seen_timeouts and all(x > 1.0 for x in seen_timeouts), \
        f"deadline collapsed to the 1 s floor -> it is following the wall clock: {seen_timeouts}"


def test_addressing_cells_handles_a_claim_coarser_than_the_addressing_grid():
    """claim_res backs off with area, so a big region claims coarser than ATL03 addresses (res 6). Taking a parent
    then raises H3ResMismatchError — which crashed any build large enough to trigger the back-off, e.g. Greenland."""
    import h3

    fine9 = planner.coverage_cells((-50.05, 69.10, -49.80, 69.20))          # small area -> claims at res 9
    assert h3.get_resolution(h3.int_to_str(int(fine9[0]))) > index.H3_RES
    up = planner.addressing_cells(fine9, index.H3_RES)
    assert up and all(h3.get_resolution(h3.int_to_str(int(c))) == index.H3_RES for c in up)

    coarse = [h3.str_to_int(h3.latlng_to_cell(69.15, -49.9, 5))]            # a claim COARSER than res 6
    down = planner.addressing_cells(coarse, index.H3_RES)
    assert len(down) == 7, f"a res-5 cell holds 7 res-6 children, got {len(down)}"
    assert all(h3.get_resolution(h3.int_to_str(int(c))) == index.H3_RES for c in down)

    same = planner.addressing_cells(coarse, 5)
    assert same == coarse


def test_a_region_too_big_for_a_cmr_polygon_falls_back_to_the_bbox():
    """CMR carries the polygon in the query string. Greenland's densified hull is 725 vertices and CMR answers
    414 Request-URI Too Large, so an index build over it died before fetching anything.

    The fallback must be the BOUNDING BOX, not a coarser polygon: coarsening lets the great-circle bow cut into the
    region, and a bow excludes ground — it would drop granules silently. A bbox is a strict superset.
    """
    small = planner.search_polygon(planner.coverage_cells((-50.05, 69.10, -49.80, 69.20)))
    assert 3 < len(small) <= planner.MAX_SEARCH_VERTICES

    greenland = planner.coverage_cells((-74.0, 59.5, -11.0, 84.0))
    assert planner.search_polygon(greenland) == [], "an oversized region must hand back no polygon"


def test_a_schema_bump_drops_the_coverage_claim(tmp_path, monkeypatch):
    """Bumping the index version deletes every stale granule file. The claim in _build.json must go with them:
    otherwise coverage keeps reporting that ground as indexed with no rows behind it, and a scene there builds
    'successfully' from nothing."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = tmp_path / "atl03"
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", d)
    d.mkdir(parents=True)
    bbox = (-50.05, 69.10, -49.80, 69.20)
    index.write_build_manifest(d, bbox, index.H3_RES, cells=planner.coverage_cells(bbox))
    assert coverage.index_covers_area(d, bbox)

    # a granule file written under an OLD schema version
    tbl = pa.table({"granule": pa.array(["g.h5"])}).replace_schema_metadata({"aicesat_index_version": "0"})
    pq.write_table(tbl, d / "g.h5.parquet")

    assert index.indexed_granules() == set(), "a stale file must not count as indexed"
    assert not (d / "g.h5.parquet").exists(), "and must be deleted, not left to serve old rows"
    assert not coverage.index_covers_area(d, bbox), "the claim must go with the rows that backed it"
