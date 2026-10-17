"""The lake writer splits a chunk by the cells the ingest already assigned; it does not assign them again.

The ingest assigns every decoded point an H3 cell to decide what to keep, and lake._cell_tables assigned the same
points again to split them into files: 24-31 thread-seconds of an 8-cell ATL06 ingest (59 M points, 2026-10-17).
Now the ingest attaches `h3_cell` to the arrays it hands the writer (the CSV parsers already attached `cell`), and the
writer only recomputes for a caller that brought nothing. The constant columns (granule, beam) are also built without a
Python list of n strings, with the same Arrow type as before so readers see no change.
"""
import numpy as np
import pyarrow as pa
import pytest

from aicesat import lake, planner


def _arrays(n=1000, cells=(1, 2, 3)):
    rng = np.random.default_rng(0)
    lat = 72.5 + rng.random(n) * 0.2
    lon = -38.5 + rng.random(n) * 0.2
    return {"lon": lon, "lat": lat, "h": rng.normal(size=n), "t": np.full(n, np.datetime64("2020-01-01", "ms")),
            "quality": np.zeros(n, "i1"), "h3_cell": np.asarray(cells, "u8")[rng.integers(0, len(cells), n)]}


def test_cell_tables_split_by_the_carried_cells_without_reassigning(monkeypatch):
    monkeypatch.setattr(planner, "_cells_vectorized", lambda *a, **k: pytest.fail("the writer must not assign cells"))
    arrays = _arrays()
    tables = lake._cell_tables("g.h5", "gt1l", 7, arrays, 5, extras=("quality",), only_cells=None)
    assert set(tables) == {1, 2, 3}
    for cell, tbl in tables.items():
        m = arrays["h3_cell"] == cell
        assert tbl.num_rows == int(m.sum())
        assert np.array_equal(np.sort(tbl.column("native_lon").to_numpy()), np.sort(arrays["lon"][m]))
        assert tbl.column("source_chunk_index").to_pylist() == [7] * tbl.num_rows
        assert set(tbl.column("quality").to_pylist()) == {0}


def test_the_csv_parsers_cell_key_is_accepted(monkeypatch):
    monkeypatch.setattr(planner, "_cells_vectorized", lambda *a, **k: pytest.fail("the writer must not assign cells"))
    arrays = _arrays()
    arrays["cell"] = arrays.pop("h3_cell")
    assert set(lake._cell_tables("g.csv", "na", 0, arrays, 5, extras=(), only_cells=None)) == {1, 2, 3}


def test_only_cells_and_unpositioned_points_still_apply():
    arrays = _arrays()
    arrays["lat"][:10] = np.nan                                   # no position: cannot be placed, whatever the column says
    tables = lake._cell_tables("g.h5", "gt1l", 0, arrays, 5, extras=(), only_cells=(2,))
    assert set(tables) == {2}
    m = (arrays["h3_cell"] == 2) & np.isfinite(arrays["lat"])
    assert tables[2].num_rows == int(m.sum())


def test_a_caller_without_cells_still_gets_them_assigned(monkeypatch):
    calls = []
    monkeypatch.setattr(planner, "_cells_vectorized", lambda lat, lon, res: calls.append(res) or np.full(lat.size, 9, "u8"))
    arrays = _arrays()
    del arrays["h3_cell"]
    tables = lake._cell_tables("g.h5", "gt1l", 0, arrays, 5, extras=(), only_cells=None)
    assert set(tables) == {9} and calls == [5]


def test_constant_columns_keep_the_dictionary_type_readers_expect():
    before = pa.array(["ATL06_x.h5"] * 4).dictionary_encode()
    now = lake._const_dict("ATL06_x.h5", 4)
    assert now.type == before.type and now.to_pylist() == before.to_pylist()
    tbl = lake._cell_tables("ATL06_x.h5", "gt2r", 3, _arrays(50, cells=(5,)), 5, extras=(), only_cells=None)[5]
    assert tbl.schema.field("source_granule").type == before.type
    assert tbl.column("beam").to_pylist() == ["gt2r"] * 50
