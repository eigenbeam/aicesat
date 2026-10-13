"""reset_region returns one clicked hex to its state before anyone clicked it: only the index built.

What a click leaves behind, and therefore what the reset must remove: the hex's change-map scene and the studies
started from it, the extracts those scenes point at, and every lake cell holding ground inside the hex — files AND
ingest rows, because the planner skips any chunk whose cells all have rows. What it must leave: the index, imagery,
the DEM, and everything belonging to other hexes.
"""
import h3
import numpy as np
import pytest

from aicesat import api, cache, lake, scene

HX = "8506f257fffffff"                                       # a real demo hex (West Greenland)
NB = sorted(set(h3.grid_disk(HX, 1)) - {HX})[0]               # one of its neighbours
POLY = h3.LatLngPoly(h3.cell_to_boundary(HX))
R6_OVERLAP = set(h3.h3shape_to_cells_experimental(POLY, 6, contain="overlap"))
R6_INSIDE = h3.cell_to_center_child(HX, 6)                    # the one res-6 cell wholly inside the hex
R6_EDGE = sorted(R6_OVERLAP - set(h3.h3shape_to_cells_experimental(POLY, 6, contain="full")))[0]
R6_OUTSIDE = h3.cell_to_center_child(NB, 6)                   # ATL03 ground entirely in the neighbour
I = h3.str_to_int


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    for sub in ("cache", "scenes", "lake", "index"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    monkeypatch.setattr(api, "REGISTRY", tmp_path / "scenes" / "registry.json")
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "index" / "meta.duckdb")
    monkeypatch.setattr(lake, "EVICTION_LOG", tmp_path / "index" / "evictions.jsonl")
    monkeypatch.setattr(lake, "_STATS_CACHE", {})
    monkeypatch.setattr(api, "_jobs", {})
    monkeypatch.setattr(api, "_region_jobs", {})
    return tmp_path


def _scene(sid, question, keys):
    doc = scene.new_scene(sid, (-50.2, 69.2, -49.7, 69.4), question, polygon=None)
    doc["series"] = {m: {"mission": m, "cache_key": k} for m, k in keys.items()}
    cache.save_scene(sid, doc)
    api.registry_upsert(sid, question=question, status="ready", series=sorted(keys))
    (cache.SCENE_DIR / sid).mkdir(exist_ok=True)
    (cache.SCENE_DIR / sid / "ATL06.lon.f32").write_bytes(b"\0" * 8)
    for k in keys.values():
        if k:
            cache.save(k, {"lon": np.array([1.0])}, {"n": 1})
    return doc


def _lake_cell(mission, cell, granule="G1"):
    d = lake.cell_dir(mission, I(cell)); d.mkdir(parents=True, exist_ok=True)
    (d / f"{granule}__gt1l__c0.parquet").write_bytes(b"p" * 100)
    lake.mark_ingested(mission, granule, "gt1l", {0: [I(cell)]})


def _has_rows(mission, cell):
    return any(c == I(cell) for _, _, _, c in lake.ingested_chunk_cells(mission, ["G1"]))


def test_the_hexs_change_map_and_its_studies_go_and_other_scenes_stay():
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    _scene("stu0000001", f"study: 2 cells of H3 {HX}", {"ATL03": "a2"})
    _scene("stu0000002", "study: 1 cells of reg0000001", {"ATL03": "a3"})   # parent had no question: named by id
    _scene("nbr0000001", f"H3 {NB}", {"GLAS": "b1"})
    _scene("nbs0000001", f"study: 1 cells of H3 {NB}", {"ATL03": "b2"})
    _scene("exp0000001", "my drawn box", {"GLAS": "b3"})

    out = api.reset_region(HX)

    assert sorted(out["scenes_deleted"]) == ["reg0000001", "stu0000001", "stu0000002"]
    left = {s["scene_id"] for s in api.scenes()}
    assert left == {"nbr0000001", "nbs0000001", "exp0000001"}
    assert not (cache.SCENE_DIR / "reg0000001").exists() and not (cache.SCENE_DIR / "reg0000001.json").exists()
    assert (cache.SCENE_DIR / "nbr0000001" / "ATL06.lon.f32").exists()


