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


def granule_name(g) -> str:
    """Canonical granule identity: the .h5 filename from the data link, not the CMR native-id (which is sometimes a
    concept-id like 'SC:ATL03.007:NNN' for revision duplicates). Falls back to the native-id."""
    try:
        links = g.data_links()
        if links:
            base = links[0].rsplit("/", 1)[-1]
            if base.endswith(".h5") or base.endswith(".H5"):
                return base
    except Exception:
        pass
    return g["meta"]["native-id"]


def dedup_granules(granules: list) -> list:
    """Keep one entry per .h5 file (drops CMR revision duplicates), preserving order."""
    seen, out = set(), []
    for g in granules:
        n = granule_name(g)
        if n not in seen:
            seen.add(n); out.append(g)
    return out


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
    granules = dedup_granules(earthaccess.search_data(count=-1, **kw))
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
                    "granules": [granule_name(g) for g in a][:50]}

    gl = search(GLAS_SHORT_NAME, GLAS_VERSION, bbox, glas_window)
    by_campaign = Counter()
    for g in gl:
        t = _granule_start(g)
        by_campaign[campaign_for(t.date()) if t else "?"] += 1
    out["GLAH06"] = {"version": GLAS_VERSION, "window": list(glas_window), "n_granules": len(gl),
                     "by_campaign": dict(by_campaign),
                     "granules": [granule_name(g) for g in gl][:50]}
    out["both_present"] = bool(a) and bool(gl)
    return out
