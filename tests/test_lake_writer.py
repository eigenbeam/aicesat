"""The lake write is off the response path (one source, two sinks).

A cold ATL06 leg spent 34.8 of 88.6 pool thread-seconds inside write_point_chunk, on the critical path of a request
that needs the POINTS, not the files. So the fetch now reads the lake FIRST, returns freshly fetched points from
memory, and hands the Parquet write to a background writer.

That reordering is only safe if three things hold, and each has a test here:
  * the request really does not wait for the write (otherwise nothing was gained);
  * a partially cached chunk contributes each point EXACTLY once — the lake read already returned its cached cells,
    so re-fetching it must not add them again;
  * coverage is marked only AFTER the file lands, so a failed write is re-fetched rather than silently believed.
"""
import threading

import numpy as np
import pytest

from aicesat import index_atl06, lake

# rootdir-prepend import mode (no tests/__init__.py): sibling test modules are top-level. _lake_env is autouse, so
# importing it here is what redirects LAKE_DIR/META_DB to tmp and mocks the byte-range reader.
from test_lake_cache import _atl06_scene, _build_atl06, _lake_env, _same   # noqa: F401

BBOX = (-45.5, 69.5, -44.5, 71.5)
GRANULE = "ATL06_20200115000000_11760601_007_01.h5"
GRANULE_2 = "ATL06_20200220000000_11760601_007_01.h5"


def _rows(arr):
    """Every returned point as a hashable tuple, so duplicates are countable rather than merely 'a size mismatch'."""
    return [tuple(np.asarray(arr[k])[i] for k in ("lon", "lat", "h")) for i in range(arr["lon"].size)]


def test_fetch_returns_before_the_lake_is_written():
    """The whole point of the change: the response does not wait on Parquet."""
    _atl06_scene()
    entered, release = threading.Event(), threading.Event()
    real = lake.write_point_chunk

    def _blocked(*a, **kw):
        entered.set()
        assert release.wait(10.0), "writer was never released"
        return real(*a, **kw)

    orig, lake.write_point_chunk = lake.write_point_chunk, _blocked
    try:
        got, st = index_atl06.fetch_bbox(BBOX)
        assert entered.wait(10.0), "no write was ever queued"
        # fetch_bbox has returned complete, correct points while a write is still parked inside the writer thread
        assert got["lon"].size == 28
        assert not list((lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*/*.parquet")), "the write was done inline"
        assert st["chunks_from_nasa"] == 3
        release.set()
        assert lake.drain_writes(timeout=20.0)
    finally:
        release.set()
        lake.write_point_chunk = orig
        lake.drain_writes(timeout=20.0)
    assert list((lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*/*.parquet")), "the write never landed"


def test_partially_cached_chunk_contributes_each_point_exactly_once():
    """The duplication hazard the reorder creates, reached the only way it can be.

    A chunk is re-fetched when ANY of its wanted cells is missing, and it is then decoded in FULL — including the cells
    that are still cached. Those cells' points are already in the lake read, so the fresh points must exclude them.

    Getting a chunk into that half-cached state takes work: write_point_chunk materializes every cell a chunk touches
    and all of them are marked together, so a normal overlapping query only ever sees whole chunks (an earlier version
    of this test used a wider bbox and silently proved nothing). EVICTION is what splits one: it deletes files and
    coverage per CELL, leaving the chunk's other cells cached.
    """
    _atl06_scene()
    index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)

    # evict a single cell that shares a chunk with a surviving cell
    by_chunk = {}
    for _g, _b, k, c in lake.ingested_chunk_cells("ATL06", [GRANULE]):
        by_chunk.setdefault(k, set()).add(c)
    straddling = next((cs for cs in by_chunk.values() if len(cs) > 1), None)
    assert straddling, "the fixture has no chunk spanning two cells — the hazard cannot occur"
    victim = sorted(straddling)[0]
    assert lake.evict_cells([victim], mission="ATL06", reason="test")

    got, st = index_atl06.fetch_bbox(BBOX)
    assert st["chunks_from_nasa"] >= 1 and st["chunks_from_lake"] >= 1, "the half-cached chunk was not re-fetched"
    rows = _rows(got)
    assert len(rows) == len(set(rows)), "a re-fetched chunk's still-cached cells came back twice"
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)


