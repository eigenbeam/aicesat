"""Timing, the cost guards, and command dispatch. Collaborators are monkeypatched, so none of this touches an
index, a lake or the network."""
import io

import pytest
from rich.console import Console

from aicesat.tui import app as app_mod
from aicesat.tui import ops


def _console():
    return Console(file=io.StringIO(), width=100, force_terminal=False, no_color=True)


def _app(**kw):
    return app_mod.App(_console(), kw.pop("region", "jakobshavn_margin"), kw.pop("bbox", (-51, 69, -49, 69.4)), 5)


# --- Timing ---------------------------------------------------------------------------------------------------

def test_timing_records_phases_in_order_with_details():
    t = ops.Timing("op")
    with t.phase("first") as p:
        p[2] = "detail"
    with t.phase("second"):
        pass
    rows = t.stop().rows()
    assert [r[0] for r in rows] == ["first", "second"]
    assert rows[0][2] == "detail"
    assert all(r[1] >= 0 for r in rows)
    assert t.seconds >= 0


def test_timing_phase_is_recorded_even_when_the_block_raises():
    t = ops.Timing("op")
    with pytest.raises(ValueError):
        with t.phase("boom"):
            raise ValueError("x")
    assert t.rows()[0][0] == "boom"


def test_timing_add_takes_a_phase_measured_elsewhere():
    """Library `*_seconds` are sub-phases of a call already on the clock; re-timing them would double-count."""
    t = ops.Timing("op")
    t.add("nasa fetch", 1.5, "detail")
    t.add("claim", None, "stamped")
    assert t.rows() == [("nasa fetch", 1.5, "detail"), ("claim", None, "stamped")]


# --- the cost guard -------------------------------------------------------------------------------------------

def test_lake_overview_does_not_read_row_counts_unless_asked(monkeypatch):
    """cell_stats(with_rows=True) opens every Parquet footer — 91 s over 23k files. It must be opt-in."""
    from aicesat import lake
    seen = {}

    def _stats(m, with_rows):
        seen["with_rows"] = with_rows
        return {}

    monkeypatch.setattr(lake, "cell_stats", _stats)
    monkeypatch.setattr(lake, "missions", lambda: [])
    monkeypatch.setattr(lake, "lake_summary", lambda m: pytest.fail("lake_summary walks rows; must not be called"))
    data, t = ops.lake_overview(with_rows=False)
    assert seen["with_rows"] is False
    assert data["summary"] is None
    assert "SKIPPED" in dict((r[0], r[2]) for r in t.rows())["cell stats"]


def test_lake_overview_with_rows_reads_them(monkeypatch):
    from aicesat import lake
    seen = {}

    def _stats(m, with_rows):
        seen["with_rows"] = with_rows
        return {}

    monkeypatch.setattr(lake, "cell_stats", _stats)
    monkeypatch.setattr(lake, "missions", lambda: [])
    monkeypatch.setattr(lake, "lake_summary", lambda m: {"cells": 1, "files": 2})
    data, _ = ops.lake_overview(with_rows=True)
    assert seen["with_rows"] is True and data["summary"]["cells"] == 1


def test_plan_does_not_block_on_the_background_writer(monkeypatch):
    from aicesat import index_atl06
    seen = {}
    monkeypatch.setattr(index_atl06, "plan_bbox",
                        lambda *a, **k: seen.update(k) or {"want_cells": [], "rows": [], "names": [],
                                                          "chunk_cells": {}, "todo": [], "n_lake": 0, "by_url": {}})
    ops.plan((0, 0, 1, 1))
    assert seen["settle"] is False


# --- chunk map ------------------------------------------------------------------------------------------------

def _fake_plan(rows, cells=(10,)):
    cc = {}
    for r in rows:
        cc.setdefault((r["granule"], r["beam"], r["chunk_index"]), set()).add(int(r["h3_cell"]))
    return {"want_cells": list(cells), "rows": rows, "names": sorted({r["granule"] for r in rows}),
            "beams": sorted({r["beam"] for r in rows}), "chunk_cells": cc, "todo": [], "n_lake": 0, "by_url": {}}


def _row(g, beam, chunk, off):
    r = {"granule": g, "beam": beam, "chunk_index": chunk, "h3_cell": 10}
    for i, ds in enumerate(ops.__dict__ and ["latitude", "longitude", "h_li", "delta_time",
                                             "atl06_quality_summary"]):
        r[f"{ds}_offset"] = off + i * 100
        r[f"{ds}_size"] = 100
    return r


def test_chunk_map_picks_the_busiest_granule_when_none_is_named(monkeypatch):
    from aicesat import index_atl06
    rows = [_row("small.h5", "gt1l", 0, 0), _row("big.h5", "gt1l", 0, 500), _row("big.h5", "gt1l", 1, 900)]
    monkeypatch.setattr(index_atl06, "plan_bbox", lambda *a, **k: _fake_plan(rows))
    data, _ = ops.chunk_map((0, 0, 1, 1))
    assert data["granule"] == "big.h5" and data["chunks"] == 2


