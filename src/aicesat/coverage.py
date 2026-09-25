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
GEDI_SHORT_NAME, GEDI_VERSION = "GEDI02_A", "003"


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


def _cloud_query(**kw):
    """A granule query restricted to cloud-hosted collections, with the restriction VERIFIED.

    cloud_hosted resolves a provider from short_name, so it must be applied after the parameters — and when it fails
    to resolve it does so silently. Checking that `provider` actually landed turns that into a visible warning
    instead of a search that quietly returns twice the rows."""
    import earthaccess

    a = earthaccess.__auth__
    q = (earthaccess.search.DataGranules(a) if a.authenticated else earthaccess.search.DataGranules())
    q = q.parameters(**kw).cloud_hosted(True)
    if not q.params.get("provider"):
        log.warning("cloud_hosted did not resolve a provider for %s; the search will return on-prem duplicates too",
                    kw.get("short_name"))
    return q


def search(short_name: str, version: str, bbox, window, use_cache: bool = True, polygon=None):
    """CMR granule search, cached on disk for CMR_CACHE_TTL_S: the search is ~1 s per call and every warm query paid it.

    `polygon` (a closed CCW [(lon, lat), ...] ring, from planner.search_polygon) searches that shape instead of the
    rectangle. An index build uses it because the rectangle around a selection's cells can be ~10x the area actually
    wanted, and every extra granule it returns costs a structure parse — the dominant cost of a build."""
    import pickle
    import time

    import earthaccess

    from . import cache

    key = cache.key("cmr", short_name, version, [round(float(v), 6) for v in bbox], list(window) if window else None,
                    [[round(float(x), 5), round(float(y), 5)] for x, y in polygon] if polygon else None)
    path = cache.CACHE_DIR / f"cmr_{key}.pkl"
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CMR_CACHE_TTL_S:
        try:
            granules = pickle.loads(path.read_bytes())
            log.info("%s v%s: %d granules (CMR cache)", short_name, version, len(granules))
            return granules
        except Exception:
            pass
    auth.login()
    kw = dict(short_name=short_name, version=version)
    if polygon:
        kw["polygon"] = [(float(x), float(y)) for x, y in polygon]
    else:
        kw["bounding_box"] = tuple(bbox)
    if window:
        kw["temporal"] = tuple(window)
    # cloud_hosted: CMR lists every file TWICE — the Cumulus copy and the retired on-prem one — so an unfiltered
    # search pages through double the results and dedup_granules throws half away. Filtering at the source measured
    # 3.49 s -> 2.38 s median on a 1,449-granule area, and matters far more region-wide, where the cost IS the
    # pagination. Verified identical output on all four collections: same files, none missing, none extra.
    #
    # Applied by CHAINING, not as a kwarg, because earthaccess evaluates .parameters(**kwargs) IN ORDER and
    # cloud_hosted resolves the provider from short_name — its own docstring says it "is valid ... when using the
    # short_name parameter". Passed before short_name it resolves nothing and is dropped WITHOUT ERROR:
    #
    #     .parameters(cloud_hosted=True, short_name=...)  -> hits 2,898, provider unset   (silent no-op)
    #     .parameters(short_name=..., ...).cloud_hosted() -> hits 1,449, provider NSIDC_CPRD
    #
    # search_data(count=-1, cloud_hosted=True, **kw) put it first, so the filter never once took effect in
    # production while a benchmark that happened to pass it last measured the speedup.
    granules = dedup_granules(_cloud_query(**kw).get_all())
    if not granules:
        # A filter that silently returns nothing would build an empty index and report success. If the unfiltered
        # search finds something the filter missed, take it and say so loudly.
        granules = dedup_granules(earthaccess.search_data(count=-1, **kw))
        if granules:
            log.warning("%s v%s: cloud_hosted found nothing but the unfiltered search found %d granules — "
                        "the collection may not be flagged cloud-hosted; using the unfiltered result",
                        short_name, version, len(granules))
    log.info("%s v%s: %d granules over %s %s", short_name, version, len(granules),
             f"a {len(polygon)}-vertex polygon" if polygon else bbox, window)
    if not granules:
        # Not cached: an empty answer is the one a re-run should re-ask, not repeat for 24 h. It may be a real
        # absence or a search that quietly matched nothing (write_build_manifest records it either way).
        return granules
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
        # GEDI is OFF by default. It is the densest lidar flown (25 m footprints) and beats ATL06 on gentle ground,
        # but its accuracy is strongly slope-dependent -- measured against the HMA 8 m DEM over Langtang, MAD ran
        # 2.7 m under 15 deg and 14.7 m over 50 deg, with the median residual sliding -4.0 -> -12.4 m, while ATL06
        # held 2.9-4.5 m on the same ground. Opting in should be a decision, not a default.
        {"key": "GEDI", "mission": "GEDI", "flag": "with_gedi", "label": "GEDI (L2A)", "short_name": GEDI_SHORT_NAME, "product": "GEDI02_A",
         "version": GEDI_VERSION, "epoch": "2019-", "window": list(regions.DEFAULT_GEDI_WINDOW), "default": False},
    ]


