"""Per-granule batched lake writes (issue #23), behind AICESAT_LAKE_BATCH_WRITES.

Why bother: on the box a 4,566-chunk leg spent 115.9 write thread-seconds over ~32k files, ~3.6 ms each, and adding
writer threads made it WORSE (247.9 thread-seconds at 4 threads) — a fixed serialized resource, not thread starvation.
So the lever is fewer files, not more threads.

The risk is in what a re-fetch does to a file that already exists. The name is DETERMINISTIC per
(cell, granule, beam), so a re-fetch MERGES: read the file, drop the rows for the chunks being rewritten, concatenate
the new ones. An earlier attempt named each file by the chunk SET it held, which forced a d.glob() per cell per job
to discover what was there — a scan whose cost grows with the LAKE, and it made a batched build 36% SLOWER on the box
(84.9s vs 62.5s). Tests here pin: no directory listing, no loss on a partial re-fetch, no duplication against either
a merged file or a legacy per-chunk one.
"""
import numpy as np
import pytest

from aicesat import index_atl06, lake

from test_lake_cache import _atl06_scene, _build_atl06, _lake_env, _same   # noqa: F401

BBOX = (-45.5, 69.5, -44.5, 71.5)
GRANULE = "ATL06_20200115000000_11760601_007_01.h5"
LONG_GRANULE = "ATL06_20200310000000_11760601_007_01.h5"


@pytest.fixture
def batched(monkeypatch):
    monkeypatch.setenv(lake.BATCH_WRITE_ENV, "1")


