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


# --- trend: one source of truth for the rate the model states and the chart prints -----------------------------
def _cand(monkeypatch, recs, **kw):
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    out = timeseries.candidates(DOC, h3_res=9, delta_t=1.0, **kw)
    assert len(out["candidates"]) == 1
    return out["candidates"][0]


def test_trend_cm_yr_recovers_a_known_rate(monkeypatch):
    """A surface rising 0.25 m/yr about the GLAS-era plane must read as +25 cm/yr. The trend used to exist only in
    the UI's linfit, so the number a caller got and the number the chart drew came from two implementations."""
    rate = 0.25
    epochs = [("GLAS", [2005.05], 0.0)] + [("ATL06", [y], rate * (y - 2005.05)) for y in (2019.5, 2020.5, 2021.5)]
    c = _cand(monkeypatch, _recs(epochs))
    assert abs(c["trend_cm_yr"] - 100 * rate) < 1.5, c["trend_cm_yr"]


def test_trend_cm_yr_matches_a_least_squares_fit_of_the_series(monkeypatch):
    """Whatever the windows come out as, the reported trend is the unweighted LS slope through them -- the same
    statistic the UI drew, so the readout and the tool payload cannot drift apart."""
    c = _cand(monkeypatch, _recs(GLAS_PLUS_ATL06))
    x = np.array([p["year"] for p in c["series"]]); y = np.array([p["value_m"] for p in c["series"]])
    expect = 100.0 * np.polyfit(x, y, 1)[0]
    assert abs(c["trend_cm_yr"] - expect) < 0.01, (c["trend_cm_yr"], expect)


# --- plate-motion propagation is per realization, and a failure is reported (#8) -----------------------------------
def _icessn_doc(monkeypatch, arrays, meta):
    from aicesat import coreg, scene as scene_mod
    monkeypatch.setattr(coreg, "_reload_arrays", lambda s: (arrays, meta))
    return {"frame": scene_mod.local_frame((-49.6, 68.9, -49.2, 69.1)), "series": {"ICESSN": {"cache_key": "k"}}}


def _icessn_arrays(n=40, itrf_years=None):
    rng = np.random.default_rng(3)
    a = {"lon": -49.4 + rng.uniform(-0.05, 0.05, n), "lat": 69.0 + rng.uniform(-0.02, 0.02, n),
         "h": 1500.0 + rng.normal(0, 0.3, n), "t": np.full(n, np.datetime64("2011-05-01"), "datetime64[ms]")}
    if itrf_years is not None:
        a["itrf_year"] = np.asarray(itrf_years, "i2")
    return a


def test_each_itrf_realization_is_propagated_through_its_own_frame(monkeypatch):
    from aicesat import coreg
    years = [2005] * 20 + [2008] * 20
    a = _icessn_arrays(itrf_years=years)
    doc = _icessn_doc(monkeypatch, a, {"native_frame": "ITRF (mixed: ITRF2005, ITRF2008; see itrf_year per row)"})
    rec = timeseries._load_all(doc, 2005.0)[0]
    assert rec["propagated"] is True and rec["frame_note"] is None
    moved = coreg.horizontal_displacement_m(a["lon"], a["lat"], rec["lon"], rec["lat"])
    assert (moved > 0.01).all(), "a point was left at its observed position"
    yr = coreg.decimal_year(a["t"])
    for y in (2005, 2008):
        m = np.asarray(years) == y
        want = coreg.propagate(a["lon"][m], a["lat"][m], a["h"][m], yr[m], 2005.0, f"ITRF{y}")
        assert np.allclose(rec["lon"][m], want[0], atol=1e-10) and np.allclose(rec["h"][m], want[2], atol=1e-6)


def test_a_series_that_cannot_be_propagated_is_reported_not_hidden(monkeypatch):
    """ICESSN's frame label was "ITRF (campaign-dependent)", which the frame step rightly refuses; the time series
    used to catch that, log it and carry on with raw positions, indistinguishable in its output from a propagated
    series."""
    a = _icessn_arrays()
    doc = _icessn_doc(monkeypatch, a, {"native_frame": "ITRF (campaign-dependent)"})
    rec = timeseries._load_all(doc, 2005.0)[0]
    assert rec["propagated"] is False and "campaign-dependent" in rec["frame_note"]
    params = timeseries.candidates(doc)["params"]
    assert "ICESSN" in params["not_propagated"]
    assert "EXCEPT ICESSN" in params["notes"]


def test_rows_with_no_frame_in_their_header_are_reported(monkeypatch):
    a = _icessn_arrays(itrf_years=[2008] * 30 + [0] * 10)
    doc = _icessn_doc(monkeypatch, a, {"native_frame": "ITRF (mixed)"})
    rec = timeseries._load_all(doc, 2005.0)[0]
    assert rec["propagated"] is False and "10 points in no frame in the granule header" in rec["frame_note"]
