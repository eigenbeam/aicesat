"""Build the JSON document the deck.gl widget consumes.

Local frame: EPSG:3413 (NSIDC polar stereographic north) metres, minus the bbox centre; z = height
minus the ICESat-2 median height. All positions are given in that frame; the widget applies vertical
exaggeration and, in Slice 3, horizontal offset exaggeration, itself (and labels both).
"""
from __future__ import annotations

import logging

import numpy as np
from pyproj import Transformer

from . import cache

from functools import lru_cache

log = logging.getLogger(__name__)


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
COLORS = {"ICESAT2": [40, 200, 120], "GLAS": [240, 228, 66], "ATL06": [40, 140, 225], "ICESSN": [230, 75, 60],
          "GEDI": [200, 130, 235],   # violet: the remaining Okabe-Ito-adjacent hue, distinct from all four
          "GPSTRUTH": [230, 159, 0]}  # Okabe-Ito orange: clear of GLAS yellow, which the traverse overlaps in time


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


# The addressing resolution the point collections are read at. ATL06/GLAS/ICESSN all use res 5 and ATL03 res 6, so
# the res-5 hull is the widest ground any enabled collection can return — which makes it the scene's real footprint.
DATA_RES = 5


def data_extent(frame: dict, polygon=None, res: int = DATA_RES) -> tuple[float, float, float, float]:
    """Local-metre extent of the H3 cells a query over this frame's bbox will actually READ.

    The bbox is what the user drew; the points that come back cover WHOLE res-`res` cells, because the lake is
    addressed by cell and `clip_cells` keeps cell membership rather than the rectangle. On a scene-sized box that
    is 5.4x the drawn area, and only 28% of the returned points fell inside the box — so drawing the DEM and
    imagery over the bbox alone left three-quarters of the point cloud hanging past the edge of its own terrain.

    Falls back to the bbox extent if the cell fill fails, which is the old behaviour and never worse than nothing.
    """
    try:
        from . import planner

        cells = planner.cells_for_bbox(frame["bbox"], res=res, polygon=polygon)
        if not cells:
            return bbox_extent(frame)
        import h3

        lats, lons = [], []
        for c in cells:
            for la, lo in h3.cell_to_boundary(c if isinstance(c, str) else h3.int_to_str(int(c))):
                lats.append(la); lons.append(lo)
        xs, ys = to_local(frame, np.asarray(lons), np.asarray(lats))
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())
    except Exception as e:
        log.warning("data_extent failed (%s: %s); falling back to the bbox extent", type(e).__name__, e)
        return bbox_extent(frame)


def add_imagery(doc: dict, width_px: int = 4096, source: str | None = None) -> dict:
    """Fetch/warp the imagery base layer for the scene's bbox (network); records the file path and extent.
    `source` (from the UI selector) overrides the AICESAT_IMAGERY default; None uses the env/default."""
    from . import imagery

    meta = imagery.build(doc["frame"], data_extent(doc["frame"], doc.get("polygon")), width_px, source=source)
    doc["imagery"] = {**meta, "url": f"/api/scene/{doc['scene_id']}/imagery.jpg"}
    return doc


def to_local(frame: dict, lon, lat) -> tuple[np.ndarray, np.ndarray]:
    x, y = _tr(frame["crs"]).transform(np.asarray(lon), np.asarray(lat))
    ox, oy = frame["origin_xy"]
    return np.asarray(x) - ox, np.asarray(y) - oy


