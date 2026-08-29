"""Per-granule batched lake writes (issue #23), behind AICESAT_LAKE_BATCH_WRITES.

Why bother: on the box a 4,566-chunk leg spent 115.9 write thread-seconds over ~32k files, ~3.6 ms each, and adding
writer threads made it WORSE (247.9 thread-seconds at 4 threads) — a fixed serialized resource, not thread starvation.
So the lever is fewer files, not more threads.

The whole risk of batching is in the FILENAME. A file must name the chunk set it holds:
  * name it by granule+beam only, and a later partial re-fetch overwrites a file holding chunks it does not carry,
    silently DELETING cached data;
  * name it by the set, and every write is either an exact rewrite or a new file.
Then a batch that strictly supersedes older files may delete them, and a PARTIAL overlap must fall back to the
per-chunk layout rather than duplicate rows. Each of those is a test here.
"""
from pathlib import Path

import numpy as np
import pytest

from aicesat import index_atl06, lake

from test_lake_cache import _atl06_scene, _lake_env, _same   # noqa: F401

BBOX = (-45.5, 69.5, -44.5, 71.5)
GRANULE = "ATL06_20200115000000_11760601_007_01.h5"


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


def test_the_filename_names_the_chunk_set(batched):
    """Not negotiable: without the set in the name, a partial re-fetch cannot tell what it is about to overwrite."""
    _atl06_scene()
    index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    names = _files()
    assert names, "nothing was written"
    for n in names:
        chunks = lake._file_chunks(Path(n))
        assert chunks, f"{n} does not name its chunk set"
    assert any(len(lake._file_chunks(Path(n))) > 1 for n in names), "nothing was actually batched"


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
    assert all(len(lake._file_chunks(Path(n))) == 1 for n in before)

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


def test_a_too_long_tag_falls_back_to_per_chunk(batched, monkeypatch):
    """Filenames are bounded; a batch that cannot name its set must use the always-safe layout, not a lossy name."""
    monkeypatch.setattr(lake, "MAX_TAG_LEN", 3)             # any multi-chunk tag is now too long
    _atl06_scene()
    got, _ = index_atl06.fetch_bbox(BBOX)
    assert lake.drain_writes(timeout=20.0)
    names = _files()
    assert names and all(len(lake._file_chunks(Path(n))) == 1 for n in names), names
    golden, _ = index_atl06._fetch_direct(BBOX)
    _same(golden, got)
