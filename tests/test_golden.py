"""Golden numbers from the UWG demo store: the refactor stages (S1-S5) must leave every one of them unchanged.

Runs only against a CLONE of the demo store, and only when asked:
    cp -c -R data-uwg data-golden          # APFS clone: instant, shares blocks
    AICESAT_DATA_DIR=<repo>/data-golden uv run pytest -m golden
Refresh after an INTENDED change (and say why in the commit):
    AICESAT_GOLDEN_UPDATE=1 AICESAT_DATA_DIR=<repo>/data-golden uv run pytest -m golden
A clone, not data-uwg itself: the read path is not read-only. coverage._ensure_manifest rebuilds a stale coverage
manifest from any reader (index_status found GPSTRUTH's stale on the first run), so these "reads" can write. The
tests themselves save nothing (coreg is computed, not stored in the scene).

The expected values were taken from demo/uwg-ladder at the recorded demo (tag uwg-demo-2026-09-30), whose time-series
defaults main adopts: ATL06 reference plane, res 8 cells, one-year windows.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from aicesat import api, cache, coreg, coverage, survey, timeseries

pytestmark = pytest.mark.golden

GOLDEN = Path(__file__).parent / "golden" / "uwg.json"
STORY_SCENE, STORY_CELL = "f66beabec8", "8806f21187fffff"   # lower trunk of Jakobshavn Isbrae, the demo's story cell
COREG_SCENE = "dd538c5633"                                   # this morning's study scene: GLAS + ICESat-2 photons
TS = dict(h3_res=8, delta_t=1.0, ref_missions=["ATL06"], min_bins=3)

if cache.load_scene(STORY_SCENE) is None:
    pytest.skip("golden numbers need a clone of the UWG demo store (AICESAT_DATA_DIR=.../data-golden)", allow_module_level=True)


def _canon(x, nd=4):
    """Floats rounded so the hash is about the science, not the last bit of a sum's order."""
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _canon(v, nd) for k, v in sorted(x.items())}
    if isinstance(x, (list, tuple)):
        return [_canon(v, nd) for v in x]
    if hasattr(x, "tolist"):
        return _canon(x.tolist(), nd)
    return x


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(_canon(x), sort_keys=True, default=str).encode()).hexdigest()[:16]


def _h3_scenes() -> list[str]:
    reg = json.loads((cache.DATA_DIR / "scenes" / "registry.json").read_text())
    return sorted(sid for sid, r in reg.items() if str(r.get("question", "")).startswith("H3 ") and r.get("status") == "ready")


def snapshot() -> dict:
    out = {"candidates": {}, "index_status": {}}
    for sid in _h3_scenes():
        cands = timeseries.candidates(cache.load_scene(sid), **TS)["candidates"]
        out["candidates"][sid] = {"n": len(cands), "n_low": sum(c["level"] == "low" for c in cands), "sha": _sha(cands)}
    story = {c["h3"]: c for c in timeseries.candidates(cache.load_scene(STORY_SCENE), **TS)["candidates"]}[STORY_CELL]
    out["story_cell"] = {"trend_cm_yr": round(story["trend_cm_yr"], 2), "windows": len(story["series"]),
                         "confidence": round(story["confidence"], 3), "level": story["level"],
                         "plane_err_max_m": story["components"].get("plane_err_max_m")}
    out["sample_geometry_sha"] = _sha(api.cell_geometry(STORY_SCENE, STORY_CELL))
    out["coreg_sha"] = _sha(coreg.coregister_scene(cache.load_scene(COREG_SCENE)))
    out["area_summary"] = _canon(survey.area_summary(survey.area_bbox(69.18, -49.3, 50)))
    hexes = survey.coverage_hexes(5)["hexes"]
    out["coverage_hexes_res5"] = {"n": len(hexes), "sha": _sha(hexes)}
    for c in coverage.collections():
        s = api.index_status(c["key"])
        out["index_status"][c["key"]] = {k: s.get(k) for k in ("indexed", "res", "granules", "target", "pct", "span_max")} | {
            "n_cells": len(s.get("cells") or []), "cells_sha": _sha(s.get("cells") or [])}
    return out


@pytest.fixture(scope="module")
def snap():
    return snapshot()


@pytest.fixture(scope="module")
def expected(snap):
    if os.environ.get("AICESAT_GOLDEN_UPDATE") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    return json.loads(GOLDEN.read_text())


def test_story_cell_reads_what_the_demo_said(snap):
    # the recorded demo: -4.17 m/yr over 2004-2026 in 19 one-year windows, medium confidence 0.45, slope error 0.39 m
    assert snap["story_cell"] == {"trend_cm_yr": -416.63, "windows": 19, "confidence": 0.45, "level": "medium",
                                  "plane_err_max_m": 0.391}


def test_area_summary_reads_what_the_demo_said(snap):
    s = snap["area_summary"]
    assert [(s[k]["passes"], s[k]["days"]) for k in ("GLAS", "ICESSN", "ATL06")] == [(143, 143), (1728, 65), (800, 681)]


@pytest.mark.parametrize("part", ["candidates", "story_cell", "sample_geometry_sha", "coreg_sha", "area_summary",
                                  "coverage_hexes_res5", "index_status"])
def test_unchanged(snap, expected, part):
    assert snap[part] == expected[part]