# Naming convention for a collection's index, so scripts and tests can iterate collections() instead of keeping their
# own lists (a hand-written list is where a new collection goes missing -- GEDI did, from eight of them). Every
# collection follows it except ATL03, whose index predates the convention and lives in aicesat.index.
def index_module(key: str):
    """The module that builds and reads a collection's sub-granule index."""
    import importlib
    return importlib.import_module("aicesat.index" if key == "ATL03" else f"aicesat.index_{key.lower()}")


def index_version(key: str) -> tuple[str, bytes]:
    """(current schema version, the Parquet schema-metadata key it is stamped under) for a collection's index."""
    m = index_module(key)
    if key == "ATL03":
        return m.INDEX_SCHEMA_VERSION, b"aicesat_index_version"
    return getattr(m, f"{key}_INDEX_VERSION"), f"aicesat_{key.lower()}_index_version".encode()


def build_script(key: str) -> str:
    """The script (relative to the repo root) that builds a collection's index."""
    return "scripts/build_index.py" if key == "ATL03" else f"scripts/build_{key.lower()}_index.py"


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
    if key == "GEDI":
        from . import index_gedi
        return index_gedi._index_dir(index_gedi.GEDI_RES), index_gedi.GEDI_RES, gdate_ym
    if key == "ATL03":
        return atl03_index.ATL03_INDEX_DIR, atl03_index.H3_RES, name_ym
    return None, None, None


# --- coverage manifest -----------------------------------------------------------------------------------------
# The index dirs are flat (one parquet per granule, no partitioning), so a coverage query that scans them directly
# opens every granule's footer regardless of the selected box (ATL06 alone is ~2300 files). We roll each index down
# to ONE small manifest of DISTINCT (h3_cell, granule, ym) rows: a coverage query then scans a single tiny file with
# h3_cell pushdown — tens of ms instead of seconds, independent of granule count. It is built lazily on first use and
# rebuilt whenever the index grows (a source parquet newer than the manifest, or the source file count changed), so
# existing deployments self-heal with no re-index.
#
# The manifest lives in a `_coverage/` SUBDIR, never as a top-level `{d}/*.parquet`: the index modules fetch byte
# spans with `read_parquet('{d}/*.parquet')` (non-recursive) and their empty-parquet ref pickers grab the first
# `*.parquet` — a schema-mismatched manifest sitting alongside the granule files would break both. A subdir is invisible
# to those non-recursive globs.
_MANIFEST_DIR = "_coverage"


def _manifest_paths(d):
    md = d / _MANIFEST_DIR
    return md, md / "manifest.parquet", md / "meta.json"


def _source_parquets(d) -> list:
    return sorted(d.glob("*.parquet"))   # granule index files only; the manifest lives one level down in _coverage/


def _dir_mtime_ns(d) -> int | None:
    try:
        return d.stat().st_mtime_ns
    except OSError:
        return None


def _manifest_fresh(d) -> bool:
    """Is the manifest current? ONE stat of the index directory, not a walk of it.

    The old check globbed every source parquet and stat()ed each one — on the deployed box that is ~32,060 files for
    ATL06, paid on every coverage query just to conclude that nothing had changed. A directory's mtime moves whenever
    an entry is added, replaced or removed, and index files land by tmp-then-rename, so the directory answers the
    question by itself. (Writes inside `_coverage/` do not touch the parent, so the manifest cannot invalidate itself.)
    """
    import json
    _md, manifest, meta = _manifest_paths(d)
    if not manifest.exists():
        return False
    try:
        return json.loads(meta.read_text()).get("dir_mtime_ns") == _dir_mtime_ns(d)
    except Exception:
        return False


