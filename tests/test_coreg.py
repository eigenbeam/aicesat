import numpy as np
import pytest

from aicesat import coreg


def test_decimal_year():
    t = np.array(["2020-01-01T00:00", "2020-07-01T12:00", "2005-12-31T23:59"], dtype="datetime64[ms]")
    y = coreg.decimal_year(t)
    assert abs(y[0] - 2020.0) < 1e-6
    assert 2020.49 < y[1] < 2020.51
    assert 2005.99 < y[2] < 2006.0


def test_plate_motion_magnitude_and_direction_greenland():
    """Independent expectation: v = omega x r with the ITRF2014-PMM NOAM Euler vector gives ~2.1 cm/yr toward NW at
    70N 40W, so propagating a 2020.2 observation back to 2005.0 must move it ~SE: dE ~ +0.21 m, dN ~ -0.24 m."""
    lon, lat, h = np.array([-40.0]), np.array([70.0]), np.array([2600.0])
    clon, clat, ch = coreg.propagate(lon, lat, h, np.array([2020.2]), 2005.0, "ITRF2014")
    R = 6_371_000.0
    dE = R * np.cos(np.radians(70)) * np.radians(clon - lon)[0]
    dN = R * np.radians(clat - lat)[0]
    w = np.array([0.024, -0.694, -0.063]) * 4.848e-9  # mas/yr -> rad/yr
    r = R * np.array([np.cos(np.radians(70)) * np.cos(np.radians(-40)), np.cos(np.radians(70)) * np.sin(np.radians(-40)), np.sin(np.radians(70))])
    v = np.cross(w, r)
    e = np.array([-np.sin(np.radians(-40)), np.cos(np.radians(-40)), 0.0])
    n = np.array([-np.sin(np.radians(70)) * np.cos(np.radians(-40)), -np.sin(np.radians(70)) * np.sin(np.radians(-40)), np.cos(np.radians(70))])
    dt = 2005.0 - 2020.2
    assert abs(dE - dt * v @ e) < 0.01 and abs(dN - dt * v @ n) < 0.01, (dE, dN)
    assert dE > 0.15 and dN < -0.15
    assert abs(ch[0] - h[0]) < 0.01  # plate rotation is horizontal to within mm


def test_same_epoch_is_identity():
    lon, lat, h = np.array([-40.0]), np.array([70.0]), np.array([2600.0])
    clon, clat, ch = coreg.propagate(lon, lat, h, np.array([2005.0]), 2005.0, "ITRF2014")
    assert coreg.horizontal_displacement_m(lon, lat, clon, clat)[0] < 0.001


def test_silent_identity_trap_detected():
    """If the 4th (time) coordinate is dropped, PROJ returns the input unchanged. Prove the trap exists so the
    guard in coregister_scene is meaningful."""
    tr = coreg._pipeline(2020.0, "ITRF2014")
    x, y, z = tr.transform([-40.0], [70.0], [2600.0])
    assert abs(x[0] + 40.0) < 1e-12 and abs(y[0] - 70.0) < 1e-12  # unchanged: the trap


def test_itrf2008_step_is_mm_level_and_inverted_correctly():
    lon, lat, h = np.array([-40.0]), np.array([70.0]), np.array([2600.0])
    a = coreg.propagate(lon, lat, h, np.array([2005.0]), 2005.0, "ITRF2008")
    d = coreg.horizontal_displacement_m(lon, lat, a[0], a[1])[0]
    assert 0 < d < 0.02, d
    # PROJ's init entry is ITRF2014 -> ITRF2008 forward (adds ~+1.6,+1.9,+2.4 mm in ECEF at 2010.0);
    # our frame step must be its inverse, i.e. ITRF2008 -> ITRF2014 subtracts that translation.
    from pyproj import Transformer
    tr = coreg._frame_pipeline("ITRF2008")
    to_cart = Transformer.from_pipeline("+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step +proj=cart +ellps=GRS80")
    x0, y0, z0 = to_cart.transform([0.0], [0.0], [0.0])
    lo, la, hh, _ = tr.transform([0.0], [0.0], [0.0], [2010.0])
    x1, y1, z1 = to_cart.transform(lo, la, hh)
    assert x1[0] - x0[0] < -0.001 and y1[0] - y0[0] < -0.001 and z1[0] - z0[0] < -0.002


def test_slope_fit():
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-1000, 1000, 500), rng.uniform(-1000, 1000, 500)
    z = np.tan(np.radians(2.0)) * x + 10
    s, _ = coreg.fit_slope_deg(x, y, z)
    assert abs(s - 2.0) < 1e-6


def test_colocate_and_artifact_sign():
    # photons along one beam (x), surface z = 0.05 x; one GLAS shot at the origin. Shifting the photon cloud by
    # +5 m in x brings the photon that was at x-5 to x, so the fitted height at the shot centre drops by 0.25 m.
    rng = np.random.default_rng(1)
    gx, gy, gh = np.array([0.0]), np.array([0.0]), np.array([0.0])
    px = rng.uniform(-60, 60, 5000)
    py = rng.normal(0, 0.5, px.size)
    ph = 0.05 * px
    _, dh0, _, sl = coreg.colocate(gx, gy, gh, px, py, ph, 35.0)
    _, dh1, _, _ = coreg.colocate(gx, gy, gh, px + 5.0, py, ph, 35.0)
    assert abs(dh0[0]) < 0.005
    assert abs(dh1[0] + 0.25) < 0.005
    assert abs(sl[0] - 0.05) < 1e-3  # magnitude of the along-beam slope


def test_colocate_resolves_sub_cm_shift_on_a_single_beam():
    """The estimator must be continuous in position: a 0.3 m along-beam shift on a 0.4% slope -> 1.2 mm."""
    rng = np.random.default_rng(2)
    s = rng.uniform(-40, 40, 3000)                       # photons along one beam (x axis)
    px, py = s, np.zeros_like(s) + rng.normal(0, 0.5, s.size)
    ph = 0.004 * s + rng.normal(0, 0.15, s.size)         # 0.23 deg slope, 15 cm photon noise
    gx, gy, gh = np.array([0.0]), np.array([0.0]), np.array([0.0])
    _, d0, _, _ = coreg.colocate(gx, gy, gh, px, py, ph, 35.0)
    _, d1, _, _ = coreg.colocate(gx - 0.3, gy, gh, px, py, ph, 35.0)  # shot moved 30 cm "back" along the beam
    assert abs((d1[0] - d0[0]) + 0.0012) < 0.0003


def test_colocate_drops_gross_pairs():
    gx, gy, gh = np.array([0.0, 500.0]), np.array([0.0, 0.0]), np.array([0.0, 900.0])  # second "shot" is a cloud return
    px = np.concatenate([np.linspace(-30, 30, 50), np.linspace(470, 530, 50)])
    py = np.zeros_like(px)
    ph = np.zeros_like(px)
    pairs, dh, gross, _ = coreg.colocate(gx, gy, gh, px, py, ph, 35.0)
    assert pairs.tolist() == [0] and gross == 1
