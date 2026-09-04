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