def _build_manifest(d, ym: str, srcs: list) -> None:
    """Roll the per-granule index parquets down to one DISTINCT (h3_cell, granule, ym) manifest. Atomic (unique tmp +
    os.replace) so concurrent coverage requests never read a torn file; a lost build race just rebuilds next time."""
    import json
    import os
    import uuid

    import duckdb

    md, manifest, meta = _manifest_paths(d)
    md.mkdir(parents=True, exist_ok=True)
    # Stamp the directory mtime BEFORE reading the sources. A granule landing mid-build then leaves the recorded
    # mtime older than the directory's, so the next read rebuilds — stale-and-corrected rather than silently missing.
    stamp = _dir_mtime_ns(d)
    files = "[" + ",".join("'" + str(p) + "'" for p in srcs) + "]"   # explicit list, never a glob
    tmp = md / f".manifest.{uuid.uuid4().hex}.tmp"
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT DISTINCT h3_cell, granule, {ym} AS ym FROM read_parquet({files})) "
                    f"TO '{tmp}' (FORMAT PARQUET)")
    finally:
        con.close()
    os.replace(tmp, manifest)
    meta.write_text(json.dumps({"n_source_files": len(srcs), "dir_mtime_ns": stamp,
                                "built_at": datetime.now().isoformat(timespec="seconds")}))


def build_manifest(collection: str) -> dict:
    """Roll up a collection's index NOW. Call this when an index build finishes.

    The rollup is what makes coverage queries cheap, and it belongs to whoever wrote the index — not to whichever
    user request happens to arrive first afterwards and gets billed for a full re-read of every granule file.
    _ensure_manifest still rebuilds lazily if something wrote an index without calling this, but on the normal path
    that fallback should never fire.
    """
    d, _res, ym = _index_for(collection)
    if d is None or not d.exists():
        return {"collection": collection, "built": False, "reason": "no index directory"}
    srcs = _source_parquets(d)
    if not srcs:
        return {"collection": collection, "built": False, "reason": "no granule files"}
    _build_manifest(d, ym, srcs)
    return {"collection": collection, "built": True, "granule_files": len(srcs),
            "manifest": str(_manifest_paths(d)[1])}


def _ensure_manifest(d, ym: str):
    """Fresh coverage manifest path for index dir `d`, or None if the index has no granules. Normally a single stat:
    the builder calls build_manifest() when it finishes, so the rebuild below is a repair path, not the usual one."""
    if _manifest_fresh(d):
        return _manifest_paths(d)[1]
    srcs = _source_parquets(d)
    if not srcs:
        return None
    _build_manifest(d, ym, srcs)
    return _manifest_paths(d)[1]


def cell_coverage(collection: str) -> dict[int, tuple] | None:
    """Per-cell index coverage straight from the manifest: {cell: (granules, epochs, ym_min, ym_max)}, or None.

    This is what index_status is built on. Deriving the same answer by opening every per-granule index parquet cost
    43.5 s for ATL06 on the deployed box (33,064 files) — measured against 0.35 s here, for an identical cell set
    (14,482; GLAS 23,142 and ICESSN 7,782 also identical). Going through _ensure_manifest rather than reading the
    file directly matters: the manifest is rebuilt lazily when the index grows, so a blind read can lag it.
    """
    import duckdb

    d, _res, ym = _index_for("ATL03" if collection == "ICESAT2" else collection)
    if d is None or not d.exists():
        return None
    manifest = _ensure_manifest(d, ym)
    if manifest is None:
        return None
    con = duckdb.connect()
    try:
        # ORDER BY is not cosmetic. DuckDB's parallel hash aggregate returns groups in an arbitrary order that
        # varies BETWEEN IDENTICAL CALLS (measured: 8 orderings from 8 runs of this query), so index_status handed
        # every consumer a differently-ordered `cells` list each time its mtime gate missed. That is a diffable
        # response that never compares equal to itself; sorting 14k rows the scan has already materialised is free.
        rows = con.execute("SELECT h3_cell, count(DISTINCT granule), count(DISTINCT ym), min(ym), max(ym) "
                           "FROM read_parquet(?) GROUP BY h3_cell ORDER BY h3_cell", [str(manifest)]).fetchall()
    finally:
        con.close()
    return {int(c): (int(g), int(e), y0, y1) for c, g, e, y0, y1 in rows}


