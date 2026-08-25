"""Build the JSON document the deck.gl widget consumes.

Local frame: EPSG:3413 (NSIDC polar stereographic north) metres, minus the bbox centre; z = height
minus the ICESat-2 median height. All positions are given in that frame; the widget applies vertical
exaggeration and, in Slice 3, horizontal offset exaggeration, itself (and labels both).
"""
from __future__ import annotations

import numpy as np
from pyproj import Transformer

from . import cache

_to_ps = Transformer.from_crs("EPSG:4326", "EPSG:3413", always_xy=True)

COLORS = {"ICESAT2": [55, 138, 221], "GLAS": [216, 90, 48]}


def local_frame(bbox) -> dict:
    w, s, e, n = bbox
    cx, cy = _to_ps.transform((w + e) / 2, (s + n) / 2)
    return {"crs": "EPSG:3413", "origin_xy": [float(cx), float(cy)], "bbox": list(bbox)}


def to_local(frame: dict, lon, lat) -> tuple[np.ndarray, np.ndarray]:
    x, y = _to_ps.transform(np.asarray(lon), np.asarray(lat))
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


def new_scene(scene_id: str, bbox, question: str | None = None) -> dict:
    return {"scene_id": scene_id, "question": question, "frame": local_frame(bbox), "bbox": list(bbox),
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
    if mission == "GLAS":
        arrays, meta = drop_glas_outliers(arrays, meta, doc["frame"])
        cache.save(cache_key + "-clean", arrays, meta)
        cache_key = cache_key + "-clean"
    doc["series"][mission] = series(doc["frame"], mission, arrays, meta, doc["z0"], cache_key)
    return doc
