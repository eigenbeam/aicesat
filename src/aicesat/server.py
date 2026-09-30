"""MCP server (stdio) + background localhost HTTP server for the deck.gl widget.

Never print to stdout: stdio is the MCP transport. Logging goes to stderr.
"""
from __future__ import annotations

import hashlib
import hmac
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
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver.exceptions import ToolError

from . import api, atl03, cache, coverage, geom, regions, scene, stream, survey, uibuild

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

WIDGET_DIR = Path(__file__).parent / "widget"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.environ.get("AICESAT_PORT", "8765"))  # updated by start_http() if the port is taken
_lock = api._lock  # serialise compute (one user, one demo)

# Public deployment (beta web app). AICESAT_PUBLIC_URL is the externally-reachable base (e.g. https://host) that
# CADDY terminates TLS for and reverse-proxies to this localhost server; widget URLs handed back to browsers must
# use it, not 127.0.0.1. AICESAT_ACCESS_CODE, when set, gates every page and /api behind a shared code (a signed
# cookie). Neither is set for the owner's private SSH-forwarded process, so that path is unchanged and ungated.
ACCESS_CODE = os.environ.get("AICESAT_ACCESS_CODE", "").strip()


def base_url() -> str:
    pub = os.environ.get("AICESAT_PUBLIC_URL", "").strip().rstrip("/")
    return pub or f"http://{HTTP_HOST}:{HTTP_PORT}"


def widget_url(scene_id: str) -> str:
    return f"{base_url()}/#scene/{scene_id}"


def _gate_token(code: str) -> str:
    return hmac.new(code.encode(), b"aicesat-gate-v1", hashlib.sha256).hexdigest()


_GATE_TOKEN = _gate_token(ACCESS_CODE) if ACCESS_CODE else ""


# ----------------------------------------------------------------------------- compute (delegated to api.py)
api._widget_url = lambda sid: widget_url(sid)
run_coregister = api.coregister
build_scene = api.build_scene
start_job = lambda params: api.start_job(params)["id"]
lake_cells_geojson = lambda mission="ICESAT2": api.lake_cells(stats=False, mission=mission)


# ----------------------------------------------------------------------------- HTTP (widget + api)