def span_years(ym0: str | None, ym1: str | None) -> float:
    """First-to-last observation span in years, from two 'YYYY-MM' stamps. 0.0 when a cell was seen only once.

    Span replaces the old per-cell "distinct cycles": only ICESat-2 has repeat cycles, and index_status silently
    substituted the YEAR for GLAS and IceBridge, so one colour scale meant two different things. Span means the same
    thing for every collection.
    """
    try:
        y0, m0 = int(ym0[:4]), int(ym0[5:7])
        y1, m1 = int(ym1[:4]), int(ym1[5:7])
    except (TypeError, ValueError):
        return 0.0
    return round(max(0, (y1 * 12 + m1) - (y0 * 12 + m0)) / 12, 1)


def read_parquet_src(index_dir, files: list[str] | None) -> str:
    """DuckDB source clause for an index query: the named granule files when the manifest resolved them, else the
    whole-directory glob (the safe fallback)."""
    if files:
        return "read_parquet([" + ", ".join("'" + f + "'" for f in files) + "])"
    return f"read_parquet('{index_dir}/*.parquet')"


def index_files_for_cells(collection: str, cells) -> list[str] | None:
    """Paths of the per-granule index parquets whose rows touch any of `cells`, resolved through the coverage manifest.

    The indexes are flat (one parquet per granule), so `_index_rows` scanning `{dir}/*.parquet` reads every granule's
    file to answer a few-cell query — 32,060 files / 12 s for ATL06 on the deployed box. The manifest already maps
    h3_cell -> granule, and index files are named `<granule>.parquet`, so it can name the handful of files that matter.

    Returns [] when no granule touches those cells (a real, empty answer), or None when the manifest is unavailable —
    the caller must then fall back to scanning the whole directory rather than silently returning nothing."""
    try:
        d, _res, ym = _index_for(collection)
        if d is None or not d.exists():
            return None
        manifest = _ensure_manifest(d, ym)
        if manifest is None:
            return None
        import duckdb
        pred = ",".join(str(int(c)) for c in cells)
        if not pred:
            return []
        con = duckdb.connect()
        try:
            names = [r[0] for r in con.execute(
                f"SELECT DISTINCT granule FROM read_parquet('{manifest}') WHERE h3_cell IN ({pred})").fetchall()]
        finally:
            con.close()
        return [str(p) for p in ((d / f"{n}.parquet") for n in names) if p.exists()]
    except Exception as e:                       # never let this optimisation break the query path
        log.debug("index_files_for_cells(%s) unavailable: %s", collection, e)
        return None


def index_covers_area(d, bbox, polygon=None) -> bool:
    """True if every cell of the SELECTION's ground was built. Exact set membership at the claim resolution.

    Not the addressing resolution: a res-5 addressing cell juts up to ~10 km past a drawn shape and nothing searched
    out there, so claiming one whole would assert ground the build never covered."""
    import json

    from . import index as atl03_index
    from . import planner

    mf = d / "_build.json"
    if not mf.exists():
        return False
    try:
        doc = json.loads(mf.read_text())
    except Exception:
        return False
    b = doc.get("bounds")
    w, s, e, n = bbox
    if b and not (b[0] <= w and b[1] <= s and e <= b[2] and n <= b[3]):
        return False          # cheap reject: outside the claim's own extent, so no polyfill is needed at all
    # Test at the resolution the manifest claims at. Finer would be wasted work; COARSER would be wrong, since
    # covers_cells matches by walking UP and would never reach a finer claim cell.
    res = doc.get("coverage_res") or atl03_index.COVERAGE_RES
    return atl03_index.covers_cells(d, planner.coverage_cells(bbox, polygon, res=res))


def searched_empty(d, bbox, polygon=None) -> dict | None:
    """{at, window} of the recorded empty search covering this selection, or None. Reads the manifest first and only
    polyfills when it holds an empty search, so the common case costs one small JSON read."""
    import json

    from . import index as atl03_index
    from . import planner

    mf = d / "_build.json"
    try:
        recs = json.loads(mf.read_text()).get("empty_searches") if mf.exists() else None
    except Exception:
        return None
    if not recs:
        return None
    res = recs[-1].get("coverage_res") or atl03_index.COVERAGE_RES
    rec = atl03_index.empty_search_covering(d, planner.coverage_cells(bbox, polygon, res=res))
    return {"at": rec.get("at"), "window": rec.get("window")} if rec else None