def test_chunk_map_scopes_ranges_to_one_granule(monkeypatch):
    """Ranges from two granules index two different files; pooling them would report nonsense spans."""
    from aicesat import index_atl06
    rows = [_row("a.h5", "gt1l", 0, 0), _row("b.h5", "gt1l", 0, 10_000_000)]
    monkeypatch.setattr(index_atl06, "plan_bbox", lambda *a, **k: _fake_plan(rows))
    data, _ = ops.chunk_map((0, 0, 1, 1), granule="a.h5")
    assert data["granule"] == "a.h5"
    assert max(o for o, _ in data["ranges"]) < 10_000_000
    assert set(k[0] for k in data["chunk_cells"]) == {"a.h5"}


def test_chunk_map_rejects_a_granule_that_does_not_touch_the_box(monkeypatch):
    from aicesat import index_atl06
    monkeypatch.setattr(index_atl06, "plan_bbox", lambda *a, **k: _fake_plan([_row("a.h5", "gt1l", 0, 0)]))
    with pytest.raises(ValueError, match="no granule matching"):
        ops.chunk_map((0, 0, 1, 1), granule="nope")


def test_chunk_map_on_an_empty_box_returns_empty_rather_than_raising(monkeypatch):
    from aicesat import index_atl06
    monkeypatch.setattr(index_atl06, "plan_bbox", lambda *a, **k: _fake_plan([]))
    data, _ = ops.chunk_map((0, 0, 1, 1))
    assert data["granule"] is None and data["ranges"] == []


# --- flag parsing ---------------------------------------------------------------------------------------------

def test_flags_splits_positionals_bare_flags_and_valued_flags():
    pos, fl = app_mod._flags(["a", "--rows", "--top", "5", "--granule", "g.h5"],
                             valued=("granule",), n_valued=("top",))
    assert pos == ["a"]
    assert fl == {"rows": True, "top": 5, "granule": "g.h5"}


def test_flags_rejects_a_valued_flag_with_no_value():
    with pytest.raises(ValueError, match="needs a value"):
        app_mod._flags(["--top"], n_valued=("top",))


# --- dispatch -------------------------------------------------------------------------------------------------

def test_dispatch_prefers_the_two_word_command(monkeypatch):
    a = _app()
    seen = []
    monkeypatch.setattr(a, "cmd_index", lambda args: seen.append(("index", args)))
    monkeypatch.setattr(a, "cmd_index_cells", lambda args: seen.append(("index cells", args)))
    a.dispatch("index cells --top 3")
    a.dispatch("index --full")
    assert seen == [("index cells", ["--top", "3"]), ("index", ["--full"])]


def test_dispatch_reports_an_unknown_command_without_raising():
    a = _app()
    a.dispatch("frobnicate")
    assert "unknown command" in a.c.file.getvalue()


def test_region_rejects_an_unknown_name():
    a = _app()
    with pytest.raises(ValueError, match="unknown region"):
        a.cmd_region(["atlantis"])


def test_bbox_requires_four_numbers_and_rejects_an_inverted_box():
    a = _app()
    with pytest.raises(ValueError, match="four numbers"):
        a.cmd_bbox(["1", "2"])
    with pytest.raises(ValueError, match="west<east"):
        a.cmd_bbox(["1", "2", "0", "3"])


def test_window_set_and_clear():
    a = _app()
    a.cmd_window(["2019-01-01", "2020-01-01"])
    assert a.window == ("2019-01-01", "2020-01-01")
    a.cmd_window(["none"])
    assert a.window is None


def test_overlaps():
    assert app_mod.App._overlaps((-51, 69, -49, 69.4), (-51.5, 68.7, -48.5, 69.8))
    assert not app_mod.App._overlaps((-45, 69.8, -43, 70.2), (-51.5, 68.7, -48.5, 69.8))


# --- fetch progress -------------------------------------------------------------------------------------------

def test_fetch_counter_counts_distinct_fetched_granules_not_lake_batches():
    """on_granule fires per lake batch too, and a granule can stream twice; counting either would run the bar
    past its own denominator."""
    ctr = app_mod.FetchCounter("query")
    assert ctr.description == "query: planning"
    ctr.plan({"granules": 2, "chunks": 9, "cached": 4})
    assert "2 granules" in ctr.description and "9 chunks" in ctr.description
    assert [ctr.granule({"granule": "lake"}) for _ in range(5)] == [False] * 5
    assert ctr.granule({"granule": "a.h5"}) is True
    assert ctr.granule({"granule": "a.h5"}) is False     # same granule streaming again
    assert ctr.granule({"granule": "b.h5"}) is True
    assert ctr.done == 2 <= ctr.total