# A browser that navigates away, closes a tab, or supersedes a poll drops the socket mid-response. The work already
# succeeded; only the delivery was interrupted. socketserver's default handle_error prints a full traceback for it,
# which buried real errors in the journal — /api/index_status (polled every 8 s by the Data Lake view, and slow
# enough to still be in flight when the view is left) produced a steady stream of them.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], _CLIENT_GONE):
            log.debug("client %s disconnected mid-response", client_address)
            return
        super().handle_error(request, client_address)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WIDGET_DIR), **kw)

    def log_message(self, fmt, *args):  # keep stdout clean
        log.debug("http " + fmt, *args)

    def end_headers(self):  # widget files change during development; never let the browser cache them
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _write(self, body: bytes) -> None:
        """Write a response body, treating a vanished client as normal. Every response goes through here."""
        try:
            self.wfile.write(body)
        except _CLIENT_GONE:
            self.close_connection = True
            log.debug("client gone before %s could be written (%d bytes)", self.path, len(body))

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, default=cache._json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _scene_stream(self, sid: str, qs: dict) -> None:
        """The push transport (see stream.py): the scene's point and surface frames, written as they land.

        No Content-Length and no chunked encoding — this handler speaks HTTP/1.0, so the body simply ends when the
        connection closes. `fetch()` + `response.body.getReader()` reads that happily; the reader knows it is finished
        from the terminal `done` control frame rather than from EOF, so a dropped connection is distinguishable from
        a completed build and can be resumed with `?from=`.

        The per-frame flush is belt-and-braces, not the mechanism: http.server's wfile is a _SocketWriter and already
        writes straight through (removing the flush does not change the behaviour — measured, not assumed). What DOES
        matter is that this loop writes each frame as the generator yields it; joining the frames and writing once at
        the end passes every unit test and silently reverts the transport to a slow poll.
        See test_frames_reach_the_client_before_the_build_finishes, which reads frames off a live socket.
        """
        if cache.load_scene(sid) is None and sid not in api._registry():
            return self._json(404, {"error": "no such scene"})
        cur = stream.parse_cursors(qs.get("from", [None])[0])
        lim = stream.parse_limit(qs.get("limit", [None])[0])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Accel-Buffering", "no")   # ask any proxy in front not to sit on frames
        self.end_headers()
        sent = 0
        try:
            for fr in stream.frames(sid, cur, limit=lim):
                self.wfile.write(fr)
                self.wfile.flush()
                sent += len(fr)
        except _CLIENT_GONE:
            self.close_connection = True
            log.debug("scene %s: client left after %d stream bytes", sid, sent)

    # -------- access-code gate (public beta). No-op when AICESAT_ACCESS_CODE is unset (owner's private process). --------
    def _authed(self) -> bool:
        if not ACCESS_CODE:
            return True
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith("aicesat_gate="):
                return hmac.compare_digest(part[len("aicesat_gate="):], _GATE_TOKEN)
        return False

    def _gate_page(self, msg: str = "") -> None:
        html = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>AIcesat — beta access</title>"
                "<style>body{background:#0b1422;color:#e6edf3;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0}"
                "form{background:#111a2b;padding:28px 30px;border-radius:12px;border:1px solid #243247;width:280px}"
                "h1{font-size:18px;margin:0 0 4px}p{color:#9fb0c3;font-size:13px;margin:0 0 16px}"
                "input{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:7px;border:1px solid #2c3d55;background:#0b1422;color:#e6edf3;font-size:15px}"
                "button{margin-top:12px;width:100%;padding:9px;border:0;border-radius:7px;background:#2f6fed;color:#fff;font-size:15px;cursor:pointer}"
                ".err{color:#ff8080;font-size:13px;margin-top:10px;min-height:16px}</style>"
                "<form method=POST action=/gate><h1>AIcesat</h1><p>Cross-mission altimetry — beta. Enter your access code.</p>"
                "<input name=code type=password autofocus placeholder='access code' autocomplete='off'>"
                f"<button type=submit>Enter</button><div class=err>{msg}</div></form>")
        body = html.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self._write(body)

    def _gate_guard(self) -> bool:
        """True if the request may proceed. Handles /gate itself and blocks everything else when unauthenticated."""
        path = self.path.split("?")[0]
        if path == "/gate":
            if self.command == "POST":
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")) or 0).decode("utf-8", "replace")
                code = parse_qs(raw).get("code", [""])[0]
                if ACCESS_CODE and hmac.compare_digest(_gate_token(code), _GATE_TOKEN):
                    self.send_response(303); self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"aicesat_gate={_GATE_TOKEN}; Max-Age=2592000; Path=/; HttpOnly; Secure; SameSite=Lax")
                    self.end_headers()
                else:
                    self._gate_page("Incorrect code.")
            else:
                self._gate_page()
            return False
        if self._authed():
            return True
        if path.startswith("/api/"):
            self._json(401, {"error": "access code required"})
        else:
            self._gate_page()
        return False

    def do_GET(self):
        if not self._gate_guard():
            return
        if self.path == "/" or self.path.startswith("/?") or self.path == "/index.html":
            dist = uibuild.DIST
            if dist.exists():
                body = dist.read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers()
                return self._write(body)
        if self.path.startswith("/api/scene/") and self.path.endswith("/imagery.jpg"):
            sid = self.path.split("/")[3]
            doc = cache.load_scene(sid)
            img = doc and doc.get("imagery") and Path(doc["imagery"]["path"])
            if not img or not img.exists():
                return self.send_error(404)
            body = img.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            return self._write(body)
        u = urlparse(self.path); qs = parse_qs(u.query)
        if u.path == "/api/regions":
            return self._json(200, api.list_regions())
        if u.path == "/api/collections":
            return self._json(200, api.list_collections())
        if u.path == "/api/lake_cells":
            return self._json(200, api.lake_cells(stats=False))
        if u.path == "/api/lake/cells":
            return self._json(200, api.lake_cells(stats=True, mission=qs.get("mission", ["ICESAT2"])[0]))
        if u.path == "/api/lake/summary":
            return self._json(200, api.lake_summary(qs.get("mission", ["ICESAT2"])[0]))
        if u.path == "/api/lake/log":
            return self._json(200, api.lake_log(int(qs.get("after", ["0"])[0])))
        if u.path == "/api/lake/settings":
            return self._json(200, api.lake_settings())
        if u.path == "/api/scenes":
            return self._json(200, [{**r, "widget_url": widget_url(r["scene_id"])} for r in api.scenes()])
        if u.path == "/api/jobs":
            return self._json(200, api.jobs())
        if u.path == "/api/index_status":
            return self._json(200, api.index_status(qs.get("collection", ["ATL06"])[0]))
        if u.path.startswith("/api/scene/") and u.path.endswith("/stream"):
            return self._scene_stream(u.path.split("/")[3], qs)
        if u.path.startswith("/api/scene/") and u.path.endswith("/part"):
            sid = u.path.split("/")[3]
            try:
                return self._json(200, api.scene_part(sid, qs.get("part", ["meta"])[0], int(qs.get("chunk", ["0"])[0])))
            except KeyError:
                return self._json(404, {"error": "no such scene"})
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/bench":
            bp = cache.DATA_DIR / "bench" / "results.json"
            return self._json(200, json.loads(bp.read_text())) if bp.exists() else self._json(404, {"error": "no benchmark results yet"})
        if u.path == "/api/coverage":
            try:
                bb, poly = geom.normalize_area(json.loads(qs["bbox"][0]) if "bbox" in qs else None,
                                               json.loads(qs["polygon"][0]) if "polygon" in qs else None)
                return self._json(200, coverage.check_coverage(bb, polygon=poly))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/coverage_hexes":
            try:
                return self._json(200, survey.coverage_hexes(int(qs.get("res", ["5"])[0]),
                                                             json.loads(qs["bbox"][0]) if "bbox" in qs else None))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/claims":
            return self._json(200, api.claim_regions())
        if u.path == "/api/fit":
            try:
                return self._json(200, api.fit_to_coverage(
                    json.loads(qs["bbox"][0]) if "bbox" in qs else None,
                    json.loads(qs["polygon"][0]) if "polygon" in qs else None,
                    [k for k in qs.get("keys", [""])[0].split(",") if k]))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if u.path.startswith("/api/job/"):
            j = api.job(u.path.split("/")[3])
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
            return self._write(body)
        return super().do_GET()

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def do_POST(self):
        if not self._gate_guard():
            return
        try:
            if self.path == "/api/extract":
                body = self._body()
                geom.normalize_area(body.get("bbox"), body.get("polygon"))  # validate early
                j = api.start_job(body)
                return self._json(202, {"job_id": j["id"], "scene_id": j["scene_id"]})
            if self.path == "/api/lake/settings":
                return self._json(200, api.lake_settings(max_bytes=self._body().get("max_bytes")))
            if self.path == "/api/lake/load":
                body = self._body()
                j = api.lake_load(body["cells"], body.get("window"))
                return self._json(202, {"job_id": j["id"]})
            if self.path == "/api/lake/evict":
                return self._json(200, api.lake_evict(self._body()["cells"]))
        except Exception as e:
            return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/api/scene/") and self.path.split("?")[0].endswith("/delete"):
            sid = self.path.split("/")[3].split("?")[0]
            try:
                return self._json(200, api.delete_scene(sid))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/api/scene/") and self.path.split("?")[0].endswith("/imagery"):
            sid = self.path.split("/")[3].split("?")[0]
            try:
                return self._json(200, api.scene_add_imagery(sid, self._body().get("source", "s2")))
            except KeyError:
                return self._json(404, {"error": "no such scene"})
            except Exception as e:  # surfaced to the widget imagery status line
                log.exception("imagery re-fetch failed")
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/api/coregister/"):
            sid = self.path.split("/")[3].split("?")[0]
            try:
                return self._json(200, api.coregister(sid))
            except KeyError:
                return self._json(404, {"error": "no such scene"})
            except Exception as e:  # surfaced to the widget status line
                log.exception("coregister failed")
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if self.path.startswith("/api/candidates/"):
            sid = self.path.split("/")[3].split("?")[0]
            body = self._body()
            try:
                return self._json(200, api.scene_candidates(sid, h3_res=body.get("h3_res", 9), delta_t=body.get("delta_t", 1.0),
                                                             ref_missions=body.get("ref_missions"), min_bins=body.get("min_bins", 3)))
            except KeyError:
                return self._json(404, {"error": "no such scene"})
            except Exception as e:
                log.exception("candidates failed")
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        self.send_error(404)


