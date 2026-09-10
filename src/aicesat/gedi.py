"""GEDI L2A (GEDI02_A v003) footprint extraction over a bbox.

Full-waveform lidar on the ISS: 25 m footprints, 8 beams (4 full-power, 4 coverage), global to +-51.6 deg.
`elev_lowestmode` is height above the WGS84 ellipsoid (the granule's own attribute says so), so it is directly
comparable to ATL06/ATL03 and to the ellipsoid-corrected GLAS with no datum conversion.

Quality: `l2a_quality_flag_rel3 == 1` (V003; `quality_flag` on V002) AND `degrade_flag == 0`. Over Langtang that
keeps 41% of shots.

SLOPE CAVEAT, measured against the HMA 8 m DEM over this terrain, not inherited from the literature: MAD by DEM
slope ran 2.7 m (0-15 deg), 5.2, 6.6, 7.3, then 14.7 m above 50 deg, with the median residual sliding -4.0 -> -12.4 m,
while ATL06 held 2.9-4.5 m across the same ground. `elev_lowestmode` is the LOWEST return in a 25 m footprint, which
on a steep face is its downhill edge. Under ~15 degrees GEDI is the better instrument; over 30 it should not carry
an elevation-change argument.
"""
from __future__ import annotations

import logging

import numpy as np

from . import cache, coverage

log = logging.getLogger(__name__)

GEDI_VERSION = "003"


def _index_covers(bbox, polygon=None) -> bool:
    from . import index_gedi
    return coverage.index_covers_area(index_gedi._index_dir(index_gedi.GEDI_RES), bbox, polygon)


def extract(bbox, window, polygon=None, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    """Index-only: byte-range the indexed spans the area's H3 cells point at. The index is a PRECONDITION, not an
    optimisation -- no CMR search and no whole-granule download to fall back to."""
    from . import index_gedi

    k = cache.key("gedi", GEDI_VERSION, bbox, window, polygon)
    hit = cache.load(k)
    if hit:
        log.info("gedi cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    if not _index_covers(bbox, polygon):
        from . import coverage as _cov, index_gedi as _ix
        why = _cov.coverage_gap(_ix._index_dir(_ix.GEDI_RES), bbox, polygon) or "the coverage gate refused it"
        raise RuntimeError(f"GEDI not usable over {tuple(round(float(v), 4) for v in bbox)}: {why}. "
                           f"Build with: uv run scripts/build_gedi_index.py <W> <S> <E> <N> 5 8")
    arr, st = index_gedi.fetch_bbox(bbox, window=window, res=index_gedi.GEDI_RES, polygon=polygon,
                                    clip_cells=True, on_granule=on_granule, on_plan=on_plan)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no usable GEDI footprints over {bbox} in {window}")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    meta = {"mission": "GEDI", "product": f"GEDI02_A v{GEDI_VERSION}", "bbox": list(bbox), "window": list(window),
            "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (elev_lowestmode native WGS84 ellipsoid)",
            "quality_filter": "l2a_quality_flag_rel3 == 1 and degrade_flag == 0",
            "beams": "all 8 (4 full-power + 4 coverage)", "footprint_m": 25,
            "slope_caveat": "MAD 2.7 m under 15 deg slope but 14.7 m over 50 deg (measured vs HMA 8 m); "
                            "elev_lowestmode is the lowest return in the footprint",
            "n": int(arrays["lon"].size), "source": "sub-granule H3 index (byte-range)", "access": st,
            "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("gedi via index: %d footprints, %d GETs, %.1f MB", arrays["lon"].size, st.get("requests", 0),
             st.get("bytes", 0) / 1e6)
    return arrays, meta
