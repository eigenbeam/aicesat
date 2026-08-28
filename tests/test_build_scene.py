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


def _z_values(series):
    """z components of a flat [x,y,z,...] positions list."""
    return np.asarray(series["positions"], dtype="f8").reshape(-1, 3)[:, 2]


def test_all_collections_final_doc(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)

    # series dict keyed in priority order (GLAS, ICESSN, ATL06, then ATL03's ICESAT2 key)
    assert list(doc["series"]) == ["GLAS", "ICESSN", "ATL06", "ICESAT2"]

    # z0 anchored on GLAS (highest priority), from its RAW heights (pre-cleaning median)
    assert doc["z0"] == pytest.approx(float(np.median(GLAS_A["h"])))

    # every series' z is relative to that one z0 (check via ICESSN, whose base height is well separated)
    assert _z_values(doc["series"]["ICESSN"]).mean() == pytest.approx(ICESSN_A["h"].mean() - doc["z0"], abs=1e-2)

    # surface + imagery attached and z0-independent for the surface (mock ignores z0)
    assert doc["surface"] == SURFACE
    assert doc["imagery"]["width"] == 256 and doc["imagery"]["url"].endswith("/imagery.jpg")

    # the returned doc is exactly the final persisted doc
    assert cache.load_scene(doc["scene_id"])["z0"] == doc["z0"]

    # registry marked ready with the full series list
    rec = api._registry()[doc["scene_id"]]
    assert rec["status"] == "ready" and rec["series"] == sorted(doc["series"])


def test_z0_priority_when_glas_absent(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    doc = api.build_scene(bbox=BBOX, with_glas=False, with_icessn=True, with_atl06=True, with_atl03=False)
    assert list(doc["series"]) == ["ICESSN", "ATL06"]
    assert doc["z0"] == pytest.approx(float(np.median(ICESSN_A["h"])))   # ICESSN is now the priority anchor


def test_leg_failure_degrades_not_fatal(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, fail=("GLAS",))
    logs = []
    doc = api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True,
                          log_fn=logs.append)
    assert "GLAS" not in doc["series"]                     # failed leg skipped
    assert list(doc["series"]) == ["ICESSN", "ATL06"]
    assert doc["z0"] == pytest.approx(float(np.median(ICESSN_A["h"])))   # z0 falls through to next priority
    assert any(l.startswith("GLAS unavailable") for l in logs)


def test_no_data_raises_and_marks_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, fail=("GLAS", "ICESSN", "ATL06", "ATL03"))
    with pytest.raises(RuntimeError):
        api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    # a scene_id was registered; even though we can't see it, the registry has exactly one error record
    recs = list(api._registry().values())
    assert recs and all(r["status"] == "error" for r in recs)


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


def test_legs_overlap_in_time(monkeypatch, tmp_path):
    delay = 0.25
    _install(monkeypatch, tmp_path, delay=delay)
    t0 = time.time()
    api.build_scene(bbox=BBOX, with_glas=True, with_icessn=True, with_atl06=True, with_atl03=True)
    elapsed = time.time() - t0
    # 6 independent legs (4 collections + DEM + imagery) each sleep `delay`. Serial would be >= 6*delay = 1.5 s;
    # concurrent should finish in a small multiple of a single delay. Generous bound proves overlap without flakiness.
    assert elapsed < 3 * delay, f"legs did not overlap: {elapsed:.2f}s for 6x{delay}s legs"