def start_http() -> ThreadingHTTPServer:
    global HTTP_PORT
    try:
        if uibuild.needs_build():
            uibuild.build()
            log.info("built UI %s", uibuild.DIST)
    except Exception as e:  # the legacy pages still work
        log.warning("UI build failed: %s", e)
    last = None
    for port in range(HTTP_PORT, HTTP_PORT + 10):
        try:
            srv = Server((HTTP_HOST, port), Handler)
            HTTP_PORT = port
            break
        except OSError as e:  # in use (e.g. scripts/serve.py running): try the next one, never kill the MCP server
            last = e
    else:
        raise RuntimeError(f"no free port in {HTTP_PORT}..{HTTP_PORT + 9}: {last}")
    threading.Thread(target=srv.serve_forever, daemon=True, name="widget-http").start()
    from . import logbuf
    logbuf.install()   # start capturing pipeline logs for the Lake page's running log
    log.info("widget server on http://%s:%d", HTTP_HOST, HTTP_PORT)
    return srv


# ----------------------------------------------------------------------------- MCP tools

UI_URI = "ui://aicesat/app.html"
apps = Apps()


def _ui_html() -> str:
    try:
        if uibuild.needs_build():
            uibuild.build()
    except Exception as e:
        log.warning("UI build failed: %s", e)
    return uibuild.DIST.read_text() if uibuild.DIST.exists() else "<!doctype html><p>UI not built</p>"


