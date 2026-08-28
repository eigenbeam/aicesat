"""ICESat-2 ATL06 (Land Ice Height) v007 extraction over a bbox.

Per-beam land-ice segments (40 m windows posted every 20 m). `h_li` is height above the **WGS84 ellipsoid in
ITRF2014** — the same frame and applied-correction set as ATL03 (solid-earth + pole tides, ocean loading, atmospheric
delay applied; ocean tide + DAC NOT applied), so the ITRF2014 plate-motion co-registration carries over unchanged and
no ellipsoid conversion is needed. Row identity: (granule, beam, segment_id).

**All six beams are read by default** (`strong_only=True` restores the older strong-only behaviour). Over bright
dry snow the weak beams are fully usable — at Summit they return slightly *more* good segments than their strong
partners — and keeping the pair matters for geometry: the two members of a pair sit ~90 m apart across track, so a
pair samples two offset lines rather than one. `beam` indexes `GT_BEAMS` and means the same physical beam in every
granule regardless of `/orbit_info/sc_orient`; `beam_strong` records 1 strong / 0 weak / -1 unknown (the yaw-flip
transition, `sc_orient == 2`, where the side is genuinely ambiguous — such granules are now read rather than
skipped, since the heights are valid even when the strong/weak label is not).

Surface slope comes from the product rather than being re-derived: `fit_statistics/dh_fit_dx` (along-track) and
`dh_fit_dy` (across-track, derived from the strong/weak beam pair), both in rise/run. `ground_track/seg_azimuth`
gives the track heading.

Granules are ~100 MB (full RGT), so we read them remotely with earthaccess.open (fsspec chunk cache) and slice only
the `land_ice_segments` a small bbox touches — far cheaper than downloading whole files. Reading six beams instead
of three roughly doubles that sliced volume.
"""
from __future__ import annotations

import logging
import time

import h5py
import numpy as np

from . import auth, cache, coverage, geom

log = logging.getLogger(__name__)

ATLAS_EPOCH = np.datetime64("2018-01-01T00:00:00", "ms")   # ATLAS SDP epoch; delta_time is seconds after it
FILL = 3.0e38                                              # h_li / lat / lon fill ~ 3.4028235e38
QUALITY_GOOD = 0                                           # atl06_quality_summary == 0 -> best


# Canonical beam order. `beam` indexes into this and is independent of sc_orient, so a beam index means the same
# physical beam in every granule; pair = beam // 2 (the two members of a pair are ~90 m apart across track).
GT_BEAMS = ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]
STRONG_UNKNOWN = -1                         # sc_orient == 2 (yaw-flip transition): side is ambiguous


def _strong_side(sc_orient) -> str | None:
    """Which side of each pair carries the strong beam. None during the yaw-flip transition."""
    v = int(np.asarray(sc_orient).ravel()[0])
    return {0: "l", 1: "r"}.get(v)          # 0 backward, 1 forward, 2 transition


def _strong_beams(sc_orient) -> list[str]:
    side = _strong_side(sc_orient)
    return [b for b in GT_BEAMS if b.endswith(side)] if side else []


def _optional(g, path: str, keep: np.ndarray) -> np.ndarray:
    """A `land_ice_segments` sub-group variable (fit_statistics/…, ground_track/…) as f8, FILL -> NaN.

    Returns all-NaN if the product layout lacks it, so an unexpected granule degrades to "no slope" rather
    than failing the whole read — matching how `_extract_granule` already tolerates a missing beam group.
    """
    n = int(keep.sum())
    try:
        v = g[path][:][keep].astype("f8")
    except (KeyError, TypeError, ValueError):
        log.debug("ATL06: %s unavailable", path)
        return np.full(n, np.nan)
    return np.where(np.abs(v) >= FILL, np.nan, v)


