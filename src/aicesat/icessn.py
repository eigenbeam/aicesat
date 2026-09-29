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
# "# International Terrestrial Reference Frame: ITRF05". Case varies across campaigns (2019 granules say
# "itrf14"), and the year is written two-digit. (Ben Smith, PR #18.)
_ITRF_RE = re.compile(r"International\s+Terrestrial\s+Reference\s+Frame\s*:\s*ITRF\s*(\d{2,4})", re.I)
ITRF_UNKNOWN = 0           # itrf_year sentinel when the header carries no frame line


def _itrf_year_from_lines(lines) -> int:
    """The ITRF realization year named in the leading '#' header block, or ITRF_UNKNOWN."""
    for line in lines:
        if not line.startswith("#"):
            break                                   # header is the leading '#' block only
        m = _ITRF_RE.search(line)
        if m:
            y = int(m.group(1))
            if y < 100:                             # two-digit: ITRF97 -> 1997, ITRF05 -> 2005
                y += 1900 if y >= 80 else 2000
            return y
    return ITRF_UNKNOWN


def _itrf_year(path: str) -> int:
    """The granule's ITRF realization year from its header, or ITRF_UNKNOWN.

    The frame is campaign-dependent and really does change mid-record: over the EGIG box, ILATM2 reports ITRF05
    for 2011, ITRF08 for 2012-2016 and ITRF14 from 2017 — so this must be read per granule, not assumed.
    """
    try:
        with open(path, errors="replace") as fh:
            return _itrf_year_from_lines(fh)
    except OSError as ex:
        log.debug("%s: could not read header for ITRF: %s", path, ex)
    return ITRF_UNKNOWN


def itrf_year_from_bytes(data: bytes) -> int:
    """_itrf_year for the file's bytes, as the index builder holds them (a whole-file GET)."""
    return _itrf_year_from_lines(data.decode("utf-8", "replace").splitlines())


def _frame_name(year: int) -> str:
    return f"ITRF{year}" if year else "ITRF (unknown; granule header carried no frame)"


def _native_frame(itrf_years) -> tuple[str, list[int]]:
    """(native_frame label, the realizations present). One realization is named exactly, so coreg can transform it;
    several are labelled "mixed" -- deliberately not a name the frame step accepts -- and the per-row itrf_year
    carries them, which timeseries propagates realization by realization."""
    ys = sorted({int(v) for v in np.asarray(itrf_years).ravel()})
    if len(ys) == 1:
        return _frame_name(ys[0]), ys
    return "ITRF (mixed: " + ", ".join(_frame_name(y) for y in ys) + "; see itrf_year per row)", ys


def _index_covers(bbox, polygon=None) -> bool:
    """True if every H3 cell the selection touches is in the ICESSN line-offset index's built cell set.

    `polygon` matters: a drawn shape's bounding box touches cells the shape itself never enters, so testing
    the box refused areas whose own cells are all indexed."""
    from . import index_icessn
    return coverage.index_covers_area(index_icessn._index_dir(index_icessn.ICESSN_RES), bbox, polygon)


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
    # Per-row ITRF realization. Cells cached before it was indexed read back NaN, which is "unknown" (0): reported
    # as unpropagated by the time series rather than guessed.
    itrf = np.asarray(arr.get("itrf_year", np.zeros(arrays["lon"].size)), "f8")
    arrays["itrf_year"] = np.where(np.isfinite(itrf), itrf, ITRF_UNKNOWN).astype("i2")
    native_frame, frame_years = _native_frame(arrays["itrf_year"])
    years = np.unique(arrays["t"].astype("datetime64[Y]")).astype(str).tolist()
    meta = {"mission": "ICESSN", "product": f"ILATM2 v{coverage.ICESSN_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": native_frame, "native_frame_years": frame_years,
            "native_frame_source": "granule header 'International Terrestrial Reference Frame'",
            "height_ref": "WGS84 ellipsoid",
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
    from .index_icessn import ICESSN_INDEX_VERSION
    k = cache.key("icessn", coverage.ICESSN_VERSION, bbox, window, MAX_RMS_CM, polygon, ICESSN_INDEX_VERSION)
    hit = cache.load(k)
    if hit:
        log.info("icessn cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    if not _index_covers(bbox, polygon):
        from . import coverage as _cov, index_icessn as _ix
        why = _cov.coverage_gap(_ix._index_dir(_ix.ICESSN_RES), bbox, polygon) or "the coverage gate refused it"
        raise RuntimeError(f"ICESSN not usable over {tuple(round(float(v), 4) for v in bbox)}: {why}. "
                           f"Build with: uv run scripts/build_icessn_index.py <W> <S> <E> <N> 5 8")
    return _extract_via_index(bbox, window, polygon, k, on_granule=on_granule, on_plan=on_plan)