apps.add_html_resource(
    UI_URI, _ui_html(), name="aicesat-ui", title="Cross-mission altimetry",
    description="Altimetry by place: a coverage globe, a change map per area, and a 3-D study view",
    csp=ResourceCsp(connect_domains=["https://tiles.maps.eox.at"], resource_domains=["https://tiles.maps.eox.at"]),
    prefers_border=False,
)


def show_photons(region: str | None = None, bbox: list[float] | None = None, polygon: list[list[float]] | None = None,
                 time_window: list[str] | None = None, question: str | None = None) -> dict:
    """Slice 1: extract real ICESat-2 ATL03 land-ice signal photons (all 6 beams, medium+high confidence) over an area
    and create a 3D scene with an imagery base layer. Area = region name, bbox [W,S,E,N], or polygon [[lon,lat],...].
    Uses the H3 chunk index + byte-range reads + Parquet lake: first touch of an area fetches only the chunks it needs,
    later calls hit the lake. Returns the widget URL to open plus extraction/access provenance."""
    if region and not (bbox or polygon):
        bbox = list(regions.resolve_bbox(region))
    doc = build_scene(bbox, polygon, question, with_glas=False, with_atl03=True)
    meta = doc["series"]["ICESAT2"]["meta"]
    return {"scene_id": doc["scene_id"], "widget_url": widget_url(doc["scene_id"]), "n_photons": meta["n"],
            "product": meta["product"], "native_frame": meta["native_frame"], "height_ref": meta["height_ref"],
            "access": meta.get("access"), "granules": doc["series"]["ICESAT2"]["granules"], "bbox": doc["bbox"],
            "polygon": doc.get("polygon"), "time_window": meta["window"], "imagery": bool(doc.get("imagery"))}


def open_ui(view: str = "explore") -> dict:
    """URL of the unified UI: Explore (imagery map, draw a box or polygon on Sentinel-2 imagery, coverage check, build
    scenes, open the 3-D viewer) and Lake (H3 grid with per-cell stats, storage limit, background loading, eviction)."""
    view = view if view in ("explore", "lake") else "explore"
    return {"view": view, "url": f"http://{HTTP_HOST}:{HTTP_PORT}/#{view}", "lake": f"http://{HTTP_HOST}:{HTTP_PORT}/#lake",
            "how": "Explore: drag a box (or Polygon: click vertices, Enter), Check coverage / Build scene; Lake: click cells, Load in background / Evict. "
                   "In Claude Desktop the UI renders inline; elsewhere open the URL."}



