"""Transport-neutral API: every UI/agent operation as a plain function returning JSON-able dicts.

server.py exposes these twice — as HTTP routes (the localhost widget, "fetch adapter") and as MCP tools (model-visible
and app-visible, the MCP Apps "app adapter"). Nothing here knows which transport called it.
"""
from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from . import atl03, cache, coverage, geom, regions, scene

log = logging.getLogger(__name__)
_lock = threading.Lock()          # serialise compute (one user, one demo)
_jobs: dict[str, dict] = {}
REGISTRY = cache.SCENE_DIR / "registry.json"


# ----------------------------------------------------------------------------- scenes registry
def _registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text())
        except Exception:
            pass
    return {}


def registry_upsert(scene_id: str, **fields) -> dict:
    cache.SCENE_DIR.mkdir(parents=True, exist_ok=True)
    reg = _registry()
    rec = reg.get(scene_id, {"scene_id": scene_id, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    rec.update({k: v for k, v in fields.items() if v is not None})
    reg[scene_id] = rec
    tmp = REGISTRY.with_suffix(f".{os.getpid()}.tmp")   # atomic: registry is now upserted per-leg during a build
    tmp.write_text(json.dumps(reg, indent=1, default=str))
    os.replace(tmp, REGISTRY)
    return rec


def scenes() -> list[dict]:
    """All scenes, newest first: registry entries plus any scene file not yet registered (backfill)."""
    reg = _registry()
    for p in cache.SCENE_DIR.glob("*.json"):
        if p.name == "registry.json" or p.stem in reg:
            continue
        try:
            doc = json.loads(p.read_text())
            reg[p.stem] = registry_upsert(p.stem, question=doc.get("question"), bbox=doc.get("bbox"), polygon=doc.get("polygon"),
                                          series=sorted(doc.get("series", {})), status="ready", coreg=bool(doc.get("coreg")),
                                          created=datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"))
        except Exception:
            continue
    out = sorted(reg.values(), key=lambda r: r.get("created", ""), reverse=True)
    for r in out:
        j = next((j for j in _jobs.values() if j.get("scene_id") == r["scene_id"]), None)
        if j and j["status"] == "running":
            r["status"] = "loading"; r["job_id"] = j["id"]
    return out


def scene_doc(scene_id: str) -> dict | None:
    return cache.load_scene(scene_id)


def delete_scene(scene_id: str) -> dict:
    """Permanently remove ONE scene from Explore: its registry row + its scene doc (cache.SCENE_DIR/<id>.json) + any
    scene-scoped subdirectory (cache.SCENE_DIR/<id>/...). Irreversible and user-initiated — that's fine here.

    Scoped tightly to the scene's OWN files. It deliberately touches NOTHING shared:
      * the Parquet lake (data/lake/mission=*/... + its coverage DuckDB) — the materialized cell cache is shared across
        scenes and is the point of the lake-cache feature; never evicted/removed here;
      * the content-addressed extract cache (cache.py CACHE_DIR entries the scene's series reference by cache_key) —
        the fetched GLAS/ICESSN/ATL06/ATL03 arrays, reused by other scenes over overlapping areas;
      * the content-addressed imagery JPEG (data/cache/imagery/<hash>.jpg, keyed by extent, shared by any scene over
        the same area) — the scene doc only points at it by path.
    So re-building the same area afterward hits the lake/extract cache (zero NASA GETs), proving the data survived."""
    import re
    import shutil

    if not re.fullmatch(r"[A-Za-z0-9_-]+", scene_id or ""):   # scene ids are uuid4 hex[:10]; refuse path-traversal input
        raise ValueError(f"invalid scene id {scene_id!r}")
    existed = False
    # 1) registry row — atomic rewrite, mirroring registry_upsert
    reg = _registry()
    if scene_id in reg:
        del reg[scene_id]
        existed = True
        cache.SCENE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(reg, indent=1, default=str))
        os.replace(tmp, REGISTRY)
    # 2) the scene doc itself (SCENE_DIR/<id>.json)
    p = cache.SCENE_DIR / f"{scene_id}.json"
    if p.exists():
        p.unlink()
        existed = True
    # 3) any scene-scoped render subdirectory (SCENE_DIR/<id>/...) — never anything outside SCENE_DIR/<id>
    d = cache.SCENE_DIR / scene_id
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        existed = True
    return {"scene_id": scene_id, "deleted": True, "existed": existed}


MCP_CHUNK_BYTES = 96_000        # an MCP host caps a tool result; the app adapter fetches in these
HTTP_CHUNK_BYTES = 1_000_000    # a browser has no such cap, and 23 sequential requests for one array is the cost


def scene_part(scene_id: str, part: str = "meta", chunk: int = 0, chunk_bytes: int = HTTP_CHUNK_BYTES) -> dict:
    """Scene metadata and the non-point payloads. parts: meta | surface | imagery | coreg | dh.

    Point arrays are NOT served here. They go out over the push stream (see stream.py), which is the only transport
    for them — there is no chunked/base64 point path any more, and nothing takes a prefix of a series."""
    doc = _scene_for_read(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    if part == "meta":
        for _m in list(doc.get("series") or {}):
            _migrate_series_arrays(scene_id, doc, _m)   # pre-sidecar scenes self-heal here now that no other read does
        # Everything EXCEPT the bulk arrays (positions, slopes) and surface z — those are fetched via their own chunked
        # parts and appended incrementally, so this stays small and is safe to poll repeatedly during a build. `has_slopes`
        # tells the client whether to fetch a slopes:<mission> part (ICESSN platelets).
        def _series_meta(s):   # drop bulk arrays and internal bookkeeping (keys starting with "_")
            m = {k: v for k, v in s.items() if k not in ("positions", "slopes") and not k.startswith("_")}
            m["has_slopes"] = bool(s.get("has_slopes")) or cache.scene_array_len(scene_id, s.get("mission", ""), "slopes") > 0
            return m
        return {"scene_id": scene_id, "question": doc.get("question"), "frame": doc["frame"], "bbox": doc["bbox"], "polygon": doc.get("polygon"),
                "z0": doc["z0"], "labels": doc.get("labels"), "markers": doc.get("markers"),
                "imagery_status": doc.get("imagery_status"),
                "imagery": ({k: v for k, v in doc["imagery"].items() if k != "path"} if doc.get("imagery") else None),
                "series": {m: _series_meta(s) for m, s in doc["series"].items()},
                "has_coreg": bool(doc.get("coreg")), "surface": ({k: v for k, v in doc["surface"].items() if k != "z"} if doc.get("surface") else None)}
    if part == "surface":
        return _chunked(np.asarray([np.nan if v is None else v for v in doc["surface"]["z"]], dtype="f4"), chunk, chunk_bytes, "z")
    if part == "coreg":
        c = doc.get("coreg") or {}
        return {k: v for k, v in c.items() if k not in ("dh_native", "dh_coreg", "artifact")}
    if part == "dh":
        c = doc.get("coreg") or {}
        return {k: c.get(k) for k in ("dh_native", "dh_coreg", "artifact")}
    if part == "imagery":
        img = doc.get("imagery")
        if not img:
            return {"data": None}
        data = open(img["path"], "rb").read()
        return _chunked_bytes(data, chunk, chunk_bytes, "image/jpeg")
    raise ValueError(f"unknown part {part}")


# One parsed scene doc, reused across the chunk requests of a single page load. Rendering a scene issues ~38 part
# requests and EVERY one re-read and re-parsed the whole document to hand back a 96 KB slice: 117 ms per request, of
# which 9 ms was the actual array work and 120 ms the JSON parse of a 14.7 MB file. Work proportional to the store
# rather than the request, again. Keyed on the file's mtime+size, so a build writing progressive updates invalidates
# it on the next request rather than serving a stale doc.
#
# READ PATH ONLY. cache.load_scene stays uncached for everyone else: callers like run_coregister mutate the doc they
# load and save it back, and handing them a shared object would publish half-finished state to concurrent readers.
_READ_MEMO: dict = {"key": None, "doc": None}
_READ_MEMO_LOCK = threading.Lock()


def _scene_for_read(scene_id: str):
    p = cache.SCENE_DIR / f"{scene_id}.json"
    try:
        st = p.stat()
        key = (scene_id, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (scene_id, None, None)
    with _READ_MEMO_LOCK:
        if _READ_MEMO["key"] == key:
            return _READ_MEMO["doc"]
    doc = cache.load_scene(scene_id)          # outside the lock: a 14.7 MB parse must not serialise other readers
    with _READ_MEMO_LOCK:
        _READ_MEMO["key"], _READ_MEMO["doc"] = key, doc
    return doc


def _migrate_series_arrays(scene_id: str, doc: dict, mission: str) -> None:
    """Move a pre-sidecar scene's bulk arrays out of the doc, once, on first read.

    Scenes written before the sidecar carry positions/slopes as JSON lists. Rather than break them (or keep two read
    paths forever), the first request that needs an array writes it out and rewrites the doc without it. Self-healing
    and idempotent: after this the doc is metadata only, like a freshly built one."""
    s = doc.get("series", {}).get(mission) or {}
    moved = False
    for kind, per in (("positions", 3), ("slopes", 2)):
        vals = s.get(kind)
        if vals and cache.scene_array_len(scene_id, mission, kind) == 0:
            cache.scene_array_write(scene_id, mission, kind, np.asarray(vals, dtype="f4"))
            moved = True
        if kind in s:
            if kind == "slopes":
                s["has_slopes"] = True
            del s[kind]
            moved = True
    if moved:
        log.info("scene %s/%s: migrated bulk arrays out of the doc", scene_id, mission)
        cache.save_scene(scene_id, doc)


def _chunked(arr: np.ndarray, chunk: int, chunk_bytes: int, name: str) -> dict:
    raw = arr.tobytes()
    n_chunks = max(1, -(-len(raw) // chunk_bytes))
    piece = raw[chunk * chunk_bytes:(chunk + 1) * chunk_bytes]
    return {"name": name, "dtype": "float32", "n_values": int(arr.size), "chunk": chunk, "n_chunks": n_chunks,
            "chunk_values": chunk_bytes // arr.itemsize,   # so a resuming client need not assume the chunk size
            "b64": base64.b64encode(piece).decode("ascii")}


def _chunked_bytes(raw: bytes, chunk: int, chunk_bytes: int, mime: str) -> dict:
    n_chunks = max(1, -(-len(raw) // chunk_bytes))
    return {"mime": mime, "n_bytes": len(raw), "chunk": chunk, "n_chunks": n_chunks,
            "b64": base64.b64encode(raw[chunk * chunk_bytes:(chunk + 1) * chunk_bytes]).decode("ascii")}


# ----------------------------------------------------------------------------- building scenes (jobs)
def _enforce_lake_limit(bb, poly, log_fn=lambda m: None) -> list[dict]:
    """After a build, evict LRU cells across ALL missions until the lake is under the Lake UI disk budget, protecting
    this scene's cells. The scene touches each mission at its own H3 resolution, so the protect set is the union of the
    area's cells at every resolution in play (res 6 for ATL03, res 5 for the index missions)."""
    from . import coverage, lake, planner
    try:
        protect = set()
        for res in {coverage._index_for(c["key"])[1] for c in coverage.collections()}:
            protect |= set(planner.cells_for_bbox(bb, res=res, polygon=poly))
        evicted = lake.enforce_global_limit(protect=protect, reason="limit (scene build)")
        if evicted:
            log_fn(f"storage limit: evicted {len(evicted)} cells to stay under the lake budget")
        return evicted
    except Exception as e:
        log.warning("lake limit enforcement failed: %s", e)
        return []


IMAGERY_JOIN_TIMEOUT_S = 300   # a tile mosaic over a large scene is minutes, not seconds; past this, give up on it


def build_scene(bbox=None, polygon=None, question=None, with_glas=True, with_coreg=False,
                with_atl06=False, with_icessn=False, with_atl03=False, with_gedi=False, with_gpstruth=False,
                with_imagery=True,
                imagery_source=None,
                log_fn=lambda m: None, scene_id: str | None = None, markers=None,
                wait_for_imagery: bool = False) -> dict:
    """Full pipeline for an area: any subset of the collections (GLAS, IceBridge ICESSN, ATL06, ATL03 photons),
    plus a DEM surface, imagery, and — when both ATL03 and GLAS are present — co-registration. Every collection is
    optional and non-fatal: a miss over the area is logged and the scene still builds from whatever is available.
    Returns the scene doc. (ATL03 is heavy and off by default; co-registration currently needs it.)

    `wait_for_imagery` (opt-in): join the imagery thread before returning. The default False is what the SERVER
    wants -- the scene reaches "ready" the moment the points paint and a slow tile source cannot hold it there.
    But that thread is a DAEMON, so a caller that exits when build_scene returns kills it mid-flight: a script gets
    a scene saved with imagery_status "pending" and no imagery, and a test gets its scene written into the real
    data dir after monkeypatch has restored cache.SCENE_DIR (resolved at call time, not at launch). Any caller that
    is not a long-lived process should pass True."""
    bb, poly = geom.normalize_area(bbox, polygon)
    sid = scene_id or uuid.uuid4().hex[:10]
    registry_upsert(sid, question=question, bbox=list(bb), polygon=poly, status="loading", series=[])

    lake_grew = {"v": False}   # set when any leg actually fetched+materialized new chunks -> eviction worth running
    granules_seen: dict[str, set] = {}   # mission -> streamed granule names, for the progress denominator (not in doc)

    def _mark_done(mission, meta):
        """A collection is finished when its authoritative series is in the doc — regardless of how many granules
        streamed, since a cache hit streams none. Pin done==total so the bar lands exactly on 100%."""
        pr = doc.setdefault("progress", {}).setdefault(mission, {})
        pr["total"] = pr.get("total") or 1
        pr["done"] = pr["total"]
        pr["phase"] = "done"
        pr["points"] = int(meta.get("n") or 0)

    def _log_cache(mission, meta):
        """Surface the lake-cache effect for an index mission (fetch_bbox threads it through meta['access'])."""
        st = meta.get("access", {}) or {}
        if st.get("chunks_from_nasa"):
            lake_grew["v"] = True
        if "chunks_from_nasa" in st:
            log_fn(f"{mission}: {st['chunks_from_nasa']} chunks from NASA ({st.get('bytes', 0) / 1e6:.1f} MB, "
                   f"{st.get('requests', 0)} GETs), {st.get('chunks_from_lake', 0)} served from the lake")
        if st.get("evicted_for_limit"):
            log_fn(f"storage limit: evicted {len(st['evicted_for_limit'])} cells")

    # --- pure extract workers: run concurrently, touch no shared/doc state, return (arrays, meta, cache_key). The
    #     index missions thread an on_granule callback (defined below, once `doc`/frame exist) so a cache-MISS build
    #     streams each satellite pass as it lands; ATL03 has no per-granule stream. --------------------------------
    def _ex_glas():
        from . import glas
        a, m = glas.extract(bb, regions.DEFAULT_GLAS_WINDOW, polygon=poly, on_granule=_on_granule("GLAS"), on_plan=_on_plan("GLAS"))
        return a, m, m["cache_key"]

    def _ex_gedi():
        from . import gedi
        a, m = gedi.extract(bb, regions.DEFAULT_GEDI_WINDOW, polygon=poly, on_granule=_on_granule("GEDI"),
                            on_plan=_on_plan("GEDI"))
        return a, m, m["cache_key"]

    def _ex_icessn():
        from . import icessn
        a, m = icessn.extract(bb, regions.DEFAULT_ICESSN_WINDOW, polygon=poly, on_granule=_on_granule("ICESSN"), on_plan=_on_plan("ICESSN"))
        return a, m, m["cache_key"]

    def _ex_atl06():
        from . import atl06
        a, m = atl06.extract(bb, regions.DEFAULT_ATL06_WINDOW, polygon=poly, on_granule=_on_granule("ATL06"), on_plan=_on_plan("ATL06"))
        return a, m, m["cache_key"]

    def _ex_gpstruth():
        from . import gpstruth
        a, m = gpstruth.extract(bb, regions.DEFAULT_GPSTRUTH_WINDOW, polygon=poly, on_granule=_on_granule("GPSTRUTH"),
                                on_plan=_on_plan("GPSTRUTH"))
        return a, m, m["cache_key"]

    def _ex_atl03():
        a, m = atl03.extract(bb, regions.DEFAULT_ATL03_WINDOW, polygon=poly)
        return a, m, m["cache_key"]

    # --- integrators: mutate `doc`; ONLY ever called on the build thread, in priority order, so z0 and the
    #     series-insertion order are byte-for-byte what the old serial loop produced. -------------------------------
    def _int_glas(a, m, ck):
        scene.add_series(doc, "GLAS", a, m, ck)
        _mark_done("GLAS", m)
        log_fn(f"GLAS: {m['n']:,} shots across {len(m['campaigns'])} campaigns")
        _log_cache("GLAS", m)

    def _int_icessn(a, m, ck):
        scene.add_series(doc, "ICESSN", a, m, ck)
        _mark_done("ICESSN", m)
        log_fn(f"ICESSN: {m['n']:,} nadir platelets across {len(m['years'])} campaign years")
        _log_cache("ICESSN", m)

    def _int_atl06(a, m, ck):
        scene.add_series(doc, "ATL06", a, m, ck)
        _mark_done("ATL06", m)
        log_fn(f"ATL06: {m['n']:,} land-ice segments")
        _log_cache("ATL06", m)

    def _int_gedi(a, m, ck):
        scene.add_series(doc, "GEDI", a, m, ck)
        _mark_done("GEDI", m)
        log_fn(f"GEDI: {m['n']:,} footprints (25 m, 8 beams)")
        _log_cache("GEDI", m)

    def _int_gpstruth(a, m, ck):
        scene.add_series(doc, "GPSTRUTH", a, m, ck)
        _mark_done("GPSTRUTH", m)
        log_fn(f"GPSTRUTH: {m['n']:,} GPS epochs across {len(m['years'])} survey years")
        _log_cache("GPSTRUTH", m)

    def _int_atl03(a, m, ck):
        st = m.get("access", {})
        if st.get("chunks_fetched"):
            lake_grew["v"] = True
        log_fn(f"ATL03: {m['n']:,} photons; {st.get('chunks_fetched', 0)} chunks fetched "
               f"({st.get('bytes', 0) / 1e6:.0f} MB, {st.get('requests', 0)} requests), "
               f"{st.get('chunks_skipped_already_materialized', 0)} already in the lake")
        if st.get("evicted_for_limit"):
            log_fn(f"storage limit: evicted {len(st['evicted_for_limit'])} cells")
        scene.add_series(doc, "ICESAT2", a, m, ck)

    # (mission_key, enabled, extract_worker, integrator, display_name); priority order == the old serial order, which
    # is what decides the z0 anchor (first success sets doc["z0"]) and the series-dict key order.
    LEGS = [
        ("GLAS",    with_glas,   _ex_glas,   _int_glas,   "GLAS"),
        ("ICESSN",  with_icessn, _ex_icessn, _int_icessn, "ICESSN"),
        ("ATL06",   with_atl06,  _ex_atl06,  _int_atl06,  "ATL06"),
        ("GEDI",    with_gedi,   _ex_gedi,   _int_gedi,   "GEDI"),
        ("GPSTRUTH", with_gpstruth, _ex_gpstruth, _int_gpstruth, "GPSTRUTH"),
        ("ICESAT2", with_atl03,  _ex_atl03,  _int_atl03,  "ATL03"),
    ]
    # Drop legs whose instrument never surveyed this ground. An impossible leg is not a failure worth reporting:
    # IceBridge flew the Arctic and Antarctic only, so asking it for Nepal produced a coverage error that read like
    # a missing index and invited a build that would find nothing. The UI disables these too; this is the guard for
    # every other caller (MCP tools, scripts, an older UI).
    _COLL_FOR_LEG = {c["mission"]: c["key"] for c in coverage.collections()}
    enabled = []
    for leg in LEGS:
        if not leg[1]:
            continue
        if not coverage.collection_can_cover(_COLL_FOR_LEG[leg[0]], bb):
            log_fn(f"{leg[4]}: not flown over this area — skipped")
            log.info("%s never surveyed %s; leg skipped", leg[4], bb)
            continue
        enabled.append(leg)

    try:
        with _lock:
            doc = scene.new_scene(sid, bb, question, polygon=poly, markers=markers)
            cache.save_scene(sid, doc)               # persist the shell (frame/bbox) immediately -> UI opens instantly

            frame = doc["frame"]
            # The DATA extent, not the drawn bbox: the imagery must cover the same ground as the surface mesh it is
            # draped on. scene.set_surface sizes the mesh with data_extent, so leaving this as bbox_extent gave a
            # 19.5 km texture on a 52.6 km mesh — texCoords ran past 1.0 and the texture REPEATED, which is what
            # "the imagery is striped" was. Computed once here; the shared _tr transformer is build-thread only.
            extent = scene.data_extent(frame, poly)

            # --- per-granule progressive streaming (cache-miss builds only) -------------------------------------------
            # An index mission's fetch_bbox calls on_granule ONCE per satellite pass as its chunks land, from the
            # concurrent granule pool (a NON-build thread). We bake those partials into the doc so the widget's poll
            # paints a growing cloud. Every doc mutation + save that can now race — these callbacks, the DEM z0/surface
            # block, and each integrator (add_series) — is serialised by `stream_lock`, because cache.save_scene writes
            # a single per-PID temp file and json.dumps(doc) must never see the doc mutate mid-serialisation.
            stream_lock = threading.Lock()
            stream_pending: dict[str, list] = {}     # mission -> partials buffered before z0 is known (baking needs z0)
            finalized: set[str] = set()              # missions whose authoritative add_series has replaced the preview
            last_stream_save = [0.0]                  # coalesce the progressive saves: json.dumps(doc) per granule is O(N^2)
            STREAM_SAVE_MIN_S = 1.0                   # persist the growing preview at most ~1/s; finalize always saves

            def _flush_pending_locked():
                """Append every buffered partial now that z0 is known. Caller holds stream_lock; does not save."""
                for mission, batches in stream_pending.items():
                    if mission in finalized:
                        batches.clear(); continue
                    for pts in batches:
                        scene.append_partial(doc, mission, pts)
                    batches.clear()

            def _count_granule_locked(mission, granule):
                """One streamed granule -> one unit of progress. Caller holds stream_lock.

                Counts DISTINCT granule names: a granule can emit more than once (the lake read streams in cell
                groups all labelled "lake"), and a bar that runs past its own total is worse than no bar. The name
                set is kept OUT of the doc — the doc is re-shipped to the browser on every poll, and 900-odd granule
                names is a lot of bytes to send to render one integer.
                """
                seen = granules_seen.setdefault(mission, set())
                if granule:
                    seen.add(granule)
                pr = doc.setdefault("progress", {}).setdefault(mission, {})
                pr["done"] = min(len(seen), pr.get("total") or len(seen))
                pr["points"] = int((doc.get("series", {}).get(mission, {}) or {}).get("n") or 0)

            def _on_plan(mission):
                """Record the leg's planned work once, before any network, so the UI has a denominator.

                A progress bar needs one and nothing else on this path knows it: the point cloud is a poor proxy
                (lake-served data lands all at once) and the job log is prose. `granules` is the unit the fetch
                actually streams, so it is what the bar counts.
                """
                def cb(plan):
                    with stream_lock:
                        pr = doc.setdefault("progress", {}).setdefault(mission, {})
                        pr.update({"total": int(plan.get("granules") or 0), "done": 0,
                                   "cached_chunks": int(plan.get("cached") or 0),
                                   "fetch_chunks": int(plan.get("chunks") or 0),
                                   "phase": "reading cache" if not plan.get("granules") else "fetching"})
                        cache.save_scene(sid, doc)
                return cb

            def _on_granule(mission):
                def cb(pts):
                    if poly is not None and pts["lon"].size:   # trim to the exact drawn shape, like the final read does
                        keep = geom.points_in_polygon(pts["lon"], pts["lat"], poly)
                        pts = {**pts, "lon": pts["lon"][keep], "lat": pts["lat"][keep],
                               "h": pts["h"][keep], "t": pts["t"][keep]}
                    with stream_lock:
                        if mission in finalized:               # authoritative series already in place: drop the preview
                            return
                        if doc.get("z0") is None:              # no z0 yet: buffer; the DEM/first collection flushes it
                            stream_pending.setdefault(mission, []).append(pts)
                            _count_granule_locked(mission, pts.get("granule")); return
                        _flush_pending_locked()                # drain anything buffered before z0, then this granule
                        scene.append_partial(doc, mission, pts)
                        _count_granule_locked(mission, pts.get("granule"))
                        now = time.time()                      # coalesce saves: re-dumping the whole doc every granule is O(N^2)
                        if now - last_stream_save[0] >= STREAM_SAVE_MIN_S:
                            cache.save_scene(sid, doc); last_stream_save[0] = now
                return cb

            def _imagery_worker():
                """Fetch the imagery base layer and attach it to the doc — entirely off the build's critical path.

                It runs on its own daemon thread, NOT in the collection ThreadPoolExecutor: exiting that pool's `with`
                block waits on every future it holds, so submitting imagery there would block the build on it no matter
                what we did afterwards. Imagery is a base layer the scene is fully usable without; a slow tile source
                must not keep the scene in 'loading' long after the points have painted."""
                from . import imagery
                try:
                    # ALL network happens here, OUTSIDE stream_lock. Do not call scene.add_imagery under the lock: it
                    # re-enters imagery.build, and even on a warm cache that repeats the S2 STAC search (60 s timeout)
                    # before falling back — while the build thread is blocked on the same lock for its final save, so
                    # the job never reaches "ready" and the scene shows "Streaming data…" forever with data on screen.
                    meta = imagery.build(frame, extent, 4096, source=imagery_source)
                    with stream_lock:                # doc is shared with the streaming callbacks — serialise mutation
                        doc["imagery"] = {**meta, "url": f"/api/scene/{sid}/imagery.jpg"}   # mirrors scene.add_imagery
                        # log BEFORE publishing the status: imagery_status leaving "pending" is the signal that this
                        # leg is fully done (the widget and the tests both wait on it), so nothing may follow it.
                        log_fn(f"imagery: {meta.get('source','?')} · {meta['width']}x{meta['height']} at z{meta['zoom']}")
                        doc["imagery_status"] = "ready"
                        cache.save_scene(sid, doc)
                except Exception as e:
                    log.warning("imagery unavailable: %s", e)
                    with stream_lock:
                        log_fn(f"imagery unavailable: {e}")
                        doc["imagery_status"] = "unavailable"
                        cache.save_scene(sid, doc)

            def _prefetch_dem():                     # fetch the DEM grid (z0=0 -> raw ellipsoidal heights); warms the
                from . import dem                    # tile npz AND gives us the median for z0 (real z0 applied later)
                return dem.surface_for_frame(frame, extent, 0.0)

            # t=0: every independent leg starts at once. Extracts are I/O-bound (requests/DuckDB/rasterio release the
            # GIL; ATL03 spawns its own ProcessPoolExecutor internally, fine on a thread). Imagery & DEM depend only on
            # frame+extent, so they run without waiting on the z0 barrier. All `doc` mutation stays on this thread.
            imagery_thread = None
            with ThreadPoolExecutor(max_workers=min(8, len(enabled) + 2), thread_name_prefix=f"build-{sid}") as ex:
                cfuts = {leg[0]: ex.submit(leg[2]) for leg in enabled}
                if with_atl03:
                    log_fn(f"ATL03: planner over {bb}" + (f" (polygon, {len(poly)} vertices)" if poly else ""))
                if with_imagery:   # own thread, never the pool (the pool's exit would wait on it) — see _imagery_worker
                    doc["imagery_status"] = "pending"
                    imagery_thread = threading.Thread(target=_imagery_worker, name=f"imagery-{sid}", daemon=True)
                    imagery_thread.start()
                dem_fut = ex.submit(_prefetch_dem)

                # z0 + DEM surface from the DEM, up front: terrain-centred z0 (deterministic, independent of the
                # collections, so a slow GLAS no longer blocks the rest) AND paint the DEM mesh FIRST so the terrain
                # shell shows immediately and the collections then rain in on top. Falls back to the first collection's
                # median (and no surface) if no DEM covers the scene.
                try:
                    dem_raw = dem_fut.result()
                    if dem_raw and dem_raw.get("z"):
                        zv = np.asarray(dem_raw["z"], dtype="f8"); zv = zv[np.isfinite(zv)]
                        if zv.size:
                            with stream_lock:                      # z0 gates baking; set it, paint terrain, then drain
                                doc["z0"] = float(np.median(zv))    # any partials that streamed before the DEM resolved
                                scene.set_surface(doc)              # attach the DEM mesh NOW -> terrain paints first
                                _flush_pending_locked()
                                cache.save_scene(sid, doc)
                            log_fn(f"z0 from DEM: {doc['z0']:.1f} m ellipsoidal")
                            log_fn("surface: DEM base surface")
                except Exception as e:
                    log.info("DEM z0/surface unavailable; z0 will come from the first collection to arrive: %s", e)

                # Integrate each collection AS IT COMPLETES (not in priority order): the fastest paints first so the
                # scene streams. z0 is already set from the DEM above, so add_series just uses it (no collection sets
                # it unless the DEM was absent). Each integrated series is persisted immediately -> paintable mid-build.
                leg_by_fut = {cfuts[leg[0]]: (leg[0], leg[4], leg[3]) for leg in enabled}   # future -> (mission, display, integrator)
                # The doc's progress note is truncated for the UI payload; keep the FULL text too, because the
                # actionable half of a coverage refusal (the fix, and the why_not_covered command) lives past 160
                # characters and was being cut off exactly when it was needed.
                leg_errors: dict[str, str] = {}
                for fut in as_completed(leg_by_fut):
                    mkey, disp, integrator = leg_by_fut[fut]
                    try:
                        a, m, ck = fut.result()
                        with stream_lock:            # serialise vs still-streaming granules of the OTHER missions
                            integrator(a, m, ck)     # add_series over the authoritative arrays REPLACES the preview
                            finalized.add(mkey); stream_pending.pop(mkey, None)
                            if doc.get("z0") is not None:
                                _flush_pending_locked()   # z0 may have just been set here (no-DEM case): drain buffers
                            registry_upsert(sid, series=sorted(doc["series"]))
                            cache.save_scene(sid, doc)    # progressive persistence: this series is now paintable
                    except Exception as e:
                        # exc_info: a leg's traceback is the only record of WHY it failed, and discarding it has now
                        # twice sent a diagnosis down the wrong path — "GLAS unavailable: ..." reads like missing
                        # data whether the cause is an empty region or a TypeError on the first line.
                        log.warning("%s unavailable: %s: %s", disp, type(e).__name__, e, exc_info=True)
                        log_fn(f"{disp} unavailable: {type(e).__name__}: {e}")
                        leg_errors[mkey] = f"{type(e).__name__}: {e}"
                        with stream_lock:   # a leg that never lands must stop showing as in-flight
                            doc.setdefault("progress", {}).setdefault(mkey, {}).update(
                                {"phase": "unavailable", "note": f"{type(e).__name__}: {e}"[:160]})
                            # Its streamed preview is still in the doc and would render as if it were the mission's
                            # real data — a partial GLAS cloud looks like sparse coverage, not a failed leg. Mark it
                            # so the UI can say so; the points are real, the SERIES is not complete.
                            ser = (doc.get("series") or {}).get(mkey)
                            if ser is not None:
                                ser.setdefault("meta", {})["failed"] = f"{type(e).__name__}: {e}"[:160]
                        continue

                if not doc["series"]:
                    # Every leg's own reason is already in doc["progress"][m]["note"]. Reporting them beats the
                    # old blanket "check your selection and the token", which named neither the cause nor a fix
                    # and sent at least one diagnosis toward auth when the truth was a coverage-gate refusal.
                    # Preferred order first, then any mission it does not name: a mission left out of a hand-written
                    # tuple here used to have its error dropped, and the message fell through to "not attempted".
                    pref = ("ATL06", "ICESAT2", "GLAS", "ICESSN", "GEDI")
                    order = sorted(leg_errors, key=lambda m: pref.index(m) if m in pref else len(pref))
                    detail = ("\n\n" + "\n\n".join(f"{m}: {leg_errors[m]}" for m in order)) if order else \
                        " No collection was even attempted — check the selection and the Earthdata token."
                    raise RuntimeError("no collection returned data over this area." + detail)
                # streaming used arrival order; normalise the final series-dict to the canonical priority order
                doc["series"] = {m: doc["series"][m] for m in (leg[0] for leg in LEGS) if m in doc["series"]}
                # Disk-budget eviction is pure housekeeping — the scene is already built, saved and streaming. Run it
                # OFF the build path (background daemon) and ONLY when this build actually materialized new chunks, so
                # footer-scanning never delays the response and idle/cache-hit builds skip it entirely. The synchronous
                # hard trigger stays on the UI's lake_settings (lowering the limit evicts immediately).
                if lake_grew["v"]:
                    threading.Thread(target=_enforce_lake_limit, args=(bb, poly, log_fn),
                                     name=f"lake-evict-{sid}", daemon=True).start()

                # surface fallback: the DEM normally set z0+surface up front; only reach here if z0 came from a
                # collection instead (DEM gave no z0). If no DEM covers the scene, set_surface attaches nothing.
                if doc.get("surface") is None and doc.get("z0") is not None:
                    try:
                        scene.set_surface(doc)
                        if doc.get("surface"):
                            log_fn("surface: DEM base surface"); cache.save_scene(sid, doc)
                    except Exception as e:
                        log.info("surface unavailable: %s", e)

                # Persist the finished scene: the canonical series order set above, plus any surface. This save used to
                # be a side effect of the (now backgrounded) imagery finalize; it has to be explicit, or the doc on disk
                # keeps the arrival order the streaming path wrote. Locked because the imagery thread shares the doc.
                with stream_lock:
                    cache.save_scene(sid, doc)
                # (imagery finishes on its own thread — see _imagery_worker; the build never waits on it)
        can_coreg = "ICESAT2" in doc["series"] and "GLAS" in doc["series"]
        if with_coreg and can_coreg:
            coregister(sid)
            log_fn("co-registration computed and cached")
        registry_upsert(sid, status="ready", series=sorted(doc["series"]), coreg=bool(with_coreg and can_coreg))
    except Exception:
        registry_upsert(sid, status="error")
        raise
    if wait_for_imagery and imagery_thread is not None:
        # Bounded: the point is that the caller can safely exit, not that imagery is guaranteed. A tile source that
        # hangs past the timeout leaves the scene exactly as the default path would -- status "pending", no imagery.
        imagery_thread.join(timeout=IMAGERY_JOIN_TIMEOUT_S)
        if imagery_thread.is_alive():
            log.warning("imagery still running after %.0fs; returning the scene without it", IMAGERY_JOIN_TIMEOUT_S)
    return cache.load_scene(sid)


# build_scene's own defaults for the collection flags, read once from the real function, so start_job can pass every
# collection's flag through without listing them (and a stubbed build_scene in a test does not change them).
_FLAG_DEFAULTS = {n: p.default for n, p in inspect.signature(build_scene).parameters.items() if n.startswith("with_")}


def start_job(params: dict, kind: str = "scene") -> dict:
    """Run a build in a background thread; kind = 'scene' (area -> scene) or 'cells' (materialize H3 cells)."""
    jid = uuid.uuid4().hex[:8]
    sid = uuid.uuid4().hex[:10] if kind == "scene" else None
    job = _jobs[jid] = {"id": jid, "kind": kind, "status": "running", "log": [], "scene_id": sid, "widget_url": None, "error": None,
                        "started": time.time(), "params": {k: v for k, v in params.items() if k != "question"}}

    def run():
        try:
            if kind == "scene":
                # One flag per collection, defaulting to build_scene's own default, so a new collection is passed
                # through without being listed here.
                flags = {c["flag"]: bool(params.get(c["flag"], _FLAG_DEFAULTS[c["flag"]])) for c in coverage.collections()}
                doc = build_scene(params.get("bbox"), params.get("polygon"), params.get("question"),
                                  with_coreg=bool(params.get("with_coreg", False)),
                                  with_imagery=bool(params.get("with_imagery", True)), imagery_source=params.get("imagery_source"),
                                  log_fn=lambda m: job["log"].append(m), scene_id=sid, **flags)
                job.update(status="done", widget_url=_widget_url(doc["scene_id"]))
            else:
                from . import planner
                cells = [int(c) for c in params["cells"]]
                job["log"].append(f"materializing {len(cells)} cells")
                with _lock:
                    out = planner.ensure_cells(cells, tuple(params.get("window") or regions.DEFAULT_ATL03_WINDOW))
                st = out["stats"]
                job["log"].append(f"{st['chunks_fetched']} chunks fetched ({st['bytes'] / 1e6:.0f} MB), {st['chunks_skipped_already_materialized']} already present, "
                                  f"{st['cell_files_written']} cell files written" + (f"; evicted {len(st['evicted_for_limit'])} cells for the limit" if st.get("evicted_for_limit") else ""))
                job.update(status="done", result=st)
        except Exception as e:
            log.exception("job failed")
            job.update(status="error", error=f"{type(e).__name__}: {e}")
            job["log"].append(traceback.format_exc().splitlines()[-1])
        job["seconds"] = round(time.time() - job["started"], 1)

    threading.Thread(target=run, daemon=True, name=f"job-{jid}").start()
    return job


_widget_url = lambda sid: f"/?scene={sid}"  # server.py replaces with the absolute URL


def job(jid: str) -> dict | None:
    return _jobs.get(jid)


def jobs(n: int = 20) -> list[dict]:
    return sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)[:n]


# ----------------------------------------------------------------------------- co-registration
def coregister(scene_id: str, common_epoch: float | None = None, colocation_radius_m: float | None = None,
               exaggeration: float | None = None) -> dict:
    from . import coreg

    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    kw = {k: v for k, v in dict(common_epoch=common_epoch, colocation_radius_m=colocation_radius_m, exaggeration=exaggeration).items() if v is not None}
    with _lock:
        # recompute if params changed OR the saved result predates a schema addition (e.g. the GIA block)
        if doc.get("coreg") and doc["coreg"].get("params") == coreg.params(**kw) and "gia" in doc["coreg"]:
            out = dict(doc["coreg"]); out["cached"] = True
            return out
        t0 = time.time()
        result = coreg.coregister_scene(doc, **kw)
        result["compute_seconds"] = round(time.time() - t0, 2)
        result["cached"] = False
        doc["coreg"] = result
        cache.save_scene(scene_id, doc)
    registry_upsert(scene_id, coreg=True)
    return result


_INDEX_CACHE: dict = {}   # collection -> {"seen": {name: mtime}, "cells": set()} incremental cache for the live view
# One scan per collection at a time. The Lake view polls all 4 collections concurrently every 8 s, and the first scan
# after a restart reads EVERY index parquet (thousands for ATL06) — far longer than the poll interval, so requests used
# to stack up, each redoing the same scan and holding the GIL against whatever else the server is doing (a scene build,
# notably). Serialising per collection means concurrent callers wait for one scan instead of multiplying it.
_INDEX_LOCKS: dict = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock(collection: str) -> threading.Lock:
    with _INDEX_LOCKS_GUARD:
        return _INDEX_LOCKS.setdefault(collection, threading.Lock())


def _index_source(collection: str):
    """(index_dir, res) for a collection's sub-granule H3 index, or (None, None) if it has none yet. Accepts the
    mission name ICESAT2 for ATL03. One switch, coverage._index_for, rather than a second copy of it."""
    from . import coverage
    d, res, _ym = coverage._index_for("ATL03" if collection == "ICESAT2" else collection)
    return d, res


def index_status(collection: str = "ATL06") -> dict:
    """Indexed H3 cells + granule count for a collection's sub-granule index (drives the Data Lake index view).
    Incremental: only newly-written parquets are read each call, so it stays cheap as the index grows."""
    d, res = _index_source(collection)
    if d is None:
        return {"collection": collection, "indexed": False, "res": None, "granules": 0, "cells": []}
    with _index_lock(collection):   # one scan per collection; concurrent pollers wait rather than duplicating it
        return _index_status_locked(collection, d, res)


def _index_status_locked(collection: str, d, res) -> dict:
    import h3

    from . import coverage

    c = _INDEX_CACHE.setdefault(collection, {})
    # Gate the whole thing on ONE stat of the directory. A directory's mtime changes whenever an entry is added or
    # replaced, and index files are written tmp-then-rename (see build_atl06_index), so a rename always trips it.
    try:
        dir_mt = d.stat().st_mtime_ns if d.exists() else None
    except OSError:
        dir_mt = None
    if dir_mt is not None and c.get("dir_mt") == dir_mt and c.get("out") is not None:
        return c["out"]
    # Per-cell coverage comes from the rolled-up manifest, never from the per-granule index parquets. Opening those
    # cost 43.5 s for ATL06 on the deployed box (33,064 files) against 0.35 s here, for an identical cell set — and
    # it was most of why the Data Lake page took ~30 s on its first load after a restart. `granules` still comes
    # from the directory, which is a readdir (no per-file stat) and is exact: the manifest can lag by a few granules
    # between builds, and this number drives the "index build N% done" readout.
    cov = coverage.cell_coverage(collection) or {}
    cells, span_max = [], 0.0
    for cell, (g, e, ym0, ym1) in cov.items():
        sp = coverage.span_years(ym0, ym1)
        span_max = max(span_max, sp)
        cells.append({"h": h3.int_to_str(cell), "g": g, "e": e, "sp": sp,
                      "y0": int(ym0[:4]) if ym0 else 0, "y1": int(ym1[:4]) if ym1 else 0})
    granules = sum(1 for _ in d.glob("*.parquet")) if d.exists() else 0
    target = None
    mf = d / "_build.json"
    if mf.exists():
        try:
            target = int(json.loads(mf.read_text()).get("target"))
        except Exception:
            target = None
    pct = (min(100, round(100 * granules / target)) if target else None)
    out = {"collection": collection, "indexed": True, "res": res, "granules": granules,
           "target": target, "pct": pct, "span_max": round(span_max, 1), "cells": cells}
    c["dir_mt"], c["out"] = dir_mt, out
    return out


# Memoised candidate searches. The compute is proportional to the SCENE, not the request: every call reloads each
# mission's npz and re-runs pyproj over every point (measured 0.99 s then 0.94 s back-to-back on a 264k-point scene,
# and it grows from there). Two callers make that bite -- the UI re-fires the search on every h3_res / delta_t /
# reference-mission nudge, and the MCP flow is inherently multi-call (list the candidates, then fetch one cell's
# series). Both were paying the full load twice for a byte-identical answer.
#
# Keyed on the scene file's mtime+size like _scene_for_read, so a build writing progressive updates invalidates the
# entry instead of being served a stale one. Results measure 35-160 KB, so four of them is under a megabyte.
_CAND_MEMO: dict = {}
_CAND_MEMO_MAX = 4
_CAND_MEMO_LOCK = threading.Lock()


def _cand_key(scene_id: str, h3_res, delta_t, ref_missions, min_bins) -> tuple:
    p = cache.SCENE_DIR / f"{scene_id}.json"
    st = p.stat()          # OSError here means no such scene; the caller turns it into KeyError
    # ref_missions is order-insensitive downstream (_reference_set makes it a set), so normalise it here too --
    # otherwise ["GLAS","ATL06"] and ["ATL06","GLAS"] split the memo and recompute the same answer.
    return (scene_id, st.st_mtime_ns, st.st_size, int(h3_res), float(delta_t),
            tuple(sorted(ref_missions)) if ref_missions else None, int(min_bins))


def _cand_memo_get(key):
    with _CAND_MEMO_LOCK:
        return _CAND_MEMO.get(key)


def _cand_memo_put(key, val) -> None:
    with _CAND_MEMO_LOCK:
        _CAND_MEMO[key] = val
        while len(_CAND_MEMO) > _CAND_MEMO_MAX:
            _CAND_MEMO.pop(next(iter(_CAND_MEMO)))


def scene_candidates(scene_id: str, h3_res: int = 9, delta_t: float = 1.0, ref_missions=None, min_bins: int = 3) -> dict:
    """Candidate coincident-observation cells + their elevation time series for a built scene.

    The result is every qualifying cell, ranked -- never truncated (see tests/test_no_caps.py). Callers that need a
    short list slim the RESPONSE and say how many they dropped; they do not cap the search."""
    from . import timeseries
    try:
        key = _cand_key(scene_id, h3_res, delta_t, ref_missions, min_bins)
    except OSError:
        raise KeyError(scene_id) from None
    hit = _cand_memo_get(key)               # outside _lock: a memo hit must not queue behind a running build
    if hit is not None:
        return hit
    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    with _lock:
        hit = _cand_memo_get(key)           # another caller may have computed it while we waited for the lock
        if hit is not None:
            return hit
        out = timeseries.candidates(doc, h3_res=int(h3_res), delta_t=float(delta_t),
                                    ref_missions=ref_missions, min_bins=int(min_bins))
    _cand_memo_put(key, out)
    return out


# --- model-facing projection of the search -------------------------------------------------------------------
# A caller with a context window cannot take the full result: it carries every cell's time series plus the hex
# boundary in scene-local metres (42 KB / 36 cells on one real scene, 159 KB / 133 on another). The viewer wants all
# of that; a model wants the ranked verdict and then ONE cell's series. So the response is slimmed here, at the
# transport edge -- the search itself is never capped. A result cap used to live in the search and silently dropped
# the cells past it; tests/test_no_caps.py greps this package to keep it from coming back, which is why the retired
# parameter is not named here. Anything the RESPONSE drops is accounted for by n_candidates_total.
_SUMMARY_FIELDS = ("h3", "lat", "lon", "level", "confidence", "n_bins", "span_years",
                   "slope_deg", "n_points", "n_ref", "trend_cm_yr", "why")
_CELL_DROP = ("xy", "center")     # scene-local render geometry: for the deck.gl layer, meaningless to a reader


def _ranked(out: dict) -> list[dict]:
    """The search result is already sorted by (confidence, n_bins, span_years). Number it so a caller can say
    "the third one" and mean something stable."""
    return [dict(c, rank=i + 1) for i, c in enumerate(out["candidates"])]


def timeseries_candidates(scene_id: str, h3_res: int = 9, delta_t: float = 1.0, ref_missions=None,
                          min_bins: int = 3, limit: int | None = 10) -> dict:
    """Ranked candidate cells for a time series, summarised: one row per cell, no series and no geometry.

    limit=None returns every cell (large). n_candidates_total is always the true count, so a truncated answer
    still says how much it left behind."""
    out = scene_candidates(scene_id, h3_res=h3_res, delta_t=delta_t, ref_missions=ref_missions, min_bins=min_bins)
    ranked = _ranked(out)
    rows = ranked if limit is None else ranked[: max(1, int(limit))]
    return {"scene_id": scene_id, "n_candidates_total": len(ranked), "returned": len(rows),
            "params": out["params"], "candidates": [{"rank": c["rank"], **{k: c[k] for k in _SUMMARY_FIELDS}} for c in rows]}


def timeseries_cell(scene_id: str, h3: str, h3_res: int = 9, delta_t: float = 1.0, ref_missions=None,
                    min_bins: int = 3) -> dict:
    """One cell's full record: the series, the confidence breakdown, the trend.

    The search parameters are required because a cell id only exists under the parameters that produced it -- a
    res-9 cell is not in a res-10 search. Repeating them is free: scene_candidates memoises."""
    out = scene_candidates(scene_id, h3_res=h3_res, delta_t=delta_t, ref_missions=ref_missions, min_bins=min_bins)
    for c in _ranked(out):
        if c["h3"] == h3:
            return {"scene_id": scene_id, "params": out["params"],
                    **{k: v for k, v in c.items() if k not in _CELL_DROP}}
    raise ValueError(
        f"cell {h3} is not among the {len(out['candidates'])} candidates for scene {scene_id} at "
        f"h3_res={h3_res}, delta_t={delta_t}, min_bins={min_bins}, ref_missions={out['params']['ref_missions']}. "
        "A cell id is only valid for the search that produced it — re-run the search with these parameters, or "
        "use the parameters the cell came from.")


# ----------------------------------------------------------------------------- lake
def lake_cells(stats: bool = True, mission: str = "ICESAT2") -> dict:
    """Materialized H3 cells as GeoJSON; with per-cell stats in properties when stats=True."""
    import h3
    from . import lake

    st = lake.cell_stats(mission) if stats else {}
    cells = set(st) if stats else {int(p.name.split("=")[1]) for p in lake.LAKE_DIR.glob(f"mission={mission}/h3_cell=*")} if lake.LAKE_DIR.exists() else set()
    feats = []
    for c in cells:
        ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(h3.int_to_str(int(c)))]
        props = {"cell": str(c), **({k: v for k, v in st[c].items() if k != "cell"} if stats and c in st else {})}
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}})
    return {"type": "FeatureCollection", "features": feats}


def lake_summary(mission: str = "ICESAT2") -> dict:
    from . import lake
    return lake.lake_summary(mission)


def lake_log(after: int = 0) -> dict:
    """Recent pipeline activity for the Lake page's running log (entries with seq > after)."""
    from . import logbuf
    logbuf.install()
    return logbuf.entries(int(after))


def lake_settings(max_bytes: int | None = None) -> dict:
    from . import lake
    if max_bytes is not None:
        lake.set_settings(max_bytes=int(max_bytes))
        evicted = lake.enforce_global_limit()   # the one budget governs every collection together
        return {**lake.get_settings(), "evicted": evicted}
    return lake.get_settings()


def lake_evict(cells: list) -> dict:
    from . import lake
    return {"evicted": lake.evict_cells([int(c) for c in cells])}


def lake_load(cells: list, window=None) -> dict:
    return start_job({"cells": [int(c) for c in cells], "window": window}, kind="cells")


# ----------------------------------------------------------------------------- misc
def scene_add_imagery(scene_id: str, source: str = "s2") -> dict:
    """Re-fetch the satellite-imagery base layer for an existing scene with a different source (see imagery.SOURCES:
    "s2" = in-region Sentinel-2 L2A, "eox" = EOX cloudless), re-save the scene, and return the new imagery meta
    (minus the on-disk path). Drives the scene page's imagery source selector."""
    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    scene.add_imagery(doc, source=source)
    cache.save_scene(scene_id, doc)
    return {k: v for k, v in doc["imagery"].items() if k != "path"}


def list_regions() -> dict:
    return {k: {"bbox": list(v["bbox"]), "note": v["note"]} for k, v in regions.REGIONS.items()}


def check_coverage(bbox=None, polygon=None, **_ignored) -> dict:
    bb, _ = geom.normalize_area(bbox, polygon)
    return coverage.check_coverage(bb)


def list_collections() -> list[dict]:
    return coverage.collections()


def bench() -> dict | None:
    bp = cache.DATA_DIR / "bench" / "results.json"
    return json.loads(bp.read_text()) if bp.exists() else None
