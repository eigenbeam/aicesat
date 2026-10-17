"""One lake file per (cell, granule, chunk, beam GROUP), and the planner fetches a group whole.

Six files per (cell, chunk) were 6,897 files for 2,960 chunks in one 8-cell ATL06 ingest, at 3-6 ms each. The lake
now writes the strong beams' chunk k into one file and the weak beams' into another. Two things make that safe and
both are pinned here: the planner asks for the strong beams alone by default and for all six from atl06.extract, so
the two sets must never share a name; and a group's file is a full overwrite of a deterministic name, so a group must
always be re-fetched whole, or one beam's re-fetch would silently drop its siblings' points from the file.
"""
import numpy as np
import pytest

from aicesat import index_atl06, lake

from test_lake_cache import _build_atl06, _lake_env, _same   # noqa: F401  (autouse env + helpers)

GRANULE = "ATL06_20200115000000_11760601_007_01.h5"
BBOX = (-45.5, 69.5, -44.5, 71.5)


def _two_beam_scene():
    """A strong and a weak beam over the same track; 30 segments, 3 chunks of 10."""
    n = 30
    lat = np.linspace(69.6, 71.4, n)
    lon = np.full(n, -45.0)
    strong = ("gt1r", True, lat, lon, np.linspace(2500.0, 2530.0, n).astype("f4"), np.zeros(n, "i1"))
    weak = ("gt1l", False, lat, lon + 0.001, np.linspace(2600.0, 2630.0, n).astype("f4"), np.zeros(n, "i1"))
    _build_atl06(lat, lon, None, None, beams=[strong, weak])


def _files():
    return sorted(p.name for p in (lake.LAKE_DIR / "mission=ATL06").glob("h3_cell=*/*.parquet"))


def test_strong_then_all_beams_write_two_group_files_and_never_duplicate():
    _two_beam_scene()
    got_strong, st = index_atl06.fetch_bbox(BBOX)                       # the planner's default: strong only
    assert lake.drain_writes(timeout=20.0)
    names = _files()
    assert names and all("__strong__c" in n for n in names), names
    assert st["chunks_from_nasa"] == 3
    _same(index_atl06._fetch_direct(BBOX)[0], got_strong)

    got_all, st = index_atl06.fetch_bbox(BBOX, strong_only=False)        # what atl06.extract asks for
    assert lake.drain_writes(timeout=20.0)
    names = _files()
    assert {n.split("__")[1] for n in names} == {"strong", "weak"}
    assert st["chunks_from_lake"] == 3 and st["chunks_from_nasa"] == 3, "only the weak group was fetched"
    golden, _ = index_atl06._fetch_direct(BBOX, strong_only=False)
    _same(golden, got_all)

    again, st = index_atl06.fetch_bbox(BBOX, strong_only=False)          # a pure cache hit now
    assert st["chunks_from_nasa"] == 0
    _same(golden, again)

    forced, _ = index_atl06.fetch_bbox(BBOX, strong_only=False, force=True)
    assert lake.drain_writes(timeout=20.0)
    assert _files() == names, "force rewrote the same names"
    _same(golden, forced)


def test_a_group_is_refetched_whole_so_a_siblings_points_survive():
    """Beam gt2r reaches a cell gt1r never enters. A first query over the shared cells writes the strong file with both
    beams; a second query that adds the far cell needs chunk k of gt2r only. Fetching gt2r alone would rewrite the
    shared cells' file with gt2r's points and drop gt1r's while gt1r stayed marked."""
    n = 30
    lat = np.linspace(69.6, 71.4, n)
    lon = np.full(n, -45.0)
    far_lat = lat.copy(); far_lat[-10:] += 0.9                          # gt2r's last chunk leaves for a cell of its own
    a = ("gt1r", True, lat, lon, np.linspace(2500.0, 2530.0, n).astype("f4"), np.zeros(n, "i1"))
    b = ("gt2r", True, far_lat, lon + 0.002, np.linspace(2600.0, 2630.0, n).astype("f4"), np.zeros(n, "i1"))
    _build_atl06(lat, lon, None, None, beams=[a, b])

    shared = (-45.5, 69.5, -44.5, 71.5)
    first, _ = index_atl06.fetch_bbox(shared)
    assert lake.drain_writes(timeout=20.0)
    _same(index_atl06._fetch_direct(shared)[0], first)

    wider = (-45.5, 69.5, -44.5, 72.5)                                  # adds the far cell only gt2r reaches
    second, st = index_atl06.fetch_bbox(wider)
    assert lake.drain_writes(timeout=20.0)
    assert st["chunks_from_nasa"] >= 1
    _same(index_atl06._fetch_direct(wider)[0], second)

    # the lake alone must still hold every point of both beams over the shared box
    cached, st = index_atl06.fetch_bbox(shared)
    assert st["chunks_from_nasa"] == 0, "everything should now come from the lake"
    _same(index_atl06._fetch_direct(shared)[0], cached)


def test_write_cell_file_lands_by_rename(tmp_path):
    import pyarrow as pa
    p = tmp_path / "h3_cell=1" / "g__strong__c0.parquet"
    lake.write_cell_file(p, pa.table({"x": [1, 2]}))
    assert p.exists() and not list(p.parent.glob(".*.tmp"))
    lake.write_cell_file(p, pa.table({"x": [3]}))                        # a rewrite replaces, never appends
    import pyarrow.parquet as pq
    assert pq.read_table(p).column("x").to_pylist() == [3]


def test_a_failed_cell_file_marks_no_beam_of_the_group_there(monkeypatch):
    _two_beam_scene()
    real = lake.write_cell_file
    failed = []

    def _fail_one(path, tbl):
        if not failed:
            failed.append((path.stem.split("__")[1], int(path.stem.rsplit("__c", 1)[1]), int(path.parent.name.split("=", 1)[1])))
            raise OSError("disk full")
        return real(path, tbl)

    monkeypatch.setattr(lake, "write_cell_file", _fail_one)
    got, _ = index_atl06.fetch_bbox(BBOX, strong_only=False)
    assert lake.drain_writes(timeout=20.0)
    (group, k, cell), = failed
    bad_beam, other_beam = ("gt1r", "gt1l") if group == "strong" else ("gt1l", "gt1r")
    marked = {(b, kk, c) for _g, b, kk, c in lake.ingested_chunk_cells("ATL06", [GRANULE])}
    assert (bad_beam, k, cell) not in marked, f"{bad_beam} was marked in cell {cell} for chunk {k} although its group's file failed"
    assert (other_beam, k, cell) in marked, "the other group's file in that cell landed and is marked"
    monkeypatch.setattr(lake, "write_cell_file", real)
    again, st = index_atl06.fetch_bbox(BBOX, strong_only=False)
    assert lake.drain_writes(timeout=20.0)
    _same(got, again)
    assert (bad_beam, k, cell) in {(b, kk, c) for _g, b, kk, c in lake.ingested_chunk_cells("ATL06", [GRANULE])}
