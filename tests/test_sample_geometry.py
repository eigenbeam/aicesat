"""show_timeseries explains WHY the answer needs the sample geometry: where each mission sampled the cell, the slopes the
data implies and measures, and what the change would read if those positions were ignored."""
import math

import h3
import numpy as np

from aicesat import scene as scene_mod
from aicesat import timeseries

FRAME = scene_mod.local_frame((-49.6, 69.1, -49.0, 69.3))
DOC = {"frame": FRAME, "z0": 0.0}
CELL = h3.latlng_to_cell(69.2, -49.3, 8)


def test_a_mission_sampled_along_one_track_reports_no_fitted_slope(monkeypatch):
    # Points on one line cannot say how the ground slopes across it: a plane fitted to them read 13 deg on real data
    # where ICESat-2 measured 2.9 deg. Report no slope rather than that number.
    recs = _recs()
    g0 = recs[0]
    g0["x"] = np.full_like(g0["x"], g0["x"].mean()) + np.linspace(-0.1, 0.1, g0["x"].size)   # a north-south line
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    monkeypatch.setattr(timeseries, "_cells_of", lambda lat, lon, res: np.full(len(lat), h3.str_to_int(CELL), "u8"))
    g = timeseries.sample_geometry(DOC, CELL, [{"year": 2005.1, "value_m": 0.0}, {"year": 2022.5, "value_m": -20.0}])
    assert g["missions"]["GLAS"]["fitted_slope"] is None
    assert g["missions"]["ATL06"]["fitted_slope"] is not None


def test_a_missions_implied_slope_comes_from_one_window_so_its_own_change_is_not_read_as_slope(monkeypatch):
    # GLAS in two campaigns on different tracks, 20 m apart in height from thinning between them: fitted together the
    # thinning becomes slope (the 13 deg on real data). Each campaign alone sees the true 3 cm/m.
    clat, clon = h3.cell_to_latlng(CELL)
    cx, _ = scene_mod.to_local(FRAME, np.array([clon]), np.array([clat]))
    rng = np.random.default_rng(2)
    xs, ys, hs, yrs, lats, lons = [], [], [], [], [], []
    for east, yr, dh in (((300, 400), 2004.1, 0.0), ((0, 100), 2008.1, -20.0)):
        lon = clon + rng.uniform(*east, 40) / (111320 * math.cos(math.radians(clat)))
        lat = clat + rng.uniform(-150, 150, 40) / 111320
        x, y = scene_mod.to_local(FRAME, lon, lat)
        xs.append(x); ys.append(y); lats.append(lat); lons.append(lon)
        hs.append(1000.0 + 0.03 * (x - cx[0]) + dh + rng.normal(0, 0.05, 40)); yrs.append(np.full(40, yr))
    glas = {"mission": "GLAS", "x": np.concatenate(xs), "y": np.concatenate(ys), "h": np.concatenate(hs),
            "yr": np.concatenate(yrs), "lat": np.concatenate(lats), "lon": np.concatenate(lons)}
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: [glas])
    monkeypatch.setattr(timeseries, "_cells_of", lambda lat, lon, res: np.full(len(lat), h3.str_to_int(CELL), "u8"))
    g = timeseries.sample_geometry(DOC, CELL, [{"year": 2004.1, "value_m": 0.0}, {"year": 2008.1, "value_m": -20.0}])
    assert abs(g["missions"]["GLAS"]["fitted_slope"]["slope_deg"] - 1.72) < 0.2


def _recs(with_slopes=False):
    """A cell on ground rising 3 cm per metre eastward. GLAS (2005) sampled ~400 m east, uphill; ATL06 (2020-22)
    sampled the middle and is 20 m lower from real thinning. The truth is -20 m."""
    clat, clon = h3.cell_to_latlng(CELL)
    cx, _ = scene_mod.to_local(FRAME, np.array([clon]), np.array([clat]))
    rng = np.random.default_rng(1)
    out = []
    for mission, east_m, years, dh in (("GLAS", (350, 450), [2005.1], 0.0),
                                       ("ATL06", (-100, 100), [2020.5, 2021.5, 2022.5], -20.0)):
        n = 60 * len(years)
        lon = clon + rng.uniform(*east_m, n) / (111320 * math.cos(math.radians(clat)))
        lat = clat + rng.uniform(-150, 150, n) / 111320
        x, y = scene_mod.to_local(FRAME, lon, lat)
        h = 1000.0 + 0.03 * (x - cx[0]) + dh + rng.normal(0, 0.05, n)
        rec = {"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y, "h": h,
               "yr": np.repeat(np.asarray(years, "f8"), 60)}
        if with_slopes and mission == "ATL06":
            rec["sn"] = np.zeros(n)
            rec["we"] = np.full(n, 0.03)
        out.append(rec)
    return out


def _geom(monkeypatch, **kw):
    recs = _recs(**kw)
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    monkeypatch.setattr(timeseries, "_cells_of", lambda lat, lon, res: np.full(len(lat), h3.str_to_int(CELL), "u8"))
    reported = [{"year": 2005.1, "value_m": 0.0}, {"year": 2022.5, "value_m": -20.0}]
    return timeseries.sample_geometry(DOC, CELL, reported)


def test_it_reports_what_ignoring_the_positions_would_read(monkeypatch):
    g = _geom(monkeypatch)
    assert g["change_m"]["reported"] == -20.0
    assert abs(g["change_m"]["ignoring_positions"] - (-32.0)) < 1.5       # 0.03 m/m x ~400 m of uphill offset
    assert abs(g["change_m"]["one_plane_across_all_eras"]) < 5.0          # the thinning vanished into the slope


def test_it_says_where_each_mission_sampled_and_what_slope_its_points_imply(monkeypatch):
    g = _geom(monkeypatch)
    assert g["missions"]["GLAS"]["centroid_offset_m"][0] > 250            # east of the cell's sample centroid
    assert abs(g["missions"]["ATL06"]["fitted_slope"]["dh_dx_m_per_km"] - 30) < 3
    assert "m apart" in g["explanation"]


def test_a_measured_product_slope_is_reported_when_the_data_carries_one(monkeypatch):
    g = _geom(monkeypatch, with_slopes=True)
    m = g["measured_slope"]["ATL06"]
    assert m["n"] == 180 and abs(m["slope_deg"] - 1.72) < 0.05
    assert "GLAS" not in g["measured_slope"]                              # GLAH06 carries no per-shot slope
