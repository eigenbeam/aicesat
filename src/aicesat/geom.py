"""Small geometry helpers: polygon bbox, vectorised point-in-polygon (ray casting), and surface slope from a
natively measured gradient. No extra dependency."""
from __future__ import annotations

import numpy as np

Polygon = list[tuple[float, float]]  # [(lon, lat), ...], not necessarily closed


def slope_deg_median(dzdx, dzdy) -> float | None:
    """Median surface slope (degrees) from a two-component gradient (rise/run, i.e. tan of the slope angle).

    Both products that measure slope natively deliver it this way and in these units — ATL06
    `fit_statistics/dh_fit_dx` + `dh_fit_dy`, ILATM2 `South-to-North_Slope` + `West-to-East_Slope` — so the two
    are directly comparable. NaNs (fill) are ignored; None when nothing is finite.
    """
    m = np.hypot(np.asarray(dzdx, dtype="f8"), np.asarray(dzdy, dtype="f8"))
    m = m[np.isfinite(m)]
    return float(np.degrees(np.arctan(np.median(m)))) if m.size else None


def polygon_bbox(poly: Polygon) -> tuple[float, float, float, float]:
    lons, lats = zip(*poly)
    return (float(min(lons)), float(min(lats)), float(max(lons)), float(max(lats)))


def points_in_polygon(lon: np.ndarray, lat: np.ndarray, poly: Polygon) -> np.ndarray:
    """Even-odd rule in lon/lat space; fine for polygons a few degrees across."""
    x, y = np.asarray(lon, dtype="f8"), np.asarray(lat, dtype="f8")
    inside = np.zeros(x.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        crosses = (y1 > y) != (y2 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (x < xi)
    return inside


def normalize_area(bbox=None, polygon=None) -> tuple[tuple[float, float, float, float], Polygon | None]:
    """Accept either a bbox or a polygon; returns (bbox, polygon-or-None)."""
    if polygon:
        poly = [(float(a), float(b)) for a, b in polygon]
        if len(poly) < 3:
            raise ValueError("polygon needs at least 3 vertices")
        return polygon_bbox(poly), poly
    if bbox is None:
        raise ValueError("need bbox or polygon")
    w, s, e, n = map(float, bbox)
    if not (w < e and s < n):
        raise ValueError(f"bad bbox {bbox}")
    return (w, s, e, n), None