def test_the_extracts_its_scenes_point_at_go_and_no_others():
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1", "ATL06": "a2", "ICESSN": None})   # a partial series has no key
    _scene("stu0000001", f"study: 1 cells of H3 {HX}", {"ATL03": "a3"})
    _scene("nbr0000001", f"H3 {NB}", {"GLAS": "b1"})

    out = api.reset_region(HX)

    assert out["extracts_deleted"] == 3
    for k in ("a1", "a2", "a3"):
        assert cache.load(k) is None, k
    assert cache.load("b1") is not None


def test_res5_missions_forget_only_the_hex_cell_never_its_neighbours():
    # The fetch cover of a hex includes its neighbours (they share edges), but no point inside the hex is ever
    # filed under a neighbour's res-5 cell, so the reset has no reason to touch them.
    for m in ("GLAS", "ATL06", "ICESSN"):
        _lake_cell(m, HX); _lake_cell(m, NB)

    out = api.reset_region(HX)

    for m in ("GLAS", "ATL06", "ICESSN"):
        assert not lake.cell_dir(m, I(HX)).exists(), m
        assert not _has_rows(m, HX), m
        assert lake.cell_dir(m, I(NB)).exists(), m
        assert _has_rows(m, NB), m
        assert out["lake"][m]["cells"] == [HX]


def test_ingest_rows_go_even_for_a_cell_that_holds_no_file():
    # index_atl06 marks every wanted cell of a fetched chunk, so an all-fill cell has rows and no directory.
    # evict_cells skips such a cell; left behind, its rows would stop the next build from fetching those chunks.
    lake.mark_ingested("ATL06", "G1", "gt1l", {0: [I(HX)]})
    assert _has_rows("ATL06", HX)

    api.reset_region(HX)

    assert not _has_rows("ATL06", HX)


def test_res6_atl03_cells_overlapping_the_hex_go_and_the_straddling_ones_are_reported():
    for c in (R6_INSIDE, R6_EDGE, R6_OUTSIDE):
        _lake_cell("ICESAT2", c)

    out = api.reset_region(HX)

    assert not lake.cell_dir("ICESAT2", I(R6_INSIDE)).exists()
    assert not lake.cell_dir("ICESAT2", I(R6_EDGE)).exists()       # it holds ground inside the hex
    assert lake.cell_dir("ICESAT2", I(R6_OUTSIDE)).exists()
    assert sorted(out["lake"]["ICESAT2"]["cells"]) == sorted([R6_INSIDE, R6_EDGE])
    assert out["straddling"] == {"ICESAT2": [R6_EDGE]}


def test_the_index_imagery_and_dem_are_kept():
    doc = _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    img = cache.CACHE_DIR / "imagery" / "4fb2f06a7361ce73.jpg"; img.parent.mkdir(); img.write_bytes(b"jpg")
    dem = cache.CACHE_DIR / "dem" / "0123456789abcdef.npz"; dem.parent.mkdir(); dem.write_bytes(b"dem")
    doc["imagery"] = {"path": str(img)}; cache.save_scene("reg0000001", doc)
    idx = lake.INDEX_DIR / "atl06" / "res5" / "G1.parquet"; idx.parent.mkdir(parents=True); idx.write_bytes(b"idx")
    _lake_cell("ATL06", HX)

    api.reset_region(HX)

    assert img.read_bytes() == b"jpg" and dem.read_bytes() == b"dem" and idx.read_bytes() == b"idx"


def test_after_a_reset_the_next_click_starts_a_build(monkeypatch):
    # region_scene remembers the job it started per hex. A reset that left that memo would hand the next click the
    # deleted scene as "ready".
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    api._jobs["j1"] = {"id": "j1", "status": "done", "scene_id": "reg0000001", "started": 0}
    api._region_jobs[HX] = {"scene_id": "reg0000001", "job_id": "j1"}
    started = []
    monkeypatch.setattr(api, "start_job", lambda p, kind="scene": started.append(p) or {"id": "j2", "scene_id": "new0000001"})

    api.reset_region(HX)

    assert api.region_scene(HX) == {"scene_id": "new0000001", "job_id": "j2", "status": "loading"}
    assert len(started) == 1


