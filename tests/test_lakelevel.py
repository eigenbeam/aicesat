"""Water-surface extraction from ATL03 photons (lakelevel.py).

A lake's photon distribution is not symmetric — a dense surface spike, noise across the telemetry window, and a
subsurface tail BELOW the surface from volume scattering. The tests build exactly that and check the estimator
recovers the surface rather than the distribution's centre, because the difference between those two IS the method.
"""
import numpy as np
import pytest

from aicesat import lakelevel

RNG = np.random.default_rng(0)


def photons(z_surf, n_surf=800, n_noise=400, n_sub=300, sub_depth=3.0, sigma=0.06, window=40.0):
    """A realistic pass: surface spike + uniform noise over the telemetry window + a subsurface tail below."""
    surf = RNG.normal(z_surf, sigma, n_surf)
    noise = RNG.uniform(z_surf - window / 2, z_surf + window / 2, n_noise)
    sub = z_surf - np.abs(RNG.exponential(sub_depth, n_sub))       # strictly BELOW the surface
    return np.concatenate([surf, noise, sub])


def test_recovers_a_known_surface():
    z = 4966.4
    s = lakelevel.surface_height(photons(z))
    assert s is not None
    assert abs(s["z"] - z) < 0.05, f"got {s['z']:.3f}, want {z}"
    assert s["mad"] < 0.10


def test_a_plain_median_is_fine_while_the_surface_dominates():
    """Stated because the opposite is tempting to assume: with the surface a clear majority, the median sits inside
    the spike and needs no help. The mode-anchored estimator must not be sold on a bias that is not there."""
    z = 4966.4
    h = photons(z, n_surf=800, n_noise=400, n_sub=300)
    assert abs(np.median(h) - z) < 0.15
    assert abs(lakelevel.surface_height(h)["z"] - z) < 0.05


def test_the_mode_beats_the_median_once_the_subsurface_tail_dominates():
    """The regime the estimator is actually for — turbid or shallow water, where most returns come from below the
    surface. Here the median tracks the water column and the mode still tracks the surface."""
    z = 4966.4
    h = photons(z, n_surf=300, n_noise=200, n_sub=1500, sub_depth=4.0)
    s = lakelevel.surface_height(h)
    naive = np.median(h)
    assert abs(naive - z) > 0.5, f"premise: the median should be dragged down, got {naive - z:+.2f} m"
    assert abs(s["z"] - z) < 0.10, f"the mode-anchored estimate should hold, got {s['z'] - z:+.2f} m"
    assert s["bias_note"] > 0.5, "bias_note must report the tail rather than hide it"


def test_bias_note_goes_NEGATIVE_when_the_mask_lets_in_higher_ground():
    """Observed on the first real run: a 400 m circle over a 2.8 x 0.6 km lake caught shoreline, and bias_note came
    back -1.53 m. The sign is the diagnostic — negative means land above the water is in the mask, not a subsurface
    tail — so it must not be documented or tested as one-sided."""
    z = 4967.2
    water = photons(z, n_surf=900, n_noise=200, n_sub=100)
    shore = RNG.normal(z + 12.0, 4.0, 1200)                    # moraine standing above the lake
    s = lakelevel.surface_height(np.concatenate([water, shore]))
    assert abs(s["z"] - z) < 0.10, "the mode should still find the water"
    assert s["bias_note"] < -0.5, f"expected a negative bias_note, got {s['bias_note']:+.2f}"


def test_survives_mostly_noise():
    z = 5000.0
    s = lakelevel.surface_height(photons(z, n_surf=300, n_noise=1500, n_sub=100))
    assert s is not None and abs(s["z"] - z) < 0.08
    assert s["frac"] < 0.5, "most photons are noise; the surface is still found"


def test_rejects_a_pass_with_no_surface():
    """Pure noise has a histogram peak too. Requiring the peak to hold a real share of the photons is what stops a
    noise cluster being reported as a lake level."""
    h = RNG.uniform(4900, 5100, 2000)
    assert lakelevel.surface_height(h) is None


def test_rejects_too_few_photons():
    assert lakelevel.surface_height(RNG.normal(5000, 0.05, 5)) is None


def test_handles_empty_and_nan():
    assert lakelevel.surface_height(np.array([])) is None
    assert lakelevel.surface_height(np.full(50, np.nan)) is None


