"""Per-cell lake stats must survive a process restart, so the first Data Lake load after a deploy is not a full walk.

The mtime memo (test_lake_summary_cost.py) made a REPEAT summary free but left the FIRST one after a restart paying
the whole walk: 10.6 s on the deployed box, and ~30 s in practice because it competes with the four cold index_status
scans on the same page load. The rollup belongs in meta.duckdb, which cell_stats already opens for provenance.

Why a per-cell rollup and not a per-file ledger: correctness needs to know whether the disk moved under us (relayout
rewrites files, eviction removes them, a build can die mid-write), and the cheapest sound check is one stat() per cell
directory. Given we pay that anyway, per-file rows buy no extra guarantee — and a rollup keyed on the directory mtime
self-heals against a writer that never recorded, which a ledger cannot.
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import lake


@pytest.fixture(autouse=True)
def _lake_env(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "index" / "meta.duckdb")
    monkeypatch.setattr(lake, "SETTINGS_PATH", tmp_path / "index" / "settings.json")
    monkeypatch.setattr(lake, "EVICTION_LOG", tmp_path / "index" / "evictions.jsonl")
    lake._STATS_CACHE.clear()
    yield
    lake._STATS_CACHE.clear()


def _write(mission, cell, name, n_rows):
    d = lake.cell_dir(mission, cell)
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"native_height": np.arange(n_rows, dtype="f4")}), d / f"{name}.parquet")


def _restart():
    """Simulate a fresh server process: the in-memory memo is gone, meta.duckdb is not."""
    lake._STATS_CACHE.clear()


def _count_footer_reads(monkeypatch):
    seen = []
    real = pq.read_metadata
    monkeypatch.setattr(pq, "read_metadata", lambda p, *a, **k: (seen.append(str(p)), real(p, *a, **k))[1])
    return seen


def test_a_restart_does_not_rewalk_the_lake(monkeypatch):
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    _write("ATL06", 222, "g1__gt1l__c1", 7)
    first = lake.cell_stats("ATL06")
    assert {c: s["rows"] for c, s in first.items()} == {111: 5, 222: 7}

    _restart()
    footers = _count_footer_reads(monkeypatch)
    after = lake.cell_stats("ATL06")
    assert after == first, "the persisted answer differs from the scanned one"
    assert not footers, f"re-read {len(footers)} Parquet footers after a restart — the rollup did not persist"


def test_a_cell_changed_while_the_process_was_down_is_rescanned():
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    assert lake.cell_stats("ATL06")[111]["rows"] == 5
    _restart()
    _write("ATL06", 111, "g1__gt1l__c1", 4)          # a file lands while "the server is off"
    after = lake.cell_stats("ATL06")[111]
    assert (after["files"], after["rows"]) == (2, 9), "the persisted rollup outranked the directory mtime"


def test_an_evicted_cell_leaves_no_persisted_row():
    import shutil

    _write("ATL06", 111, "g1__gt1l__c0", 5)
    _write("ATL06", 222, "g1__gt1l__c0", 3)
    assert set(lake.cell_stats("ATL06")) == {111, 222}
    shutil.rmtree(lake.cell_dir("ATL06", 222))
    _restart()
    assert set(lake.cell_stats("ATL06")) == {111}, "an evicted cell came back from meta.duckdb"


def test_the_persisted_rollup_equals_a_full_walk():
    """The whole point: persistence must not change a single reported number."""
    for cell, (name, n) in {111: ("g1__gt1l__c0", 5), 222: ("g1__gt1l__c1", 7), 333: ("g2__gt2r__c0", 11)}.items():
        _write("ATL06", cell, name, n)
    _write("ATL06", 111, "g2__gt2r__c3", 2)
    persisted = lake.cell_stats("ATL06")
    _restart()
    lake._forget_persisted_stats("ATL06")            # force the honest full walk to compare against
    walked = lake.cell_stats("ATL06")
    assert persisted == walked


def test_with_rows_false_does_not_persist_bogus_row_counts():
    """Eviction calls cell_stats(with_rows=False); that must not be remembered as if the rows were known."""
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    assert lake.cell_stats("ATL06", with_rows=False)[111]["rows"] == 0
    _restart()
    assert lake.cell_stats("ATL06")[111]["rows"] == 5, "a rows-free scan was persisted as if it knew the row counts"