def coverage_gap(d, bbox, polygon=None) -> str | None:
    """WHY index_covers_area refuses this selection, as one line for an error message. None when it is covered.

    The gate demands CONTAINMENT, so an index covering 99.98% of a selection still refuses it — and the message
    users actually saw ("check your selection and the token") named neither the real cause nor a fix, sending at
    least one diagnosis toward auth. This says which of the four cases it is, in the same terms
    scripts/why_not_covered.py reports them, so the message and the diagnostic tool cannot drift apart.

    Cheap by construction: cases 1 and 2 cost a stat and a compare, and only case 3/4 polyfills — the same work
    index_covers_area was going to do anyway.
    """
    import json
    import pathlib

    from . import index as atl03_index
    from . import planner

    d = pathlib.Path(d)
    where = " ".join(f"{v:g}" for v in bbox)
    tail = f"; run `uv run scripts/why_not_covered.py {where}` for the full diagnosis"
    if not d.exists():
        return f"no index directory at {d} — nothing was ever built here{tail}"
    mf = d / "_build.json"
    if not mf.exists():
        return ("granule files may exist but no coverage claim was stamped — a build killed before it finished "
                f"leaves this state; re-run the build{tail}")
    try:
        doc = json.loads(mf.read_text())
    except Exception as e:
        return f"_build.json is unreadable ({type(e).__name__}) — re-run the build{tail}"

    b, res = doc.get("bounds"), doc.get("coverage_res") or atl03_index.COVERAGE_RES
    if doc.get("empty_searches"):
        # Checked before the extent test: an area whose last build found no granules is most usefully told THAT,
        # whether or not a claim exists elsewhere. Rare, so the polyfill here costs nothing in the common case.
        want = planner.coverage_cells(bbox, polygon, res=res)
        rec = None if atl03_index.covers_cells(d, want) else atl03_index.empty_search_covering(d, want)
        if rec:
            win = f" for {rec['window'][0]}..{rec['window'][1]}" if rec.get("window") else ""
            return (f"the last build here ({str(rec.get('at', ''))[:10]}) found no granules in NASA's catalog{win}, "
                    f"so it claimed no coverage. That is expected where the instrument never passed over this "
                    f"ground; if it should have data, check the product version the build searched for and re-run "
                    f"the build{tail}")
    w, s, e, n = bbox
    if b and not (b[0] <= w and b[1] <= s and e <= b[2] and n <= b[3]):
        over = ", ".join(f"{side} by {abs(v):.3f}deg" for side, v in
                         (("west", b[0] - w), ("south", b[1] - s), ("east", e - b[2]), ("north", n - b[3])) if v > 0)
        return (f"the selection is outside the claimed extent {[round(x, 3) for x in b]} ({over}) — "
                f"draw inside the claim, or build over a box that contains it{tail}")

    want = planner.coverage_cells(bbox, polygon, res=res)
    if atl03_index.covers_cells(d, want):
        return None
    missing = sum(1 for c in want if not atl03_index.covers_cells(d, [c]))
    return (f"the claim's extent contains this selection but {missing:,} of {len(want):,} res-{res} cells are "
            f"unclaimed, and the gate needs every one. A selection flush with the build box's own edge lands here "
            f"even when the build succeeded: H3 cells straddle their parents, so the claim's polyfill and the "
            f"selection's need not agree at the boundary. Try a slightly smaller selection, or build over a box "
            f"that contains this one{tail}")


# Where a collection CAN have data at all, as CMR declares it (BoundingRectangles on the collection record,
# [W, S, E, N]). IceBridge only ever flew the poles, so ILATM2 exists at 60..90 N and -90..-53 S and nowhere in
# between: offering it over Nepal is offering a leg that cannot succeed, and the failure it produces reads like a
# missing index rather than an impossibility. GLAS reaches +-86 and ICESat-2 is near-polar, so both are effectively
# global for our purposes.
FOOTPRINTS: dict[str, list[tuple[float, float, float, float]]] = {
    "ICESSN": [(-180.0, 60.0, 180.0, 90.0), (-180.0, -90.0, 180.0, -53.0)],   # ILATM2 v2
    "GLAS":   [(-180.0, -86.0, 180.0, 86.0)],                                  # GLAH06 v034
    "ATL06":  [(-180.0, -88.0, 180.0, 88.0)],                                  # ICESat-2, 92 deg inclination
    "ATL03":  [(-180.0, -88.0, 180.0, 88.0)],
    # GEDI flies on the ISS, whose 51.6 deg inclination is a hard ceiling: there is no GEDI over any ice sheet.
    # Offering it over Greenland would be offering a leg that cannot succeed.
    "GEDI":   [(-180.0, -51.6, 180.0, 51.6)],
}