def test_deleting_a_model_built_scene_lets_its_hex_build_again(monkeypatch):
    # The same memo bug, through Explore's delete: before this, region_scene returned the deleted id as "ready".
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    api._jobs["j1"] = {"id": "j1", "status": "done", "scene_id": "reg0000001", "started": 0}
    api._region_jobs[HX] = {"scene_id": "reg0000001", "job_id": "j1"}
    monkeypatch.setattr(api, "start_job", lambda p, kind="scene": {"id": "j2", "scene_id": "new0000001"})

    api.delete_scene("reg0000001")

    assert api.region_scene(HX)["scene_id"] == "new0000001"


def test_a_running_build_refuses_the_reset_and_nothing_is_deleted():
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    _lake_cell("ATL06", HX)
    api._jobs["j1"] = {"id": "j1", "status": "running", "scene_id": "other00001", "started": 0}

    with pytest.raises(api.Busy):
        api.reset_region(HX)

    assert cache.load_scene("reg0000001") is not None and cache.load("a1") is not None
    assert lake.cell_dir("ATL06", I(HX)).exists() and _has_rows("ATL06", HX)


def test_a_registry_row_stuck_at_loading_does_not_block_the_reset():
    # A server restart kills a build's thread but not its "loading" registry row; only a live job means busy.
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    api.registry_upsert("reg0000001", status="loading")

    assert api.reset_region(HX)["scenes_deleted"] == ["reg0000001"]


def test_a_second_reset_finds_nothing():
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})
    _lake_cell("ATL06", HX)
    api.reset_region(HX)

    out = api.reset_region(HX)

    assert out["scenes_deleted"] == [] and out["extracts_deleted"] == 0 and out["lake"] == {}


@pytest.mark.parametrize("bad", ["../etc", "not-a-cell", "", None])
def test_an_invalid_hex_is_refused(bad):
    with pytest.raises(ValueError):
        api.reset_region(bad)


# --- transport: the HTTP route the browser calls, and the app-only MCP tool -----------------------------------
@pytest.fixture
def live(store, monkeypatch):
    import threading

    from aicesat import server
    monkeypatch.setattr(server, "ACCESS_CODE", "")
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()


def _post(addr, path):
    import json
    from http.client import HTTPConnection
    conn = HTTPConnection(*addr, timeout=10)
    conn.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:                       # send_error's HTML page (e.g. a 404 for a route that is not there)
        return r.status, {}


def test_the_route_resets_the_hex_and_reports_what_went(live):
    _scene("reg0000001", f"H3 {HX}", {"GLAS": "a1"})

    status, body = _post(live, f"/api/region/{HX}/reset")

    assert status == 200 and body["scenes_deleted"] == ["reg0000001"]
    assert cache.load_scene("reg0000001") is None


def test_the_route_answers_409_while_a_build_runs(live):
    api._jobs["j1"] = {"id": "j1", "status": "running", "scene_id": "x", "started": 0}

    status, body = _post(live, f"/api/region/{HX}/reset")

    assert status == 409 and "running" in body["error"]


def test_the_route_answers_400_for_a_bad_hex(live):
    assert _post(live, "/api/region/zzz/reset")[0] == 400


def test_the_app_can_reset_a_hex_and_the_model_cannot():
    import asyncio

    from aicesat import server
    tools = {t.name: ((t.meta or {}).get("ui") or {}).get("visibility") for t in asyncio.run(server.mcp.list_tools())}
    assert tools.get("ui_region_reset") == ["app"]
    assert not [n for n, vis in tools.items() if "reset" in n and vis != ["app"]]


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node not available")
def test_both_adapters_send_the_reset_to_the_server():
    import subprocess
    from pathlib import Path
    r = subprocess.run(["node", str(Path(__file__).with_name("test_adapter_reset.js"))], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, f"adapter reset checks failed:\n{r.stdout}\n{r.stderr}"
