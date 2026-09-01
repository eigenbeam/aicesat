"""ICESat-2 ATL06 (Land Ice Height) v007 extraction over a bbox.

Per-beam land-ice segments (40 m windows posted every 20 m). `h_li` is height above the **WGS84 ellipsoid in
ITRF2014** — the same frame and applied-correction set as ATL03 (solid-earth + pole tides, ocean loading, atmospheric
delay applied; ocean tide + DAC NOT applied), so the ITRF2014 plate-motion co-registration carries over unchanged and
no ellipsoid conversion is needed. Strong beams are chosen from `/orbit_info/sc_orient`. Row identity:
(granule, beam, segment_id).

Granules are ~100 MB (full RGT), so we read them remotely with earthaccess.open (fsspec chunk cache) and slice the
`land_ice_segments` of the strong beams — far cheaper than downloading whole files.
"""
from __future__ import annotations

import logging

import numpy as np

from . import cache, coverage

log = logging.getLogger(__name__)

def _index_covers(bbox, polygon=None) -> bool:
    """True if every H3 cell the selection touches is in the ATL06 sub-granule index's built cell set.

    `polygon` matters: a drawn shape's bounding box touches cells the shape itself never enters, so testing
    the box refused areas whose own cells are all indexed."""
    from . import index_atl06
    return coverage.index_covers_area(index_atl06._index_dir(index_atl06.ATL06_RES), bbox, index_atl06.ATL06_RES, polygon)


def extract(bbox, window, polygon=None, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    k = cache.key("atl06", coverage.ATL06_VERSION, bbox, window, polygon)
    hit = cache.load(k)
    if hit:
        log.info("atl06 cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    # Index-only: byte-range fetch just the chunks whose H3 cell touches the bbox. The sub-granule index is
    # always built for the area of interest first, so there is no whole-granule fallback.
    if not _index_covers(bbox, polygon):
        raise RuntimeError(f"ATL06 not indexed over {bbox} \u2014 build the sub-granule index first "
                           f"(uv run scripts/build_atl06_index.py)")
    from . import index_atl06
    # All 6 beams (strong + weak). The index already stores every beam; weak beams add coverage/cross-mission
    # coincidence, and atl06_quality_summary==0 (quality_zero) still filters their higher-noise returns.
    # clip_cells: build from the H3 cells the selection actually touches (see glas._extract_via_index for the rationale).
    # on_granule (opt-in): threaded through for per-granule progressive streaming on a cache-miss build.
    arr, st = index_atl06.fetch_bbox(bbox, window=window, res=index_atl06.ATL06_RES, strong_only=False,
                                     polygon=polygon, clip_cells=True, on_granule=on_granule, on_plan=on_plan)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no ATL06 segments over {bbox} in {window}")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    meta = {"mission": "ATL06", "product": f"ATL06 v{coverage.ATL06_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (h_li native WGS84 ellipsoid, ITRF2014)",
            "quality_filter": "atl06_quality_summary == 0", "beams": "all 6 (strong + weak)", "n": int(arrays["lon"].size),
            "source": "sub-granule H3 index (byte-range)", "access": st, "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("atl06 via index: %d segments, %d GETs, %.1f MB", arrays["lon"].size, st.get("requests", 0), st.get("bytes", 0) / 1e6)
    return arrays, meta
