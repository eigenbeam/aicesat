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
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server import MCPServer

from . import atl03, cache, coverage, regions, scene

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


# ----------------------------------------------------------------------------- HTTP (widget + api)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WIDGET_DIR), **kw)

    def log_message(self, fmt, *args):  # keep stdout clean
        log.debug("http " + fmt, *args)

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, default=cache._json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
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
def show_photons(region: str | None = None, bbox: list[float] | None = None,
                 time_window: list[str] | None = None, max_granules: int = 2, question: str | None = None) -> dict:
    """Slice 1: extract real ICESat-2 ATL03 land-ice signal photons (strong beams, medium+high confidence) over
    a region and create a 3D scene. Returns the widget URL to open plus extraction provenance.
    Note: ATL03 granules are large; the first call for a region takes minutes, later calls are cached."""
    bb = regions.resolve_bbox(region, tuple(bbox) if bbox else None)
    window = tuple(time_window) if time_window else regions.DEFAULT_ATL03_WINDOW
    with _lock:
        arrays, meta = atl03.extract(bb, window, max_granules=max_granules)
        sid = uuid.uuid4().hex[:10]
        doc = scene.new_scene(sid, bb, question)
        scene.add_series(doc, "ICESAT2", arrays, meta, meta["cache_key"])
        cache.save_scene(sid, doc)
    return {"scene_id": sid, "widget_url": widget_url(sid), "n_photons": meta["n"], "n_in_bbox": meta["n_total_in_bbox"],
            "product": meta["product"], "native_frame": meta["native_frame"], "height_ref": meta["height_ref"],
            "granules": meta["granules"], "bbox": list(bb), "time_window": list(window)}


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