def add_glas(scene_id: str, time_window: list[str] | None = None) -> dict:
    """Slice 2: add ICESat/GLAS GLAH06 40 Hz shots (2003-2009 campaigns) to an existing scene, in native
    coordinates (ITRF2008; heights converted TOPEX/Poseidon -> WGS84 ellipsoid). Returns provenance by campaign."""
    from . import glas

    doc = cache.load_scene(scene_id)
    if doc is None:
        raise ValueError(f"no such scene {scene_id}")
    window = tuple(time_window) if time_window else regions.DEFAULT_GLAS_WINDOW
    with _lock:
        arrays, meta = glas.extract(tuple(doc["bbox"]), window)
        scene.add_series(doc, "GLAS", arrays, meta, meta["cache_key"])
        doc["coreg"] = None
        cache.save_scene(scene_id, doc)
    return {"scene_id": scene_id, "widget_url": widget_url(scene_id), "n_shots": meta["n"],
            "campaigns": meta["campaigns"], "native_frame": meta["native_frame"], "height_ref": meta["height_ref"],
            "ellipsoid_correction": meta["ellipsoid_correction"], "access": meta.get("access")}


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


def ts_url(scene_id: str, h3: str | None = None) -> str:
    return f"{base_url()}/#ts/{scene_id}" + (f"?sel={h3}" if h3 else "")


def _anticipated(fn, *a, **kw):
    """Run an api.* call, turning its two expected failures into ToolError.

    The SDK treats any other exception as a crash and withholds its text: the model reads only "Error executing
    tool <name>" and has nothing to act on. Both failures here are ones a caller can fix from the message -- a
    scene id that does not exist, or a cell id from a different search -- so they must arrive intact."""
    try:
        return fn(*a, **kw)
    except KeyError as e:
        raise ToolError(f"no such scene {e.args[0] if e.args else ''} — elevation_change returns the scene_id for a "
                        "place") from e
    except ValueError as e:
        raise ToolError(str(e)) from e


def find_timeseries_candidates(scene_id: str, h3_res: int = 9, delta_t: float = 1.0,
                               ref_missions: list[str] | None = None, min_bins: int = 3, limit: int = 10) -> dict:
    """Find and rank the places in a built scene where an elevation TIME SERIES can actually be measured.

    Bins every mission's points into H3 cells and fixed time windows, keeps cells seen in >= min_bins windows, fits
    one local reference plane per cell (so surface slope is removed rather than mistaken for change), and reports
    each window's median residual. Each cell gets a deterministic 0-1 confidence dominated by within-cell roughness
    — the real failure mode, where two missions sample different sub-cell relief and fake a trend.

    Needs an ALREADY-BUILT scene (see list_scenes); it fetches nothing. Returns the top `limit` cells ranked by
    confidence, with n_candidates_total giving the true count. Use show_timeseries for one cell's actual series.
    Cell size is h3_res (7 ~1.2 km, 9 ~174 m, 11 ~25 m); delta_t is the time-window width in years."""
    out = _anticipated(api.timeseries_candidates, scene_id, h3_res=h3_res, delta_t=delta_t,
                       ref_missions=ref_missions, min_bins=min_bins, limit=limit)
    return {**out, "view": "ts", "url": ts_url(scene_id)}


def scene_level_url(scene_id: str, level: str) -> str:
    return f"{base_url()}/#scene/{scene_id}?level={level}"