def collection_can_cover(key: str, bbox) -> bool:
    """Could this collection have ANY data over bbox? A declared-footprint test, not an index test.

    `indexed` and `covered` both answer "have we built it here"; neither distinguishes ground nobody ever flew from
    ground we simply have not indexed yet. Overlap, not containment: a selection straddling 60 N genuinely has
    IceBridge data in its northern half.
    """
    w, s, e, n = (float(v) for v in bbox)
    return any(w < fe and fw < e and s < fn and fs < n for fw, fs, fe, fn in FOOTPRINTS.get(key, []) or [(-180.0, -90.0, 180.0, 90.0)])


def check_coverage(bbox, **_ignored) -> dict:
    """Granule counts per collection over a bbox, straight from the sub-granule INDEX — no CMR at query time. The
    index IS the discovery layer (CMR is paid once, at build time), and it counts granules with points that actually
    fall in the box's cells (not CMR's footprint over-claim).

    TWO different facts, because conflating them made the Explore panel contradict the Lake view:

      `indexed`  — this collection has index rows we can read. The counts (n_granules, cells, by_month) are measured
                   over the query's OWN cells, so they answer "what data exists over my area".
      `covered`  — a declared build box fully CONTAINS the query area, so a build will accept it. Partial overlap is
                   not enough: planner._ensure and each collection's _index_covers require containment, because
                   building over a half-indexed area would silently return less data than the area asks for.

    An area that overlaps a built box without being inside it is therefore `indexed=True, covered=False`: the Lake
    view is right that cells are indexed there, AND a build will still refuse. Reporting only the containment (the
    old behaviour) claimed "not indexed" over hundreds of genuinely indexed cells.

    A third fact, `searched_empty` ({at, window} or None): the last build over this area found no granules at all,
    so it claimed nothing. Without it that area reads as merely "not indexed", and re-building finds nothing again.

    Returns {bbox, collections:[{key,label,product,version,epoch,window,n_granules,indexed,covered,cells,by_month,
    searched_empty}]}."""
    import duckdb

    from . import planner
    out = []
    for c in collections():
        row = {k: c[k] for k in ("key", "label", "product", "version", "epoch", "window")}
        row["possible"] = collection_can_cover(c["key"], bbox)
        row["searched_empty"] = None   # {at, window} when the last build here found no granules (see write_build_manifest)
        if not row["possible"]:
            # No index question to ask: this instrument never flew here. Say so instead of reporting "not indexed",
            # which invites the user to go build an index that would find nothing.
            row.update(n_granules=0, indexed=False, covered=False, cells=0, by_month={})
            out.append(row)
            continue
        d, res, ym = _index_for(c["key"])
        covered = bool(d is not None and d.exists() and index_covers_area(d, bbox))
        if d is not None and d.exists() and not covered:
            row["searched_empty"] = searched_empty(d, bbox)
        if d is None or not d.exists():
            row.update(n_granules=None, indexed=False, covered=False, cells=0, by_month={})
            out.append(row)
            continue
        manifest = _ensure_manifest(d, ym)   # one tiny rolled-up file, built/refreshed lazily (see above)
        if manifest is None:                 # index dir present but no granule parquets yet
            row.update(n_granules=None, indexed=False, covered=covered, cells=0, by_month={})
            out.append(row)
            continue
        cells = planner.cells_for_bbox(bbox, res=res)
        pred = f"h3_cell IN ({','.join(str(int(x)) for x in cells)})"
        con = duckdb.connect()
        try:
            ng, ncells = con.execute(f"SELECT count(DISTINCT granule), count(DISTINCT h3_cell) "
                                     f"FROM read_parquet('{manifest}') WHERE {pred}").fetchone()
            by = con.execute(f"SELECT ym AS m, count(DISTINCT granule) FROM read_parquet('{manifest}') "
                             f"WHERE {pred} GROUP BY m ORDER BY m").fetchall()
        finally:
            con.close()
        row.update(n_granules=int(ng or 0), indexed=True, covered=covered, cells=int(ncells or 0),
                   by_month={m: int(n) for m, n in by if m})
        out.append(row)
    return {"bbox": list(bbox), "collections": out}
