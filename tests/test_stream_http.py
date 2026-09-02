"""End-to-end for the prototype push transport: the real HTTP handler, over a real socket.

The unit tests cover the generator and the browser reader in isolation. What they cannot show is the thing most
likely to be wrong in a streaming endpoint — that bytes actually LEAVE the server before the response is finished.
Python's buffered wfile will happily hold every early frame until the handler returns, which looks identical in a
unit test and destroys the entire point of the transport. So this test reads the first frames off the socket while
the build is still "running", and only then lets it finish.

Also runs the node reader checks (tests/test_stream_reader.js), so both halves of the wire format travel together.
"""
import json
import shutil
import subprocess
import threading
from http.client import HTTPConnection
from pathlib import Path

import numpy as np
import pytest

from aicesat import api, cache, server, stream

JS = Path(__file__).parent / "test_stream_reader.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_reader_checks():
    r = subprocess.run(["node", str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"stream reader checks failed:\n{r.stdout}\n{r.stderr}"


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The real Handler on a real port, with the scene store pointed at tmp_path."""
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    monkeypatch.setattr(api, "REGISTRY", tmp_path / "scenes" / "registry.json")
    monkeypatch.setattr(server, "ACCESS_CODE", "")          # no gate in tests
    monkeypatch.setattr(api, "_jobs", {})
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()


def _scene(sid="s1"):
    cache.save_scene(sid, {"scene_id": sid, "frame": {}, "z0": 0.0,
                           "series": {"ATL06": {"mission": "ATL06", "color": [1, 2, 3], "n": 0}}})
    api.registry_upsert(sid, status="loading")


def _read_frames(fp, want_bulk):
    """Pull whole frames off the socket until `want_bulk` bulk frames have arrived. Reading header-then-payload from
    the live response is itself the assertion: if the handler buffered instead of flushing, these reads block until
    the test times out."""
    out = []
    while sum(1 for k, _m, _p in out if k != stream.KIND_CONTROL) < want_bulk:
        head = fp.read(stream.HEADER.size)
        if len(head) < stream.HEADER.size:
            break
        kind, mission, _flags, n = stream.HEADER.unpack(head)
        out.append((kind, mission, fp.read(n) if n else b""))
    return out


def test_frames_reach_the_client_before_the_build_finishes(live):
    """The flush assertion. The scene stays `loading`, so the generator will not terminate — if the first frames only
    arrived at the end, this read would block until the test timed out."""
    host, port = live
    _scene()
    cache.scene_array_append("s1", "ATL06", "positions", np.arange(30, dtype=cache.ARRAY_DTYPE))

    conn = HTTPConnection(host, port, timeout=20)
    conn.request("GET", "/api/scene/s1/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/octet-stream"

    got = _read_frames(resp, want_bulk=1)
    kinds = [k for k, _m, _p in got]
    assert stream.KIND_POSITIONS in kinds, "no point frame arrived while the build was still running"
    ctl = [json.loads(p) for k, _m, p in got if k == stream.KIND_CONTROL]
    assert ctl[0]["t"] == "init" and any(c["t"] == "mission" and c["name"] == "ATL06" for c in ctl)
    vals = np.concatenate([np.frombuffer(p, dtype=cache.ARRAY_DTYPE) for k, _m, p in got if k == stream.KIND_POSITIONS])
    assert list(vals[:3]) == [0.0, 1.0, 2.0]
    conn.close()


def test_a_finished_scene_streams_and_closes(live):
    host, port = live
    _scene("s2")
    cache.scene_array_append("s2", "ATL06", "positions", np.ones(12, dtype=cache.ARRAY_DTYPE))
    api.registry_upsert("s2", status="ready")

    conn = HTTPConnection(host, port, timeout=20)
    conn.request("GET", "/api/scene/s2/stream")
    body = conn.getresponse().read()          # reads to EOF: the handler must close when the build is terminal
    frames = list(stream.iter_frames(body))
    ctl = [json.loads(p) for k, _m, p in frames if k == stream.KIND_CONTROL]
    assert ctl[-1]["t"] == "done" and ctl[-1]["drained"] is True
    assert ctl[-1]["cursors"]["ATL06:positions"] == 12
    conn.close()


def test_resume_over_http_sends_only_the_tail(live):
    host, port = live
    _scene("s3")
    cache.scene_array_append("s3", "ATL06", "positions", np.arange(24, dtype=cache.ARRAY_DTYPE))
    api.registry_upsert("s3", status="ready")

    conn = HTTPConnection(host, port, timeout=20)
    conn.request("GET", "/api/scene/s3/stream?from=ATL06%3Apositions%3D12")
    body = conn.getresponse().read()
    vals = np.concatenate([np.frombuffer(p, dtype=cache.ARRAY_DTYPE)
                           for k, _m, p in stream.iter_frames(body) if k == stream.KIND_POSITIONS])
    assert list(vals) == list(np.arange(12, 24, dtype=cache.ARRAY_DTYPE))
    conn.close()


def test_unknown_scene_is_404(live):
    host, port = live
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/scene/nope/stream")
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()


def test_bench_transport_gate_token_matches_the_server():
    """scripts/bench_transport.py authenticates by reproducing the gate cookie. If server._gate_token changes, the
    bench starts failing with 401s that look like a deployment problem rather than a stale script."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bench_transport", Path(__file__).parent.parent / "scripts" / "bench_transport.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.gate_token("hunter2") == server._gate_token("hunter2")
    assert mod.DISPLAY_BUDGET == 400_000, "keep in step with ui/adapter.js"