@apps.tool(resource_uri=UI_URI, name="survey_coverage")
def survey_coverage(lat: float, lon: float, radius_km: float = 50.0) -> dict:
    """Which laser-altimetry missions have measured a place, how often, and in which years -- before fetching anything.

    Covers ICESat (GLAS, 2003-09), Operation IceBridge ATM (2009-19) and ICESat-2 ATL06 land ice (2018-). Reads only
    the pre-built index, so it is instant. `indexed` false means no index covers the area: say that, never "no data".
    `long_record` true means all three missions overlap there, so a 20-year elevation record is possible.
    `days` is how many distinct days a mission measured the area -- the count to quote. `passes` counts granules, and
    an IceBridge granule is a few minutes of one flight, so its passes overstate how often it flew there.
    Opens the globe on the area, each hex shaded by its granule count."""
    bbox = survey.area_bbox(lat, lon, radius_km)
    s = survey.area_summary(bbox)
    return {"area": {"lat": lat, "lon": lon, "radius_km": radius_km}, "bbox": bbox,
            "missions": {survey.LABELS[k]: v for k, v in s.items()},
            "indexed": any(v["hexes"] for v in s.values()),
            "long_record": all(v["passes"] for v in s.values()),
            "view": "survey"}


@apps.tool(resource_uri=UI_URI, name="elevation_change")
def elevation_change(lat: float, lon: float, h3_res: int = 8, limit: int = 10) -> dict:
    """How the ice or land surface height has changed at a place, across ICESat, IceBridge and ICESat-2 (2003-now),
    and where the record is long enough to tell.

    Fetches every mission's measurements over the ~17 km H3 hex containing (lat, lon), bins them into res-`h3_res`
    cells (8 ~ 530 m edge) and one-year windows, and removes each cell's surface slope -- which matters, because the
    missions sampled different spots on sloping ground. The slope is taken only from the spread of samples WITHIN each
    year, so change between years is never mistaken for slope. A cell whose slope removal is uncertain by more than 1 m
    where some year sampled it is low confidence: counted, not listed. Returns the reliable cells ranked by record
    length: total change in metres, first/last year, rate, contributing missions, slope_removal_err_m and a
    confidence reason. status "building" means the area is still being fetched: call again with the same arguments.
    Quote every rate with its caveats (no inter-mission bias correction, no GIA). Use show_timeseries for one cell."""
    out = _anticipated(api.elevation_change, lat, lon, radius_km=10.0, h3_res=h3_res, limit=limit)
    return {**out, "view": "scene", "query": "level=region", "open_url": scene_level_url(out["scene_id"], "region")}


@apps.tool(resource_uri=UI_URI, name="show_timeseries")
def show_timeseries(scene_id: str, h3: str, h3_res: int = 8, delta_t: float = 1.0,
                    ref_missions: list[str] | None = None, min_bins: int = 3) -> dict:
    """Show one cell's elevation time series and open the chart on it, with the cell outlined on satellite imagery.

    `scene_id` and `h3` come from elevation_change; keep its h3_res (default 8) -- a cell id only exists within the
    search that produced it. Returns each one-year window's median height relative to the cell's ICESat-2 reference
    plane, with its MAD error bar, its slope-removal error (plane_err_m) and the missions that contributed, plus the
    confidence breakdown and the least-squares trend in cm/yr. The trend is uncorrected for inter-mission bias and
    GIA -- relay that with it.

    `sample_geometry` says where each mission sampled the cell, the slopes the data implies and (where the product
    carries one) measures, and what the change would read if those positions were ignored -- differencing raw heights,
    or one plane fitted across all eras. Use it to explain why the answer needs the geometry."""
    ref = ref_missions or api.CHANGE_REF
    out = _anticipated(api.timeseries_cell, scene_id, h3, h3_res=h3_res, delta_t=delta_t,
                       ref_missions=ref, min_bins=min_bins)
    try:   # an explanation: its failure must never cost the chart
        geometry = api.cell_geometry(scene_id, h3, h3_res, delta_t, ref)
    except Exception as e:
        log.warning("sample geometry for %s/%s failed: %s", scene_id, h3, e)
        geometry = {"error": f"{type(e).__name__}: {e}"}
    return {**out, "view": "ts", "select": h3, "res": h3_res, "url": ts_url(scene_id, h3),
            "open_url": scene_level_url(scene_id, "region"), "sample_geometry": geometry}


# ----------------------------------------------------------------------------- app-visible tools (MCP Apps data plane)
_APP = dict(resource_uri=UI_URI, visibility=["app"])


