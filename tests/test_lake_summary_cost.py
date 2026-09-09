"""lake_summary must not do work proportional to the size of the lake.

The Data Lake view polls /api/lake/summary every 8 s. lake_summary calls cell_stats(mission), which opened a Parquet
footer for EVERY file in the mission to total `rows` — 98,522 files for mission=ATL06 on the deployed box, 10.6 s a
call uncontended. Arriving faster than they complete, the polls stacked: 30 concurrent full scans, ~2 cores burned
flat, and in 225 s of a live page not one /api/lake/summary ever returned. The panel sat on "loading…" forever.
`missions()`, called from the same lake_summary, walked and stat()ed the whole tree a second time (1.2 s).

Same shape, same fix as index_status (test_index_status_cost.py): gate on directory mtime. A cell directory's mtime
changes whenever a Parquet lands in it or is removed — every lake write path either creates a new file or renames one
in (write_photons / write_point_chunk create; relayout is tmp-then-rename) — so one stat() per cell directory answers
"has anything here changed?" for the whole cell.
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
    getattr(lake, "_STATS_CACHE", {}).clear()      # module-level memo; tmp_path differs per test
    yield
    getattr(lake, "_STATS_CACHE", {}).clear()


def _write(mission, cell, name, n_rows):
    d = lake.cell_dir(mission, cell)
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"native_height": np.arange(n_rows, dtype="f4")}), d / f"{name}.parquet")


def _count_footer_reads(monkeypatch):
    """Count Parquet footer opens — the per-file work that must not repeat on an unchanged lake."""
    seen = []
    real = pq.read_metadata
    monkeypatch.setattr(pq, "read_metadata", lambda p, *a, **k: (seen.append(str(p)), real(p, *a, **k))[1])
    return seen


def _count_file_stats(monkeypatch):
    """Count stat() calls against Parquet files — missions() totalled bytes by stat()ing every one of them."""
    import pathlib
    seen = []
    real = pathlib.Path.stat
    monkeypatch.setattr(pathlib.Path, "stat",
                        lambda self, *a, **k: (seen.append(self.name) if self.name.endswith(".parquet") else None,
                                               real(self, *a, **k))[1])
    return seen


def test_a_repeat_summary_does_not_reread_parquet_footers(monkeypatch):
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    _write("ATL06", 222, "g1__gt1l__c1", 7)
    first = lake.cell_stats("ATL06")
    assert {c: s["rows"] for c, s in first.items()} == {111: 5, 222: 7}

    footers = _count_footer_reads(monkeypatch)
    again = [lake.cell_stats("ATL06") for _ in range(5)][-1]
    assert again == first, "the cached answer differs from the scanned one"
    assert not footers, f"re-read {len(footers)} Parquet footers on an unchanged lake"


def test_a_repeat_missions_does_not_restat_every_file(monkeypatch):
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    first = lake.missions()
    assert first == [{"mission": "ATL06", "product": lake.PRODUCTS.get("ATL06", "ATL06"), "cells": 1,
                      "bytes": first[0]["bytes"]}] and first[0]["bytes"] > 0

    stats = _count_file_stats(monkeypatch)
    again = [lake.missions() for _ in range(5)][-1]
    assert again == first, "the cached answer differs from the scanned one"
    assert not stats, f"re-stat()ed {len(stats)} Parquet files on an unchanged lake"


def test_a_new_file_in_an_existing_cell_is_picked_up():
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    assert lake.cell_stats("ATL06")[111]["rows"] == 5
    _write("ATL06", 111, "g1__gt1l__c1", 4)
    after = lake.cell_stats("ATL06")[111]
    assert (after["files"], after["rows"]) == (2, 9), "the directory-mtime gate hid a newly materialized chunk"


def test_a_new_cell_is_picked_up():
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    assert set(lake.cell_stats("ATL06")) == {111}
    _write("ATL06", 222, "g1__gt1l__c0", 3)
    assert set(lake.cell_stats("ATL06")) == {111, 222}, "the cache hid a newly materialized cell"


def test_an_evicted_cell_disappears():
    import shutil

    _write("ATL06", 111, "g1__gt1l__c0", 5)
    _write("ATL06", 222, "g1__gt1l__c0", 3)
    assert set(lake.cell_stats("ATL06")) == {111, 222}
    shutil.rmtree(lake.cell_dir("ATL06", 222))
    assert set(lake.cell_stats("ATL06")) == {111}, "the cache kept reporting an evicted cell"


def test_with_rows_false_does_not_poison_the_row_counts():
    """Eviction calls cell_stats(with_rows=False); a later summary must still get real row counts."""
    _write("ATL06", 111, "g1__gt1l__c0", 5)
    assert lake.cell_stats("ATL06", with_rows=False)[111]["rows"] == 0
    assert lake.cell_stats("ATL06")[111]["rows"] == 5, "the rows-free scan was cached as if it knew the row counts"