def test_force_refetch_does_not_double_the_result():
    """`force` re-fetches every chunk, so the pre-existing lake rows must NOT also be read back."""
    _atl06_scene()
    first, _ = index_atl06.fetch_bbox(BBOX)
    forced, st = index_atl06.fetch_bbox(BBOX, force=True)
    assert st["chunks_from_nasa"] == 3          # everything re-fetched
    _same(first, forced)


def test_coverage_is_marked_only_after_the_file_lands():
    """A write that fails must leave the chunk unmarked, so the next request re-fetches instead of trusting a gap."""
    _atl06_scene()
    real = lake.write_point_chunk
    failed = []          # _index_rows selects DISTINCT with no ORDER BY, so WHICH chunk comes first is not fixed

    def _fails_once(mission, granule, beam, chunk_index, *a, **kw):
        if not failed:
            failed.append(chunk_index)
            raise OSError("disk full")
        return real(mission, granule, beam, chunk_index, *a, **kw)

    orig, lake.write_point_chunk = lake.write_point_chunk, _fails_once
    try:
        got, _ = index_atl06.fetch_bbox(BBOX)
        assert lake.drain_writes(timeout=20.0)
    finally:
        lake.write_point_chunk = orig
    assert got["lon"].size == 28, "the failed write must not cost the caller its points"
    marked = lake.ingested_chunk_cells("ATL06", [GRANULE])
    assert marked, "the chunks that did write should be marked"
    assert {k for _g, _b, k, _c in marked} == {0, 1, 2} - set(failed), f"chunk {failed} was marked despite failing"

    # the next request re-fetches exactly the chunk that failed, and still returns the full result
    again, st = index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    assert st["chunks_from_nasa"] == 1 and st["chunks_from_lake"] == 2
    _same(got, again)


def test_drain_is_a_barrier_for_the_cells_it_names():
    """A second build over the same cells must not observe a half-written lake."""
    _atl06_scene()
    real = lake.write_point_chunk
    started, release = threading.Event(), threading.Event()

    def _slow(*a, **kw):
        started.set()
        release.wait(10.0)
        return real(*a, **kw)

    orig, lake.write_point_chunk = lake.write_point_chunk, _slow
    try:
        index_atl06.fetch_bbox(BBOX)
        assert started.wait(10.0)
        done = threading.Event()
        threading.Thread(target=lambda: (lake.drain_writes(timeout=20.0), done.set()), daemon=True).start()
        assert not done.wait(0.5), "drain returned while a write was still parked"
        release.set()
        assert done.wait(20.0), "drain never returned after the write was released"
    finally:
        release.set()
        lake.write_point_chunk = orig
        lake.drain_writes(timeout=20.0)
    assert lake.ingested_chunk_cells("ATL06", [GRANULE]), "drain returned before coverage was marked"


