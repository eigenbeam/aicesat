"""The cells a selection covers: every cell it really overlaps, and not the ones that only share its edge.

A change map's selection IS an H3 cell (api.hex_polygon, vertices rounded to 1e-6 deg). h3's "overlap" containment
counted the six neighbours that merely share an edge with it, so every mission fetched the neighbours too and the scene
clip threw them away. Measured on EC2, hex 8506f377fffffff: 6 cells, 1,747 ATL06 beam-chunks of which 534 touch the
hex; 16.9 s / 2,214 MB cold, 7.2 s / 773 MB with the hex alone."""
import h3
import numpy as np

from aicesat import api, planner

HX = "8506f377fffffff"


def _ids(cells):
    return {h3.int_to_str(int(c)) for c in cells}


def _bbox(poly):
    return [min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly)]


def test_a_hex_covers_itself_not_its_neighbours():
    for hx in (HX, "8506f257fffffff", "8506f2cffffffff"):
        poly = api.hex_polygon(hx)
        assert _ids(planner.cells_for_bbox(_bbox(poly), res=5, polygon=poly)) == {hx}


def test_finer_cells_straddling_the_edge_still_count():
    """At res 6 (ATL03's index) the hex's edge cuts through cells: every res-6 cell holding part of the hex is covered."""
    poly = api.hex_polygon(HX)
    got = _ids(planner.cells_for_bbox(_bbox(poly), res=6, polygon=poly))
    # sample points just inside the hex boundary: each one's res-6 cell must be in the cover
    lat_c, lon_c = h3.cell_to_latlng(HX)
    ring = h3.cell_to_boundary(HX)
    for (la0, lo0), (la1, lo1) in zip(ring, ring[1:] + ring[:1]):
        for t in np.linspace(0, 1, 25):
            la, lo = la0 + t * (la1 - la0), lo0 + t * (lo1 - lo0)
            la, lo = lat_c + 0.995 * (la - lat_c), lon_c + 0.995 * (lo - lon_c)   # ~50 m inside the edge
            assert h3.latlng_to_cell(la, lo, 6) in got
    assert len(got) > 7, "a res-5 hex spans many res-6 cells"


def test_a_selection_across_an_edge_covers_both_sides():
    a = HX
    b = h3.grid_ring(a, 1)[0]
    (la0, lo0), (la1, lo1) = [h3.cell_to_latlng(c) for c in (a, b)]
    mla, mlo = (la0 + la1) / 2, (lo0 + lo1) / 2           # on the shared edge, roughly
    d = 0.01
    poly = [[mlo - d, mla - d], [mlo + d, mla - d], [mlo + d, mla + d], [mlo - d, mla + d]]
    assert {a, b} <= _ids(planner.cells_for_bbox(_bbox(poly), res=5, polygon=poly))


def test_a_box_still_covers_the_cells_its_corners_sit_in():
    bbox = [-49.9, 69.1, -49.3, 69.25]
    got = _ids(planner.cells_for_bbox(bbox, res=6))
    for lo, la in ((-49.9, 69.1), (-49.3, 69.1), (-49.3, 69.25), (-49.9, 69.25)):
        assert h3.latlng_to_cell(la + (1e-4 if la < 69.2 else -1e-4), lo + (1e-4 if lo < -49.5 else -1e-4), 6) in got


def test_a_selection_smaller_than_the_shrink_keeps_its_cell():
    lat, lon = h3.cell_to_latlng(HX)
    e = 1e-7
    poly = [[lon - e, lat - e], [lon + e, lat - e], [lon + e, lat + e], [lon - e, lat + e]]
    assert _ids(planner.cells_for_bbox(_bbox(poly), res=5, polygon=poly)) == {HX}
