"""Coverage from the sub-granule index (no CMR at query time). CMR search here is used only to ENUMERATE granules at index-build time; coverage counts come straight from the built index."""
from __future__ import annotations

import logging
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
            base = links[0].rsplit("/", 1)[-1].split("?")[0]
            if base.endswith((".h5", ".H5", ".csv")):
                return base
    except Exception:
        pass
    return g["meta"]["native-id"]


def _has_cloud_link(g) -> bool:
    try:
        return any("earthdatacloud" in u or "cumulus" in u for u in (g.data_links() or []))
    except Exception:
        return False


def dedup_granules(granules: list) -> list:
    """Keep one entry per data file, PREFERRING the Earthdata Cloud copy. CMR lists many granules twice — a cloud
    entry and an on-prem (n5eil01u, now retired) entry with the same filename but a different native-id — besides the
    usual revision duplicates. Both would otherwise be built, and the on-prem copy is unreadable (connection refused)."""
    by_name: dict[str, object] = {}
    order: list[str] = []
    for g in granules:
        n = granule_name(g)
        if n not in by_name:
            by_name[n] = g
            order.append(n)
        elif _has_cloud_link(g) and not _has_cloud_link(by_name[n]):
            by_name[n] = g   # replace a dead on-prem copy already seen with the cloud copy
    return [by_name[n] for n in order]


CMR_CACHE_TTL_S = 24 * 3600  # granule lists for a (product, bbox, window) change only when NSIDC reprocesses


def sample_evenly(granules: list, n) -> list:
    """Take up to n granules spread evenly across the (time-ordered) granule list, so a capped extraction
    still spans the full record instead of truncating to the earliest N (CMR returns them oldest-first)."""
    if not n or n <= 0 or n >= len(granules):
        return granules
    import numpy as np
    idx = sorted(set(np.linspace(0, len(granules) - 1, int(n)).round().astype(int).tolist()))
    return [granules[i] for i in idx]


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


def _index_for(key: str):
    """(index_dir, h3_res, SQL 'YYYY-MM' expr) for a collection's sub-granule index, or (None, None, None)."""
    from . import index_atl06, index_glas, index_icessn
    from . import index as atl03_index
    gdate_ym = "substr(gdate,1,4) || '-' || substr(gdate,5,2)"        # GLAS/ICESSN store gdate=YYYYMMDD
    name_ym = "substr(granule,7,4) || '-' || substr(granule,11,2)"    # ATL0x_YYYYMMDD... in the granule name
    if key == "GLAS":
        return index_glas._index_dir(index_glas.GLAS_RES), index_glas.GLAS_RES, gdate_ym
    if key == "ICESSN":
        return index_icessn._index_dir(index_icessn.ICESSN_RES), index_icessn.ICESSN_RES, gdate_ym
    if key == "ATL06":
        return index_atl06._index_dir(index_atl06.ATL06_RES), index_atl06.ATL06_RES, name_ym
    if key == "ATL03":
        return atl03_index.ATL03_INDEX_DIR, atl03_index.H3_RES, name_ym
    return None, None, None


def _index_covers_bbox(d, bbox) -> bool:
    """True if any region in the index's build manifest (_build.json) contains this bbox."""
    from . import index_manifest
    return index_manifest.covers(d, bbox)


def check_coverage(bbox, **_ignored) -> dict:
    """Granule counts per collection over a bbox, straight from the sub-granule INDEX — no CMR at query time. The
    index IS the discovery layer (CMR is paid once, at build time), and it counts granules with points that actually
    fall in the box's cells (not CMR's footprint over-claim). A collection whose index does not cover this bbox is
    reported as not-indexed (n_granules=None, indexed=False) — never fetched from CMR: we always build the index.
    Returns {bbox, collections:[{key,label,product,version,epoch,window,n_granules,indexed,cells,by_month}, ...]}."""
    import duckdb

    from . import planner
    out = []
    for c in collections():
        row = {k: c[k] for k in ("key", "label", "product", "version", "epoch", "window")}
        d, res, ym = _index_for(c["key"])
        if d is None or not d.exists() or not any(d.glob("*.parquet")) or not _index_covers_bbox(d, bbox):
            row.update(n_granules=None, indexed=False, cells=0, by_month={})
            out.append(row)
            continue
        cells = planner.cells_for_bbox(bbox, res=res)
        pred = f"h3_cell IN ({','.join(str(int(x)) for x in cells)})"
        con = duckdb.connect()
        try:
            ng, ncells = con.execute(f"SELECT count(DISTINCT granule), count(DISTINCT h3_cell) "
                                     f"FROM read_parquet('{d}/*.parquet') WHERE {pred}").fetchone()
            by = con.execute(f"SELECT {ym} AS m, count(DISTINCT granule) FROM read_parquet('{d}/*.parquet') "
                             f"WHERE {pred} GROUP BY m ORDER BY m").fetchall()
        finally:
            con.close()
        row.update(n_granules=int(ng or 0), indexed=True, cells=int(ncells or 0),
                   by_month={m: int(n) for m, n in by if m})
        out.append(row)
    return {"bbox": list(bbox), "collections": out}