def test_synchronous_kill_switch_gives_the_same_result(monkeypatch):
    """AICESAT_LAKE_ASYNC_WRITE=0 restores inline writes; the points must be identical either way."""
    _atl06_scene()
    monkeypatch.setenv(lake.ASYNC_WRITE_ENV, "0")
    got, st = index_atl06.fetch_bbox(BBOX)
    # no drain: with the switch off the files and coverage are already there when fetch_bbox returns
    assert list((lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*/*.parquet"))
    assert lake.ingested_chunk_cells("ATL06", [GRANULE])
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)
    assert st["chunks_from_nasa"] == 3


def _two_granule_scene():
    """Two granules over the same track. One granule cannot distinguish 'one mark per leg' from 'one mark per
    granule' — the counts are equal — so the batching tests need at least two."""
    n = 30
    lat = np.linspace(69.6, 71.4, n); lon = np.full(n, -45.0)
    h = np.linspace(2500.0, 2530.0, n).astype("f4")
    q = np.zeros(n, "i1")
    _build_atl06(lat, lon, h, q)                                     # the default granule/url
    _build_atl06(lat, lon + 0.01, h, q, granule=GRANULE_2, url="https://x/atl06b.h5")


def _count_marks(fn):
    """Run fn() counting mark_ingested_many calls — meta.duckdb opens, ~150 ms each, serialised by _META_LOCK."""
    opens = []
    real = lake.mark_ingested_many
    orig, lake.mark_ingested_many = lake.mark_ingested_many, lambda m, items: (opens.append(len(items)), real(m, items))[1]
    try:
        fn()
    finally:
        lake.mark_ingested_many = orig
    return opens


def test_writer_batches_the_coverage_mark():
    """Marking per granule was ~150 ms per meta.duckdb open; the writer must keep what mark_ingested_many won."""
    _two_granule_scene()
    opens = _count_marks(lambda: (index_atl06.fetch_bbox(BBOX), lake.drain_writes(timeout=20.0)))
    assert opens, "coverage was never marked"
    assert len(opens) == 1, f"two granules took {len(opens)} meta.duckdb transactions"


def test_the_kill_switch_batches_the_mark_too(monkeypatch):
    """The switch is a BASELINE, so it must reproduce the pre-writer path — including its single batched mark.

    It did not: the sync path called _flush(force=self._q.empty()), and in sync mode the queue is ALWAYS empty, so it
    opened meta.duckdb once per granule. On the box that put 44.1 s over 518 calls into the baseline alone (against
    0.7 s over 3 with the writer on), inflating the measured win. A baseline carrying a cost the real code never had
    is not a baseline.
    """
    _two_granule_scene()
    monkeypatch.setenv(lake.ASYNC_WRITE_ENV, "0")
    # No drain here: the mark must land INSIDE the call, like the pre-writer path, or the baseline defers its own cost
    # past the measured wall time.
    opens = _count_marks(lambda: index_atl06.fetch_bbox(BBOX))
    assert opens, "the inline path deferred its coverage mark past the end of the leg"
    assert len(opens) == 1, f"the inline path took {len(opens)} meta.duckdb transactions for two granules"
    assert {g for g, _b, _k, _c in lake.ingested_chunk_cells("ATL06", [GRANULE, GRANULE_2])} == {GRANULE, GRANULE_2}


@pytest.mark.parametrize("workers", [1, 3])
def test_concurrent_builds_over_the_same_cells_agree(workers):
    """Two overlapping builds must each return the golden result, whichever wins the fetch."""
    _atl06_scene()
    out, errs = [], []

    def _go():
        try:
            out.append(index_atl06.fetch_bbox(BBOX)[0])
        except Exception as e:      # noqa: BLE001 - surfaced by the assert below
            errs.append(e)

    ts = [threading.Thread(target=_go) for _ in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60.0)
    assert not errs, errs
    assert lake.drain_writes(timeout=20.0)
    golden, _ = index_atl06._fetch_direct(BBOX)
    for got in out:
        rows = _rows(got)
        assert len(rows) == len(set(rows)), "concurrent builds duplicated points"
        _same(golden, got)


def test_the_fetch_pool_is_not_clamped_to_cpu_count():
    """Byte-range GETs wait on the network; sizing that pool by cores throws away the concurrency it needs.

    Measured on an 8-vCPU box: the same 1,415 MB leg took 34.8 s of fetch wall at 4 workers and 18.0 s at 16. The
    default path clamped to min(cap, cpu_count), so raising FETCH_WORKER_CAP alone would have silently capped it at
    8 — only the env override got past it, which is why every measurement needed AICESAT_FETCH_WORKERS set by hand.
    """
    import os as _os

    from aicesat.access import FETCH_MIN_GRANULES, FETCH_WORKER_CAP, FETCH_WORKER_ENV, pool_size

    ncpu = _os.cpu_count() or 1
    n = FETCH_WORKER_CAP + 8                       # plenty of granules, so n_items is not the binding term
    got = pool_size(n, cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV, cpu_bound=False)
    assert got == FETCH_WORKER_CAP, got
    if ncpu < FETCH_WORKER_CAP:                    # the clamp that was silently capping the fetch pool
        assert pool_size(n, cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV) == ncpu
    # a CPU-bound pool keeps the clamp
    assert pool_size(n, cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV,
                     cpu_bound=True) == min(FETCH_WORKER_CAP, ncpu)
    # never more workers than there is work
    assert pool_size(3, cap=FETCH_WORKER_CAP, min_items=FETCH_MIN_GRANULES, env=FETCH_WORKER_ENV, cpu_bound=False) == 3


# --------------------------------------------------------------------------------- the same contract for GLAS/ICESSN
def _glas_scene():
    from test_lake_cache import _build_glas
    n = 24
    _build_glas(np.linspace(69.6, 71.4, n), np.full(n, -45.0), np.linspace(2400.0, 2430.0, n))


def _icessn_scene():
    from test_lake_cache import _build_icessn
    n = 24
    _build_icessn(np.linspace(69.6, 71.4, n), np.full(n, -45.0), np.linspace(2400.0, 2430.0, n),
                  np.full(n, 4.5), np.zeros(n))


@pytest.mark.parametrize("mission", ["GLAS", "ICESSN"])
def test_every_mission_returns_before_its_lake_write(mission):
    """GLAS and ICESSN were still writing inline long after ATL06 stopped; this is the property they now share.

    Each returns its points from memory while a Parquet write is parked inside the writer thread. Without it the
    request waits on the filesystem for data it is already holding.
    """
    from aicesat import index_glas, index_icessn
    mod, scene, bbox = ((index_glas, _glas_scene, (-45.5, 69.5, -44.5, 71.5)) if mission == "GLAS"
                        else (index_icessn, _icessn_scene, (-45.5, 69.5, -44.5, 71.5)))
    scene()
    entered, release = threading.Event(), threading.Event()
    real = lake.write_point_chunk

    def _blocked(*a, **kw):
        entered.set()
        assert release.wait(10.0), "writer was never released"
        return real(*a, **kw)

    orig, lake.write_point_chunk = lake.write_point_chunk, _blocked
    try:
        got, st = mod.fetch_bbox(bbox)
        assert entered.wait(10.0), "no write was ever queued"
        assert got["lon"].size, "the fetch returned nothing"
        assert not list((lake.LAKE_DIR / f"mission={mission}").glob("h3_cell=*/*.parquet")), "the write was inline"
        release.set()
        assert lake.drain_writes(timeout=20.0)
    finally:
        release.set()
        lake.write_point_chunk = orig
        lake.drain_writes(timeout=20.0)
    golden, _ = mod._fetch_direct(bbox)
    _same(golden, got)
    rows = _rows(got)
    assert len(rows) == len(set(rows)), "points came back twice"
    assert list((lake.LAKE_DIR / f"mission={mission}").glob("h3_cell=*/*.parquet")), "the write never landed"


@pytest.mark.parametrize("mission", ["GLAS", "ICESSN"])
def test_every_mission_serves_a_repeat_from_the_lake_with_no_duplicates(mission):
    """The cache hit must go through the reordered read, and the second call must not double anything."""
    from aicesat import index_glas, index_icessn
    mod, scene = (index_glas, _glas_scene) if mission == "GLAS" else (index_icessn, _icessn_scene)
    bbox = (-45.5, 69.5, -44.5, 71.5)
    scene()
    first, st1 = mod.fetch_bbox(bbox)
    assert st1["chunks_from_nasa"] > 0
    second, st2 = mod.fetch_bbox(bbox)
    assert st2["chunks_from_nasa"] == 0 and st2.get("requests", 0) == 0, "the repeat hit the network"
    _same(first, second)
    rows = _rows(second)
    assert len(rows) == len(set(rows)), "the lake read doubled the cached points"
