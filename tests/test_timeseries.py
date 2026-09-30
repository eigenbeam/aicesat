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
        # rtol=0: allclose's default relative tolerance allows ~20 m in longitude and ~1.5 cm in height, far more
        # than the mm between realizations this is checking.
        np.testing.assert_allclose(rec["lon"][m], want[0], rtol=0, atol=1e-10)
        np.testing.assert_allclose(rec["h"][m], want[2], rtol=0, atol=1e-6)


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


def test_roughness_alone_does_not_gate_a_cell():
    # Roughness is a real surface property (crevasses), already weighed in the score. It is not evidence that the
    # slope removal failed, so it must not force "low" by itself.
    conf, level, why, comps = timeseries._confidence(roughness=2.5, n_bins=18, span=21.0, n_ref=1500, plane_err_max=0.4)
    assert comps["gated"] == []


def test_slope_removal_error_beyond_the_error_budget_is_low_whatever_the_record():
    # 1 m of possible slope-removal error at a window swamps the ~0.1-0.3 m the instruments measure ice to.
    conf, level, why, comps = timeseries._confidence(roughness=0.3, n_bins=18, span=21.0, n_ref=1500, plane_err_max=1.5)
    assert level == "low" and conf < 0.35 and comps["gated"] and "gated" in why


def test_a_singular_reference_is_gated():
    conf, level, _, comps = timeseries._confidence(roughness=0.3, n_bins=10, span=15.0, n_ref=8, plane_err_max=float("inf"))
    assert level == "low" and comps["gated"] and comps["plane_err_max_m"] is None


def test_the_gate_leaves_a_well_measured_cell_alone():
    conf, level, _, comps = timeseries._confidence(roughness=0.2, n_bins=10, span=20.0, n_ref=40, plane_err_max=0.3)
    assert level == "high" and comps["gated"] == []