def series(frame: dict, mission: str, arrays: dict, meta: dict, z0: float, cache_key: str, scene_id: str) -> dict:
    x, y = to_local(frame, arrays["lon"], arrays["lat"])
    z = np.asarray(arrays["h"], dtype="f8") - z0
    n = x.size
    # Natural order. The shuffle here existed only so that a client taking a PREFIX of the array got a fair spatial
    # sample; the push transport delivers every point, so there is no prefix and nothing to make fair.
    pos = np.column_stack([x, y, z]).astype("f4")
    out = {
        "mission": mission,
        "color": COLORS[mission],
        "n": int(pos.shape[0]),
        "n_extracted": int(n),
        "cache_key": cache_key,
        "meta": {k: v for k, v in meta.items() if k != "granules"},
        "granules": meta.get("granules", []),
    }
    # Platelet orientation (ICESSN): each nadir platelet is a plane fit with a South->North and West->East slope
    # (dz/dnorth, dz/deast; metres per metre). Carry them in lock-step with positions, as a flat [sn,we,...]
    # array so the widget can render each platelet as a facet tilted to its own fitted plane. NaN (pre-slope cached
    # cells) is passed through — the widget treats it as a flat facet. Absent for missions without a plane fit.
    # Bulk arrays go to the binary sidecar, never into the doc: see cache.scene_array_* for why.
    cache.scene_array_write(scene_id, mission, "positions", np.round(pos, 3).ravel())
    if "sn_slope" in arrays and "we_slope" in arrays:
        sn = np.asarray(arrays["sn_slope"], "f8")
        we = np.asarray(arrays["we_slope"], "f8")
        slopes = np.column_stack([sn, we]).astype("f4")
        out["has_slopes"] = True
        cache.scene_array_write(scene_id, mission, "slopes",
                                np.where(np.isfinite(slopes), np.round(slopes, 5), 0.0).ravel())   # NaN -> 0 = flat facet
    return out


def new_scene(scene_id: str, bbox, question: str | None = None, polygon=None, markers=None) -> dict:
    """`markers` are named coordinates to point at in the rendered scene — [{lon, lat, label}]. The widget draws a
    labelled stick at each, which is how you find a specific feature in a frame whose axes are local metres."""
    return {"scene_id": scene_id, "question": question, "frame": local_frame(bbox), "bbox": list(bbox), "polygon": polygon,
            "z0": None, "series": {}, "coreg": None, "markers": normalize_markers(markers),
            "labels": {"note": "Native coordinates as delivered; no co-registration applied."}}


def normalize_markers(markers) -> list[dict]:
    """Validate [{lon, lat, label}] into the widget's shape. A bad marker is DROPPED, never guessed at: a pin in the
    wrong place is worse than no pin, because the viewer reads it as a location."""
    out = []
    for m in (markers or []):
        try:
            lon, lat = float(m["lon"]), float(m["lat"])
        except (KeyError, TypeError, ValueError):
            log.warning("marker %r has no usable lon/lat; dropped", m)
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            log.warning("marker lon/lat out of range: %s %s; dropped", lon, lat)
            continue
        out.append({"lon": lon, "lat": lat, "label": str(m.get("label") or "")[:60]})
    return out


def set_surface(doc: dict) -> dict:
    """Attach the DEM base surface for the scene's frame (independent of which missions are loaded). Needs z0, so
    call it after at least one series has been added. No photon-interpolated fallback: no DEM -> no surface."""
    doc["surface"] = None
    if doc.get("z0") is None:
        return doc
    try:
        from . import dem
        doc["surface"] = dem.surface_for_frame(doc["frame"], data_extent(doc["frame"], doc.get("polygon")), doc["z0"])
    except Exception as e:  # DEM is a base layer, never a blocker
        import logging
        logging.getLogger(__name__).warning("DEM unavailable, no surface shown: %s", e)
    return doc


