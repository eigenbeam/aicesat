"""Build the JSON document the deck.gl widget consumes.

Local frame: EPSG:3413 (NSIDC polar stereographic north) metres, minus the bbox centre; z = height
minus the ICESat-2 median height. All positions are given in that frame; the widget applies vertical
exaggeration and, in Slice 3, horizontal offset exaggeration, itself (and labels both).
"""
from __future__ import annotations

import numpy as np
from pyproj import Transformer

from . import cache

from functools import lru_cache


def frame_crs(lat: float, lon: float) -> str:
    """Local projected CRS for a scene centred at (lat, lon): polar stereographic near the poles, else a per-scene
    azimuthal-equidistant so any region on Earth renders in metres with minimal distortion for a small box."""
    if lat >= 55:
        return "EPSG:3413"      # NSIDC Sea Ice Polar Stereographic North (Arctic; matches ArcticDEM)
    if lat <= -55:
        return "EPSG:3031"      # Antarctic Polar Stereographic (matches REMA)
    return f"+proj=aeqd +lat_0={lat:.6f} +lon_0={lon:.6f} +datum=WGS84 +units=m +no_defs +type=crs"


@lru_cache(maxsize=64)
def _tr(crs: str) -> Transformer:
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)

COLORS = {"ICESAT2": [55, 138, 221], "GLAS": [216, 90, 48]}


def local_frame(bbox) -> dict:
    w, s, e, n = bbox
    clon, clat = (w + e) / 2, (s + n) / 2
    crs = frame_crs(clat, clon)
    tr = _tr(crs)
    cx, cy = tr.transform(clon, clat)
    # true-north / east unit vectors at the bbox centre (the projected +y is not generally north)
    nx, ny = tr.transform(clon, clat + 0.01)
    ex, ey = tr.transform(clon + 0.01, clat)
    nv = np.array([nx - cx, ny - cy]); ev = np.array([ex - cx, ey - cy])
    return {"crs": crs, "origin_xy": [float(cx), float(cy)], "bbox": list(bbox),
            "north_xy": (nv / np.linalg.norm(nv)).round(6).tolist(),
            "east_xy": (ev / np.linalg.norm(ev)).round(6).tolist()}


