"""Transport-neutral API: every UI/agent operation as a plain function returning JSON-able dicts.

server.py exposes these twice — as HTTP routes (the localhost widget, "fetch adapter") and as MCP tools (model-visible
and app-visible, the MCP Apps "app adapter"). Nothing here knows which transport called it.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import traceback
import uuid
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
    REGISTRY.write_text(json.dumps(reg, indent=1, default=str))
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

    def _add(flag, name, fn):
        if not flag:
            return
        try:
            fn()
        except Exception as e:
            log.warning("%s unavailable: %s", name, e); log_fn(f"{name} unavailable: {e}")

    try:
        with _lock:
            doc = scene.new_scene(sid, bb, question, polygon=poly)

            def _atl03():
                log_fn(f"ATL03: planner over {bb}" + (f" (polygon, {len(poly)} vertices)" if poly else ""))
                arrays, meta = atl03.extract(bb, regions.DEFAULT_ATL03_WINDOW, max_granules=max_granules, polygon=poly)
                st = meta.get("access", {})
                log_fn(f"ATL03: {meta['n']:,} photons; {st.get('chunks_fetched', 0)} chunks fetched "
                       f"({st.get('bytes', 0) / 1e6:.0f} MB, {st.get('requests', 0)} requests), "
                       f"{st.get('chunks_skipped_already_materialized', 0)} already in the lake")
                if st.get("evicted_for_limit"):
                    log_fn(f"storage limit: evicted {len(st['evicted_for_limit'])} cells")
                scene.add_series(doc, "ICESAT2", arrays, meta, meta["cache_key"])

            def _glas():
                from . import glas
                g_arrays, g_meta = glas.extract(bb, regions.DEFAULT_GLAS_WINDOW, polygon=poly)
                scene.add_series(doc, "GLAS", g_arrays, g_meta, g_meta["cache_key"])
                log_fn(f"GLAS: {g_meta['n']:,} shots across {len(g_meta['campaigns'])} campaigns")

            def _atl06():
                from . import atl06
                a_arrays, a_meta = atl06.extract(bb, regions.DEFAULT_ATL06_WINDOW, polygon=poly)
                scene.add_series(doc, "ATL06", a_arrays, a_meta, a_meta["cache_key"])
                log_fn(f"ATL06: {a_meta['n']:,} land-ice segments")

            def _icessn():
                from . import icessn
                i_arrays, i_meta = icessn.extract(bb, regions.DEFAULT_ICESSN_WINDOW, polygon=poly)
                scene.add_series(doc, "ICESSN", i_arrays, i_meta, i_meta["cache_key"])
                log_fn(f"ICESSN: {i_meta['n']:,} nadir platelets across {len(i_meta['years'])} campaign years")

            _add(with_glas, "GLAS", _glas)          # chronological, matching the collection list
            _add(with_icessn, "ICESSN", _icessn)
            _add(with_atl06, "ATL06", _atl06)
            _add(with_atl03, "ATL03", _atl03)
            if not doc["series"]:
                raise RuntimeError("no collection returned data over this area (check your selection and the token)")
            scene.set_surface(doc)                   # DEM base surface, independent of which collections loaded
            try:
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