def _files():
    return sorted(p.name for p in (lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*/*.parquet"))


def _rows(arr):
    return [tuple(np.asarray(arr[k])[i] for k in ("lon", "lat", "h")) for i in range(arr["lon"].size)]


def _reread():
    """A pure cache-hit fetch, i.e. the lake read on its own.

    This is where on-disk duplication actually shows up, and the reason an earlier version of these tests was
    vacuous: the fetch that WRITES a bad file cannot see it. `force=True` skips the lake read altogether, and on the
    partial-refetch path the lake read happens BEFORE the write. Both returned correct points while leaving a
    corrupted lake behind. Only the next query touches it.
    """
    got, st = index_atl06.fetch_bbox(BBOX)
    assert st["chunks_from_nasa"] == 0, "not a cache hit — this is not reading what is on disk"
    return got


def test_batched_write_matches_the_direct_golden_and_writes_fewer_files(batched):
    """Same points, fewer files. The fixture's granule has 3 chunks over shared cells."""
    _atl06_scene()
    got, _ = index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    batched_files = _files()

    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)
    rows = _rows(got)
    assert len(rows) == len(set(rows)), "batched write duplicated points"

    # same scene again with batching OFF, into a clean lake, to compare file counts on equal terms
    import shutil
    shutil.rmtree(lake.LAKE_DIR, ignore_errors=True)
    lake.META_DB.unlink(missing_ok=True)
    import os
    os.environ[lake.BATCH_WRITE_ENV] = "0"
    try:
        per_chunk, _ = index_atl06.fetch_bbox(BBOX)
        assert lake.drain_writes(timeout=20.0)
        unbatched_files = _files()
    finally:
        os.environ[lake.BATCH_WRITE_ENV] = "1"
    _same(per_chunk, got)                                   # identical points either way
    assert len(batched_files) < len(unbatched_files), (batched_files, unbatched_files)


def test_the_write_never_lists_a_cell_directory(batched, monkeypatch):
    """The regression that made batching 36% SLOWER than per-chunk on the box.

    Naming each file by the chunk set it held meant discovering what already existed with d.glob(...) per cell per
    job — a directory scan whose cost grows with the LAKE, not the request. That is the same shape as the 145 s
    query_points bug and the cell_stats footer reads, and it is why the filename is now deterministic and existence
    is one O(1) stat.
    """
    import pathlib
    globbed = []
    real = pathlib.Path.glob
    monkeypatch.setattr(pathlib.Path, "glob",
                        lambda self, pat, *a, **k: (globbed.append((str(self), pat)), real(self, pat, *a, **k))[1])
    _atl06_scene()
    index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    scans = [g for g in globbed if "h3_cell=" in g[0]]
    assert not scans, f"the write path listed cell directories: {scans[:5]}"


def test_one_file_per_cell_granule_beam(batched):
    _atl06_scene()
    index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    names = _files()
    assert names, "nothing was written"
    assert all(n.endswith(f"__{lake.BATCH_SUFFIX}.parquet") for n in names), names


def test_partial_refetch_neither_loses_nor_duplicates(batched):
    """The hazard batching creates, reached the only way it can be: eviction splits a chunk's cells, so the re-fetch
    carries only some of the chunks an existing file holds."""
    _atl06_scene()
    index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)

    by_chunk = {}
    for _g, _b, k, c in lake.ingested_chunk_cells("ATL06", [GRANULE]):
        by_chunk.setdefault(k, set()).add(c)
    straddling = next((cs for cs in by_chunk.values() if len(cs) > 1), None)
    assert straddling, "the fixture has no chunk spanning two cells — the hazard cannot occur"
    assert lake.evict_cells([sorted(straddling)[0]], mission="ATL06", reason="test")

    got, st = index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    assert st["chunks_from_nasa"] >= 1 and st["chunks_from_lake"] >= 1, "no partial re-fetch happened"
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)                                      # nothing lost from the cells it did not evict

    again = _reread()                                       # the lake as the re-fetch left it
    rows = _rows(again)
    assert len(rows) == len(set(rows)), "the partial re-fetch duplicated rows against the surviving batch file"
    _same(golden, again)


def test_a_superseding_batch_removes_the_files_it_replaces(batched, monkeypatch):
    """A batch covering a superset of an existing file's chunks must delete it, or its rows come back twice."""
    _atl06_scene()
    monkeypatch.setenv(lake.BATCH_WRITE_ENV, "0")
    index_atl06.fetch_bbox(BBOX)                            # lay down per-chunk files first
    assert lake.drain_writes(timeout=20.0)
    before = _files()
    assert before and all("__c" in n and not n.endswith(f"__{lake.BATCH_SUFFIX}.parquet") for n in before)

    monkeypatch.setenv(lake.BATCH_WRITE_ENV, "1")
    got, _ = index_atl06.fetch_bbox(BBOX, force=True)        # re-fetch everything, now batched
    assert lake.drain_writes(timeout=20.0)
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)

    # force skips the lake read, so `got` cannot see a stale per-chunk file left beside the new batch — only the
    # next query can. That is where the duplication would be.
    again = _reread()
    rows = _rows(again)
    assert len(rows) == len(set(rows)), "the batch did not supersede the per-chunk files it replaced"
    _same(golden, again)


def _long_track_scene():
    """A track far longer than the query box, which is the production geometry and what the small fixture lacks.

    An ATL06 chunk is 10,000 segments (~400 km) while a scene bbox is tens of km, so a chunk touches many cells the
    query never asked for. _atl06_scene()'s track sits entirely INSIDE its bbox, so it writes no out-of-bbox cells at
    all — a wanted-cells-only test built on it passes whether the restriction works or not.
    """
    n = 60
    lat = np.linspace(66.0, 75.0, n); lon = np.full(n, -45.0)
    h = np.linspace(2500.0, 2560.0, n).astype("f4")
    q = np.zeros(n, "i1")
    _build_atl06(lat, lon, h, q, C=20, granule=LONG_GRANULE, url="https://x/atl06long.h5")


def _cells_written():
    return {int(p.name.split("=")[1]) for p in (lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*")}


def test_the_default_does_not_pre_cache_cells_outside_the_request(monkeypatch):
    """A fetched block covers far more track than the request, and by default we now DISCARD the rest.

    The smallest readable unit is one 10,000-segment block, which spans a whole scene-sized box (measured: 1.02
    blocks per granule-beam for a 33 km box). Writing the entire decoded strip pre-cached the neighbouring cells for
    a later pan and cost 2.6x the write work on every build (114.5 -> 43.9 thread-seconds on the box). The points
    returned must be identical either way — only the cache footprint changes.
    """
    narrow = (-45.5, 69.5, -44.5, 70.0)                     # a slice of a track running 66N to 75N
    _long_track_scene()
    monkeypatch.setenv(index_atl06.PRECACHE_ENV, "1")       # the old behaviour, kept for A/B
    full, _ = index_atl06.fetch_bbox(narrow)
    assert lake.drain_writes(timeout=20.0)
    full_cells = _cells_written()

    import shutil
    shutil.rmtree(lake.LAKE_DIR, ignore_errors=True)
    lake.META_DB.unlink(missing_ok=True)
    monkeypatch.delenv(index_atl06.PRECACHE_ENV, raising=False)   # the DEFAULT
    assert not index_atl06.precache_adjacent()
    lean, _ = index_atl06.fetch_bbox(narrow)
    assert lake.drain_writes(timeout=20.0)
    lean_cells = _cells_written()

    golden, _ = index_atl06._fetch_direct(narrow)
    _same(golden, full)
    _same(golden, lean)                                     # identical points either way — only the cache differs

    want = set(index_atl06._index_rows(narrow, None, index_atl06.ATL06_RES, False)[0])
    assert lean_cells <= want, (lean_cells - want)
    assert len(lean_cells) < len(full_cells), (
        f"the fixture wrote no out-of-bbox cells ({len(full_cells)}), so this proves nothing")
