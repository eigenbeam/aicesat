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
from concurrent.futures import ThreadPoolExecutor
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


def scene_part(scene_id: str, part: str = "meta", chunk: int = 0, chunk_bytes: int = 96_000, stride: int = 1) -> dict:
    """Chunked access for hosts with small result limits. parts: meta | surface | imagery | coreg | positions:<MISSION>
    (base64 float32 xyz, chunked) | dh (histogram data)."""
    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    if part == "meta":
        return {"scene_id": scene_id, "question": doc.get("question"), "frame": doc["frame"], "bbox": doc["bbox"], "polygon": doc.get("polygon"),
                "z0": doc["z0"], "labels": doc.get("labels"), "imagery": ({k: v for k, v in doc["imagery"].items() if k != "path"} if doc.get("imagery") else None),
                "series": {m: {k: v for k, v in s.items() if k != "positions"} for m, s in doc["series"].items()},
                "has_coreg": bool(doc.get("coreg")), "surface": ({k: v for k, v in doc["surface"].items() if k != "z"} if doc.get("surface") else None)}
    if part == "surface":
        return _chunked(np.asarray([np.nan if v is None else v for v in doc["surface"]["z"]], dtype="f4"), chunk, chunk_bytes, "z")
    if part.startswith("positions:"):
        m = part.split(":", 1)[1]
        arr = np.asarray(doc["series"][m]["positions"], dtype="f4").reshape(-1, 3)[:: max(1, int(stride))]
        return _chunked(arr.ravel(), chunk, chunk_bytes, "xyz")
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
def build_scene(bbox=None, polygon=None, question=None, max_granules=8, with_glas=True, with_coreg=False,
                with_atl06=False, with_icessn=False, with_atl03=False, log_fn=lambda m: None,
                scene_id: str | None = None) -> dict:
    """Full pipeline for an area: any subset of the collections (GLAS, IceBridge ICESSN, ATL06, ATL03 photons),
    plus a DEM surface, imagery, and — when both ATL03 and GLAS are present — co-registration. Every collection is
    optional and non-fatal: a miss over the area is logged and the scene still builds from whatever is available.
    Returns the scene doc. (ATL03 is heavy and off by default; co-registration currently needs it.)"""
    bb, poly = geom.normalize_area(bbox, polygon)
    sid = scene_id or uuid.uuid4().hex[:10]
    registry_upsert(sid, question=question, bbox=list(bb), polygon=poly, status="loading", series=[])

    def _mat(mission, arrays, meta):
        try:
            from . import lake
            lake.write_points(mission, arrays, meta)
        except Exception as e:
            log.warning("%s: lake materialization failed: %s", mission, e)

    # --- pure extract workers: run concurrently, touch no shared/doc state, return (arrays, meta, cache_key) --------
    def _ex_glas():
        from . import glas
        a, m = glas.extract(bb, regions.DEFAULT_GLAS_WINDOW, polygon=poly)
        return a, m, m["cache_key"]

    def _ex_icessn():
        from . import icessn
        a, m = icessn.extract(bb, regions.DEFAULT_ICESSN_WINDOW, polygon=poly)
        return a, m, m["cache_key"]

    def _ex_atl06():
        from . import atl06
        a, m = atl06.extract(bb, regions.DEFAULT_ATL06_WINDOW, polygon=poly)
        return a, m, m["cache_key"]

    def _ex_atl03():
        a, m = atl03.extract(bb, regions.DEFAULT_ATL03_WINDOW, max_granules=max_granules, polygon=poly)
        return a, m, m["cache_key"]

    # --- integrators: mutate `doc`; ONLY ever called on the build thread, in priority order, so z0 and the
    #     series-insertion order are byte-for-byte what the old serial loop produced. -------------------------------
    def _int_glas(a, m, ck):
        scene.add_series(doc, "GLAS", a, m, ck)
        _mat("GLAS", a, m)
        log_fn(f"GLAS: {m['n']:,} shots across {len(m['campaigns'])} campaigns")

    def _int_icessn(a, m, ck):
        scene.add_series(doc, "ICESSN", a, m, ck)
        _mat("ICESSN", a, m)
        log_fn(f"ICESSN: {m['n']:,} nadir platelets across {len(m['years'])} campaign years")

    def _int_atl06(a, m, ck):
        scene.add_series(doc, "ATL06", a, m, ck)
        _mat("ATL06", a, m)
        log_fn(f"ATL06: {m['n']:,} land-ice segments")

    def _int_atl03(a, m, ck):
        st = m.get("access", {})
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

            def _prefetch_imagery():                 # warm the imagery JPEG cache; add_imagery() below then cache-hits
                from . import imagery
                imagery.build(frame, extent, 4096)   # width matches scene.add_imagery's default

            def _prefetch_dem():                     # warm the DEM grid npz (keyed by extent, independent of z0)
                from . import dem
                dem.surface_for_frame(frame, extent, 0.0)   # real z0 is applied later, inside set_surface()

            # t=0: every independent leg starts at once. Extracts are I/O-bound (requests/DuckDB/rasterio release the
            # GIL; ATL03 spawns its own ProcessPoolExecutor internally, fine on a thread). Imagery & DEM depend only on
            # frame+extent, so they run without waiting on the z0 barrier. All `doc` mutation stays on this thread.
            with ThreadPoolExecutor(max_workers=min(8, len(enabled) + 2), thread_name_prefix=f"build-{sid}") as ex:
                cfuts = {leg[0]: ex.submit(leg[2]) for leg in enabled}
                if with_atl03:
                    log_fn(f"ATL03: planner over {bb}" + (f" (polygon, {len(poly)} vertices)" if poly else ""))
                img_fut = ex.submit(_prefetch_imagery)
                dem_fut = ex.submit(_prefetch_dem)

                # z0 barrier + series. Integrate in priority order: the first collection to succeed sets doc["z0"]
                # (via add_series), exactly as the serial loop did; every series' z is then relative to that z0. Each
                # integrated series is persisted so it becomes paintable mid-build.
                for mkey, flag, extractor, integrator, disp in enabled:
                    try:
                        a, m, ck = cfuts[mkey].result()
                        integrator(a, m, ck)
                    except Exception as e:
                        log.warning("%s unavailable: %s", disp, e); log_fn(f"{disp} unavailable: {e}")
                        continue
                    registry_upsert(sid, series=sorted(doc["series"]))
                    cache.save_scene(sid, doc)       # progressive persistence: this series is now readable

                if not doc["series"]:
                    raise RuntimeError("no collection returned data over this area (check your selection and the token)")

                # surface: needs z0 (now known). The grid was fetched concurrently, so set_surface() is a cache hit;
                # set_surface() is the authoritative attempt (fetches inline if the prefetch failed) -> identical result.
                try:
                    dem_fut.result()
                except Exception as e:
                    log.info("DEM prefetch failed; set_surface will fetch inline: %s", e)
                scene.set_surface(doc)               # DEM base surface, independent of which collections loaded
                log_fn("surface: DEM base surface")
                cache.save_scene(sid, doc)

                # imagery: independent of z0 and collections; fetched concurrently, finalised here. add_imagery() is
                # the authoritative attempt (cache hit if the prefetch succeeded, inline fetch otherwise).
                try:
                    try:
                        img_fut.result()
                    except Exception as e:
                        log.info("imagery prefetch failed; add_imagery will fetch inline: %s", e)
                    scene.add_imagery(doc)
                    log_fn(f"imagery: {doc['imagery']['width']}x{doc['imagery']['height']} at z{doc['imagery']['zoom']}")
                except Exception as e:
                    log.warning("imagery unavailable: %s", e); log_fn(f"imagery unavailable: {e}")
                cache.save_scene(sid, doc)
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
    import pyarrow.parquet as pq
    import h3
    d, res = _index_source(collection)
    if d is None:
        return {"collection": collection, "indexed": False, "res": None, "granules": 0, "cells": []}
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
        evicted = lake.enforce_limit()
        return {**lake.get_settings(), "evicted": evicted}
    return lake.get_settings()


def lake_evict(cells: list) -> dict:
    from . import lake
    return {"evicted": lake.evict_cells([int(c) for c in cells])}


def lake_load(cells: list, window=None, max_granules: int = 40) -> dict:
    return start_job({"cells": [int(c) for c in cells], "window": window, "max_granules": max_granules}, kind="cells")


# ----------------------------------------------------------------------------- misc
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
