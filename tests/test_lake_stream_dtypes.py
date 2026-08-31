"""A streamed lake read must survive an empty cell group.

query_points(on_batch=...) splits the requested cells into groups, queries each, and concatenates. An empty group
returned `np.array([])` — float64 — for every column including `t`, and numpy 2 refuses to concatenate a float64
array with a datetime64 one:

    DTypePromotionError: The DType DateTime64DType could not be promoted by Float64DType

The failure needed a mix of empty and non-empty groups AND on_batch set, so it only ever appeared during a real
scene build (which passes the streaming callback) and never in a direct extract() call, which is what made it look
like "GLAS returned nothing" rather than a crash.
"""
import numpy as np

from aicesat import lake


def _part(n, t0="2005-06-01"):
    return {"lon": np.linspace(-50, -49, n), "lat": np.linspace(69, 70, n), "h": np.full(n, 2500.0),
            "t": np.full(n, np.datetime64(t0, "ms"), dtype="datetime64[ms]"), "quality": np.zeros(n, "i1")}


def _empty():
    """Exactly what _query_points returns for a cell group with no materialized points."""
    return {k: np.array([]) for k in ("lon", "lat", "h", "t", "quality")}


def test_streamed_read_concatenates_across_an_empty_group(monkeypatch):
    calls = []

    def fake_query(bbox, cells, mission, granules=None, beams=None, extra_cols=(), quality_zero=False,
                   clip_cells=False):
        calls.append(tuple(cells))
        return _part(4) if 1 in cells else _empty()

    monkeypatch.setattr(lake, "_query_points", fake_query)
    seen = []
    out = lake.query_points((-51, 68, -49, 70), [1, 2, 3, 4], "GLAS", extra_cols=("quality",),
                            on_batch=seen.append)

    assert len(calls) > 1, "the streaming path should split the cells into groups"
    assert out["lon"].size == 4
    assert out["t"].dtype.kind == "M", f"t came back as {out['t'].dtype}, not a datetime"
    assert len(seen) == 1, "only the non-empty group should be emitted to the callback"


def test_streamed_read_of_entirely_empty_groups_keeps_datetime_t(monkeypatch):
    monkeypatch.setattr(lake, "_query_points",
                        lambda *a, **k: {kk: np.array([]) for kk in ("lon", "lat", "h", "t")})
    out = lake.query_points((-51, 68, -49, 70), [1, 2, 3, 4], "GLAS", on_batch=lambda r: None)
    assert out["lon"].size == 0 and out["t"].size == 0


def test_empty_result_carries_the_column_dtypes_not_bare_float64():
    """The root cause: an empty read must not describe `t` as float64."""
    out = lake._query_points((-51, 68, -49, 70), [], "GLAS", None, None, (), False, False)
    assert out["t"].dtype.kind == "M", f"empty read reports t as {out['t'].dtype}"
    assert out["lon"].dtype == np.float64