def surface_grid(frame: dict, arrays: dict, z0: float, cell_m: float = 500.0, max_cells: int = 40_000) -> dict:
    """Coarse height field from the ICESat-2 photons: per-cell median where a track crosses the cell, linear
    interpolation across the gaps between tracks (inside the convex hull only). A depth cue, labelled as such."""
    from scipy.interpolate import griddata

    x, y = to_local(frame, arrays["lon"], arrays["lat"])
    z = np.asarray(arrays["h"], dtype="f8") - z0
    x0, y0, x1, y1 = bbox_extent(frame)  # full box, so the imagery drape and axes cover the requested area
    cell = float(cell_m)
    while ((x1 - x0) / cell + 1) * ((y1 - y0) / cell + 1) > max_cells:
        cell *= 1.5
    nx, ny = int(np.ceil((x1 - x0) / cell)) + 1, int(np.ceil((y1 - y0) / cell)) + 1
    ix = ((x - x0) / cell).astype(np.int64); iy = ((y - y0) / cell).astype(np.int64)
    key = iy * nx + ix
    order = np.argsort(key, kind="stable")
    ks, zs = key[order], z[order]
    uniq, start = np.unique(ks, return_index=True)
    med = np.array([np.median(chunk) for chunk in np.split(zs, start[1:])])
    cxs, cys = x0 + (uniq % nx) * cell, y0 + (uniq // nx) * cell
    gx, gy = np.meshgrid(x0 + np.arange(nx) * cell, y0 + np.arange(ny) * cell)
    if uniq.size >= 3:
        grid = griddata((cxs, cys), med, (gx, gy), method="linear")
        outside = ~np.isfinite(grid)
        # outside the hull: distance-weighted mean of the nearest observed cells (smooth), not a nearest-cell cliff
        from scipy.spatial import cKDTree
        tree = cKDTree(np.column_stack([cxs, cys]))
        dist, nn = tree.query(np.column_stack([gx[outside], gy[outside]]), k=min(8, cxs.size))
        wgt = 1.0 / np.maximum(dist, 1.0) ** 2
        grid[outside] = (med[nn] * wgt).sum(axis=1) / wgt.sum(axis=1)
    else:
        grid, outside = np.full(gx.shape, np.nan), np.ones(gx.shape, bool)
    zlist = [None if not np.isfinite(v) else round(float(v), 2) for v in grid.ravel()]
    return {"x0": float(x0), "y0": float(y0), "cell": cell, "nx": nx, "ny": ny, "z": zlist,
            "n_cells_observed": int(uniq.size), "n_cells_extrapolated": int(outside.sum()),
            "note": (f"interpolated from ICESat-2 tracks ({uniq.size} of {nx * ny} cells observed, {cell:.0f} m grid; "
                     f"{int(outside.sum())} cells outside the track hull nearest-filled); depth cue only")}


def bbox_extent(frame: dict) -> tuple[float, float, float, float]:
    """Local-metre extent of the bbox polygon (its four corners, since the projection is not axis-aligned)."""
    w, s, e, n = frame["bbox"]
    xs, ys = to_local(frame, np.array([w, e, e, w]), np.array([s, s, n, n]))
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def add_imagery(doc: dict, width_px: int = 4096) -> dict:
    """Fetch/warp the imagery base layer for the scene's bbox (network); records the file path and extent."""
    from . import imagery

    meta = imagery.build(doc["frame"], bbox_extent(doc["frame"]), width_px)
    doc["imagery"] = {**meta, "url": f"/api/scene/{doc['scene_id']}/imagery.jpg"}
    return doc


def to_local(frame: dict, lon, lat) -> tuple[np.ndarray, np.ndarray]:
    x, y = _tr(frame["crs"]).transform(np.asarray(lon), np.asarray(lat))
    ox, oy = frame["origin_xy"]
    return np.asarray(x) - ox, np.asarray(y) - oy


def series(frame: dict, mission: str, arrays: dict, meta: dict, z0: float, cache_key: str, max_points: int = 300_000) -> dict:
    x, y = to_local(frame, arrays["lon"], arrays["lat"])
    z = np.asarray(arrays["h"], dtype="f8") - z0
    n = x.size
    stride = max(1, int(np.ceil(n / max_points)))
    sel = slice(None, None, stride)
    pos = np.column_stack([x[sel], y[sel], z[sel]]).astype("f4")
    return {
        "mission": mission,
        "color": COLORS[mission],
        "n": int(pos.shape[0]),
        "n_extracted": int(n),
        "stride": int(stride),
        "cache_key": cache_key,
        "positions": np.round(pos, 3).ravel().tolist(),  # flat [x,y,z,...] to keep JSON compact
        "meta": {k: v for k, v in meta.items() if k != "granules"},
        "granules": meta.get("granules", []),
    }


def new_scene(scene_id: str, bbox, question: str | None = None, polygon=None) -> dict:
    return {"scene_id": scene_id, "question": question, "frame": local_frame(bbox), "bbox": list(bbox), "polygon": polygon,
            "z0": None, "series": {}, "coreg": None,
            "labels": {"note": "Native coordinates as delivered; no co-registration applied."}}


GLAS_OUTLIER_M = 50.0     # a shot this far from the median of its neighbours is a cloud/atmosphere return
GLAS_NEIGHBOR_M = 400.0   # neighbourhood radius: ~2 shots along-track plus repeat-track shots from other campaigns
GLAS_MIN_NEIGHBORS = 6


def drop_glas_outliers(arrays: dict, meta: dict, frame: dict) -> tuple[dict, dict]:
    """Remove GLAS shots whose height differs from the median of their spatial neighbours (mostly other campaigns
    on the repeat track) by more than GLAS_OUTLIER_M. Shots with too few neighbours are kept: no judgement possible."""
    from scipy.spatial import cKDTree

    gx, gy = to_local(frame, arrays["lon"], arrays["lat"])
    h = np.asarray(arrays["h"], dtype="f8")
    tree = cKDTree(np.column_stack([gx, gy]))
    keep = np.ones(h.size, dtype=bool)
    n_judged = 0
    for i, lst in enumerate(tree.query_ball_point(np.column_stack([gx, gy]), r=GLAS_NEIGHBOR_M)):
        if len(lst) > GLAS_MIN_NEIGHBORS:
            n_judged += 1
            others = [j for j in lst if j != i]
            if abs(h[i] - np.median(h[others])) > GLAS_OUTLIER_M:
                keep[i] = False
    meta = dict(meta, n_outliers_dropped=int((~keep).sum()), n_outlier_judged=int(n_judged),
                outlier_rule=f"|h - median(h of neighbours within {GLAS_NEIGHBOR_M:.0f} m)| > {GLAS_OUTLIER_M:.0f} m, "
                             f"needs > {GLAS_MIN_NEIGHBORS} neighbours")
    return {k: v[keep] for k, v in arrays.items()}, meta


def add_series(doc: dict, mission: str, arrays: dict, meta: dict, cache_key: str) -> dict:
    if doc["z0"] is None:
        doc["z0"] = float(np.median(arrays["h"]))
    if mission == "ICESAT2":
        doc["surface_photon"] = surface_grid(doc["frame"], arrays, doc["z0"])
        doc["surface"] = doc["surface_photon"]
        try:
            from . import dem
            d = dem.surface_for_frame(doc["frame"], bbox_extent(doc["frame"]), doc["z0"])
            if d is not None:
                doc["surface"] = d
        except Exception as e:  # DEM is a base layer, never a blocker
            import logging
            logging.getLogger(__name__).warning("DEM unavailable, using photon-interpolated surface: %s", e)
    if mission == "GLAS":
        arrays, meta = drop_glas_outliers(arrays, meta, doc["frame"])
        cache.save(cache_key + "-clean", arrays, meta)
        cache_key = cache_key + "-clean"
    doc["series"][mission] = series(doc["frame"], mission, arrays, meta, doc["z0"], cache_key)
    return doc
