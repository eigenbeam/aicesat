"""Granule-level coverage counts (CMR search only, no data fetch)."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from . import auth, regions
from .campaigns import campaign_for

log = logging.getLogger(__name__)

ATL03_SHORT_NAME, ATL03_VERSION = "ATL03", "007"
GLAS_SHORT_NAME, GLAS_VERSION = "GLAH06", "034"


def _granule_start(g) -> datetime | None:
    try:
        rng = g["umm"]["TemporalExtent"]["RangeDateTime"]
        return datetime.fromisoformat(rng["BeginningDateTime"].replace("Z", "+00:00"))
    except Exception:
        return None


CMR_CACHE_TTL_S = 24 * 3600  # granule lists for a (product, bbox, window) change only when NSIDC reprocesses


def search(short_name: str, version: str, bbox, window, use_cache: bool = True):
    """CMR granule search, cached on disk for CMR_CACHE_TTL_S: the search is ~1 s per call and every warm query paid it."""
    import pickle
    import time

    import earthaccess

    from . import cache

    key = cache.key("cmr", short_name, version, [round(float(v), 6) for v in bbox], list(window) if window else None)
    path = cache.CACHE_DIR / f"cmr_{key}.pkl"
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CMR_CACHE_TTL_S:
        try:
            granules = pickle.loads(path.read_bytes())
            log.info("%s v%s: %d granules (CMR cache)", short_name, version, len(granules))
            return granules
        except Exception:
            pass
    auth.login()
    kw = dict(short_name=short_name, version=version, bounding_box=tuple(bbox))
    if window:
        kw["temporal"] = tuple(window)
    granules = earthaccess.search_data(count=-1, **kw)
    log.info("%s v%s: %d granules over %s %s", short_name, version, len(granules), bbox, window)
    try:
        cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(granules))
    except Exception as e:
        log.debug("CMR cache write failed: %s", e)
    return granules


def check_coverage(bbox, atl03_window=None, glas_window=None) -> dict:
    atl03_window = atl03_window or regions.DEFAULT_ATL03_WINDOW
    glas_window = glas_window or regions.DEFAULT_GLAS_WINDOW
    out = {"bbox": list(bbox), "ATL03": {}, "GLAH06": {}}

    a = search(ATL03_SHORT_NAME, ATL03_VERSION, bbox, atl03_window)
    by_month = Counter()
    for g in a:
        t = _granule_start(g)
        by_month[t.strftime("%Y-%m") if t else "?"] += 1
    out["ATL03"] = {"version": ATL03_VERSION, "window": list(atl03_window), "n_granules": len(a),
                    "by_month": dict(sorted(by_month.items())),
                    "granules": [g["meta"]["native-id"] for g in a][:50]}

    gl = search(GLAS_SHORT_NAME, GLAS_VERSION, bbox, glas_window)
    by_campaign = Counter()
    for g in gl:
        t = _granule_start(g)
        by_campaign[campaign_for(t.date()) if t else "?"] += 1
    out["GLAH06"] = {"version": GLAS_VERSION, "window": list(glas_window), "n_granules": len(gl),
                     "by_campaign": dict(by_campaign),
                     "granules": [g["meta"]["native-id"] for g in gl][:50]}
    out["both_present"] = bool(a) and bool(gl)
    return out
