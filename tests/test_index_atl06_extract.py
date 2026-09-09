"""plan_bbox and build_bbox were lifted out of fetch_bbox and scripts/build_atl06_index.py so that the TUI and the
existing callers run ONE implementation. These pin the behaviour that lift had to preserve."""
import concurrent.futures as cf

import pyarrow as pa
import pytest

from aicesat import build_atl06, index_atl06

DS = index_atl06.ATL06_DATASETS


def _row(granule, beam, chunk, cell, off=0):
    r = {"granule": granule, "beam": beam, "chunk_index": chunk, "h3_cell": cell,
         "url": f"https://x/{granule}", "s3url": f"s3://b/{granule}", "sdp_epoch": 0.0,
         "seg_start": 0, "seg_end": 10}
    for i, ds in enumerate(DS):
        r.update({f"{ds}_offset": off + i * 100, f"{ds}_size": 100, f"{ds}_dtype": "f8",
                  f"{ds}_filters": "", f"{ds}_mask": 0})
    return r


@pytest.fixture
def rows():
    # one granule, one beam, two chunks; chunk 0 touches cells 10 and 11, chunk 1 touches cell 11 only
    return [_row("g.h5", "gt1l", 0, 10), _row("g.h5", "gt1l", 0, 11), _row("g.h5", "gt1l", 1, 11, off=1000)]


def _patch_plan(monkeypatch, rows, have):
    monkeypatch.setattr(index_atl06, "_index_rows", lambda *a, **k: ([10, 11], rows))
    from aicesat import access, lake
    monkeypatch.setattr(lake, "drain_writes", lambda *a, **k: True)
    monkeypatch.setattr(lake, "ingested_chunk_cells", lambda *a, **k: have)
    monkeypatch.setattr(access, "access_url", lambda url, s3: url)


def test_plan_bbox_marks_nothing_todo_when_every_cell_is_materialized(monkeypatch, rows):
    have = {("g.h5", "gt1l", 0, 10), ("g.h5", "gt1l", 0, 11), ("g.h5", "gt1l", 1, 11)}
    _patch_plan(monkeypatch, rows, have)
    p = index_atl06.plan_bbox((0, 0, 1, 1))
    assert p["todo"] == [] and p["n_lake"] == 2 and p["by_url"] == {}


def test_plan_bbox_refetches_a_chunk_when_ANY_of_its_cells_is_missing(monkeypatch, rows):
    """The cell-aware skip: a partial eviction must pull the whole chunk back, not be silently accepted."""
    have = {("g.h5", "gt1l", 0, 10), ("g.h5", "gt1l", 1, 11)}      # chunk 0 is missing cell 11
    _patch_plan(monkeypatch, rows, have)
    p = index_atl06.plan_bbox((0, 0, 1, 1))
    assert p["todo"] == [("g.h5", "gt1l", 0)]
    assert p["n_lake"] == 1
    assert list(p["by_url"]) == ["https://x/g.h5"]


def test_plan_bbox_force_ignores_what_is_already_materialized(monkeypatch, rows):
    _patch_plan(monkeypatch, rows, {("g.h5", "gt1l", 0, 10), ("g.h5", "gt1l", 0, 11), ("g.h5", "gt1l", 1, 11)})
    p = index_atl06.plan_bbox((0, 0, 1, 1), force=True)
    assert len(p["todo"]) == 2 and p["n_lake"] == 0 and p["have"] == set()


def test_plan_bbox_groups_todo_by_url_never_across_granules(monkeypatch):
    """Byte offsets index a FILE. Coalescing across granules is meaningless, so the grouping must stay per URL."""
    rows = [_row("a.h5", "gt1l", 0, 10), _row("b.h5", "gt1l", 0, 10)]
    _patch_plan(monkeypatch, rows, set())
    p = index_atl06.plan_bbox((0, 0, 1, 1))
    assert set(p["by_url"]) == {"https://x/a.h5", "https://x/b.h5"}
    assert all(len(v) == 1 for v in p["by_url"].values())


def test_plan_bbox_on_an_empty_index_returns_the_empty_shape(monkeypatch):
    monkeypatch.setattr(index_atl06, "_index_rows", lambda *a, **k: ([10], []))
    p = index_atl06.plan_bbox((0, 0, 1, 1))
    assert p["rows"] == [] and p["todo"] == [] and p["n_lake"] == 0 and p["names"] == []
    assert p["want_only"] is not None          # still computed: the caller uses it whether or not there is work


def test_plan_bbox_settle_false_does_not_wait_on_the_writer(monkeypatch, rows):
    """The inspection paths (`plan`, `chunks`) must not block behind a background write."""
    called = []
    _patch_plan(monkeypatch, rows, set())
    from aicesat import lake
    monkeypatch.setattr(lake, "drain_writes", lambda *a, **k: called.append(1))
    index_atl06.plan_bbox((0, 0, 1, 1), settle=False)
    assert called == []
    index_atl06.plan_bbox((0, 0, 1, 1), settle=True)
    assert called == [1]


# --- build_bbox -----------------------------------------------------------------------------------------------

