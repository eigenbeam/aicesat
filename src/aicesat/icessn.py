"""Operation IceBridge ATM L2 icessn (ILATM2 v2) along-track surface elevation over a bbox.

Airborne laser altimetry that fills the ICESat -> ICESat-2 gap (2009-2019). Each granule is a small CSV of
along-track "platelets"; we keep the **nadir** platelet (`track == 0`) for a clean single-line profile. `elevation`
is height above the **WGS84 ellipsoid** (m), directly comparable to ICESat-2/GLAS — no datum conversion. Longitude is
delivered 0..360 E (normalized to -180..180). Time = the filename's UTC date + the record's seconds-of-day.

Format: NSIDC ILATM2 v2, DOI 10.5067/CPRXXK3F39RV; 11 comma-delimited columns, `#` header lines:
  seconds, lat(+N/-S), lon(0..360E), elev(WGS84 m), SN_slope, WE_slope, RMS(cm), npt_used, npt_edit, distance, track.
Parser cross-checked against tsutterley/read-ATM2-icessn. Row identity: (granule, along-track index).
"""
from __future__ import annotations

import logging
import re

import numpy as np

from . import cache, coverage

log = logging.getLogger(__name__)

MAX_RMS_CM = 50.0          # platelets whose plane-fit RMS exceeds 0.5 m are rough/unreliable -> drop
_NAME_RE = re.compile(r"(?:ILATM2|BLATM2)_(\d{8})_(\d{6})")


def _index_covers(bbox) -> bool:
    """True if the ICESSN line-offset index was built over a region that contains this bbox."""
    from . import index_icessn
    return coverage._index_covers_bbox(index_icessn._index_dir(index_icessn.ICESSN_RES), bbox, index_icessn.ICESSN_RES)


def _extract_via_index(bbox, window, polygon, k, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    from . import index_icessn
    # clip_cells: build from the H3 cells the selection actually touches (see glas._extract_via_index for the rationale).
    # on_granule (opt-in): threaded through for per-granule progressive streaming on a cache-miss build.
    arr, st = index_icessn.fetch_bbox(bbox, window=window, res=index_icessn.ICESSN_RES, polygon=polygon, clip_cells=True,
                                      on_granule=on_granule, on_plan=on_plan)
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arr["lon"], arr["lat"], polygon)
        arr = {kk: v[keep] for kk, v in arr.items()}
    if not arr["h"].size:
        raise RuntimeError(f"no usable ICESSN platelets over {bbox} in {window} (index)")
    arrays = {"lon": arr["lon"], "lat": arr["lat"], "h": arr["h"], "t": arr["t"]}
    if "sn_slope" in arr and "we_slope" in arr:   # platelet plane-fit slopes -> tilted-facet rendering (may be NaN for pre-slope cached cells)
        arrays["sn_slope"] = arr["sn_slope"]; arrays["we_slope"] = arr["we_slope"]
    years = np.unique(arrays["t"].astype("datetime64[Y]")).astype(str).tolist()
    meta = {"mission": "ICESSN", "product": f"ILATM2 v{coverage.ICESSN_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF (campaign-dependent)", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (icessn elevation native WGS84 ellipsoid)",
            "quality_filter": f"track==0 (nadir), plane-fit RMS < {MAX_RMS_CM:.0f} cm", "years": years,
            "n": int(arrays["lon"].size), "source": "sub-granule H3 index (byte-range)", "access": st,
            "polygon": polygon, "cache_key": k}
    cache.save(k, arrays, meta)
    log.info("icessn via index: %d platelets, %d GETs, %.2f MB", arrays["lon"].size, st.get("requests", 0), st.get("bytes", 0) / 1e6)
    return arrays, meta


def extract(bbox, window, polygon=None, on_granule=None, on_plan=None) -> tuple[dict[str, np.ndarray], dict]:
    """Index-only: byte-range the indexed line spans the area's H3 cells point at. The index is a PRECONDITION, not
    an optimisation — no CMR search, no whole-file download. See scripts/build_icessn_index.py."""
    k = cache.key("icessn", coverage.ICESSN_VERSION, bbox, window, MAX_RMS_CM, polygon)
    hit = cache.load(k)
    if hit:
        log.info("icessn cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    if not _index_covers(bbox):
        raise RuntimeError(f"ICESSN not indexed over {bbox} — build the line-offset index first "
                           f"(uv run scripts/build_icessn_index.py)")
    return _extract_via_index(bbox, window, polygon, k, on_granule=on_granule, on_plan=on_plan)
