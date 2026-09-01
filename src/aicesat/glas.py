"""ICESat/GLAS GLAH06 (release 34) 40 Hz shot extraction over a bbox.

Native frame ITRF2008 (NSIDC, EPSG:5332). Heights are delivered on the TOPEX/Poseidon ellipsoid;
we convert to WGS84 ellipsoid heights with the product's own d_deltaEllip (T/P minus WGS84, ~0.70 m)
and apply the saturation correction d_satElevCorr, which is NOT applied in d_elev.
Row identity: (granule, i_rec_ndx, i_shot_count).
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np

from . import cache, coverage
from .campaigns import campaign_for

log = logging.getLogger(__name__)

MAX_SAT_FLAG = 2  # 0 none, 1 minor, 2 corrected; >=3 not correctable / unusable


def _index_covers(bbox, polygon=None) -> bool:
    """True if every H3 cell the selection touches is in the GLAS sub-granule index's built cell set.

    `polygon` matters: a drawn shape's bounding box touches cells the shape itself never enters, so testing
    the box refused areas whose own cells are all indexed."""
    from . import index_glas
    return coverage.index_covers_area(index_glas._index_dir(index_glas.GLAS_RES), bbox, polygon)


def _extract_via_index(bbox, window, polygon, k, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    from . import index_glas
    # clip_cells: build from the H3 cells the selection actually touches (a box gains a hex-aligned fringe; a polygon no
    # longer expands to its bounding rectangle). The precise points_in_polygon below still trims a polygon to its exact
    # shape; the win is that only the touched hexes are addressed/materialized, not the whole bbox rectangle.
    # on_granule (opt-in): threaded through for per-granule progressive streaming on a cache-miss build.
    arr, st = index_glas.fetch_bbox(bbox, window=window, res=index_glas.GLAS_RES, polygon=polygon, clip_cells=True,
                                    on_granule=on_granule, on_plan=on_plan)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no usable GLAS shots over {bbox} in {window} (index)")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    days = arrays["t"].astype("datetime64[D]")
    camp: dict = {}
    for d0 in np.unique(days):
        c = campaign_for(date.fromisoformat(str(d0)))
        camp[c] = camp.get(c, 0) + int((days == d0).sum())
    meta = {"mission": "GLAS", "product": f"GLAH06 v{coverage.GLAS_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2008", "height_ref": "WGS84 ellipsoid (converted)",
            "ellipsoid_correction": "h = d_elev + d_satElevCorr - d_deltaEllip (TOPEX/Poseidon -> WGS84)",
            "n": int(arrays["lon"].size), "campaigns": dict(sorted(camp.items())), "max_sat_flag": MAX_SAT_FLAG,
            "source": "sub-granule H3 index (byte-range)", "access": st, "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("glas via index: %d shots, %d GETs, %.1f MB", arrays["lon"].size, st.get("requests", 0), st.get("bytes", 0) / 1e6)
    return arrays, meta


def extract(bbox, window, polygon=None, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    """Index-only: byte-range the indexed chunks the area's H3 cells point at. The index is a PRECONDITION, not an
    optimisation — there is no CMR search and no whole-granule download to fall back to. Discovery is paid once, at
    index build time (scripts/build_glas_index.py)."""
    k = cache.key("glas", coverage.GLAS_VERSION, bbox, window, MAX_SAT_FLAG, polygon)
    hit = cache.load(k)
    if hit:
        log.info("glas cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    if not _index_covers(bbox, polygon):
        raise RuntimeError(f"GLAS not indexed over {bbox} — build the sub-granule index first "
                           f"(uv run scripts/build_glas_index.py)")
    return _extract_via_index(bbox, window, polygon, k, on_granule=on_granule, on_plan=on_plan)