def _track_recs(ref_half_width_deg):
    """ATL06 (the reference) on one north-south track through a res-9 cell, +-ref_half_width_deg of longitude wide;
    GLAS 2005 samples ~52 m east of it. The surface rises 5 cm per metre eastward. A plane fitted to one straight
    track cannot say how the surface slopes ACROSS it, so it cannot carry the GLAS samples back to the track."""
    clat, clon = h3.cell_to_latlng(h3.latlng_to_cell(72.0, -45.0, 9))
    cx, _ = scene_mod.to_local(FRAME, np.array([clon]), np.array([clat]))
    rng = np.random.default_rng(3)
    recs = []
    for mission, years, dlon, width in (("ATL06", [2019.5, 2020.5, 2021.5], 0.0, ref_half_width_deg),
                                        ("GLAS", [2005.05], 1.5e-3, 1e-5)):
        n = 40 * len(years)
        lat = clat + rng.uniform(-6e-4, 6e-4, n)
        lon = clon + dlon + rng.uniform(-width, width, n)
        x, y = scene_mod.to_local(FRAME, lon, lat)
        h = 1000.0 + 0.05 * (x - cx[0]) + rng.normal(0, 0.3, n)
        yr = np.repeat(np.asarray(years, "f8"), 40) + rng.uniform(-0.04, 0.04, n)
        recs.append({"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y, "h": h, "yr": yr})
    return recs


def _one_cell(monkeypatch, recs):
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    out = timeseries.candidates(DOC, h3_res=9, delta_t=1.0, ref_missions=["ATL06"])
    assert len(out["candidates"]) == 1
    return out["candidates"][0]


def test_the_caveats_describe_the_slope_the_fit_actually_uses(monkeypatch):
    """The notes reach the model verbatim as elevation_change's caveats. They used to say the plane was "fit to
    ref_missions", the joint fit that read change between years as slope; the fit now takes slope from within each
    (year, mission) group only, and ref_missions merely choose which level reads 0."""
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: _track_recs(2e-4))
    notes = timeseries.candidates(DOC, h3_res=9, delta_t=1.0, ref_missions=["ATL06"])["params"]["notes"]
    assert "within each (year, mission) group" in notes
    assert "fit to ref_missions" not in notes


def test_a_single_track_reference_is_gated_where_other_samples_sit_off_it(monkeypatch):
    c = _one_cell(monkeypatch, _track_recs(1.5e-5))          # ~0.5 m wide: one track
    assert c["level"] == "low" and c["components"]["gated"]
    assert max(p["plane_err_m"] for p in c["series"]) > 2.0


def test_a_reference_spread_across_the_cell_carries_a_small_plane_error(monkeypatch):
    c = _one_cell(monkeypatch, _track_recs(1.5e-3))          # ~100 m wide
    assert c["components"]["gated"] == []
    assert max(p["plane_err_m"] for p in c["series"]) < 0.5


def _alternating_recs(rate_m_per_yr):
    """ATL06 (the reference) over four yearly windows on ground rising 3 cm per metre eastward, thinning at `rate`.
    Each year's pass pair lands on the OTHER side of the cell (centred 60 m west, then 60 m east, ...), +-40 m wide;
    GLAS 2005 sits 100 m east. The truth for ATL06 is 0, rate, 2*rate, 3*rate relative to its first window."""
    clat, clon = h3.cell_to_latlng(h3.latlng_to_cell(72.0, -45.0, 9))
    cx, _ = scene_mod.to_local(FRAME, np.array([clon]), np.array([clat]))
    k = 1 / (111320 * np.cos(np.radians(clat)))
    rng = np.random.default_rng(5)
    parts = {}
    groups = [("GLAS", 2005.1, 100.0, 0.0)] + [("ATL06", 2019.5 + i, -60.0 if i % 2 == 0 else 60.0, 5.0 + rate_m_per_yr * i)
                                              for i in range(4)]
    for mission, yr, east, dh in groups:
        lon = clon + (east + rng.uniform(-40, 40, 60)) * k
        lat = clat + rng.uniform(-60, 60, 60) / 111320
        x, y = scene_mod.to_local(FRAME, lon, lat)
        h = 1000.0 + 0.03 * (x - cx[0]) + dh + rng.normal(0, 0.1, 60)
        p = parts.setdefault(mission, {k2: [] for k2 in ("lat", "lon", "x", "y", "h", "yr")})
        for k2, v in (("lat", lat), ("lon", lon), ("x", x), ("y", y), ("h", h), ("yr", np.full(60, yr))):
            p[k2].append(v)
    return [{"mission": mi, **{k2: np.concatenate(v) for k2, v in d.items()}} for mi, d in parts.items()]


def test_the_confidence_verdict_does_not_depend_on_how_much_the_surface_changed(monkeypatch):
    # Same sampling geometry, only the thinning rate differs. A gate that reads the answer flips (the review measured
    # 0 -> -3 m/yr going from high to gated). Slope-removal error is a property of the geometry, not of the change.
    errs, levels = [], []
    for rate in (0.0, -1.0, -3.0):
        c = _one_cell(monkeypatch, _alternating_recs(rate))
        errs.append(c["components"]["plane_err_max_m"]); levels.append(c["level"])
    assert max(errs) - min(errs) < 0.1, errs
    assert len(set(levels)) == 1, levels


def test_passes_alternating_sides_of_a_cell_do_not_bend_the_slope_or_the_series(monkeypatch):
    c = _one_cell(monkeypatch, _alternating_recs(-3.0))
    assert abs(c["slope_deg"] - 1.72) < 0.1, c["slope_deg"]
    atl = [p["value_m"] for p in c["series"] if p["missions"] == ["ATL06"]]
    rel = [round(v - atl[0], 2) for v in atl]
    assert all(abs(a - b) < 0.3 for a, b in zip(rel, [0.0, -3.0, -6.0, -9.0])), rel
