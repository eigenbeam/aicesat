"""The DEM and imagery must cover the ground the POINTS cover, not the box the user drew.

Measured on the Langtang scene: the drawn bbox is 529 km2, the res-5 cells the lake was addressed by are 2,861 km2,
and only 28.1% of the 294,646 returned points fall inside the box. Drawing terrain over the bbox alone left three
quarters of the cloud hanging past the edge of its own surface.
"""
import numpy as np
import pytest

from aicesat import scene

BBOX = (85.35, 28.20, 85.62, 28.38)     # Langtang / the 26 Aug 2026 flood source
GREENLAND = (-51.0, 69.0, -49.0, 69.4)


def _frame(bbox):
    return scene.local_frame(bbox)


def test_data_extent_is_larger_than_the_drawn_box_and_contains_it():
    fr = _frame(BBOX)
    b = scene.bbox_extent(fr)
    d = scene.data_extent(fr)
    assert d[0] <= b[0] and d[1] <= b[1] and d[2] >= b[2] and d[3] >= b[3], "the cell footprint must contain the bbox"
    assert (d[2] - d[0]) > (b[2] - b[0]) and (d[3] - d[1]) > (b[3] - b[1]), "and be strictly larger for a small box"


def test_data_extent_matches_the_cells_a_query_would_read():
    """It must agree with planner.cells_for_bbox at the addressing resolution — the same cells the lake read is
    scoped to — or the terrain covers different ground from the points again, just differently wrong."""
    import h3

    from aicesat import planner
    fr = _frame(BBOX)
    cells = planner.cells_for_bbox(fr["bbox"], res=scene.DATA_RES)
    lats, lons = [], []
    for c in cells:
        for la, lo in h3.cell_to_boundary(c if isinstance(c, str) else h3.int_to_str(int(c))):
            lats.append(la); lons.append(lo)
    xs, ys = scene.to_local(fr, np.asarray(lons), np.asarray(lats))
    d = scene.data_extent(fr)
    assert d == pytest.approx((float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())))


def test_data_extent_falls_back_to_the_bbox_when_the_cell_fill_fails(monkeypatch):
    """A surface over the drawn box is the old behaviour and beats no surface at all."""
    from aicesat import planner
    monkeypatch.setattr(planner, "cells_for_bbox", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("h3 down")))
    fr = _frame(BBOX)
    assert scene.data_extent(fr) == scene.bbox_extent(fr)


def test_data_extent_falls_back_when_no_cells_come_back(monkeypatch):
    from aicesat import planner
    monkeypatch.setattr(planner, "cells_for_bbox", lambda *a, **k: [])
    fr = _frame(BBOX)
    assert scene.data_extent(fr) == scene.bbox_extent(fr)


def test_data_res_is_the_coarsest_addressing_resolution():
    """ATL06/GLAS/ICESSN read at res 5 and ATL03 at res 6, so the res-5 hull is the widest ground any enabled
    collection can return — which makes it the scene's real footprint."""
    from aicesat import index_atl06, index_glas, index_icessn
    from aicesat import index as atl03_index
    assert scene.DATA_RES == index_atl06.ATL06_RES == index_glas.GLAS_RES == index_icessn.ICESSN_RES == 5
    assert atl03_index.H3_RES == 6, "ATL03 is finer, so its footprint sits inside the res-5 hull"


def test_data_extent_works_for_a_polar_frame():
    """The polar frames are rotated relative to north; the extent is built from the cells' corners through
    to_local, so it must not assume an axis-aligned projection."""
    fr = _frame(GREENLAND)
    assert fr["crs"] == "EPSG:3413"
    d = scene.data_extent(fr)
    b = scene.bbox_extent(fr)
    assert d[0] <= b[0] and d[2] >= b[2] and d[1] <= b[1] and d[3] >= b[3]


@pytest.mark.parametrize("fn", ["add_imagery", "set_surface"])
def test_the_surface_and_imagery_both_use_the_data_extent(fn):
    """The wiring is the whole point: computing data_extent and then passing bbox_extent would change nothing."""
    import inspect
    src = inspect.getsource(getattr(scene, fn))
    assert "data_extent(" in src, f"{fn} still sizes itself to the drawn bbox"
    assert "bbox_extent(" not in src, f"{fn} still passes the bbox extent"
