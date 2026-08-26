"""DEM backend selection and addressing. Offline unit tests plus one network-gated integration test
(set AICESAT_NET_TESTS=1 to run the live Copernicus+geoid / REMA reads)."""
import os

import numpy as np
import pytest

from aicesat import dem, scene


def test_rema_tile_addressing():
    # REMA v2.0 32 m grid: 100 km tiles, origin at (-3e6, -3e6) in EPSG:3031, names zero-padded {row}_{col}.
    # Derived from real tile bounds: 06_38 covers x in [7e5,8e5], y in [-25e5,-24e5].
    assert dem._tile_rc(750_000, -2_450_000, dem.REMA_ORIGIN) == (6, 38)
    assert dem._tile_rc(650_000, -2_424_992, dem.REMA_ORIGIN) == (6, 37)
    assert "06_37" in dem.REMA_URL.format(r=6, c=37)          # zero-padded in the URL
    # ArcticDEM grid is a different origin (-4e6); its wrappers are unchanged.
    assert dem.tile_index(2_950_000, -1_650_000) == (24, 70)


def test_copernicus_url_rule():
    # Label is the integer SW corner; floor() handles negatives (lat -49.5 -> S50, lon -70.2 -> W071).
    assert "N46_00_E008" in dem._cop_url(8, 46)
    assert "S50_00_W071" in dem._cop_url(-71, -50)
    assert "N00_00_E000" in dem._cop_url(0, 0)


def test_dispatch_by_frame_crs(monkeypatch):
    calls = {}
    monkeypatch.setattr(dem, "_polar_grid", lambda *a, **k: calls.setdefault("polar", a[5:]))   # record (source, attr)
    monkeypatch.setattr(dem, "_copernicus_grid", lambda *a, **k: calls.setdefault("cop", True))
    dem.surface_for_frame({"crs": "EPSG:3413", "origin_xy": [0, 0]}, (0, 0, 1, 1), 0.0)
    dem.surface_for_frame({"crs": "EPSG:3031", "origin_xy": [0, 0]}, (0, 0, 1, 1), 0.0)
    dem.surface_for_frame({"crs": "+proj=aeqd +lat_0=0 +lon_0=0", "origin_xy": [0, 0]}, (0, 0, 1, 1), 0.0)
    assert calls["cop"] is True
    assert dem.ARCTIC_URL.startswith("https://pgc-opendata-dems") and dem.REMA_URL.startswith("https://pgc-opendata-dems")


def test_slope_deg_only_for_real_dem():
    surf = {"is_dem": True, "nx": 3, "ny": 3, "cell": 100.0, "x0": 0.0, "y0": 0.0,
            "z": [0, 1, 2, 0, 1, 2, 0, 1, 2]}   # plane sloping +x at 0.01 rad
    v = dem.slope_deg(surf, np.array([100.0]), np.array([100.0]))
    assert v is not None and abs(v - np.degrees(np.arctan(0.01))) < 0.5
    # a non-DEM dict (e.g. the old photon grid, which never set is_dem) -> None
    assert dem.slope_deg({"nx": 3, "ny": 3, "cell": 100.0, "x0": 0, "y0": 0, "z": [0] * 9}, np.array([0.0]), np.array([0.0])) is None
    assert dem.slope_deg(None, np.array([0.0]), np.array([0.0])) is None


@pytest.mark.skipif(os.environ.get("AICESAT_NET_TESTS") != "1", reason="live DEM reads; set AICESAT_NET_TESTS=1")
def test_copernicus_and_geoid_live():
    # Alpine box -> aeqd frame -> Copernicus GLO-30 + EGM2008 geoid undulation -> WGS84-ellipsoid heights.
    bbox = (8.50, 46.50, 8.60, 46.60)
    frame = scene.local_frame(bbox)
    assert frame["crs"].startswith("+proj=aeqd")
    d = dem.surface_for_frame(frame, scene.bbox_extent(frame), 2400.0, cell_m=100.0)
    assert d is not None and d["source"] == "Copernicus GLO-30" and d["is_dem"]
    z = np.array([np.nan if v is None else v for v in d["z"]], dtype="f8") + 2400.0
    assert 800.0 < np.nanmin(z) and np.nanmax(z) < 3600.0    # alpine ellipsoidal heights, geoid-corrected (~+52 m)
