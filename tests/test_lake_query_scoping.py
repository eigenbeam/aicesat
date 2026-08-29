"""The lake read must address only the REQUESTED cells' files.

A single `h3_cell=*` glob makes DuckDB reconcile the schema of every chunk file in the mission before the h3_cell
predicate can prune it — on the deployed box that was ~500k files and 145 s for a query touching 7 cells. These tests
pin the scoping (correctness with unrelated cells present) and the absence of a whole-mission glob in the SQL."""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aicesat import lake


@pytest.fixture
def lake_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    return tmp_path / "lake"


def _chunk(mission, cell, granule, lon, lat, h, n=10, chunk=0, quality=0):
    d = lake.cell_dir(mission, cell); d.mkdir(parents=True, exist_ok=True)
    t = pa.table({"native_lon": np.full(n, lon), "native_lat": np.full(n, lat),
                  "native_height": np.full(n, h, "f8"), "t": pa.array(np.zeros(n, "datetime64[ms]")),
                  "source_granule": pa.array([granule] * n).dictionary_encode(),
                  "beam": pa.array(["gt1l"] * n).dictionary_encode(),
                  "source_chunk_index": np.full(n, chunk, "i4"),
                  "quality": np.full(n, quality, "i1")})
    pq.write_table(t, d / f"{granule}__gt1l__c{chunk}.parquet")


def test_query_points_reads_only_requested_cells(lake_dir):
    # three cells; the query asks for two. The third must not appear even though its rows satisfy the bbox.
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0)
    _chunk("ATL06", 222, "G2", -49.5, 69.1, 500.0)
    _chunk("ATL06", 333, "G3", -49.5, 69.1, 600.0)   # unrelated cell, same coordinates
    out = lake.query_points((-50, 69, -49, 70), [111, 222], "ATL06")
    assert out["lon"].size == 20                       # only the two requested cells' rows
    assert set(np.unique(out["h"])) == {400.0, 500.0}  # cell 333's rows (600.0) were never read


def test_query_points_ignores_absent_and_empty_cells(lake_dir):
    """A requested cell with no directory (or no chunk files) must be skipped, not crash the query: DuckDB errors on
    a glob that matches nothing."""
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0)
    lake.cell_dir("ATL06", 999).mkdir(parents=True, exist_ok=True)   # exists but empty
    out = lake.query_points((-50, 69, -49, 70), [111, 999, 12345], "ATL06")   # 12345 never existed
    assert out["lon"].size == 10


def test_query_points_empty_when_no_requested_cell_has_data(lake_dir):
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0)
    out = lake.query_points((-50, 69, -49, 70), [777], "ATL06")
    assert out["lon"].size == 0 and out["h"].size == 0


def test_query_points_extra_cols_and_quality_filter_still_apply(lake_dir):
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0, quality=0)
    _chunk("ATL06", 111, "G2", -49.5, 69.1, 450.0, chunk=1, quality=1)   # filtered out by quality_zero
    out = lake.query_points((-50, 69, -49, 70), [111], "ATL06", extra_cols=("quality",), quality_zero=True)
    assert out["lon"].size == 10 and set(np.unique(out["quality"])) == {0}


def test_query_points_sql_does_not_glob_the_whole_mission(lake_dir, monkeypatch):
    """Guard the actual defect: the SQL must not contain an `h3_cell=*` wildcard (which forces a whole-mission scan)."""
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0)
    seen = {}
    import duckdb
    real = duckdb.connect

    class Spy:
        def __init__(self, con): self._con = con
        def execute(self, sql, *a, **kw):
            seen.setdefault("sql", []).append(sql)
            return self._con.execute(sql, *a, **kw)
        def close(self): self._con.close()

    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: Spy(real(*a, **kw)))
    lake.query_points((-50, 69, -49, 70), [111], "ATL06")
    joined = " ".join(seen["sql"])
    assert "h3_cell=*" not in joined                    # the whole-mission glob is gone
    assert "h3_cell=111" in joined                      # addressed by the requested cell instead


def test_cell_stats_without_rows_skips_footer_reads(lake_dir, monkeypatch):
    """Eviction only needs bytes (stat) and age (meta db) — never row counts, which cost a Parquet footer read per
    file across the whole mission. On the deployed lake that was ~300s per build. Guard that the cheap path really
    does not open any file."""
    import pyarrow.parquet as pq_mod
    _chunk("ATL06", 111, "G1", -49.5, 69.1, 400.0)
    _chunk("ATL06", 222, "G2", -49.5, 69.1, 500.0)

    opened = []
    real = pq_mod.read_metadata
    monkeypatch.setattr(pq_mod, "read_metadata", lambda f, *a, **k: (opened.append(f), real(f, *a, **k))[1])

    cheap = lake.cell_stats("ATL06", with_rows=False)
    assert opened == []                                    # no footer reads at all
    assert all(s["rows"] == 0 for s in cheap.values())      # rows reported as 0, not guessed

    full = lake.cell_stats("ATL06", with_rows=True)
    assert opened                                          # the full path does read them
    assert set(cheap) == set(full)                         # same cells either way
    assert all(cheap[c]["bytes"] == full[c]["bytes"] for c in cheap)   # and identical sizes — what eviction uses


def test_eviction_picks_the_same_victims_without_row_counts(lake_dir, monkeypatch):
    """The cheap stats must not change which cells eviction chooses."""
    for i, cell in enumerate((111, 222, 333)):
        _chunk("ATL06", cell, f"G{i}", -49.5, 69.1, 400.0 + i)
    monkeypatch.setattr(lake, "get_settings", lambda: {"max_bytes": 1})   # force eviction of everything unprotected
    evicted = lake.enforce_global_limit(protect=[111], reason="test")
    gone = {e["cell"] for e in evicted}
    assert 111 not in gone and gone == {222, 333}          # protected cell survives, the rest go


def test_eviction_runs_off_the_caller_thread_and_single_flights(lake_dir, monkeypatch):
    """Eviction walks every cell dir in the lake (7.5 s on the deployed box) for housekeeping the request does not
    depend on, and it was triggered from several places per build. It must not block the caller, and concurrent
    triggers must collapse into one walk."""
    import threading
    import time
    # _EVICT_RUNNING is module-global and other tests trigger real evictions, so wait for any in-flight walk and
    # start from a known state — otherwise the single-flight guard correctly skips this test's own trigger.
    for _ in range(100):
        if not lake._EVICT_RUNNING.is_set():
            break
        time.sleep(0.02)
    lake._EVICT_RUNNING.clear()

    started, release = threading.Event(), threading.Event()
    calls = []

    def _slow(protect=(), reason="limit"):
        calls.append(reason)
        started.set()
        release.wait(5.0)
        return []
    monkeypatch.setattr(lake, "enforce_global_limit", _slow)

    t0 = time.time()
    lake.enforce_global_limit_async(reason="first")
    assert started.wait(2.0), "eviction never started"
    assert time.time() - t0 < 1.0, "the caller waited for eviction"

    lake.enforce_global_limit_async(reason="second")   # while the first is still running
    lake.enforce_global_limit_async(reason="third")
    release.set()
    time.sleep(0.2)
    assert calls == ["first"], f"single-flight broken: {calls}"

    for _ in range(50):                                # once it finishes, a later trigger runs again
        if not lake._EVICT_RUNNING.is_set():
            break
        time.sleep(0.02)
    release.clear(); started.clear()
    lake.enforce_global_limit_async(reason="later")
    assert started.wait(2.0)
    release.set()
    assert "later" in calls
