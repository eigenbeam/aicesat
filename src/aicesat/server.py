"""MCP server (stdio) + background localhost HTTP server for the deck.gl widget.

Never print to stdout: stdio is the MCP transport. Logging goes to stderr.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import parse_qs, urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server import MCPServer

from . import atl03, cache, coverage, geom, regions, scene

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

WIDGET_DIR = Path(__file__).parent / "widget"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.environ.get("AICESAT_PORT", "8765"))  # updated by start_http() if the port is taken
_lock = threading.Lock()  # serialise compute (one user, one demo)


def widget_url(scene_id: str) -> str:
    return f"http://{HTTP_HOST}:{HTTP_PORT}/?scene={scene_id}"


# ----------------------------------------------------------------------------- compute entry points

def run_coregister(scene_id: str, common_epoch: float | None = None, colocation_radius_m: float | None = None,
                   exaggeration: float | None = None) -> dict:
    from . import coreg  # Slice 3

    doc = cache.load_scene(scene_id)
    if doc is None:
        raise KeyError(scene_id)
    kw = {k: v for k, v in dict(common_epoch=common_epoch, colocation_radius_m=colocation_radius_m,
                                exaggeration=exaggeration).items() if v is not None}
    with _lock:
        if doc.get("coreg") and doc["coreg"].get("params") == coreg.params(**kw):
            out = dict(doc["coreg"]); out["cached"] = True
            return out
        t0 = time.time()
        result = coreg.coregister_scene(doc, **kw)
        result["compute_seconds"] = round(time.time() - t0, 2)
        result["cached"] = False
        doc["coreg"] = result
        cache.save_scene(scene_id, doc)
    return result


# ----------------------------------------------------------------------------- scene build (shared by MCP tools + selector page)

_jobs: dict[str, dict] = {}


def build_scene(bbox=None, polygon=None, question: str | None = None, max_granules: int = 8, with_glas: bool = True,
                with_coreg: bool = False, log_fn=lambda m: None) -> dict:
    """Full pipeline for an area: ATL03 via index+lake, imagery, optional GLAS and co-registration. Returns the scene doc."""
    bb, poly = geom.normalize_area(bbox, polygon)
    with _lock:
        log_fn(f"ICESat-2: planner over {bb}" + (f" (polygon, {len(poly)} vertices)" if poly else ""))
        arrays, meta = atl03.extract(bb, regions.DEFAULT_ATL03_WINDOW, max_granules=max_granules, polygon=poly)
        st = meta.get("access", {})
        log_fn(f"ICESat-2: {meta['n']:,} photons; {st.get('chunks_fetched', 0)} chunks fetched ({st.get('bytes', 0) / 1e6:.0f} MB, "
               f"{st.get('requests', 0)} requests), {st.get('chunks_skipped_already_materialized', 0)} already in the lake")
        sid = uuid.uuid4().hex[:10]
        doc = scene.new_scene(sid, bb, question, polygon=poly)
        scene.add_series(doc, "ICESAT2", arrays, meta, meta["cache_key"])
        try:
            scene.add_imagery(doc)
            log_fn(f"imagery: {doc['imagery']['width']}x{doc['imagery']['height']} at z{doc['imagery']['zoom']}")
        except Exception as e:  # imagery is a cue, never a blocker
            log.warning("imagery unavailable: %s", e); log_fn(f"imagery unavailable: {e}")
        if with_glas:
            from . import glas
            g_arrays, g_meta = glas.extract(bb, regions.DEFAULT_GLAS_WINDOW, polygon=poly)
            scene.add_series(doc, "GLAS", g_arrays, g_meta, g_meta["cache_key"])
            log_fn(f"GLAS: {g_meta['n']:,} shots across {len(g_meta['campaigns'])} campaigns")
        cache.save_scene(sid, doc)
    if with_coreg and with_glas:
        run_coregister(sid)
        log_fn("co-registration computed and cached")
    return cache.load_scene(sid)


def start_job(params: dict) -> str:
    jid = uuid.uuid4().hex[:8]
    job = _jobs[jid] = {"id": jid, "status": "running", "log": [], "scene_id": None, "widget_url": None, "error": None,
                        "started": time.time()}

    def run():
        try:
            doc = build_scene(params.get("bbox"), params.get("polygon"), params.get("question"), int(params.get("max_granules", 8)),
                              bool(params.get("with_glas", True)), bool(params.get("with_coreg", False)), lambda m: job["log"].append(m))
            job.update(status="done", scene_id=doc["scene_id"], widget_url=widget_url(doc["scene_id"]))
        except Exception as e:
            log.exception("build job failed")
            job.update(status="error", error=f"{type(e).__name__}: {e}")
            job["log"].append(traceback.format_exc().splitlines()[-1])
        job["seconds"] = round(time.time() - job["started"], 1)

    threading.Thread(target=run, daemon=True, name=f"job-{jid}").start()
    return jid


def lake_cells_geojson(mission: str = "ICESAT2") -> dict:
    """Materialized H3 cells as polygons, for the selector map ("what is already in the lake")."""
    import h3
    from . import lake

    cells = {p.name.split("=")[1] for p in lake.LAKE_DIR.glob(f"mission={mission}/h3_cell=*")} if lake.LAKE_DIR.exists() else set()
    feats = []
    for c in cells:
        ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(h3.int_to_str(int(c)))]
        feats.append({"type": "Feature", "properties": {"cell": c}, "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}})
    return {"type": "FeatureCollection", "features": feats}


# ----------------------------------------------------------------------------- HTTP (widget + api)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WIDGET_DIR), **kw)

    def log_message(self, fmt, *args):  # keep stdout clean
        log.debug("http " + fmt, *args)

    def end_headers(self):  # widget files change during development; never let the browser cache them
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, default=cache._json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/scene/") and self.path.endswith("/imagery.jpg"):
            sid = self.path.split("/")[3]
            doc = cache.load_scene(sid)
            img = doc and doc.get("imagery") and Path(doc["imagery"]["path"])
            if not img or not img.exists():
                return self.send_error(404)
            body = img.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            return self.wfile.write(body)
        u = urlparse(self.path); qs = parse_qs(u.query)
        if u.path == "/api/regions":
            return self._json(200, {k: {"bbox": list(v["bbox"]), "note": v["note"]} for k, v in regions.REGIONS.items()})
        if u.path == "/api/lake_cells":
            return self._json(200, lake_cells_geojson())
        if u.path == "/api/bench":
            bp = cache.DATA_DIR / "bench" / "results.json"
            return self._json(200, json.loads(bp.read_text())) if bp.exists() else self._json(404, {"error": "no benchmark results yet"})
        if u.path == "/api/coverage":
            try:
                bb, poly = geom.normalize_area(json.loads(qs["bbox"][0]) if "bbox" in qs else None,
                                               json.loads(qs["polygon"][0]) if "polygon" in qs else None)
                return self._json(200, coverage.check_coverage(bb))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if u.path.startswith("/api/job/"):
            j = _jobs.get(u.path.split("/")[3])
            return self._json(200, j) if j else self._json(404, {"error": "no such job"})
        if self.path.startswith("/api/scene/"):
            sid = self.path.split("/")[3].split("?")[0]
            path = cache.scene_path(sid)
            if not path.exists():
                return self._json(404, {"error": "no such scene"})
            body = path.read_bytes()  # already JSON: stream it, don't re-parse 10 MB
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/extract":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                geom.normalize_area(body.get("bbox"), body.get("polygon"))  # validate early
                return self._json(202, {"job_id": start_job(body)})
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/api/coregister/"):
            sid = self.path.split("/")[3].split("?")[0]
            try:
                return self._json(200, run_coregister(sid))
            except KeyError:
                return self._json(404, {"error": "no such scene"})
            except Exception as e:  # surfaced to the widget status line
                log.exception("coregister failed")
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        self.send_error(404)


def start_http() -> ThreadingHTTPServer:
    global HTTP_PORT
    last = None
    for port in range(HTTP_PORT, HTTP_PORT + 10):
        try:
            srv = ThreadingHTTPServer((HTTP_HOST, port), Handler)
            HTTP_PORT = port
            break
        except OSError as e:  # in use (e.g. scripts/serve.py running): try the next one, never kill the MCP server
            last = e
    else:
        raise RuntimeError(f"no free port in {HTTP_PORT}..{HTTP_PORT + 9}: {last}")
    threading.Thread(target=srv.serve_forever, daemon=True, name="widget-http").start()
    log.info("widget server on http://%s:%d", HTTP_HOST, HTTP_PORT)
    return srv


# ----------------------------------------------------------------------------- MCP tools

mcp = MCPServer(
    "aicesat",
    instructions=(
        "Cross-mission altimetry demo (ICESat-2 ATL03 + ICESat/GLAS GLAH06). Tools compute and return "
        "structured JSON plus a widget URL the user should open; the widget renders the 3D scene. "
        "Always relay the comparability block and unresolved list to the user; never claim the missions "
        "'agree' — co-registration removes the plate-motion artifact only."
    ),
)


@mcp.tool()
def list_regions() -> dict:
    """Named candidate Greenland demo regions (bbox = west, south, east, north) with validation notes."""
    return {k: {"bbox": list(v["bbox"]), "note": v["note"]} for k, v in regions.REGIONS.items()}


@mcp.tool()
def check_coverage(region: str | None = None, bbox: list[float] | None = None,
                   atl03_window: list[str] | None = None, glas_window: list[str] | None = None) -> dict:
    """How many ATL03 (ICESat-2) and GLAH06 (ICESat/GLAS) granules touch a region, by month / laser campaign.
    Give either a region name (see list_regions) or an explicit bbox [W, S, E, N]. No data is fetched."""
    bb = regions.resolve_bbox(region, tuple(bbox) if bbox else None)
    return coverage.check_coverage(bb, atl03_window, glas_window)


@mcp.tool()
def show_photons(region: str | None = None, bbox: list[float] | None = None, polygon: list[list[float]] | None = None,
                 time_window: list[str] | None = None, max_granules: int = 8, question: str | None = None) -> dict:
    """Slice 1: extract real ICESat-2 ATL03 land-ice signal photons (strong beams, medium+high confidence) over an area
    and create a 3D scene with an imagery base layer. Area = region name, bbox [W,S,E,N], or polygon [[lon,lat],...].
    Uses the H3 chunk index + byte-range reads + Parquet lake: first touch of an area fetches only the chunks it needs,
    later calls hit the lake. Returns the widget URL to open plus extraction/access provenance."""
    if region and not (bbox or polygon):
        bbox = list(regions.resolve_bbox(region))
    doc = build_scene(bbox, polygon, question, max_granules, with_glas=False)
    meta = doc["series"]["ICESAT2"]["meta"]
    return {"scene_id": doc["scene_id"], "widget_url": widget_url(doc["scene_id"]), "n_photons": meta["n"],
            "product": meta["product"], "native_frame": meta["native_frame"], "height_ref": meta["height_ref"],
            "access": meta.get("access"), "granules": doc["series"]["ICESAT2"]["granules"], "bbox": doc["bbox"],
            "polygon": doc.get("polygon"), "time_window": meta["window"], "imagery": bool(doc.get("imagery"))}


@mcp.tool()
def open_area_selector() -> dict:
    """URL of the map page where the user draws a bounding box or polygon on Sentinel-2 imagery, checks ICESat-2 / GLAS
    coverage, and builds a scene from it (the page shows which H3 cells are already in the lake)."""
    return {"url": f"http://{HTTP_HOST}:{HTTP_PORT}/select.html",
            "how": "draw a box (drag) or polygon (click vertices, double-click to close), then Check coverage / Build scene"}


@mcp.tool()
def add_glas(scene_id: str, time_window: list[str] | None = None, max_granules: int = 400) -> dict:
    """Slice 2: add ICESat/GLAS GLAH06 40 Hz shots (2003-2009 campaigns) to an existing scene, in native
    coordinates (ITRF2008; heights converted TOPEX/Poseidon -> WGS84 ellipsoid). Returns provenance by campaign."""
    from . import glas

    doc = cache.load_scene(scene_id)
    if doc is None:
        raise ValueError(f"no such scene {scene_id}")
    window = tuple(time_window) if time_window else regions.DEFAULT_GLAS_WINDOW
    with _lock:
        arrays, meta = glas.extract(tuple(doc["bbox"]), window, max_granules=max_granules)
        scene.add_series(doc, "GLAS", arrays, meta, meta["cache_key"])
        doc["coreg"] = None
        cache.save_scene(scene_id, doc)
    return {"scene_id": scene_id, "widget_url": widget_url(scene_id), "n_shots": meta["n"],
            "campaigns": meta["campaigns"], "native_frame": meta["native_frame"], "height_ref": meta["height_ref"],
            "ellipsoid_correction": meta["ellipsoid_correction"], "granules": meta["granules"]}


@mcp.tool()
def coregister(scene_id: str, common_epoch: float = 2005.0, colocation_radius_m: float = 35.0,
               exaggeration: float = 0.0) -> dict:
    """Slice 3: run the ITRF2014 + epoch co-registration (plate motion, ITRF2014-PMM NOAM) on both missions in
    a scene, co-locate GLAS shots with ICESat-2 photons, and compute delta-h statistics in native and
    co-registered coordinates. Live pyproj on first call, cached after. Returns the comparability block.
    exaggeration <= 0 picks a display exaggeration automatically (~3% of scene span); it is always labelled on screen."""
    out = run_coregister(scene_id, common_epoch, colocation_radius_m, exaggeration)
    slim = {k: v for k, v in out.items() if k not in ("dh_native", "dh_coreg", "artifact", "display_positions")}
    slim["widget_url"] = widget_url(scene_id)
    return slim


def main() -> None:
    start_http()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
