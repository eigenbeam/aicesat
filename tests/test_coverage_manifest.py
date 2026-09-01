"""Coverage manifest: the flat per-granule index is rolled down to one small DISTINCT (h3_cell, granule, ym) file so
a coverage query scans a single tiny file instead of every granule footer. Tests the build, the lazy-rebuild
staleness logic, and that check_coverage returns the same counts through the manifest."""
import os
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from aicesat import cache, coverage, planner

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


# ------------------------------------------------------------ the rollup belongs to the builder, not to the reader
def _index_dir(monkeypatch, tmp_path):
    d = tmp_path / "idx"; d.mkdir(exist_ok=True)
    monkeypatch.setattr(coverage, "_index_for",
                        lambda key: (d, 5, GDATE_YM) if key == "ATL06" else (None, None, None))
    return d


def _granule(d, name, cells):
    _granule_parquet(d, name, [(c, "20200115") for c in cells])



def test_a_fresh_manifest_costs_one_stat_not_a_directory_walk(tmp_path, monkeypatch):
    """Checking freshness used to glob every source parquet and stat each one — ~32,060 files for ATL06 on the box,
    paid on every coverage query just to conclude nothing had changed."""
    d = _index_dir(monkeypatch, tmp_path)
    _granule(d, "ATL06_20200115000000_11760601_007_01", [600000000000000000])
    coverage.build_manifest("ATL06")

    import pathlib
    globs = []
    real = pathlib.Path.glob
    monkeypatch.setattr(pathlib.Path, "glob",
                        lambda self, pat, *a, **k: (globs.append((str(self), pat)), real(self, pat, *a, **k))[1])
    assert coverage._ensure_manifest(d, coverage._index_for("ATL06")[2]) is not None
    assert not [g for g in globs if g[1] == "*.parquet"], f"walked the index dir: {globs}"


def test_an_index_that_grew_after_the_rollup_is_still_caught(tmp_path, monkeypatch):
    """The O(1) check must not trade correctness for speed: a new granule has to invalidate the manifest."""
    d = _index_dir(monkeypatch, tmp_path)
    _granule(d, "ATL06_20200115000000_11760601_007_01", [600000000000000000])
    coverage.build_manifest("ATL06")
    assert coverage._manifest_fresh(d)

    _granule(d, "ATL06_20200220000000_11760601_007_01", [600000000000000002])   # someone indexed without rolling up
    assert not coverage._manifest_fresh(d), "a new granule did not invalidate the manifest"
    files = coverage.index_files_for_cells("ATL06", [600000000000000002])
    assert files and any("20200220" in f for f in files), files   # and the lazy repair path found it


def test_build_manifest_is_explicit_and_reports_what_it_rolled_up(tmp_path, monkeypatch):
    d = _index_dir(monkeypatch, tmp_path)
    assert coverage.build_manifest("ATL06")["built"] is False      # nothing indexed yet
    _granule(d, "ATL06_20200115000000_11760601_007_01", [600000000000000000])
    out = coverage.build_manifest("ATL06")
    assert out["built"] is True and out["granule_files"] == 1
    assert coverage._manifest_fresh(d)


def test_indexed_and_covered_are_reported_separately(tmp_path, monkeypatch):
    """A cell can hold index rows while the query area still reaches outside the built rectangle.

    The Explore panel used to gate the whole row on containment and report "not indexed" over areas the Lake view
    showed as indexed — the two views contradicted each other. `indexed` now answers "is there data here" and
    `covered` answers "will a build accept this area", because only containment guarantees the granule set is
    complete: outside the built rectangle the index holds only granules that also crossed it.
    """
    from aicesat import coverage as cov

    d = tmp_path / "idx"
    d.mkdir()
    monkeypatch.setattr(cov, "_index_for", lambda key: (d, 5, "ym") if key == "GLAS" else (None, None, None))
    monkeypatch.setattr(cov, "_ensure_manifest", lambda dd, ym: None)   # index dir present, nothing rolled up yet

    from aicesat import index as index_mod
    from aicesat import planner
    built = (-51.0, 69.0, -50.0, 69.5)
    index_mod.write_build_manifest(d, built, 5, cells=planner.cells_for_bbox(built, res=5))
    inside = cov.check_coverage((-50.8, 69.1, -50.2, 69.4))["collections"][0]
    outside = cov.check_coverage((-50.8, 69.1, -49.0, 69.4))["collections"][0]
    assert inside["covered"] is True, "an area within the built box must be buildable"
    assert outside["covered"] is False, "an area reaching past the built box must not be reported as buildable"


# --- CMR returns every file twice; filtering at the source halves the pagination -------------------------------------
def test_search_asks_for_cloud_hosted_granules_only(monkeypatch, tmp_path):
    """CMR lists a Cumulus copy and a retired on-prem copy of every file. dedup_granules threw the duplicate away
    AFTER paging through it; filtering at the source halves the result set (measured 3.49 s -> 2.38 s)."""
    import earthaccess

    seen = []

    def fake(count=-1, **kw):
        seen.append(kw)
        return [{"meta": {"native-id": "g1"}, "umm": {}}]

    monkeypatch.setattr(earthaccess, "search_data", fake)
    monkeypatch.setattr(coverage, "dedup_granules", lambda g: g)
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(coverage.auth, "login", lambda *a, **k: None)
    coverage.search("ATL06", "007", (-50, 69, -49, 70), None, use_cache=False)
    assert seen and seen[0].get("cloud_hosted") is True, seen


def test_search_falls_back_when_the_cloud_filter_finds_nothing(monkeypatch, tmp_path):
    """A filter that silently returns nothing would build an empty index and report success."""
    import earthaccess

    calls = []

    def fake(count=-1, **kw):
        calls.append(kw)
        return [] if kw.get("cloud_hosted") else [{"meta": {"native-id": "g1"}, "umm": {}}]

    monkeypatch.setattr(earthaccess, "search_data", fake)
    monkeypatch.setattr(coverage, "dedup_granules", lambda g: g)
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(coverage.auth, "login", lambda *a, **k: None)
    got = coverage.search("ATL06", "007", (-50, 69, -49, 70), None, use_cache=False)
    assert len(got) == 1, "must fall back rather than report an empty collection"
    assert len(calls) == 2 and calls[0].get("cloud_hosted") and "cloud_hosted" not in calls[1]
