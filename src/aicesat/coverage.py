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
ATL06_SHORT_NAME, ATL06_VERSION = "ATL06", "007"
ICESSN_SHORT_NAME, ICESSN_VERSION = "ILATM2", "2"


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


def collections() -> list[dict]:
    """Canonical list of the altimetry collections the app knows, ordered by science epoch — one source of truth for
    the Explore build options, the coverage check, and the Lake labels. `flag` is the build_scene keyword; `default`
    is whether it is selected by default (ATL03 photons are heavy, so off by default)."""
    # `mission` is the lake partition name (mission=<M>); ATL03 photons live under the historical "ICESAT2".
    return [
        {"key": "GLAS", "mission": "GLAS", "flag": "with_glas", "label": "ICESat / GLAS", "short_name": GLAS_SHORT_NAME, "product": "GLAH06",
         "version": GLAS_VERSION, "epoch": "2003-2009", "window": list(regions.DEFAULT_GLAS_WINDOW), "default": True},
        {"key": "ICESSN", "mission": "ICESSN", "flag": "with_icessn", "label": "IceBridge ATM (ICESSN)", "short_name": ICESSN_SHORT_NAME,
         "product": "ILATM2", "version": ICESSN_VERSION, "epoch": "2009-2019", "window": list(regions.DEFAULT_ICESSN_WINDOW), "default": True},
        {"key": "ATL06", "mission": "ATL06", "flag": "with_atl06", "label": "ICESat-2 land ice", "short_name": ATL06_SHORT_NAME, "product": "ATL06",
         "version": ATL06_VERSION, "epoch": "2018-", "window": list(regions.DEFAULT_ATL06_WINDOW), "default": True},
        {"key": "ATL03", "mission": "ICESAT2", "flag": "with_atl03", "label": "ICESat-2 photons", "short_name": ATL03_SHORT_NAME, "product": "ATL03",
         "version": ATL03_VERSION, "epoch": "2018-", "window": list(regions.DEFAULT_ATL03_WINDOW), "default": False},
    ]


def check_coverage(bbox, **_ignored) -> dict:
    """Granule counts per collection over a bbox (CMR only). Returns {bbox, collections:[{key,label,product,version,
    epoch,window,n_granules,by_month|error}, ...]} — a clear, uniform list across all collections."""
    out = []
    for c in collections():
        row = {k: c[k] for k in ("key", "label", "product", "version", "epoch", "window")}
        try:
            g = search(c["short_name"], c["version"], bbox, tuple(c["window"]))
            by = Counter()
            for gr in g:
                t = _granule_start(gr)
                by[t.strftime("%Y-%m") if t else "?"] += 1
            row.update(n_granules=len(g), by_month=dict(sorted(by.items())))
        except Exception as e:
            row.update(n_granules=None, error=str(e)[:140])
        out.append(row)
    return {"bbox": list(bbox), "collections": out}