# --- pass grouping -------------------------------------------------------------------------------------------
def _arrays(specs):
    lon, lat, h, t, gi, bi = [], [], [], [], [], []
    for g, b, day, z, n in specs:
        p = photons(z, n_surf=n, n_noise=n // 2, n_sub=n // 3)
        h.append(p)
        lon.append(np.full(p.size, 86.925)); lat.append(np.full(p.size, 27.899))
        t.append(np.full(p.size, np.datetime64(day, "ms")))
        gi.append(np.full(p.size, g, "i2")); bi.append(np.full(p.size, b, "i1"))
    return {"lon": np.concatenate(lon), "lat": np.concatenate(lat), "h": np.concatenate(h),
            "t": np.concatenate(t), "granule_idx": np.concatenate(gi), "beam_idx": np.concatenate(bi)}


def test_each_granule_beam_crossing_is_its_own_pass():
    a = _arrays([(0, 0, "2019-05-01", 4966.5, 500), (0, 1, "2019-05-01", 4966.5, 500),
                 (1, 0, "2020-05-01", 4966.2, 500)])
    rows = lakelevel.passes(a)
    assert len(rows) == 3, "three (granule, beam) crossings"
    assert [r["granule_idx"] for r in rows] == [0, 0, 1]


def test_a_beam_that_missed_the_water_is_dropped_not_averaged_in():
    """A beam crossing rock instead of water has no surface mode. It must not contribute a bogus level."""
    a = _arrays([(0, 0, "2019-05-01", 4966.5, 600)])
    rock = RNG.uniform(5200, 5600, 1500)                       # steep terrain: no dense mode
    a = {k: np.concatenate([v, np.full(rock.size, {"lon": 86.925, "lat": 27.899}.get(k, 0))
                            if k in ("lon", "lat") else
                            (rock if k == "h" else
                             np.full(rock.size, np.datetime64("2019-05-01", "ms")) if k == "t" else
                             np.full(rock.size, 0 if k == "granule_idx" else 5,
                                     "i2" if k == "granule_idx" else "i1"))])
         for k, v in a.items()}
    rows = lakelevel.passes(a)
    assert [r["beam_idx"] for r in rows] == [0], "only the water-crossing beam yields a level"


# --- series --------------------------------------------------------------------------------------------------
def test_same_day_beams_collapse_to_one_epoch():
    """Six beams of one overpass are not six independent samples of the lake level; counting them as six would
    shrink the reported uncertainty for free."""
    a = _arrays([(0, b, "2019-05-01", 4966.5, 400) for b in range(3)])
    s = lakelevel.series(lakelevel.passes(a))
    assert len(s["epochs"]) == 1 and s["epochs"][0]["n_passes"] == 3
    assert s["n_passes"] == 3


def test_a_falling_lake_gives_a_negative_trend():
    specs = [(i, 0, d, z, 500) for i, (d, z) in enumerate(
        [("2019-05-01", 4966.60), ("2020-05-01", 4966.45), ("2021-05-01", 4966.30), ("2022-05-01", 4966.15)])]
    s = lakelevel.series(lakelevel.passes(_arrays(specs)))
    assert len(s["epochs"]) == 4
    assert s["trend_m_per_yr"] == pytest.approx(-0.15, abs=0.03)
    assert s["resid_sd_m"] < 0.05
    assert s["span_yr"] == pytest.approx(3.0, abs=0.05)


def test_no_trend_is_reported_from_too_few_epochs():
    s = lakelevel.series(lakelevel.passes(_arrays([(0, 0, "2019-05-01", 4966.5, 500)])))
    assert s["trend_m_per_yr"] is None, "a single epoch is not a trend"


def test_empty_input_is_not_an_error():
    s = lakelevel.series([])
    assert s["n_passes"] == 0 and s["trend_m_per_yr"] is None


# --- photon-derived water mask -------------------------------------------------------------------------------
def _scene_photons(z=4967.0, lake_km=(2.8, 0.6), seed=1):
    """A lake plus the mountain around it, in lon/lat. The lake is elongated — the shape a circle gets wrong."""
    r = np.random.default_rng(seed)
    lo0, la0 = 86.925, 27.899
    mx = 111e3 * np.cos(np.radians(la0))
    # water: an elongated patch, dense, flat
    n_w = 40000
    wx = r.uniform(-lake_km[0] / 2 * 1000, lake_km[0] / 2 * 1000, n_w)
    wy = r.uniform(-lake_km[1] / 2 * 1000, lake_km[1] / 2 * 1000, n_w)
    wh = r.normal(z, 0.06, n_w)
    # terrain: everywhere in a 5 km box, sloping up away from the lake, plus noise photons
    n_t = 60000
    tx = r.uniform(-2500, 2500, n_t); ty = r.uniform(-2000, 2000, n_t)
    th = z + 20 + 0.25 * np.abs(ty) + r.normal(0, 8.0, n_t)      # rises away from the lake axis, rough
    x, y, h = np.concatenate([wx, tx]), np.concatenate([wy, ty]), np.concatenate([wh, th])
    return lo0 + x / mx, la0 + y / 111e3, h, z


def test_the_mask_finds_the_lake_from_a_small_off_centre_seed():
    """The seed only has to LAND on the water, not contain it — a 300 m seed recovering a 2.8 km lake is the point.

    NB the synthetic scene here fills the lake with photons everywhere. Real ICESat-2 only samples the track lines,
    so `sampled_km2` on real data is (track length through the water) x cell_m and is far smaller than the lake —
    0.19 km2 against ~1.3 km2 on Imja. That is the sampling, not a truncated mask; see test below."""
    lon, lat, h, z = _scene_photons()
    m, info = lakelevel.find_water(lon, lat, h, 86.925 - 0.008, 27.899, seed_radius_m=300)
    assert m is not None, info
    assert abs(info["z_water"] - z) < 0.05
    # the recovered footprint should be a good fraction of a 2.8 x 0.6 km lake, not a 300 m circle
    assert info["sampled_km2"] > 0.8, f"only recovered {info['sampled_km2']:.2f} km2"
    assert info["n_photons"] > 20000


def test_the_mask_excludes_terrain_that_merely_crosses_the_water_elevation():
    """A hillside passing through the lake's elevation contributes in-band photons. Requiring them to DOMINATE the
    cell is what keeps it out; without that the mask would smear up the valley walls."""
    lon, lat, h, z = _scene_photons()
    r = np.random.default_rng(2)
    n = 4000                                             # a slope 1.5 km north, sweeping through z
    lo0, la0 = 86.925, 27.899
    sy = np.full(n, 1500.0) + r.normal(0, 50, n)
    sh = z + r.uniform(-40, 40, n)                       # spans the water elevation but is nowhere flat
    lon2 = np.concatenate([lon, np.full(n, lo0)])
    lat2 = np.concatenate([lat, la0 + sy / 111e3])
    h2 = np.concatenate([h, sh])
    m, info = lakelevel.find_water(lon2, lat2, h2, lo0, la0, seed_radius_m=300)
    picked_north = lat2[m] > la0 + 1000 / 111e3
    assert picked_north.sum() == 0, f"{int(picked_north.sum())} photons from the crossing slope leaked into the mask"


def test_find_water_reports_why_it_failed():
    lon, lat, h, _ = _scene_photons()
    m, info = lakelevel.find_water(lon, lat, h, 90.0, 30.0, seed_radius_m=200)   # seed nowhere near the data
    assert m is None and "why" in info


def test_the_mask_beats_a_circle_of_the_same_seed_radius():
    """The comparison that motivated this: same seed, how much lake does each mask capture?"""
    lon, lat, h, z = _scene_photons()
    lo0, la0 = 86.925, 27.899
    d = np.hypot((lon - lo0) * 111e3 * np.cos(np.radians(la0)), (lat - la0) * 111e3)
    circle = d <= 400
    m, _info = lakelevel.find_water(lon, lat, h, lo0, la0, seed_radius_m=400)
    water = np.abs(h - z) < 0.5
    assert (m & water).sum() > 3 * (circle & water).sum(), "the grown mask should capture far more of the lake"


def test_the_mask_keeps_a_water_cell_whole_not_just_its_in_band_photons():
    """The mask returns every photon of a qualifying cell. If it returned only the in-band ones, surface_height
    would see a pre-filtered distribution: `frac` would be ~1 and `bias_note` ~0 for every pass, silently disabling
    both quality diagnostics. Caught by mutation — the docstring claimed this and nothing checked it."""
    lon, lat, h, z = _scene_photons()
    m, _ = lakelevel.find_water(lon, lat, h, 86.925, 27.899, seed_radius_m=300)
    inside = h[m]
    out_of_band = np.abs(inside - z) > lakelevel.BAND_M
    assert out_of_band.sum() > 0, "a water cell's noise and subsurface photons must survive the mask"
    s = lakelevel.surface_height(inside)
    assert s["frac"] < 1.0, "frac is meaningless if the mask pre-filters to the band"


def test_sampled_km2_is_the_ground_the_beams_crossed_not_the_lake():
    """Real passes are lines, not coverage. Reporting the sampled area as if it were the lake's area invites the
    reader to conclude the mask failed, when a 6-crossing sample of a 1.3 km2 lake SHOULD read ~0.2 km2."""
    r = np.random.default_rng(3)
    lo0, la0 = 86.925, 27.899
    mx = 111e3 * np.cos(np.radians(la0))
    lon, lat, h = [], [], []
    for i, y in enumerate((-200.0, 0.0, 150.0)):                 # three track lines across a 2 km wide lake
        x = np.linspace(-1000, 1000, 3000)
        lon.append(lo0 + x / mx); lat.append(la0 + (y + r.normal(0, 3, x.size)) / 111e3)
        h.append(r.normal(4967.0, 0.06, x.size))
    lon, lat, h = np.concatenate(lon), np.concatenate(lat), np.concatenate(h)
    _m, info = lakelevel.find_water(lon, lat, h, lo0, la0, seed_radius_m=300)
    lake_km2 = (2.0 * 2.0)                                        # the notional water body the lines cross
    assert info["sampled_km2"] < 0.4 * lake_km2, "sampled area must reflect the tracks, not the water body"
    assert info["n_cells"] >= 20, f"but it should still span the crossings, got {info['n_cells']} cells"


# --- the ground beside the water ------------------------------------------------------------------------------
def _lake_with_shore(z=4967.0, shore_heights=((0, 3.0), (90, 12.0), (180, 8.0), (270, 25.0)), seed=7):
    """A lake with four shore sectors at different heights above the water, at known bearings (0=N, 90=E)."""
    r = np.random.default_rng(seed)
    lo0, la0 = 86.925, 27.899
    mx = 111e3 * np.cos(np.radians(la0))
    x = r.uniform(-400, 400, 30000); y = r.uniform(-400, 400, 30000)
    inside = (np.abs(x) < 300) & (np.abs(y) < 300)
    lon = [lo0 + x[inside] / mx]; lat = [la0 + y[inside] / 111e3]; h = [r.normal(z, 0.06, inside.sum())]
    for bearing, up in shore_heights:                       # a shore block 450 m out along each bearing
        b = np.radians(bearing)
        cx, cy = 450 * np.sin(b), 450 * np.cos(b)
        n = 4000
        sx = cx + r.uniform(-120, 120, n); sy = cy + r.uniform(-120, 120, n)
        lon.append(lo0 + sx / mx); lat.append(la0 + sy / 111e3)
        h.append(r.normal(z + up, 0.5, n))                  # rough-ish ground
    return np.concatenate(lon), np.concatenate(lat), np.concatenate(h), z


def test_margin_reports_ground_height_above_the_water():
    lon, lat, h, z = _lake_with_shore()
    water = lakelevel.water_mask(lon, lat, h, z)
    rows = lakelevel.margin(lon, lat, h, z, water)
    assert rows, "no margin cells found"
    assert rows == sorted(rows, key=lambda r: r["above_water_m"]), "lowest ground must come first"
    lowest = rows[0]
    assert 2.0 < lowest["above_water_m"] < 4.5, f"lowest shore is 3 m up, got {lowest['above_water_m']:.2f}"


def test_margin_bearings_identify_the_sector():
    """The paper's claim is about the NORTHEASTERN margin specifically, so a bearing per cell is what makes the
    measurement answer their question rather than a generic one."""
    lon, lat, h, z = _lake_with_shore(shore_heights=((45, 2.0), (225, 20.0)))
    water = lakelevel.water_mask(lon, lat, h, z)
    rows = lakelevel.margin(lon, lat, h, z, water)
    ne = [r for r in rows if 20 <= r["bearing_deg"] <= 70]
    sw = [r for r in rows if 200 <= r["bearing_deg"] <= 250]
    assert ne and sw, f"bearings found: {sorted(round(r['bearing_deg']) for r in rows)}"
    assert np.median([r["above_water_m"] for r in ne]) < np.median([r["above_water_m"] for r in sw]) - 10


def test_margin_excludes_the_water_cells_themselves():
    lon, lat, h, z = _lake_with_shore()
    water = lakelevel.water_mask(lon, lat, h, z)
    rows = lakelevel.margin(lon, lat, h, z, water)
    assert all(r["above_water_m"] > 1.0 for r in rows), "a water cell leaked into the margin"


def test_margin_is_empty_without_water():
    lon, lat, h, _z = _lake_with_shore()
    assert lakelevel.margin(lon, lat, h, 4967.0, np.zeros(lon.size, bool)) == []


# --- time to crossing -----------------------------------------------------------------------------------------
def test_years_to_crossing():
    assert lakelevel.years_to_crossing(1.30, -0.130) == pytest.approx(10.0)
    assert lakelevel.years_to_crossing(0.90, -0.130) == pytest.approx(6.92, abs=0.01)


@pytest.mark.parametrize("above,rate", [(1.0, 0.0), (1.0, +0.05), (-0.5, -0.13), (0.0, -0.13)])
def test_no_crossing_time_when_the_extrapolation_is_meaningless(above, rate):
    """Ground that is not sinking, or already below the water, has no 'time to crossing'. Returning a number there
    would read as a forecast of something that is not happening."""
    assert lakelevel.years_to_crossing(above, rate) is None


def test_margin_drops_cells_that_are_really_lake():
    """From the first real run: seven 'margin' cells came back 0.02-0.30 m BELOW the water, with 3-5 year crossing
    times attached. Dry ground cannot sit below the lake it borders — those cells were water that missed the mask's
    threshold, so the estimator measured the lake and called it shore."""
    r = np.random.default_rng(11)
    lo0, la0 = 86.925, 27.899
    mx = 111e3 * np.cos(np.radians(la0))
    z = 4967.0
    # a water body, and one adjacent cell that is 90% water (a mask near-miss) plus a genuine shore cell
    x = r.uniform(-250, 250, 20000); y = r.uniform(-250, 250, 20000)
    lon = [lo0 + x / mx]; lat = [la0 + y / 111e3]; h = [r.normal(z, 0.06, x.size)]
    # 0.40 is the gap that matters: BELOW water_mask's min_cell_frac (0.5) so it is not masked as water, but ABOVE
    # margin's max_water_frac (0.25) so it must still be rejected as margin. A 0.9 cell would simply be masked and
    # never reach margin() at all — which is how the first version of this test passed with the guard removed.
    for cx, frac, up in ((350, 0.40, 0.0), (550, 0.0, 6.0)):    # near-miss water cell, then real shore
        n = 3000
        sx = cx + r.uniform(-45, 45, n); sy = r.uniform(-45, 45, n)
        nw = int(n * frac)
        hh = np.concatenate([r.normal(z, 0.06, nw), r.normal(z + up + 1.0, 0.5, n - nw)])
        lon.append(lo0 + sx / mx); lat.append(la0 + sy / 111e3); h.append(hh)
    lon, lat, h = np.concatenate(lon), np.concatenate(lat), np.concatenate(h)
    water = lakelevel.water_mask(lon, lat, h, z)
    rows = lakelevel.margin(lon, lat, h, z, water)
    assert rows, "the genuine shore cell should survive"
    assert all(r_["water_frac"] <= lakelevel.MAX_WATER_FRAC for r_ in rows), \
        f"a water-dominated cell survived: {[round(r_['water_frac'], 2) for r_ in rows]}"
    assert all(r_["above_water_m"] > 0 for r_ in rows), \
        f"a below-water 'ground' survived: {[round(r_['above_water_m'], 2) for r_ in rows]}"


def test_the_dispersion_metric_is_not_capped_by_the_mode_window():
    """spread_m is bounded by 2*win_m and read 3-4 m for every cell on the real run — water and rock alike. p5_p95_m
    is computed over all the cell's photons so it can actually discriminate."""
    r = np.random.default_rng(12)
    lo0, la0 = 86.925, 27.899
    mx = 111e3 * np.cos(np.radians(la0))
    z = 4967.0
    x = r.uniform(-250, 250, 20000); y = r.uniform(-250, 250, 20000)
    n = 3000
    sx = 350 + r.uniform(-45, 45, n)
    rough = r.normal(z + 20, 9.0, n)                              # ~30 m of relief in one cell
    lon = np.concatenate([lo0 + x / mx, lo0 + sx / mx])
    lat = np.concatenate([la0 + y / 111e3, la0 + r.uniform(-45, 45, n) / 111e3])
    h = np.concatenate([r.normal(z, 0.06, x.size), rough])
    rows = lakelevel.margin(lon, lat, h, z, lakelevel.water_mask(lon, lat, h, z))
    assert rows, "no margin cell"
    rr = rows[0]
    assert rr["spread_m"] <= 2 * lakelevel.MARGIN_WIN_M + 1e-6, "premise: spread_m is capped by the window"
    assert rr["p5_p95_m"] > 10.0, f"p5_p95 should see the real relief, got {rr['p5_p95_m']:.1f} m"
