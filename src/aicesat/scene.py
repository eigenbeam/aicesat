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

# Per-mission point-cloud palette (Okabe-Ito subset): distinct, colour-blind-friendly, and high-contrast against the
# grey-blue DEM base surface. The scene widget mirrors these (scene.js MISSION_COLORS) so it can also recolour scenes
# built before this palette existed.
COLORS = {"ICESAT2": [86, 180, 233], "GLAS": [230, 159, 0], "ATL06": [204, 121, 167], "ICESSN": [0, 158, 115]}


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


def bbox_extent(frame: dict) -> tuple[float, float, float, float]:
    """Local-metre extent of the bbox polygon (its four corners, since the projection is not axis-aligned)."""
    w, s, e, n = frame["bbox"]
    xs, ys = to_local(frame, np.array([w, e, e, w]), np.array([s, s, n, n]))
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def add_imagery(doc: dict, width_px: int = 4096, source: str | None = None) -> dict:
    """Fetch/warp the imagery base layer for the scene's bbox (network); records the file path and extent.
    `source` (from the UI selector) overrides the AICESAT_IMAGERY default; None uses the env/default."""
    from . import imagery

    meta = imagery.build(doc["frame"], bbox_extent(doc["frame"]), width_px, source=source)
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


def set_surface(doc: dict) -> dict:
    """Attach the DEM base surface for the scene's frame (independent of which missions are loaded). Needs z0, so
    call it after at least one series has been added. No photon-interpolated fallback: no DEM -> no surface."""
    doc["surface"] = None
    if doc.get("z0") is None:
        return doc
    try:
        from . import dem
        doc["surface"] = dem.surface_for_frame(doc["frame"], bbox_extent(doc["frame"]), doc["z0"])
    except Exception as e:  # DEM is a base layer, never a blocker
        import logging
        logging.getLogger(__name__).warning("DEM unavailable, no surface shown: %s", e)
    return doc


def add_series(doc: dict, mission: str, arrays: dict, meta: dict, cache_key: str) -> dict:
    if doc["z0"] is None:
        doc["z0"] = float(np.median(arrays["h"]))
    if mission == "GLAS":
        arrays, meta = drop_glas_outliers(arrays, meta, doc["frame"])
        cache.save(cache_key + "-clean", arrays, meta)
        cache_key = cache_key + "-clean"
    doc["series"][mission] = series(doc["frame"], mission, arrays, meta, doc["z0"], cache_key)
    return doc
