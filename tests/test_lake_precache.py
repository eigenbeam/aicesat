"""What a fetch writes to the lake beyond what the request asked for.

The smallest thing a byte-range fetch can read is one 10,000-segment block, which spans far more track than a
scene-sized box (measured: 1.02 blocks per granule-beam for a 33 km box). We used to write the ENTIRE decoded strip,
pre-caching neighbouring cells for a pan that may never happen. On the box that cost 114.5 -> 39.9 write
thread-seconds — 2.87x the write work on every build — and it was the single largest win of the performance work.
"""
import numpy as np
import pytest   # noqa: F401  (fixtures)

from aicesat import index_atl06, lake

from test_lake_cache import _build_atl06, _lake_env, _same   # noqa: F401

BBOX = (-45.5, 69.5, -44.5, 71.5)
LONG_GRANULE = "ATL06_20200310000000_11760601_007_01.h5"


def _reread(bbox):
    """A pure cache-hit fetch, i.e. the lake read on its own — where on-disk damage actually shows up."""
    got, st = index_atl06.fetch_bbox(bbox)
    assert st["chunks_from_nasa"] == 0, "not a cache hit — this is not reading what is on disk"
    return got


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