def _extract_granule(f: h5py.File, bbox, strong_only: bool = False) -> dict[str, np.ndarray] | None:
    w, s, e, n = bbox
    if "orbit_info/sc_orient" not in f:
        return None
    side = _strong_side(f["orbit_info/sc_orient"][:])
    if strong_only and side is None:
        return None                         # can't tell which beams are strong during a yaw flip
    beams = [b for b in GT_BEAMS if b.endswith(side)] if strong_only else GT_BEAMS
    parts = []
    for b in beams:
        bi = GT_BEAMS.index(b)
        strong = STRONG_UNKNOWN if side is None else int(b.endswith(side))
        grp = f"{b}/land_ice_segments"
        if grp not in f or "latitude" not in f[grp]:
            continue
        g = f[grp]
        lat = g["latitude"][:]
        lon = g["longitude"][:]
        keep = (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
        if not keep.any():
            continue
        h = g["h_li"][:]
        q = g["atl06_quality_summary"][:]
        keep &= (q == QUALITY_GOOD) & (h < FILL) & (lat < FILL) & (lon < FILL)
        if not keep.any():
            continue
        dt = g["delta_time"][:][keep]
        parts.append({"lon": lon[keep], "lat": lat[keep], "h": h[keep].astype("f8"),
                      "t": ATLAS_EPOCH + (dt * 1000).astype("timedelta64[ms]"),
                      "segment_id": g["segment_id"][:][keep].astype("i8"),
                      "beam": np.full(int(keep.sum()), bi, dtype="i1"),          # index into GT_BEAMS
                      "beam_strong": np.full(int(keep.sum()), strong, dtype="i1"),  # 1 strong, 0 weak, -1 unknown
                      # Surface slope as the product measures it. dh_fit_dy is the across-track component,
                      # derived from the beam pair — so it is available on the strong beam alone and needs no
                      # re-ingest of the weak beam. seg_azimuth carries the track heading (degrees).
                      "dh_fit_dx": _optional(g, "fit_statistics/dh_fit_dx", keep),
                      "dh_fit_dy": _optional(g, "fit_statistics/dh_fit_dy", keep),
                      "seg_azimuth": _optional(g, "ground_track/seg_azimuth", keep)})
    if not parts:
        return None
    return {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}


def extract(bbox, window, max_granules: int = 20, polygon=None,
            strong_only: bool = False) -> tuple[dict[str, np.ndarray], dict]:
    """All six beams by default. `strong_only=True` restores the older strong-beam-only read (half the data)."""
    k = cache.key("atl06", coverage.ATL06_VERSION, bbox, window, max_granules, polygon, strong_only)
    hit = cache.load(k)
    if hit:
        log.info("atl06 cache hit %s", k)
        hit[1]["cache_key"] = k
        return hit
    import earthaccess

    auth.login()
    granules = coverage.search(coverage.ATL06_SHORT_NAME, coverage.ATL06_VERSION, bbox, window)
    if not granules:
        raise RuntimeError(f"no ATL06 granules over {bbox} in {window}")
    n_found = len(granules)
    granules = granules[:max_granules]
    files = earthaccess.open(granules)   # fsspec file objects; h5py reads only the chunks a small bbox touches
    parts, prov = [], []
    for gr, fh in zip(granules, files):
        name = coverage.granule_name(gr)
        t0 = time.time()
        try:
            with h5py.File(fh, "r") as f:
                d = _extract_granule(f, bbox, strong_only=strong_only)
        except Exception as ex:
            log.warning("%s: ATL06 read failed: %s", name, ex)
            continue
        if d is None:
            log.info("%s: no good ATL06 segments in bbox", name)
            continue
        d["granule_idx"] = np.full(d["lon"].size, len(prov), dtype="i2")
        parts.append(d)
        prov.append({"granule": name, "n": int(d["lon"].size), "seconds": round(time.time() - t0, 1)})
        log.info("%s: %d ATL06 segments in bbox (%.1fs)", name, d["lon"].size, time.time() - t0)
    if not parts:
        raise RuntimeError("ATL06 granules found but no good segments in bbox")
    arrays = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    if polygon is not None:
        from .geom import points_in_polygon
        keep = points_in_polygon(arrays["lon"], arrays["lat"], polygon)
        arrays = {key: v[keep] for key, v in arrays.items()}
        if arrays["lon"].size == 0:
            raise RuntimeError("no ATL06 segments inside the polygon")
    meta = {"mission": "ATL06", "product": f"ATL06 v{coverage.ATL06_VERSION}", "bbox": list(bbox),
            "window": list(window), "native_frame": "ITRF2014", "height_ref": "WGS84 ellipsoid",
            "ellipsoid_correction": "none (h_li native WGS84 ellipsoid, ITRF2014; same applied corrections as ATL03)",
            "beams": "strong only (3)" if strong_only else "all 6 (3 pairs, strong + weak)",
            "n_by_beam": {GT_BEAMS[i]: int((arrays["beam"] == i).sum()) for i in sorted(set(arrays["beam"].tolist()))},
            "slope_source": "ATL06 fit_statistics/dh_fit_dx + dh_fit_dy (across-track component is beam-pair derived)",
            "slope_deg_median": geom.slope_deg_median(arrays["dh_fit_dx"], arrays["dh_fit_dy"]),
            "quality_filter": "atl06_quality_summary == 0", "n": int(arrays["lon"].size),
            "n_granules_found": n_found, "n_granules_read": len(granules), "granules": prov, "polygon": polygon}
    meta["cache_key"] = k
    cache.save(k, arrays, meta)
    return arrays, meta
