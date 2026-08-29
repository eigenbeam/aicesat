"""Transport-neutral API: every UI/agent operation as a plain function returning JSON-able dicts.

server.py exposes these twice — as HTTP routes (the localhost widget, "fetch adapter") and as MCP tools (model-visible
and app-visible, the MCP Apps "app adapter"). Nothing here knows which transport called it.
"""
from __future__ import annotations

import base64
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


def scene_part(scene_id: str, part: str = "meta", chunk: int = 0, chunk_bytes: int = 96_000, stride: int = 1) -> dict:
    """Chunked access for hosts with small result limits. parts: meta | surface | imagery | coreg | positions:<MISSION>
    (base64 float32 xyz, chunked) | dh (histogram data)."""
    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    if part == "meta":
        # Everything EXCEPT the bulk arrays (positions, slopes) and surface z — those are fetched via their own chunked
        # parts and appended incrementally, so this stays small and is safe to poll repeatedly during a build. `has_slopes`
        # tells the client whether to fetch a slopes:<mission> part (ICESSN platelets).
        def _series_meta(s):
            m = {k: v for k, v in s.items() if k not in ("positions", "slopes")}
            m["has_slopes"] = "slopes" in s
            return m
        return {"scene_id": scene_id, "question": doc.get("question"), "frame": doc["frame"], "bbox": doc["bbox"], "polygon": doc.get("polygon"),
                "z0": doc["z0"], "labels": doc.get("labels"), "imagery_status": doc.get("imagery_status"),
                "imagery": ({k: v for k, v in doc["imagery"].items() if k != "path"} if doc.get("imagery") else None),
                "series": {m: _series_meta(s) for m, s in doc["series"].items()},
                "has_coreg": bool(doc.get("coreg")), "surface": ({k: v for k, v in doc["surface"].items() if k != "z"} if doc.get("surface") else None)}
    if part == "surface":
        return _chunked(np.asarray([np.nan if v is None else v for v in doc["surface"]["z"]], dtype="f4"), chunk, chunk_bytes, "z")
    if part.startswith("positions:"):
        m = part.split(":", 1)[1]
        arr = np.asarray(doc["series"][m]["positions"], dtype="f4").reshape(-1, 3)[:: max(1, int(stride))]
        return _chunked(arr.ravel(), chunk, chunk_bytes, "xyz")
    if part.startswith("slopes:"):
        m = part.split(":", 1)[1]
        # ICESSN platelet slopes [sn, we, ...] — 2 per point; strided in lock-step with positions so they stay aligned.
        arr = np.asarray(doc["series"][m].get("slopes", []), dtype="f4").reshape(-1, 2)[:: max(1, int(stride))]
        return _chunked(arr.ravel(), chunk, chunk_bytes, "sw")
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


