"""Coverage manifest: the flat per-granule index is rolled down to one small DISTINCT (h3_cell, granule, ym) file so
a coverage query scans a single tiny file instead of every granule footer. Tests the build, the lazy-rebuild
staleness logic, and that check_coverage returns the same counts through the manifest."""
import os
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from aicesat import coverage, planner

GDATE_YM = "substr(gdate,1,4) || '-' || substr(gdate,5,2)"   # GLAS/ICESSN-style (matches coverage._index_for)


def _granule_parquet(d, name, rows):
    """One per-granule index parquet: rows = [(h3_cell, gdate), ...] all tagged with granule `name`."""
    cells = pa.array([c for c, _ in rows], type=pa.uint64())
    tbl = pa.table({"granule": [name] * len(rows), "h3_cell": cells, "gdate": [g for _, g in rows]})
    pq.write_table(tbl, d / f"{name}.parquet")


def _count_for(manifest, cells):
    pred = "h3_cell IN (" + ",".join(str(c) for c in cells) + ")"
    con = duckdb.connect()
    try:
        ng, nc = con.execute(f"SELECT count(DISTINCT granule), count(DISTINCT h3_cell) "
                             f"FROM read_parquet('{manifest}') WHERE {pred}").fetchone()
        by = con.execute(f"SELECT ym, count(DISTINCT granule) FROM read_parquet('{manifest}') "
                         f"WHERE {pred} GROUP BY ym ORDER BY ym").fetchall()
    finally:
        con.close()
    return int(ng), int(nc), {m: int(n) for m, n in by}


def test_manifest_builds_and_counts_are_correct(tmp_path):
    d = tmp_path / "idx"; d.mkdir()
    _granule_parquet(d, "G1", [(100, "20030415"), (101, "20030415")])            # cells 100,101
    _granule_parquet(d, "G2", [(101, "20190620"), (102, "20190620")])            # cells 101(shared),102
    manifest = coverage._ensure_manifest(d, GDATE_YM)
    assert manifest is not None and manifest.exists()
    # cell 101 is shared by both granules; a query over {101} sees 2 granules, 1 cell, across two months
    assert _count_for(manifest, [101]) == (2, 1, {"2003-04": 1, "2019-06": 1})
    # a query over {100,102} sees G1 and G2, 2 cells
    assert _count_for(manifest, [100, 102]) == (2, 2, {"2003-04": 1, "2019-06": 1})
    # a query over an unindexed cell sees nothing
    assert _count_for(manifest, [999]) == (0, 0, {})


def test_manifest_not_rebuilt_when_index_unchanged(tmp_path):
    d = tmp_path / "idx"; d.mkdir()
    _granule_parquet(d, "G1", [(100, "20030415")])
    m1 = coverage._ensure_manifest(d, GDATE_YM); mt1 = m1.stat().st_mtime
    time.sleep(0.01)
    m2 = coverage._ensure_manifest(d, GDATE_YM)                                  # unchanged index -> reuse, no rebuild
    assert m2.stat().st_mtime == mt1


def test_manifest_rebuilds_when_a_granule_is_added(tmp_path):
    d = tmp_path / "idx"; d.mkdir()
    _granule_parquet(d, "G1", [(100, "20030415")])
    manifest = coverage._ensure_manifest(d, GDATE_YM)
    assert _count_for(manifest, [100, 200])[0] == 1                             # only G1 so far
    # a newly indexed granule must be picked up (its mtime is newer than the manifest)
    time.sleep(0.01)
    _granule_parquet(d, "G2", [(200, "20190620")])
    os.utime(d / "G2.parquet", None)                                            # ensure a strictly newer mtime
    manifest = coverage._ensure_manifest(d, GDATE_YM)
    assert _count_for(manifest, [100, 200]) == (2, 2, {"2003-04": 1, "2019-06": 1})


def test_index_files_for_cells_names_only_touching_granules(tmp_path, monkeypatch):
    """The index query prunes to the granule files that actually touch the requested cells (the whole point: a
    few-cell query must not read every granule's index parquet)."""
    d = tmp_path / "glas"; d.mkdir()
    _granule_parquet(d, "G1", [(100, "20030415")])
    _granule_parquet(d, "G2", [(200, "20190620")])
    _granule_parquet(d, "G3", [(100, "20200101"), (300, "20200101")])   # also touches cell 100
    monkeypatch.setattr(coverage, "_index_for", lambda key: (d, 5, GDATE_YM) if key == "GLAS" else (None, None, None))

    files = coverage.index_files_for_cells("GLAS", [100])
    assert sorted(p.rsplit("/", 1)[-1] for p in files) == ["G1.parquet", "G3.parquet"]   # G2 never touches cell 100

    assert coverage.index_files_for_cells("GLAS", [999]) == []          # no granule touches it -> empty, NOT None
    assert coverage.index_files_for_cells("NOPE", [100]) is None        # unresolvable -> None -> caller falls back


def test_read_parquet_src_falls_back_to_directory_glob(tmp_path):
    """None (manifest unavailable) must produce the whole-directory glob, never an empty file list."""
    d = tmp_path / "idx"
    assert coverage.read_parquet_src(d, None) == f"read_parquet('{d}/*.parquet')"
    assert coverage.read_parquet_src(d, []) == f"read_parquet('{d}/*.parquet')"
    src = coverage.read_parquet_src(d, [f"{d}/G1.parquet", f"{d}/G2.parquet"])
    assert src == f"read_parquet(['{d}/G1.parquet', '{d}/G2.parquet'])"


def test_check_coverage_uses_manifest(tmp_path, monkeypatch):
    d = tmp_path / "glas"; d.mkdir()
    _granule_parquet(d, "G1", [(100, "20030415"), (101, "20030415")])
    _granule_parquet(d, "G2", [(101, "20190620")])
    (d / "_build.json").write_text('{"bbox": [-180, -90, 180, 90]}')            # index "covers" any bbox

    # only GLAS resolves to our fake dir; the other collections report not-indexed
    def fake_index_for(key):
        return (d, 5, GDATE_YM) if key == "GLAS" else (None, None, None)
    monkeypatch.setattr(coverage, "_index_for", fake_index_for)
    monkeypatch.setattr(planner, "cells_for_bbox", lambda bbox, res=5, polygon=None: [100, 101])

    res = coverage.check_coverage((-10, -10, 10, 10))
    glas = next(r for r in res["collections"] if r["key"] == "GLAS")
    assert glas["indexed"] is True and glas["n_granules"] == 2 and glas["cells"] == 2
    assert glas["by_month"] == {"2003-04": 1, "2019-06": 1}
    assert coverage._manifest_paths(d)[1].exists()                              # the manifest was materialized (in _coverage/)
    assert coverage._manifest_paths(d)[1] not in set(d.glob("*.parquet"))       # NOT a top-level *.parquet (the index byte-span globs must not see it)
    assert all(r["indexed"] is False for r in res["collections"] if r["key"] != "GLAS")
