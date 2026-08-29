"""Offline tests for the parallel-fan-out build_scene (progressive scene paint).

Every network leg (the four collection extracts, the DEM, the imagery) is monkeypatched with a synthetic, optionally
slow, in-memory stand-in, and the cache/registry are redirected to a tmp dir. These tests pin the invariants that the
fan-out must NOT change relative to the old serial build:
  * z0 comes from the highest-PRIORITY collection that returned data (GLAS > ICESSN > ATL06 > ATL03), from its raw
    heights, and every series' z is relative to that z0;
  * the series dict is keyed in priority order;
  * a failing leg is skipped and logged, never fatal;
  * the doc is persisted progressively (shell first, then per leg) so it is readable mid-build;
  * the independent legs actually overlap in time.
"""
import threading
import time

import numpy as np
import pytest

from aicesat import api, cache, scene


BBOX = [-45.0, 70.0, -44.9, 70.05]   # Greenland -> frame CRS EPSG:3413 (deterministic, no per-scene aeqd)


def _arrays(n, base_h, seed):
    rng = np.random.default_rng(seed)
    return {"lon": -45.0 + rng.uniform(0, 0.1, n), "lat": 70.0 + rng.uniform(0, 0.05, n),
            "h": (base_h + rng.normal(0, 1.0, n)).astype("f8")}


# distinct base heights so z0 (a median) reveals which collection anchored it
GLAS_A = _arrays(40, 1000.0, 1)
ICESSN_A = _arrays(30, 2000.0, 2)
ATL06_A = _arrays(20, 1500.0, 3)
ATL03_A = _arrays(50, 1200.0, 4)

SURFACE = {"x0": 0.0, "y0": 0.0, "cell": 100.0, "nx": 2, "ny": 2, "z": [1.0, 2.0, 3.0, 4.0],
           "source": "MockDEM", "attribution": "mock", "is_dem": True, "n_cells_observed": 4,
           "n_cells_extrapolated": 0, "nodata_cells": 0, "note": "mock surface"}
IMAGERY = {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0, "zoom": 11, "m_per_px": 1.0, "width": 256, "height": 256,
           "attribution": "mock imagery", "source": "mock", "path": "/dev/null"}


def _install(monkeypatch, tmp_path, *, delay=0.0, fail=()):
    """Redirect the cache/registry to tmp and stub every network leg. `fail` names legs whose extract should raise."""
    from aicesat import atl03, atl06, glas, icessn, imagery, dem, lake

    (tmp_path / "cache").mkdir(); (tmp_path / "scenes").mkdir()
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    monkeypatch.setattr(api, "REGISTRY", tmp_path / "scenes" / "registry.json")
    monkeypatch.setattr(lake, "write_points", lambda *a, **k: None)
    # Sandbox the lake so the post-build background eviction (spawned because the ATL03 mock reports chunks_fetched)
    # scans an empty tmp lake, never the real data/lake or its shared meta DuckDB.
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lake, "META_DB", tmp_path / "meta.duckdb")

    def mk(name, arrays, extra):
        def _extract(*a, **k):
            if delay:
                time.sleep(delay)
            if name in fail:
                raise RuntimeError(f"{name} boom")
            meta = {"cache_key": f"{name}-key", "n": int(arrays["h"].size), **extra}
            return dict(arrays), meta
        return _extract

    monkeypatch.setattr(glas, "extract", mk("GLAS", GLAS_A, {"campaigns": ["2003", "2004"]}))
    monkeypatch.setattr(icessn, "extract", mk("ICESSN", ICESSN_A, {"years": [2010, 2011]}))
    monkeypatch.setattr(atl06, "extract", mk("ATL06", ATL06_A, {}))
    monkeypatch.setattr(atl03, "extract", mk("ATL03", ATL03_A, {"access": {"chunks_fetched": 3, "bytes": 1e6, "requests": 9,
                                                                           "chunks_skipped_already_materialized": 1}}))

    # DEM/imagery are fetched once (the prefetch) then re-read from cache; simulate that so the authoritative second
    # call (set_surface / add_imagery) is instant, exactly like a real cache hit.
    warmed = set()

    def _surface(*a, **k):
        if delay and "dem" not in warmed:
            warmed.add("dem"); time.sleep(delay)
        return dict(SURFACE)
    monkeypatch.setattr(dem, "surface_for_frame", _surface)

    def _imagery(*a, **k):
        if delay and "img" not in warmed:
            warmed.add("img"); time.sleep(delay)
        return dict(IMAGERY)
    monkeypatch.setattr(imagery, "build", _imagery)