def test_fetch_counter_pure_cache_hit_says_so():
    """fetch_bbox calls on_plan with granules=0 when nothing needs fetching; a 0-total bar must not read as a stall."""
    ctr = app_mod.FetchCounter("query")
    ctr.plan({"granules": 0, "chunks": 0, "cached": 1824})
    assert "reading lake" in ctr.description and "1,824" in ctr.description
    assert ctr.done == 0


def test_fetch_progress_wires_the_counter_without_a_terminal():
    a = _app()
    with a._fetch_progress("query") as cbs:
        cbs["on_plan"]({"granules": 1, "chunks": 2, "cached": 0})
        cbs["on_granule"]({"granule": "a.h5"})
    assert set(cbs) == {"on_plan", "on_granule"}


def test_materialize_converts_a_cell_to_its_bbox_and_reports_the_neighbours_it_pulled(monkeypatch):
    import h3

    import numpy as np

    from aicesat import index_atl06, lake
    cell = h3.latlng_to_cell(69.2, -50.0, 5)
    seen = {}
    monkeypatch.setattr(lake, "cell_stats",
                        lambda m, with_rows: {1: {}} if "after" in seen else seen.setdefault("after", 1) and {})
    monkeypatch.setattr(lake, "drain_writes", lambda *a, **k: True)
    monkeypatch.setattr(index_atl06, "fetch_bbox",
                        lambda bbox, *a, **k: seen.update(bbox=bbox) or
                        ({"lat": np.zeros(3)}, {"chunks_from_lake": 1, "chunks_from_nasa": 2}))
    data, _ = ops.materialize([cell])
    w, s, e, n = data["bbox"]
    lat, lon = h3.cell_to_latlng(cell)
    assert w < lon < e and s < lat < n          # the cell's centre is inside the box its boundary produced
    assert data["points"] == 3


# --- cell id parsing ------------------------------------------------------------------------------------------

def test_as_cell_str_accepts_both_forms_the_tui_prints():
    """`index cells` prints hex ids, `lake cells` prints integers, and a user pastes whichever is in front of them.
    Handing an all-digit id straight to h3 parses it as hex and raises OverflowError."""
    import h3
    cell = h3.latlng_to_cell(69.2, -50.0, 5)
    as_int = h3.str_to_int(cell)
    assert ops.as_cell_str(cell) == cell
    assert ops.as_cell_str(str(as_int)) == cell
    assert ops.as_cell_str(as_int) == cell
    assert ops.as_cell_str(f"  {cell} ") == cell


@pytest.mark.parametrize("bad", ["nope", "", "12", "zzzz"])
def test_as_cell_str_rejects_a_non_cell(bad):
    with pytest.raises(ValueError, match="not an H3 cell"):
        ops.as_cell_str(bad)


# --- index verify ---------------------------------------------------------------------------------------------

def _write(path, version, rows=1):
    import pyarrow as pa
    import pyarrow.parquet as pq
    t = pa.table({"granule": pa.array(["g.h5"] * rows)})
    if version is not None:
        t = t.replace_schema_metadata({"aicesat_atl06_index_version": version})
    pq.write_table(t, path)


def test_index_verify_counts_by_schema_version(tmp_path, monkeypatch):
    """The readdir count index_status reports cannot see a stale file. On this machine that made a 57-granule
    index report "844 of 845, 100% built"."""
    from aicesat import index_atl06
    d = tmp_path / "res5"
    d.mkdir(parents=True)
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path)
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_VERSION", "2")
    _write(d / "cur.h5.parquet", "2")
    _write(d / "empty.h5.parquet", "2", rows=0)
    _write(d / "old.h5.parquet", "1")
    _write(d / "none.h5.parquet", None)
    (d / "half.h5.parquet").write_bytes(b"PAR1 not really a parquet")

    out, t = ops.index_verify(5)
    assert out == {**out, "files": 5, "current": 2, "stale": 2, "unreadable": 1, "empty": 1, "want": "2"}
    assert t.seconds >= 0


def test_index_verify_never_deletes(tmp_path, monkeypatch):
    """indexed_atl06_granules deletes what it finds stale as it scans. That is right for a build and wrong for a
    report, which must be able to name a problem without also causing it."""
    from aicesat import index_atl06
    d = tmp_path / "res5"
    d.mkdir(parents=True)
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path)
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_VERSION", "2")
    _write(d / "old.h5.parquet", "1")
    (d / "half.h5.parquet").write_bytes(b"nope")
    before = sorted(p.name for p in d.iterdir())
    ops.index_verify(5)
    assert sorted(p.name for p in d.iterdir()) == before


def test_index_verify_on_a_missing_directory_is_empty_not_an_error(tmp_path, monkeypatch):
    from aicesat import index_atl06
    monkeypatch.setattr(index_atl06, "ATL06_INDEX_DIR", tmp_path / "nothing")
    out, _ = ops.index_verify(5)
    assert out["files"] == 0 and out["current"] == 0
