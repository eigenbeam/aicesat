import numpy as np
from aicesat import geom


def test_points_in_polygon_triangle_and_bbox():
    poly = [(-45.0, 69.8), (-43.0, 69.8), (-44.0, 70.2)]
    lon = np.array([-44.0, -44.0, -45.0, -43.5]); lat = np.array([69.9, 70.19, 70.1, 70.1])
    assert geom.points_in_polygon(lon, lat, poly).tolist() == [True, True, False, False]
    assert geom.polygon_bbox(poly) == (-45.0, 69.8, -43.0, 70.2)
    bb, pg = geom.normalize_area(polygon=poly)
    assert bb == (-45.0, 69.8, -43.0, 70.2) and pg == poly
    bb, pg = geom.normalize_area(bbox=[-45, 69.8, -43, 70.2])
    assert pg is None and bb == (-45.0, 69.8, -43.0, 70.2)