@apps.tool(name="ui_regions", **_APP)
def ui_regions() -> dict:
    return api.list_regions()


@apps.tool(name="ui_scenes", **_APP)
def ui_scenes() -> dict:
    return {"scenes": [{**r, "widget_url": widget_url(r["scene_id"])} for r in api.scenes()]}


@apps.tool(name="ui_scene_part", **_APP)
def ui_scene_part(scene_id: str, part: str = "meta", chunk: int = 0) -> dict:
    return api.scene_part(scene_id, part, chunk, api.MCP_CHUNK_BYTES)


@apps.tool(name="ui_coverage", **_APP)
def ui_coverage(bbox: list[float] | None = None, polygon: list[list[float]] | None = None) -> dict:
    return api.check_coverage(bbox, polygon)


@apps.tool(name="ui_claims", **_APP)
def ui_claims() -> dict:
    return api.claim_regions()


@apps.tool(name="ui_coverage_hexes", **_APP)
def ui_coverage_hexes(res: int = 5, bbox: list[float] | None = None) -> dict:
    return survey.coverage_hexes(res, bbox)


@apps.tool(name="ui_fit", **_APP)
def ui_fit(keys: list[str], bbox: list[float] | None = None, polygon: list[list[float]] | None = None) -> dict:
    return api.fit_to_coverage(bbox, polygon, keys)


@apps.tool(name="ui_extract", **_APP)
def ui_extract(bbox: list[float] | None = None, polygon: list[list[float]] | None = None, question: str | None = None,
               with_glas: bool = True, with_coreg: bool = False,
               with_atl06: bool = False, with_icessn: bool = False, with_atl03: bool = False,
               with_gedi: bool = False, with_gpstruth: bool = False) -> dict:
    geom.normalize_area(bbox, polygon)
    j = api.start_job({"bbox": bbox, "polygon": polygon, "question": question,
                       "with_glas": with_glas, "with_coreg": with_coreg, "with_atl06": with_atl06,
                       "with_icessn": with_icessn, "with_atl03": with_atl03, "with_gedi": with_gedi,
                       "with_gpstruth": with_gpstruth})
    return {"job_id": j["id"], "scene_id": j["scene_id"]}


@apps.tool(name="ui_job", **_APP)
def ui_job(job_id: str) -> dict:
    j = api.job(job_id)
    return j if j else {"error": "no such job", "status": "error", "id": job_id, "log": []}


@apps.tool(name="ui_jobs", **_APP)
def ui_jobs() -> dict:
    return {"jobs": api.jobs()}


@apps.tool(name="ui_index_status", **_APP)
def ui_index_status(collection: str = "ATL06") -> dict:
    return api.index_status(collection)


@apps.tool(name="ui_coregister", **_APP)
def ui_coregister(scene_id: str) -> dict:
    return api.coregister(scene_id)


@apps.tool(name="ui_scene_imagery", **_APP)
def ui_scene_imagery(scene_id: str, source: str = "s2") -> dict:
    return api.scene_add_imagery(scene_id, source)


@apps.tool(name="ui_scene_delete", **_APP)
def ui_scene_delete(scene_id: str) -> dict:
    return api.delete_scene(scene_id)


