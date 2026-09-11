"""Region selection for the High Mountain Asia 8 m DEM, and the fallbacks that must survive its absence.

Measured over the Langtang/Trishuli headwaters against 23,289 ATL06 segments: HMA RMSE 11.5 m vs Copernicus
GLO-30's 29.8 m, and 15.0 m vs 51.1 m on slopes over 40 deg. Copernicus is not biased there — its median is within
~1 m at every slope band — it just cannot represent a 45 deg face on a 30 m grid.
"""
import numpy as np
import pytest

from aicesat import dem, scene

HMA_BBOX = (85.35, 28.20, 85.62, 28.38)     # Langtang / the 26 Aug 2026 flood source
ALPS_BBOX = (7.5, 45.8, 8.0, 46.1)          # non-polar, outside the HMA footprint
GREENLAND_BBOX = (-51.0, 69.0, -49.0, 69.4)


def _frame(bbox):
    fr = scene.local_frame(bbox)
    return fr, scene.bbox_extent(fr)


def test_hma_is_not_consulted_outside_its_footprint(monkeypatch):
    """The lon/lat gate must reject before any catalogue lookup — every non-HMA scene would otherwise pay a CMR
    round trip to be told no."""
    called = []
    monkeypatch.setattr(dem, "_hma_tile_urls", lambda b: called.append(b) or [])
    fr, ext = _frame(ALPS_BBOX)
    assert dem._hma_grid(fr, ext, 0.0, 100.0) is None
    assert called == [], "looked up HMA tiles for a scene outside the footprint"


def test_hma_falls_back_when_no_tiles_cover_the_scene(monkeypatch):
    monkeypatch.setattr(dem, "_hma_tile_urls", lambda b: [])
    fr, ext = _frame(HMA_BBOX)
    assert dem._hma_grid(fr, ext, 0.0, 100.0) is None


def test_surface_for_frame_falls_through_to_copernicus_when_hma_misses(monkeypatch):
    """Any HMA miss — footprint, tiles, or a missing Earthdata token — must still yield a surface."""
    monkeypatch.setattr(dem, "_hma_grid", lambda *a, **k: None)
    monkeypatch.setattr(dem, "_copernicus_grid", lambda *a, **k: {"source": "Copernicus GLO-30"})
    fr, ext = _frame(HMA_BBOX)
    assert dem.surface_for_frame(fr, ext, 0.0)["source"] == "Copernicus GLO-30"


def test_hma_never_shadows_the_polar_dems(monkeypatch):
    """HMA is checked only on the non-polar branch; a polar frame must not even reach it."""
    monkeypatch.setattr(dem, "_hma_grid", lambda *a, **k: pytest.fail("HMA consulted for a polar frame"))
    monkeypatch.setattr(dem, "_polar_grid", lambda *a, **k: {"source": "ArcticDEM"})
    fr, ext = _frame(GREENLAND_BBOX)
    assert fr["crs"] == "EPSG:3413"
    assert dem.surface_for_frame(fr, ext, 0.0)["source"] == "ArcticDEM"


def test_hma_tile_urls_are_memoised_per_box(monkeypatch):
    """One catalogue lookup per scene AREA. Without the memo every surface rebuild re-searches."""
    dem._HMA_TILES.clear()
    n = []

    class _Fake:
        def data_links(self): return ["https://data.nsidc.earthdatacloud.nasa.gov/x/y.tif"]

    import earthaccess
    monkeypatch.setattr(earthaccess, "search_data", lambda **k: n.append(1) or [_Fake()])
    monkeypatch.setattr(dem, "_HMA_TILES", {})
    from aicesat import auth
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    box = (85.0, 28.0, 85.5, 28.5)
    a = dem._hma_tile_urls(box)
    b = dem._hma_tile_urls(box)
    assert a == b and len(a) == 1
    assert len(n) == 1, "the second call re-searched instead of using the memo"


def test_hma_tile_lookup_failure_is_not_fatal(monkeypatch):
    """A catalogue outage must degrade to Copernicus, not take the scene down."""
    import earthaccess
    monkeypatch.setattr(dem, "_HMA_TILES", {})
    monkeypatch.setattr(earthaccess, "search_data", lambda **k: (_ for _ in ()).throw(RuntimeError("CMR down")))
    from aicesat import auth
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    assert dem._hma_tile_urls((85.0, 28.0, 85.5, 28.5)) == []


def test_hma_heights_are_ellipsoidal_so_no_geoid_is_added():
    """Determined by measurement, not documentation: against ATL06 the raw heights sit at a median -2.19 m, and
    adding the geoid undulation moves that to +29.26 m. A `+ N` here would be a 30 m error."""
    import inspect
    src = inspect.getsource(dem._hma_grid)
    body = src.split("gaps = ~np.isfinite(h)")[0]
    assert "_geoid_N" not in body, "geoid undulation applied to already-ellipsoidal HMA heights"
    assert "ellipsoidal" in src


def test_hma_gaps_are_filled_and_reported():
    """A hole tears the surface mesh, so gaps are filled from Copernicus — but the patch is 30 m data inside an
    8 m grid, so it is reported rather than hidden."""
    import inspect
    src = inspect.getsource(dem._hma_grid)
    assert "_copernicus_H" in src and "n_cells_filled" in src
    assert "seam" in src.lower()


def test_sample_proj_reprojects_before_windowing():
    """_sample_ll windows by lon/lat straight through the raster transform, which is only meaningful for EPSG:4326.
    HMA is Albers, so the projected sampler must reproject first or it reads the wrong window entirely."""
    import inspect
    src = inspect.getsource(dem._sample_proj)
    assert "Transformer.from_crs" in src and "s.crs" in src