def _chunked(arr: np.ndarray, chunk: int, chunk_bytes: int, name: str) -> dict:
    raw = arr.tobytes()
    n_chunks = max(1, -(-len(raw) // chunk_bytes))
    piece = raw[chunk * chunk_bytes:(chunk + 1) * chunk_bytes]
    return {"name": name, "dtype": "float32", "n_values": int(arr.size), "chunk": chunk, "n_chunks": n_chunks,
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
    from . import index, index_atl06, index_glas, index_icessn, lake, planner
    try:
        protect = set()
        for res in {index.H3_RES, index_atl06.ATL06_RES, index_glas.GLAS_RES, index_icessn.ICESSN_RES}:
            protect |= set(planner.cells_for_bbox(bb, res=res, polygon=poly))
        evicted = lake.enforce_global_limit(protect=protect, reason="limit (scene build)")
        if evicted:
            log_fn(f"storage limit: evicted {len(evicted)} cells to stay under the lake budget")
        return evicted
    except Exception as e:
        log.warning("lake limit enforcement failed: %s", e)
        return []


def build_scene(bbox=None, polygon=None, question=None, max_granules=8, with_glas=True, with_coreg=False,
                with_atl06=False, with_icessn=False, with_atl03=False, with_imagery=True, imagery_source=None,
                log_fn=lambda m: None, scene_id: str | None = None) -> dict:
    """Full pipeline for an area: any subset of the collections (GLAS, IceBridge ICESSN, ATL06, ATL03 photons),
    plus a DEM surface, imagery, and — when both ATL03 and GLAS are present — co-registration. Every collection is
    optional and non-fatal: a miss over the area is logged and the scene still builds from whatever is available.
    Returns the scene doc. (ATL03 is heavy and off by default; co-registration currently needs it.)"""
    bb, poly = geom.normalize_area(bbox, polygon)
    sid = scene_id or uuid.uuid4().hex[:10]
    registry_upsert(sid, question=question, bbox=list(bb), polygon=poly, status="loading", series=[])

    lake_grew = {"v": False}   # set when any leg actually fetched+materialized new chunks -> eviction worth running

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
        a, m = glas.extract(bb, regions.DEFAULT_GLAS_WINDOW, polygon=poly, on_granule=_on_granule("GLAS"))
        return a, m, m["cache_key"]

    def _ex_icessn():
        from . import icessn
        a, m = icessn.extract(bb, regions.DEFAULT_ICESSN_WINDOW, polygon=poly, on_granule=_on_granule("ICESSN"))
        return a, m, m["cache_key"]

    def _ex_atl06():
        from . import atl06
        a, m = atl06.extract(bb, regions.DEFAULT_ATL06_WINDOW, polygon=poly, on_granule=_on_granule("ATL06"))
        return a, m, m["cache_key"]

    def _ex_atl03():
        a, m = atl03.extract(bb, regions.DEFAULT_ATL03_WINDOW, max_granules=max_granules, polygon=poly)
        return a, m, m["cache_key"]

    # --- integrators: mutate `doc`; ONLY ever called on the build thread, in priority order, so z0 and the
    #     series-insertion order are byte-for-byte what the old serial loop produced. -------------------------------
    def _int_glas(a, m, ck):
        scene.add_series(doc, "GLAS", a, m, ck)
        log_fn(f"GLAS: {m['n']:,} shots across {len(m['campaigns'])} campaigns")
        _log_cache("GLAS", m)

    def _int_icessn(a, m, ck):
        scene.add_series(doc, "ICESSN", a, m, ck)
        log_fn(f"ICESSN: {m['n']:,} nadir platelets across {len(m['years'])} campaign years")
        _log_cache("ICESSN", m)

    def _int_atl06(a, m, ck):
        scene.add_series(doc, "ATL06", a, m, ck)
        log_fn(f"ATL06: {m['n']:,} land-ice segments")
        _log_cache("ATL06", m)

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
        ("ICESAT2", with_atl03,  _ex_atl03,  _int_atl03,  "ATL03"),
    ]
    enabled = [leg for leg in LEGS if leg[1]]

    try:
        with _lock:
            doc = scene.new_scene(sid, bb, question, polygon=poly)
            cache.save_scene(sid, doc)               # persist the shell (frame/bbox) immediately -> UI opens instantly

            frame = doc["frame"]
            extent = scene.bbox_extent(frame)        # computed once here (the shared _tr transformer is build-thread only)

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
                            stream_pending.setdefault(mission, []).append(pts); return
                        _flush_pending_locked()                # drain anything buffered before z0, then this granule
                        scene.append_partial(doc, mission, pts)
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
                    imagery.build(frame, extent, 4096, source=imagery_source)   # warms the cache add_imagery then hits
                    with stream_lock:                # doc is shared with the streaming callbacks — serialise mutation
                        scene.add_imagery(doc, source=imagery_source)
                        # log BEFORE publishing the status: imagery_status leaving "pending" is the signal that this
                        # leg is fully done (the widget and the tests both wait on it), so nothing may follow it.
                        log_fn(f"imagery: {doc['imagery'].get('source','?')} · {doc['imagery']['width']}x{doc['imagery']['height']} at z{doc['imagery']['zoom']}")
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
            with ThreadPoolExecutor(max_workers=min(8, len(enabled) + 2), thread_name_prefix=f"build-{sid}") as ex:
                cfuts = {leg[0]: ex.submit(leg[2]) for leg in enabled}
                if with_atl03:
                    log_fn(f"ATL03: planner over {bb}" + (f" (polygon, {len(poly)} vertices)" if poly else ""))
                if with_imagery:   # own thread, never the pool (the pool's exit would wait on it) — see _imagery_worker
                    doc["imagery_status"] = "pending"
                    threading.Thread(target=_imagery_worker, name=f"imagery-{sid}", daemon=True).start()
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
                        log.warning("%s unavailable: %s", disp, e); log_fn(f"{disp} unavailable: {e}")
                        continue

                if not doc["series"]:
                    raise RuntimeError("no collection returned data over this area (check your selection and the token)")
                # streaming used arrival order; normalise the final series-dict to the canonical priority order
                doc["series"] = {m: doc["series"][m] for m in ("GLAS", "ICESSN", "ATL06", "ICESAT2") if m in doc["series"]}
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
    return cache.load_scene(sid)


def start_job(params: dict, kind: str = "scene") -> dict:
    """Run a build in a background thread; kind = 'scene' (area -> scene) or 'cells' (materialize H3 cells)."""
    jid = uuid.uuid4().hex[:8]
    sid = uuid.uuid4().hex[:10] if kind == "scene" else None
    job = _jobs[jid] = {"id": jid, "kind": kind, "status": "running", "log": [], "scene_id": sid, "widget_url": None, "error": None,
                        "started": time.time(), "params": {k: v for k, v in params.items() if k != "question"}}

    def run():
        try:
            if kind == "scene":
                doc = build_scene(params.get("bbox"), params.get("polygon"), params.get("question"), int(params.get("max_granules", 8)),
                                  bool(params.get("with_glas", True)), bool(params.get("with_coreg", False)),
                                  with_atl06=bool(params.get("with_atl06", False)), with_icessn=bool(params.get("with_icessn", False)),
                                  with_atl03=bool(params.get("with_atl03", False)),
                                  with_imagery=bool(params.get("with_imagery", True)), imagery_source=params.get("imagery_source"),
                                  log_fn=lambda m: job["log"].append(m), scene_id=sid)
                job.update(status="done", widget_url=_widget_url(doc["scene_id"]))
            else:
                from . import planner
                cells = [int(c) for c in params["cells"]]
                job["log"].append(f"materializing {len(cells)} cells")
                with _lock:
                    out = planner.ensure_cells(cells, tuple(params.get("window") or regions.DEFAULT_ATL03_WINDOW),
                                               max_granules=int(params.get("max_granules", 40)))
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
    """(index_dir, res) for a collection's sub-granule H3 index, or (None, None) if it has none yet."""
    from . import index_atl06, index_glas, index_icessn
    from . import index as atl03_index
    if collection == "ATL06":
        return index_atl06._index_dir(index_atl06.ATL06_RES), index_atl06.ATL06_RES
    if collection in ("ICESAT2", "ATL03"):
        return atl03_index.ATL03_INDEX_DIR, atl03_index.H3_RES
    if collection == "GLAS":
        return index_glas._index_dir(index_glas.GLAS_RES), index_glas.GLAS_RES
    if collection == "ICESSN":
        return index_icessn._index_dir(index_icessn.ICESSN_RES), index_icessn.ICESSN_RES
    return None, None


def index_status(collection: str = "ATL06") -> dict:
    """Indexed H3 cells + granule count for a collection's sub-granule index (drives the Data Lake index view).
    Incremental: only newly-written parquets are read each call, so it stays cheap as the index grows."""
    d, res = _index_source(collection)
    if d is None:
        return {"collection": collection, "indexed": False, "res": None, "granules": 0, "cells": []}
    with _index_lock(collection):   # one scan per collection; concurrent pollers wait rather than duplicating it
        return _index_status_locked(collection, d, res)


def _index_status_locked(collection: str, d, res) -> dict:
    import pyarrow.parquet as pq
    import h3

    c = _INDEX_CACHE.setdefault(collection, {"seen": {}, "info": {}})   # info: cell -> [n_granules, {cycles}, yr_min, yr_max]
    for pth in (d.glob("*.parquet") if d.exists() else []):
        try:
            mt = pth.stat().st_mtime
        except OSError:
            continue
        if c["seen"].get(pth.name) == mt:
            continue
        try:
            names = set(pq.read_schema(pth).names)
            cols = ["h3_cell"] + [x for x in ("cycle", "gdate") if x in names]
            t = pq.read_table(pth, columns=cols)
        except Exception:
            continue   # parquet mid-write; skip this poll
        h3c = t["h3_cell"].to_pylist()
        gd = str(t["gdate"][0].as_py()) if "gdate" in cols and len(t) else ""
        try:
            yr = int(gd[:4]) if gd else int(pth.stem[6:10])   # GLAS/ICESSN: gdate column; ATL0x: name YYYYMMDD
        except ValueError:
            yr = 0
        # depth token per granule: ICESat-2 cycle where present, else the year (GLAS/ICESSN have no repeat cycle)
        cy = (int(t["cycle"][0].as_py()) if "cycle" in cols and len(t) else yr)
        for cell in {int(x) for x in h3c}:
            inf = c["info"].setdefault(cell, [0, set(), 9999, 0])
            inf[0] += 1; inf[1].add(cy)
            if yr:
                inf[2] = min(inf[2], yr); inf[3] = max(inf[3], yr)
        c["seen"][pth.name] = mt
    cells = [{"h": h3.int_to_str(cell), "g": inf[0], "c": len(inf[1]),
              "y0": (inf[2] if inf[2] != 9999 else 0), "y1": inf[3]} for cell, inf in c["info"].items()]
    granules = len(c["seen"]); target = None
    mf = d / "_build.json"
    if mf.exists():
        try:
            target = int(json.loads(mf.read_text()).get("target"))
        except Exception:
            target = None
    pct = (min(100, round(100 * granules / target)) if target else None)
    return {"collection": collection, "indexed": True, "res": res, "granules": granules,
            "target": target, "pct": pct, "cells": cells}


def scene_candidates(scene_id: str, h3_res: int = 9, delta_t: float = 1.0, ref_missions=None, min_bins: int = 3) -> dict:
    """Candidate coincident-observation cells + their elevation time series for a built scene."""
    from . import timeseries
    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    with _lock:
        return timeseries.candidates(doc, h3_res=int(h3_res), delta_t=float(delta_t),
                                     ref_missions=ref_missions, min_bins=int(min_bins))


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


def lake_load(cells: list, window=None, max_granules: int = 40) -> dict:
    return start_job({"cells": [int(c) for c in cells], "window": window, "max_granules": max_granules}, kind="cells")


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