def _await_imagery(scene_id, timeout=10.0):
    """Imagery is fetched off the build thread (it must never block the scene), so a test that asserts on it has to
    wait for imagery_status to leave 'pending' instead of reading the doc the instant build_scene returns."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = cache.load_scene(scene_id)
        if doc and doc.get("imagery_status") in ("ready", "unavailable"):
            return doc
        time.sleep(0.01)
    raise AssertionError(f"imagery still pending after {timeout}s")


def _z_values(series):
    """z components of a flat [x,y,z,...] positions list."""
    return np.asarray(series["positions"], dtype="f8").reshape(-1, 3)[:, 2]


def test_all_collections_final_doc(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)

    # series dict keyed in priority order (GLAS, ICESSN, ATL06, then ATL03's ICESAT2 key)
    assert list(doc["series"]) == ["GLAS", "ICESSN", "ATL06", "ICESAT2"]

    # z0 from the DEM median (terrain-centred, deterministic, independent of collection arrival order)
    assert doc["z0"] == pytest.approx(float(np.median(SURFACE["z"])))

    # every series' z is relative to that one z0 (check via ICESSN, whose base height is well separated)
    assert _z_values(doc["series"]["ICESSN"]).mean() == pytest.approx(ICESSN_A["h"].mean() - doc["z0"], abs=1e-2)

    # surface + imagery attached and z0-independent for the surface (mock ignores z0)
    assert doc["surface"] == SURFACE
    doc = _await_imagery(doc["scene_id"])   # imagery lands off the build thread — wait for it, don't race it
    assert doc["imagery"]["width"] == 256 and doc["imagery"]["url"].endswith("/imagery.jpg")

    # the returned doc is exactly the final persisted doc
    assert cache.load_scene(doc["scene_id"])["z0"] == doc["z0"]

    # registry marked ready with the full series list
    rec = api._registry()[doc["scene_id"]]
    assert rec["status"] == "ready" and rec["series"] == sorted(doc["series"])


def test_z0_priority_when_glas_absent(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    doc = api.build_scene(bbox=BBOX, with_glas=False, with_icessn=True, with_atl06=True, with_atl03=False)
    assert list(doc["series"]) == ["ICESSN", "ATL06"]   # final order normalised to priority even though arrival streams
    assert doc["z0"] == pytest.approx(float(np.median(SURFACE["z"])))   # z0 from DEM, independent of collections


def test_leg_failure_degrades_not_fatal(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, fail=("GLAS",))
    logs = []
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True,
                          log_fn=logs.append)
    assert "GLAS" not in doc["series"]                     # failed leg skipped
    assert list(doc["series"]) == ["ICESSN", "ATL06"]
    assert doc["z0"] == pytest.approx(float(np.median(SURFACE["z"])))   # z0 from the DEM, unaffected by the GLAS failure
    assert any(l.startswith("GLAS unavailable") for l in logs)


def test_z0_falls_back_to_collection_when_no_dem(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    from aicesat import dem
    monkeypatch.setattr(dem, "surface_for_frame", lambda *a, **k: None)   # no DEM covers the scene
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=False)
    assert doc["surface"] is None                                          # no DEM -> no surface
    # z0 falls back to whichever collection arrived first (its median)
    assert any(doc["z0"] == pytest.approx(float(np.median(a["h"]))) for a in (GLAS_A, ICESSN_A, ATL06_A))


def test_no_data_raises_and_marks_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, fail=("GLAS", "ICESSN", "ATL06", "ATL03"))
    with pytest.raises(RuntimeError):
        api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    # a scene_id was registered; even though we can't see it, the registry has exactly one error record
    recs = list(api._registry().values())
    assert recs and all(r["status"] == "error" for r in recs)


def test_slow_imagery_does_not_delay_the_build(monkeypatch, tmp_path):
    """Imagery is a base layer, not a gate. A slow tile source used to hold the whole scene in 'loading' long after
    every point had painted (it was awaited inside the build's thread pool, whose exit waits on every future)."""
    from aicesat import imagery

    _install(monkeypatch, tmp_path)
    started = threading.Event()

    def _slow(*a, **k):
        started.set()
        time.sleep(3.0)                                     # far longer than the whole build
        return dict(IMAGERY)
    monkeypatch.setattr(imagery, "build", _slow)

    t0 = time.time()
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    elapsed = time.time() - t0

    assert started.is_set()                                 # the imagery fetch really did start
    assert elapsed < 2.0, f"build waited on imagery ({elapsed:.1f}s)"
    assert len(doc["series"]) == 4                          # data is complete and usable without imagery
    assert doc["imagery_status"] == "pending" and doc.get("imagery") is None


def test_slow_imagery_does_not_hold_the_doc_lock(monkeypatch, tmp_path):
    """Regression: the imagery leg must not do network work while holding the shared doc lock. It once did (via
    scene.add_imagery, which re-enters imagery.build), so the build thread blocked on that lock for its final save —
    the scene painted every point and then sat in 'Streaming data…' because the job never reached ready."""
    from aicesat import imagery

    _install(monkeypatch, tmp_path)
    release = threading.Event()
    calls = []

    def _fast_then_slow(*a, **k):
        """Fast on the first call, slow on any re-entry. That models the real failure: the initial fetch is cheap (or
        cached), but calling scene.add_imagery under the lock re-enters imagery.build — and THAT call is the one that
        stalls (S2 STAC retry) while holding the doc lock the build thread needs."""
        calls.append(1)
        if len(calls) > 1:
            release.wait(5.0)
        return dict(IMAGERY)
    monkeypatch.setattr(imagery, "build", _fast_then_slow)

    t0 = time.time()
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    elapsed = time.time() - t0
    release.set()
    assert elapsed < 2.0, f"build blocked on the imagery leg ({elapsed:.1f}s)"
    assert len(doc["series"]) == 4
    final = _await_imagery(doc["scene_id"])                 # and it still completes afterwards
    assert final["imagery_status"] == "ready" and final["imagery"]["width"] == 256


def test_imagery_failure_marks_status_and_never_fails_the_build(monkeypatch, tmp_path):
    from aicesat import imagery

    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(imagery, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tile server down")))
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    assert len(doc["series"]) == 4                          # the scene still built
    final = _await_imagery(doc["scene_id"])
    assert final["imagery_status"] == "unavailable" and final.get("imagery") is None


def test_progressive_persistence(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    snapshots = []
    orig = cache.save_scene
    monkeypatch.setattr(cache, "save_scene", lambda sid, d: (snapshots.append(sorted(d.get("series", {}))), orig(sid, d))[1])

    api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)

    # shell persisted first (no series yet), then the series list grows monotonically, then surface + imagery saves
    assert snapshots[0] == []
    lengths = [len(s) for s in snapshots]
    assert lengths == sorted(lengths)                       # never shrinks
    assert lengths[-1] == 4                                 # ends with all four series
    assert len(snapshots) >= 1 + 4 + 2                      # shell + 4 series + surface + imagery


def test_start_job_runs_fanout_to_completion(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    job = api.start_job({"bbox": BBOX, "with_glas": True, "with_icessn": True, "with_atl06": True, "with_atl03": True},
                        kind="scene")
    sid = job["scene_id"]
    deadline = time.time() + 20
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert job["status"] == "done", job.get("error")

    _await_imagery(sid)                                     # imagery finishes after the job does; don't race it
    doc = api.scene_part(sid, "meta")                       # the HTTP-facing partial reader sees the finished scene
    assert list(doc["series"]) == ["GLAS", "ICESSN", "ATL06", "ICESAT2"]
    assert doc["surface"] is not None and doc["imagery"] is not None

    # the per-leg log lines the client checklist keys off are all present and unchanged
    log = job["log"]
    assert any(l.startswith("GLAS: ") for l in log)
    assert any(l.startswith("ICESSN: ") for l in log)
    assert any(l.startswith("ATL06: ") for l in log)
    assert any(l.startswith("ATL03: ") and "photons" in l for l in log)
    assert "surface: DEM base surface" in log
    assert any(l.startswith("imagery: ") for l in log)


def test_delete_scene_removes_only_the_scene_not_the_lake_or_cache(monkeypatch, tmp_path):
    """delete_scene removes ONLY the scene's registry row + doc (+ any scene-scoped dir). The shared lake and the
    content-addressed extract cache survive, so rebuilding the same area still hits them (zero NASA GETs)."""
    from aicesat import lake

    (tmp_path / "cache").mkdir(); (tmp_path / "scenes").mkdir()
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "SCENE_DIR", tmp_path / "scenes")
    monkeypatch.setattr(api, "REGISTRY", tmp_path / "scenes" / "registry.json")
    monkeypatch.setattr(lake, "LAKE_DIR", tmp_path / "lake")

    sid = "abc1234567"
    doc = scene.new_scene(sid, tuple(BBOX), "q", polygon=None)
    doc["series"] = {"GLAS": {"cache_key": "shared-extract-key"}}   # the scene points at a SHARED extract-cache entry
    cache.save_scene(sid, doc)
    api.registry_upsert(sid, question="q", bbox=list(BBOX), status="ready", series=["GLAS"])
    sdir = cache.SCENE_DIR / sid; sdir.mkdir(); (sdir / "artifact.bin").write_bytes(b"x")   # a scene-scoped render dir
    # SHARED state that MUST survive: the extract cache the series references + a materialized lake cell file
    cache.save("shared-extract-key", {"lon": np.array([1.0])}, {"n": 1})
    cell_dir = lake.LAKE_DIR / "mission=GLAS" / "h3_cell=123"; cell_dir.mkdir(parents=True)
    (cell_dir / "g__na__c0.parquet").write_bytes(b"lakebytes")

    assert cache.load_scene(sid) is not None and sid in api._registry()

    out = api.delete_scene(sid)
    assert out["deleted"] and out["existed"]

    # the scene's own footprint is gone (doc, registry row, scene-scoped dir), and it drops out of the listing
    assert cache.load_scene(sid) is None
    assert not (cache.SCENE_DIR / f"{sid}.json").exists()
    assert sid not in api._registry()
    assert not sdir.exists()
    assert sid not in {s["scene_id"] for s in api.scenes()}

    # SHARED lake + extract cache untouched -> the data survived the delete
    assert (cell_dir / "g__na__c0.parquet").read_bytes() == b"lakebytes"
    assert cache.load("shared-extract-key") is not None

    # deleting a non-existent scene is a no-op; path-traversal input is refused
    assert api.delete_scene(sid)["existed"] is False
    with pytest.raises(ValueError):
        api.delete_scene("../evil")


def test_legs_overlap_in_time(monkeypatch, tmp_path):
    delay = 0.25
    _install(monkeypatch, tmp_path, delay=delay)
    t0 = time.time()
    api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    elapsed = time.time() - t0
    # 6 independent legs (4 collections + DEM + imagery) each sleep `delay`. Serial would be >= 6*delay = 1.5 s;
    # concurrent should finish in a small multiple of a single delay. Generous bound proves overlap without flakiness.
    assert elapsed < 3 * delay, f"legs did not overlap: {elapsed:.2f}s for 6x{delay}s legs"


def test_per_granule_stream_buffers_before_z0_then_finalize_reconciles(monkeypatch, tmp_path):
    """The end-to-end streaming path: an index mission emits a per-granule PREVIEW via on_granule while the DEM (which
    owns z0) is still resolving. The preview must be BUFFERED (baking is z0-relative) and flushed once z0 lands, then
    the authoritative add_series must REPLACE it. A slow DEM makes the pre-z0 buffer path deterministic."""
    _install(monkeypatch, tmp_path, delay=0.05)          # DEM sleeps -> z0 is not set when ATL06 streams
    from aicesat import atl06, scene

    preview = {k: v[:10] for k, v in ATL06_A.items()}    # a 10-point subset stands in for the first pass's display pts

    def _atl06_stream(*a, **k):                          # streams a partial, THEN returns the full authoritative arrays
        cb = k.get("on_granule")
        if cb:
            cb({"granule": "ATL06_pass.h5", "lon": preview["lon"], "lat": preview["lat"], "h": preview["h"],
                "t": np.zeros(preview["lon"].size, "datetime64[ms]")})
        return dict(ATL06_A), {"cache_key": "ATL06-key", "n": int(ATL06_A["h"].size)}
    monkeypatch.setattr(atl06, "extract", _atl06_stream)

    calls, z0_at_call = [], []
    orig = scene.append_partial
    def _spy(doc, mission, pts):
        calls.append((mission, np.asarray(pts["lon"]).size)); z0_at_call.append(doc.get("z0"))
        return orig(doc, mission, pts)
    monkeypatch.setattr(scene, "append_partial", _spy)

    doc = api.build_scene(bbox=BBOX, with_glas=False, with_icessn=False, with_atl06=True, with_atl03=False)

    assert ("ATL06", 10) in calls                        # the preview was baked into the doc (buffered then flushed)
    assert all(z is not None for z in z0_at_call)        # never baked before z0 was known (no invented z0)
    # finalize reconciled: the FINAL series is the authoritative 20-point set, not the 10-point preview
    assert doc["series"]["ATL06"]["n"] == ATL06_A["h"].size
    assert doc["series"]["ATL06"]["meta"].get("partial") is not True
    assert doc["z0"] == pytest.approx(float(np.median(SURFACE["z"])))   # z0 from the DEM, as always