class _SerialPool:
    """ProcessPoolExecutor's interface, run in-process, so the build sequence is testable without spawning."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def map(self, fn, items, **k): return (fn(i) for i in items)


def _patch_build(monkeypatch, tmp_path, granules, done=(), unclaimed=(), proves=True):
    from aicesat import auth, coverage, index as index_mod, planner
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path)
    monkeypatch.setattr(planner, "coverage_cells", lambda *a, **k: [1, 2])
    monkeypatch.setattr(planner, "search_polygon", lambda *a, **k: [(0, 0)] * 4)
    monkeypatch.setattr(planner, "addressing_cells", lambda *a, **k: [10])
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(coverage, "search", lambda *a, **k: granules)
    monkeypatch.setattr(coverage, "granule_name", lambda g: g["name"])
    monkeypatch.setattr(coverage, "build_manifest", lambda c: {"rollup": c})
    monkeypatch.setattr(index_atl06, "indexed_atl06_granules", lambda res: set(done))
    monkeypatch.setattr(index_mod, "unclaimed_cells", lambda d, cells: list(unclaimed))
    monkeypatch.setattr(index_mod, "granule_proves", lambda p, needed: proves)
    stamped = []
    monkeypatch.setattr(index_mod, "write_build_manifest",
                        lambda d, bbox, res, window, n, cells=None: stamped.append(n) or {})
    monkeypatch.setattr(cf, "ProcessPoolExecutor", _SerialPool)
    return stamped


def test_build_bbox_fires_on_plan_once_before_any_granule(monkeypatch, tmp_path):
    gs = [{"name": f"g{i}.h5"} for i in range(3)]
    _patch_build(monkeypatch, tmp_path, gs)
    monkeypatch.setattr(index_atl06, "build_atl06_index", lambda g, res, cells: pa.table({"x": [1, 2]}))
    events = []
    out = build_atl06.build_bbox((0, 0, 1, 1), on_plan=lambda p: events.append(("plan", p)),
                                 on_granule=lambda n, r, e: events.append(("granule", n)))
    assert events[0][0] == "plan"
    assert events[0][1]["todo"] == 3 and events[0][1]["granules"] == 3
    assert [e[1] for e in events[1:]] == ["g0.h5", "g1.h5", "g2.h5"]
    assert out["ok"] == 3 and out["err"] == 0 and out["rows"] == 6 and out["claimed"]


def test_build_bbox_withholds_the_claim_when_a_granule_fails(monkeypatch, tmp_path):
    """Stamping a claim the files do not back is the bug that produced silent short scenes."""
    gs = [{"name": "ok.h5"}, {"name": "bad.h5"}]
    stamped = _patch_build(monkeypatch, tmp_path, gs)

    def _build(g, res, cells):
        if g["name"] == "bad.h5":
            raise RuntimeError("boom")
        return pa.table({"x": [1]})

    monkeypatch.setattr(index_atl06, "build_atl06_index", _build)
    errs = []
    out = build_atl06.build_bbox((0, 0, 1, 1), on_granule=lambda n, r, e: errs.append(e))
    assert out["ok"] == 1 and out["err"] == 1
    assert out["claimed"] is False
    assert stamped == []                       # nothing stamped
    assert any(e and "boom" in e for e in errs)


def test_build_bbox_claims_immediately_when_there_is_nothing_to_do(monkeypatch, tmp_path):
    gs = [{"name": "g0.h5"}]
    stamped = _patch_build(monkeypatch, tmp_path, gs, done=["g0.h5"])
    out = build_atl06.build_bbox((0, 0, 1, 1))
    assert out["todo"] == 0 and out["ok"] == 0 and out["claimed"] and stamped == [1]


def test_build_bbox_rebuilds_a_granule_that_cannot_prove_it_covers_new_ground(monkeypatch, tmp_path):
    """Enlarging the box: skipping by NAME alone left the added ring unindexed under an extended claim."""
    gs = [{"name": "g0.h5"}]
    _patch_build(monkeypatch, tmp_path, gs, done=["g0.h5"], unclaimed=[7], proves=False)
    monkeypatch.setattr(index_atl06, "build_atl06_index", lambda g, res, cells: pa.table({"x": [1]}))
    out = build_atl06.build_bbox((0, 0, 1, 1))
    assert out["todo"] == 1 and out["ok"] == 1 and out["new_ground_cells"] == 1


def test_plan_bbox_reaches_no_network(monkeypatch, rows):
    """The `plan` command advertises "no network". RangeReader's constructor triggers a login, and presign_all and
    auth.login are the other ways out — none may be reachable from a plan."""
    _patch_plan(monkeypatch, rows, set())
    from aicesat import access, auth

    monkeypatch.setattr(access, "RangeReader",
                        lambda *a, **k: pytest.fail("plan_bbox constructed a RangeReader (that logs in)"))
    monkeypatch.setattr(auth, "login", lambda *a, **k: pytest.fail("plan_bbox logged in"))
    p = index_atl06.plan_bbox((0, 0, 1, 1), settle=False)
    assert p["todo"] and p["by_url"]        # it still planned real work
