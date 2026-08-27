"""Offline tests for the time-series candidate search: which mission(s) define each cell's reference plane."""
import h3
import numpy as np

from aicesat import scene as scene_mod
from aicesat import timeseries

FRAME = scene_mod.local_frame((-45.5, 71.8, -44.5, 72.2))
DOC = {"frame": FRAME, "z0": 1000.0}


def _recs(missions):
    """Synthetic points inside ONE res-9 cell on a sloped plane. Each mission sits at its own epoch(s) with an
    additive height offset, so which mission anchors the plane shows up directly in the residuals."""
    clat, clon = h3.cell_to_latlng(h3.latlng_to_cell(72.0, -45.0, 9))
    rng = np.random.default_rng(0)
    recs = []
    for mission, years, offset in missions:
        n = 40 * len(years)
        lat = clat + rng.uniform(-2e-4, 2e-4, n)          # ~±22 m, well inside a 174 m-edge hex
        lon = clon + rng.uniform(-6e-4, 6e-4, n)          # ~±20 m at 72°N
        x, y = scene_mod.to_local(FRAME, lon, lat)
        h = 1000.0 + 0.02 * x + offset + rng.normal(0, 0.02, n)
        yr = np.repeat(np.asarray(years, "f8"), 40) + rng.uniform(-0.04, 0.04, n)   # keeps each epoch inside one 1-yr bin
        recs.append({"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y, "h": h, "yr": yr})
    return recs


GLAS_PLUS_ATL06 = [("GLAS", [2005.05], 0.0), ("ATL06", [2019.5, 2020.5, 2021.5], 1.0)]


def _run(monkeypatch, recs, **kw):
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    out = timeseries.candidates(DOC, h3_res=9, delta_t=1.0, **kw)
    assert len(out["candidates"]) == 1
    return out["params"]["ref_missions"], out["candidates"][0]["series"]


def _value(series, mission):
    vals = [p["value_m"] for p in series if p["missions"] == [mission]]
    assert vals, f"no window made only of {mission}"
    return float(np.median(vals))


def test_default_reference_is_glas_when_present(monkeypatch):
    ref, series = _run(monkeypatch, _recs(GLAS_PLUS_ATL06))
    assert ref == ["GLAS"]
    # plane anchored on GLAS -> GLAS-era window reads ~0, later ATL06 windows read their +1 m offset
    assert abs(_value(series, "GLAS")) < 0.03
    assert abs(_value(series, "ATL06") - 1.0) < 0.03


def test_default_reference_falls_back_to_all_missions_without_glas(monkeypatch):
    ref, _ = _run(monkeypatch, _recs([("ICESSN", [2010.05], 0.0), ("ATL06", [2019.5, 2020.5, 2021.5], 1.0)]))
    assert ref == ["ATL06", "ICESSN"]


def test_explicit_reference_is_honoured(monkeypatch):
    ref, series = _run(monkeypatch, _recs(GLAS_PLUS_ATL06), ref_missions=["ATL06"])
    assert ref == ["ATL06"]
    assert abs(_value(series, "ATL06")) < 0.03
    assert abs(_value(series, "GLAS") + 1.0) < 0.03


def test_blunder_clip_keeps_real_change_and_drops_blunders(monkeypatch):
    """3 m of change since the GLAS reference epoch must survive the clip (it is the signal); an 80 m cloud
    return inside one window must not."""
    recs = _recs([("GLAS", [2005.05], 0.0), ("ATL06", [2019.5, 2020.5, 2021.5], 3.0)])
    recs[1]["h"][45] += 80.0                                  # index 45 lies in the 2020 group (40..79)
    ref, series = _run(monkeypatch, recs)
    assert ref == ["GLAS"]
    assert abs(_value(series, "GLAS")) < 0.03
    assert abs(_value(series, "ATL06") - 3.0) < 0.03
    assert [p["n"] for p in series] == [40, 40, 39, 40]


def test_requested_reference_absent_from_scene_uses_default(monkeypatch):
    ref, _ = _run(monkeypatch, _recs(GLAS_PLUS_ATL06), ref_missions=["ICESSN"])
    assert ref == ["GLAS"]