@apps.tool(name="ui_candidates", **_APP)
def ui_candidates(scene_id: str, h3_res: int = 9, delta_t: float = 1.0, ref_missions: list[str] | None = None,
                  min_bins: int = 3, chunk: int = 0) -> dict:
    """The full candidate set as JSON text in MCP_CHUNK_BYTES slices: a demo hex's is ~470 KB and an MCP host drops
    tool results past ~150k characters. The search is memoised, so each chunk is a slice of the same answer."""
    out = api.scene_candidates(scene_id, h3_res=h3_res, delta_t=delta_t, ref_missions=ref_missions, min_bins=min_bins)
    text = json.dumps(out, default=cache._json_default)
    n, c = max(1, -(-len(text) // api.MCP_CHUNK_BYTES)), int(chunk)
    return {"n_chunks": n, "chunk": c, "text": text[c * api.MCP_CHUNK_BYTES:(c + 1) * api.MCP_CHUNK_BYTES]}


@apps.tool(name="ui_lake_cells", **_APP)
def ui_lake_cells(stats: bool = True, mission: str = "ICESAT2") -> dict:
    return api.lake_cells(stats=stats, mission=mission)


@apps.tool(name="ui_collections", **_APP)
def ui_collections() -> list:
    return api.list_collections()


@apps.tool(name="ui_lake_log", **_APP)
def ui_lake_log(after: int = 0) -> dict:
    return api.lake_log(after)


@apps.tool(name="ui_lake_summary", **_APP)
def ui_lake_summary(mission: str = "ICESAT2") -> dict:
    return api.lake_summary(mission)


@apps.tool(name="ui_lake_settings", **_APP)
def ui_lake_settings(max_bytes: int | None = None) -> dict:
    return api.lake_settings(max_bytes)


@apps.tool(name="ui_lake_load", **_APP)
def ui_lake_load(cells: list[str]) -> dict:
    j = api.lake_load(cells, None)
    return {"job_id": j["id"]}


@apps.tool(name="ui_lake_evict", **_APP)
def ui_lake_evict(cells: list[str]) -> dict:
    return api.lake_evict(cells)


@apps.tool(name="ui_bench", **_APP)
def ui_bench() -> dict:
    return api.bench() or {}


mcp = MCPServer(
    "aicesat",
    extensions=[apps],
    instructions=(
        "Cross-mission laser altimetry: ICESat (2003-09), Operation IceBridge (2009-19) and ICESat-2 (2018-), as one "
        "record per place. Three tools, one per question: survey_coverage (which missions measured here, and when -- "
        "instant, from the index), elevation_change (how the surface height changed, cell by cell, and where the "
        "record is long enough to tell), show_timeseries (one cell's full record, charted). Resolve place names to "
        "lat/lon yourself. The missions sampled different spots inside each cell on sloping ground, so each cell's "
        "slope is removed first, using only the spread of samples within each year (change is never mistaken for "
        "slope). slope_removal_err_m / plane_err_m are the standard error of that removal where each year sampled -- "
        "an optimistic estimate that treats neighbouring samples as independent; above 1 m a cell is low "
        "confidence. show_timeseries' sample_geometry shows what ignoring the positions would have read. Every rate "
        "is uncorrected for inter-mission bias and GIA: quote it "
        "with that caveat and with the cell's confidence and `why`, never as a bare number. Say how many cells were "
        "low confidence, not just the ones you were shown."
    ),
)

def list_regions() -> dict:
    """Named candidate Greenland demo regions (bbox = west, south, east, north) with validation notes."""
    return api.list_regions()


def list_scenes() -> list[dict]:
    """Scenes built so far (newest first) with status ready | loading | error, area, series present, and widget URL."""
    return [{**r, "widget_url": widget_url(r["scene_id"])} for r in api.scenes()]


def lake_status() -> dict:
    """Parquet lake summary: cells, files, rows, bytes, storage limit and usage, recent evictions."""
    return api.lake_summary()


def lake_load_cells(cells: list[str], time_window: list[str] | None = None) -> dict:
    """Materialize H3 (res 6) cells into the lake in the background (cell ids as decimal strings); returns a job id."""
    j = api.lake_load(cells, time_window)
    return {"job_id": j["id"]}


def job_status(job_id: str) -> dict:
    """Status and log of a background build job (scene or cell load)."""
    j = api.job(job_id)
    if not j:
        raise ValueError(f"no such job {job_id}")
    return j


def check_coverage(region: str | None = None, bbox: list[float] | None = None) -> dict:
    """How many granules of each collection (ICESat/GLAS, IceBridge ICESSN, ICESat-2 ATL06 and ATL03, GEDI) touch a
    region, with a per-month breakdown. Give either a region name (see list_regions) or an explicit bbox
    [W, S, E, N]. No data is fetched. Returns {bbox, collections: [...]}."""
    bb = regions.resolve_bbox(region, tuple(bbox) if bbox else None)
    return api.check_coverage(list(bb))


def main() -> None:
    start_http()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