# GLAS used to be cleaned here, and is not any more.
#
# The rule was |h - median(h of neighbours within 400 m)| > 50 m. Over Langtang it dropped 1,389 of 3,042 shots
# (45.7%) and 1,174 of those were GOOD: sampled against the HMA 8 m DEM the DROPPED shots sat at a median -0.24 m
# with 87.4% inside 20 m. The cause is geometry, not tuning — GLAS 40 Hz shots are ~172 m apart along-track and the
# repeat tracks are offset by hundreds of metres, so a 400 m neighbourhood is a sparse scatter across a glacial
# valley whose DEM relief there has a MEDIAN of 316 m, six times the threshold.
#
# Nothing replaced it, because every candidate was measured and rejected:
#   * a local PLANE fit (with a line-fit fallback for the collinear single-pass case) only cut the residual MAD from
#     55 m to 31 m, and still lost 1,145 good shots at any threshold that caught the bad ones. No self-referential
#     model can work when the shot spacing exceeds the terrain's correlation length.
#   * GLAH06's own flags do not discriminate: sat_corr_flg, d_pctSAT and sigma_att_flg each catch 0 of 87 gross
#     errors, elv_cloud_flg is set on 99.9% of shots, and the built-in d_DEM_elv is a 1 km surface (MAD 97 m).
#   * an EXTERNAL DEM gate does work (|h - HMA| is bimodal with a ~600 m gap, so 150 m drops 101 shots instead of
#     1,389) but it makes the displayed series a function of the DEM. Anyone then reading GLAS-vs-DEM as a result is
#     reading a number the gate helped produce, and no other mission is cleaned this way.
#
# So GLAS ships as delivered, with only the product's own quality flags applied (elev_use_flg == 0, saturation flag
# <= 2, saturation correction added — see index_glas). About 3% of shots are gross cloud returns and they are drawn:
# they are real ICESat-1 measurements, labelled as such. The display absorbs them because scene.js frames on the DEM
# surface extent, not the point bounds, and colours points flat per mission with no height ramp.
def add_series(doc: dict, mission: str, arrays: dict, meta: dict, cache_key: str) -> dict:
    if doc["z0"] is None:
        doc["z0"] = float(np.median(arrays["h"]))
    doc["series"][mission] = series(doc["frame"], mission, arrays, meta, doc["z0"], cache_key, doc["scene_id"])
    return doc


def append_partial(doc: dict, mission: str, arrays: dict) -> dict:
    """Progressive-build helper: bake ONE streamed granule's partial points into `mission`'s series and APPEND them to
    its position sidecar, so the widget's poll paints a growing cloud during a cache-miss build. Baking mirrors
    series() exactly (to_local + h - z0, f4, round to mm, flat [x,y,z,...]).

    Finalize still REPLACES this buffer via add_series: the streamed points are baked per granule as they land, and
    the authoritative array is written once, in one piece. No mission is filtered at finalize any more (GLAS was, and
    is not — see above add_series), so the points now match; the replacement itself is still what the stream's
    `reset` control frame exists to announce, because os.replace hands the path a new inode either way.
    Requires doc['z0'] (baking is height-relative); the caller buffers partials until z0 is known and never invents
    one here. If the mission's series does not exist yet, a minimal one is created with the
    same shape add_series produces so the client renders it immediately.

    Appending, rather than growing a list inside the doc, is what keeps a streaming build linear in granules: the doc
    used to carry every point as JSON and cache.save_scene re-serialised all of it on every granule that landed.
    """
    if doc.get("z0") is None:
        return doc                                  # caller buffers until the DEM/first collection sets z0
    sid = doc["scene_id"]
    lon = np.asarray(arrays["lon"], dtype="f8")
    if lon.size == 0 and mission not in doc["series"]:
        return doc                                  # nothing to paint yet -> don't materialise an empty series
    s = doc["series"].get(mission)
    if s is None:                                   # minimal series consistent with series() so the client can paint it
        s = {"mission": mission, "color": COLORS[mission], "n": 0, "n_extracted": 0,
             "cache_key": None, "meta": {"partial": True}, "granules": []}
        doc["series"][mission] = s
    if lon.size:
        x, y = to_local(doc["frame"], lon, np.asarray(arrays["lat"], dtype="f8"))
        z = np.asarray(arrays["h"], dtype="f8") - doc["z0"]
        pos = np.round(np.column_stack([x, y, z]).astype("f4"), 3).ravel()
        n_vals = cache.scene_array_append(sid, mission, "positions", pos)
        # No cap and no thinning. Both existed because the client fetched a PREFIX of this buffer, and a prefix of
        # append-ordered data is the earliest granules rather than a sample of all of them. The stream delivers every
        # point as it lands, so the preview can simply grow.
        s["n"] = n_vals // 3
        s["n_extracted"] = s["n"]
    return doc
