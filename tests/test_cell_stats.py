"""What each H3 cell of a scene holds: per mission, how many measurements, over which dates, at what heights.

The change map's hover and Cell panel read these. They are binned exactly as the candidate search bins (same
_load_all positions, same _cells_of), so a cell's counts here are the counts its analysis used -- and every point
counts, not the thinned set the page may draw."""
import json

import h3
import numpy as np
import pytest

from aicesat import api, cache
from aicesat import scene as scene_mod
from aicesat import timeseries
from aicesat.coreg import decimal_year

FRAME = scene_mod.local_frame((-45.5, 71.8, -44.5, 72.2))
DOC = {"frame": FRAME, "z0": 1000.0}
A = h3.latlng_to_cell(72.0, -45.0, 9)
B = h3.grid_ring(A, 3)[0]                  # a different res-9 cell, three cells away


def _rec(mission, cell, dates, heights):
    lat, lon = h3.cell_to_latlng(cell)
    n = len(dates)
    lat = np.full(n, lat); lon = np.full(n, lon)
    x, y = scene_mod.to_local(FRAME, lon, lat)
    t = np.asarray(dates, "datetime64[ms]")
    return {"mission": mission, "lat": lat, "lon": lon, "x": x, "y": y, "h": np.asarray(heights, "f8"),
            "yr": decimal_year(t)}


def _join(*recs):
    """One record per mission, as _load_all returns them."""
    out = {}
    for r in recs:
        if r["mission"] not in out:
            out[r["mission"]] = dict(r); continue
        o = out[r["mission"]]
        for k in ("lat", "lon", "x", "y", "h", "yr"):
            o[k] = np.concatenate([o[k], r[k]])
    return list(out.values())


@pytest.fixture
def recs(monkeypatch):
    rs = _join(
        _rec("GLAS", A, ["2005-03-01T12:00", "2005-03-01T12:00:01", "2008-10-20T03:00"], [1001.0, 1003.0, 999.0]),
        _rec("ATL06", A, ["2019-06-30T23:59:59", "2019-07-01T00:00:01", "2021-01-15"], [995.0, 996.0, 990.0]),
        _rec("ATL06", B, ["2020-02-29"], [1200.0]),
    )
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: rs)
    return rs


def test_counts_dates_and_heights_per_mission(recs):
    out = timeseries.cell_stats(DOC, h3_res=9)
    assert out["params"]["h3_res"] == 9
    cells = out["cells"]
    assert set(cells) == {A, B}
    g = cells[A]["GLAS"]
    assert (g["n"], g["first"], g["last"], g["days"]) == (3, "2005-03-01", "2008-10-20", 2)
    assert (g["h_med"], g["h_min"], g["h_max"]) == (1001.0, 999.0, 1003.0)
    a = cells[A]["ATL06"]
    # either side of midnight: two days, though one second apart
    assert (a["n"], a["first"], a["last"], a["days"]) == (3, "2019-06-30", "2021-01-15", 3)
    assert (a["h_med"], a["h_min"], a["h_max"]) == (995.0, 990.0, 996.0)
    assert cells[B] == {"ATL06": {"n": 1, "first": "2020-02-29", "last": "2020-02-29", "days": 1,
                                  "h_med": 1200.0, "h_min": 1200.0, "h_max": 1200.0}}


def test_every_point_lands_in_exactly_one_cell(recs):
    cells = timeseries.cell_stats(DOC, h3_res=9)["cells"]
    for r in recs:
        assert sum(c[r["mission"]]["n"] for c in cells.values() if r["mission"] in c) == r["h"].size


def test_binned_as_the_candidate_search_bins(recs):
    """A coarser cell holds the finer cells' points: the parent of A and B at res 7 counts all of them."""
    cells = timeseries.cell_stats(DOC, h3_res=7)["cells"]
    if h3.cell_to_parent(A, 7) == h3.cell_to_parent(B, 7):
        (only,) = cells.values()
        assert only["ATL06"]["n"] == 4 and only["GLAS"]["n"] == 3
    for c in cells:
        assert h3.get_resolution(c) == 7


def test_no_points_no_cells(monkeypatch):
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: [])
    assert timeseries.cell_stats(DOC, h3_res=8)["cells"] == {}


def test_api_memoises_and_reports_a_missing_scene(tmp_path, monkeypatch, recs):
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path)
    (tmp_path / "s1.json").write_text(json.dumps({"frame": FRAME, "z0": 1000.0, "series": {}}))
    calls = []
    real = timeseries.cell_stats
    monkeypatch.setattr(timeseries, "cell_stats", lambda doc, h3_res: calls.append(h3_res) or real(doc, h3_res))
    first = api.scene_cells("s1", h3_res=9)
    assert api.scene_cells("s1", h3_res=9) is first and calls == [9]
    api.scene_cells("s1", h3_res=8)
    assert calls == [9, 8]
    with pytest.raises(KeyError):
        api.scene_cells("nope", h3_res=9)


# --- the two transports ----------------------------------------------------------------------------------------
BIG = {"params": {"h3_res": 11}, "cells": {f"8b06f20b69{i:05x}": {"ATL06": {"n": i, "first": "2019-01-01",
       "last": "2026-01-01", "days": 9, "h_med": 1.0, "h_min": 0.0, "h_max": 2.0}} for i in range(3000)}}


def test_ui_cells_arrives_in_chunks_a_host_will_carry(monkeypatch):
    from aicesat import server
    monkeypatch.setattr(api, "scene_cells", lambda *a, **k: BIG)
    first = server.ui_cells("s1", h3_res=11)
    parts = [server.ui_cells("s1", h3_res=11, chunk=c) for c in range(first["n_chunks"])]
    assert first["n_chunks"] > 1
    assert all(len(p["text"]) <= api.MCP_CHUNK_BYTES for p in parts)
    assert json.loads("".join(p["text"] for p in parts)) == BIG


def test_ui_cells_is_for_the_app_only():
    import asyncio

    from aicesat import server
    tools = {t.name: ((t.meta or {}).get("ui") or {}).get("visibility") for t in asyncio.run(server.mcp.list_tools())}
    assert tools.get("ui_cells") == ["app"]


@pytest.fixture
def live(monkeypatch):
    import threading

    from aicesat import server
    monkeypatch.setattr(server, "ACCESS_CODE", "")
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()


def _post(addr, path, body):
    from http.client import HTTPConnection
    conn = HTTPConnection(*addr, timeout=10)
    conn.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, {}


def test_the_route_answers_with_the_cells_at_the_asked_size(live, monkeypatch):
    asked = []

    def cells(sid, h3_res=8):
        if sid != "s1":
            raise KeyError(sid)
        asked.append(h3_res)
        return {"params": {"h3_res": h3_res}, "cells": {}}
    monkeypatch.setattr(api, "scene_cells", cells)
    assert _post(live, "/api/cells/s1", {"h3_res": 9}) == (200, {"params": {"h3_res": 9}, "cells": {}})
    assert asked == [9]
    assert _post(live, "/api/cells/nope", {"h3_res": 9})[0] == 404


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node not available")
def test_both_adapters_fetch_the_cells():
    import subprocess
    r = subprocess.run(["node", "tests/test_adapter_cells.js"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
