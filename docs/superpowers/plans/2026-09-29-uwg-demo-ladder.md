# UWG demo ladder: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A place-centric demo in three levels: a coverage globe → a change map with per-hex time series → a 3-D study
with photons and Δh. The same three levels are exposed to Claude Desktop as three MCP tools.

**Architecture:** The backend adds one index-only module (`survey.py`) and one change-level entry point
(`api.elevation_change`) on top of the existing `build_scene` and `timeseries.candidates`. The UI adds a globe view
(`survey.js`) and a shared timeline strip (`timeline.js`). A `?level=region|study` mode in the existing scene view
turns it into the change map and the study view. The model sees exactly three tools; every `ui_*` tool stays app-only.

**Tech stack:** Python 3.13 (uv), DuckDB, h3 v4, mcp 2.1.1 Apps; vanilla JS + deck.gl 9.3.10 + h3-js 4.5.0, built by
`uv run scripts/build_ui.py` into `src/aicesat/widget/dist/aicesat.html`.

**Spec:** `docs/superpowers/specs/2026-09-29-uwg-demo-ladder-design.md`

## Global Constraints

- Branch `demo/uwg-ladder`; never merge to `main`.
- **Every command that touches data runs with `AICESAT_DATA_DIR=$PWD/data-uwg`.** `data/` and Kevin's port-8765 server
  are never written or restarted. Test servers use `AICESAT_PORT=8791` (Claude Desktop's gets 8792).
- NASA access, server binds and `git` need the Bash sandbox **off** (`dangerouslyDisableSandbox: true`).
- The change map's reference plane is **`ref_missions=["ATL06"]`**: single-era. Never all missions jointly (spec
  Backend §3).
- The confidence gate forces `level = "low"` when within-window scatter ≥ 1.5 m or there are fewer than 10 reference
  points. Gate on evidence quality, never on the size of the answer.
- Model-visible MCP tools are exactly `survey_coverage`, `elevation_change`, `show_timeseries`. No "Slice N" or
  "Greenland demo regions" wording in any model-visible text.
- ToolError, never a bare ValueError, for caller-fixable errors in MCP tools (use `server._anticipated`).
- Slim the response, never the search: counts of what was dropped travel with every truncated list.
- After any UI source edit: `uv run scripts/build_ui.py`. Parallel UI work conflicts in `widget/dist`: rebuild it,
  never hand-merge it.
- Run the suite as `set -o pipefail; uv run pytest -q 2>&1 | tail -3`, so a red run fails the command. For new tests,
  break the guard once and watch them go red.
- Commit trailers: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01KnxupbMvhFiiSqysoTcgv5`.

## Review Focus

1. **A hex on the claim's edge.** Clicking it must not start a build the gate will refuse. Only `claimed` hexes build,
   and others explain why (Task 2 test `test_claim_is_per_mission...`, Task 6 click guard).
2. **The same place reached by UI click and by the model** must share one scene, not build twice. The same
   `"H3 <hex>"` question and the reuse lookup cover this (Task 3 `test_region_scene_reuses...`; Task 8 pre-warm asserts the
   story cell's parent hex).
3. **A cell that exists only under one reference.** `show_timeseries` must search with the same reference and
   resolution `elevation_change` used, or the model's cell id "does not exist" (Task 3: shared `CHANGE_REF`, res 8 default).
4. **The MCP App transport has no point clouds.** The region view must still frame itself on the DEM and paint change
   hexes from `ui_candidates` (Task 5 `fitSurface`; Task 8 e2e over stdio).
5. **A Study area outside the ATL03 index** builds without photons. The Δh tab must say why instead of showing an empty panel
   (Task 5 `dhNote`).

---

### Task 1: Confidence gate

**Files:**
- Modify: `src/aicesat/timeseries.py` (`_confidence`, ~line 108)
- Test: `tests/test_timeseries.py` (append)

**Interfaces:**
- Produces: `timeseries.MIN_REF_PTS = 10`; `_confidence(...)` unchanged signature, returns `(conf, level, why,
  comps)` with `comps["gated"]: list[str]` (empty when not gated) and `conf <= 0.34` when gated.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_timeseries.py`)

```python
def test_a_rough_cell_is_low_whatever_its_record():
    # 8806f200d3fffff (res 8, ref ATL06) scored "medium" with 708 m of within-window scatter: epochs and span maxed out
    # and carried it. Ungated this scores 0.45 -> medium.
    conf, level, why, comps = timeseries._confidence(roughness=708.5, n_bins=10, span=15.0, n_ref=500)
    assert level == "low" and conf < 0.35
    assert comps["gated"] and "gated" in why


def test_a_sparse_reference_is_low_even_on_a_smooth_cell():
    # Ungated this scores 0.85 -> high; 8 points do not constrain a plane across a 530 m hex.
    conf, level, _, comps = timeseries._confidence(roughness=0.2, n_bins=10, span=20.0, n_ref=8)
    assert level == "low" and conf < 0.35 and comps["gated"]


def test_the_gate_leaves_a_well_measured_cell_alone():
    conf, level, _, comps = timeseries._confidence(roughness=0.2, n_bins=10, span=20.0, n_ref=40)
    assert level == "high" and comps["gated"] == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_timeseries.py -k "rough_cell or sparse_reference or gate_leaves" -q`
Expected: 2 FAIL (level medium / high), 1 FAIL (`KeyError: 'gated'`).

- [ ] **Step 3: Implement.** In `src/aicesat/timeseries.py`, add above `_confidence`:

```python
MIN_REF_PTS = 10      # fewer reference points than this do not constrain a plane across a hex
_GATED_CONF = 0.34    # just under "medium", so a gated cell also ranks below every cell that passed
```

and replace the body from `conf = 0.55 * ...` down to the `return` with:

```python
    conf = 0.55 * s_rough + 0.20 * s_epochs + 0.15 * s_span + 0.10 * s_ref
    # Hard gate on the quality of the EVIDENCE, never on the size of the answer. The weighted score let maxed-out
    # epochs and span carry 8806f200d3fffff (res 8, ref ATL06: 8 plane points, 708 m within-window scatter,
    # -263 m/yr) up to "medium". A change map colours by trend, so that cell would be the loudest thing on it.
    gated = []
    if s_rough <= 0.0:
        gated.append(f"within-window scatter {roughness:.1f} m is beyond 1.5 m")
    if n_ref < MIN_REF_PTS:
        gated.append(f"only {n_ref} reference points (need {MIN_REF_PTS})")
    if gated:
        conf = min(conf, _GATED_CONF)
    level = "high" if conf >= 0.6 else "medium" if conf >= 0.35 else "low"
    limiters = []
    if s_rough < 0.5: limiters.append(f"rough within-cell surface (scatter {roughness:.1f} m) — samples disagree at one time")
    if s_epochs < 0.5: limiters.append(f"only {n_bins} time windows")
    if s_span < 0.5: limiters.append(f"short {span:.1f}-yr baseline")
    if s_ref < 0.5: limiters.append(f"sparse reference ({n_ref} pts)")
    if gated:
        why = "Low confidence — gated: " + "; ".join(gated)
    elif limiters:
        why = f"{level.capitalize()} confidence — " + "; ".join(limiters[:2])
    else:
        why = f"{level.capitalize()} confidence — smooth cell (scatter {roughness:.1f} m), {n_bins} windows over {span:.1f} yr"
    comps = {"roughness_m": round(roughness, 2), "epochs": int(n_bins), "span_yr": round(span, 1), "ref_pts": int(n_ref),
             "scores": {"roughness": round(s_rough, 2), "epochs": round(s_epochs, 2), "span": round(s_span, 2), "density": round(s_ref, 2)},
             "gated": gated}
    return round(conf, 2), level, why, comps
```

- [ ] **Step 4: Run the new tests, then prove they can fail.** Run the Step 2 command and expect 3 PASS. Temporarily
  change `if gated:` (the `conf = min(...)` one) to `if False:`, rerun, and expect 2 FAIL. Revert.

- [ ] **Step 5: Full suite.** `set -o pipefail; uv run pytest -q 2>&1 | tail -3`. Expected: all pass. If an existing test asserted
  a level on a cell with fewer than 10 reference points, it was asserting the bug; update its fixture to 10+ points
  and say so in the commit.

- [ ] **Step 6: Commit**

```bash
git add src/aicesat/timeseries.py tests/test_timeseries.py
git commit -m "fix(timeseries): gate confidence on rough cells and sparse reference planes"
```

---

### Task 2: Coverage hexes (level 1 backend)

**Files:**
- Create: `src/aicesat/survey.py`
- Modify: `src/aicesat/server.py` (import `survey`; HTTP route in `do_GET`; app-only tool)
- Modify: `src/aicesat/ui/adapter.js` (`coverageHexes` in both adapters)
- Test: `tests/test_survey.py`

**Interfaces:**
- Produces:
  - `survey.coverage_hexes(res: int = 5, bbox: list | None = None) -> {"res": int, "hexes": [{"h3": str, "lat", "lon",
    "missions": {KEY: {"passes", "year_min", "year_max", "n_years", "claimed"}}, "n_missions": int, "claimed": bool}]}`.
  - `survey.area_bbox(lat, lon, radius_km) -> [W, S, E, N]`.
  - `survey.area_summary(bbox) -> {KEY: {"passes", "year_min", "year_max", "n_years", "hexes"}}`.
  - `survey.LABELS`.
  - `survey.MISSIONS = ("GLAS", "ICESSN", "ATL06")`.
  - HTTP `GET /api/coverage_hexes?res=5[&bbox=[..]]`.
  - App tool `ui_coverage_hexes(res, bbox)`.
  - JS `api.coverageHexes(res, bbox)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_survey.py`)

```python
"""Level 1 of the demo ladder: per-hex mission coverage from the index manifests (no NASA, no lake)."""
import h3
import pytest

from aicesat import survey

C5 = h3.latlng_to_cell(69.175, -49.328, 5)          # the story area
C5B = h3.latlng_to_cell(69.60, -48.20, 5)           # a different res-5 hex
I = h3.str_to_int
ROWS = {
    "GLAS": [(I(C5), "GLAH06_a", "2004-03"), (I(C5), "GLAH06_b", "2008-10"), (I(C5B), "GLAH06_a", "2004-03")],
    "ICESSN": [(I(C5), "ILATM2_x", "2011-05")],
    "ATL06": [(I(C5), "ATL06_1", "2019-04"), (I(C5), "ATL06_2", "2026-01"), (I(C5), "ATL06_2", "2026-01")],
}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(survey, "_rows", lambda key: ROWS[key])
    # GLAS and ATL06 claim exactly C5; IceBridge claims C5 through a coarse ancestor (the claim set is compacted)
    monkeypatch.setattr(survey, "_packed", lambda key: {h3.cell_to_parent(C5, 3)} if key == "ICESSN" else {C5})


def _by(out):
    return {h["h3"]: h for h in out["hexes"]}


def test_counts_distinct_passes_and_years_per_mission(fake):
    hx = _by(survey.coverage_hexes(5))[C5]
    assert hx["missions"]["GLAS"] == {"passes": 2, "year_min": 2004, "year_max": 2008, "n_years": 2, "claimed": True}
    assert hx["missions"]["ATL06"]["passes"] == 2               # a duplicated manifest row is one pass
    assert hx["n_missions"] == 3 and hx["claimed"] is True


def test_claim_is_per_mission_and_a_hex_builds_only_if_every_mission_is_claimed(fake):
    hx = _by(survey.coverage_hexes(5))[C5B]
    assert hx["missions"]["GLAS"]["claimed"] is False
    assert hx["claimed"] is False


def test_coarser_res_rolls_children_up_without_double_counting(fake):
    hx = _by(survey.coverage_hexes(3))[h3.cell_to_parent(C5, 3)]
    assert hx["missions"]["GLAS"]["passes"] == 2                # GLAH06_a crosses both children: counted once


def test_finer_than_the_index_is_refused(fake):
    with pytest.raises(ValueError):
        survey.coverage_hexes(6)


def test_bbox_keeps_hexes_by_centre(fake):
    lat, lon = h3.cell_to_latlng(C5)
    out = survey.coverage_hexes(5, bbox=[lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01])
    assert [h["h3"] for h in out["hexes"]] == [C5]


def test_area_summary_counts_distinct_passes_over_the_area(fake):
    lat, lon = h3.cell_to_latlng(C5)
    s = survey.area_summary(survey.area_bbox(lat, lon, 5))
    assert s["GLAS"]["passes"] == 2 and s["ICESSN"]["year_min"] == 2011 and s["ATL06"]["year_max"] == 2026
```

- [ ] **Step 2: Run to verify they fail.** `uv run pytest tests/test_survey.py -q` → FAIL, `ImportError: cannot import name 'survey'`.

- [ ] **Step 3: Implement `src/aicesat/survey.py`**

```python
"""Level 1 of the demo ladder: which missions passed over each H3 hex, how often, and in which years.

Read from each collection's coverage manifest (DISTINCT h3_cell, granule, ym; coverage._ensure_manifest keeps it
fresh) -- no NASA calls, no lake reads. Index rows are keyed at res 5, so a coarser hex is the roll-up of its res-5
children, and a granule crossing two children counts once. A PASS is a distinct granule.

`claimed` answers "would a build over this hex be accepted for this mission": the same compacted-claim containment
test the build gate uses (index.cells_within), so the globe never offers a hex the build would refuse."""
from __future__ import annotations

import math

import duckdb
import h3

from . import coverage, index

MISSIONS = ("GLAS", "ICESSN", "ATL06")
LABELS = {"GLAS": "ICESat (GLAS, 2003-09)", "ICESSN": "Operation IceBridge ATM (2009-19)",
          "ATL06": "ICESat-2 land ice ATL06 (2018-)"}
INDEX_RES = 5


def _rows(key: str) -> list[tuple]:
    """(h3_cell int, granule, 'YYYY-MM') for every manifest row of a collection; [] when it has no index."""
    d, _res, ym = coverage._index_for(key)
    if d is None or not d.exists():
        return []
    m = coverage._ensure_manifest(d, ym)
    if m is None:
        return []
    con = duckdb.connect()
    try:
        return con.execute("SELECT h3_cell, granule, ym FROM read_parquet(?)", [str(m)]).fetchall()
    finally:
        con.close()


def _packed(key: str) -> set[str]:
    """The collection's compacted claim, as h3 strings (index.manifest_cells reads _build.json)."""
    d = coverage._index_for(key)[0]
    return {h3.int_to_str(c) for c in index.manifest_cells(d)} if d is not None else set()


def _in_bbox(lat: float, lon: float, bbox) -> bool:
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def _mission_row(granules: set, years: set) -> dict:
    ys = sorted(years)
    return {"passes": len(granules), "year_min": ys[0] if ys else None, "year_max": ys[-1] if ys else None,
            "n_years": len(ys)}


def coverage_hexes(res: int = 5, bbox=None) -> dict:
    res = int(res)
    if not 0 <= res <= INDEX_RES:
        raise ValueError(f"res must be 0..{INDEX_RES}: index rows are keyed at res {INDEX_RES}; got {res}")
    acc: dict[str, dict] = {}
    for key in MISSIONS:
        for cell, granule, ym in _rows(key):
            hx = h3.int_to_str(int(cell))
            if h3.get_resolution(hx) > res:
                hx = h3.cell_to_parent(hx, res)
            m = acc.setdefault(hx, {}).setdefault(key, {"granules": set(), "years": set()})
            m["granules"].add(granule)
            if ym:
                m["years"].add(int(str(ym)[:4]))
    packed = {k: _packed(k) for k in MISSIONS}
    hexes = []
    for hx in sorted(acc):
        lat, lon = h3.cell_to_latlng(hx)
        if bbox is not None and not _in_bbox(lat, lon, bbox):
            continue
        ms = {k: {**_mission_row(v["granules"], v["years"]), "claimed": index.cells_within(packed[k], [hx])}
              for k, v in acc[hx].items()}
        hexes.append({"h3": hx, "lat": round(lat, 5), "lon": round(lon, 5), "missions": ms, "n_missions": len(ms),
                      "claimed": all(v["claimed"] for v in ms.values())})
    return {"res": res, "hexes": hexes}


def area_bbox(lat: float, lon: float, radius_km: float) -> list[float]:
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return [round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5)]


def area_summary(bbox) -> dict:
    """Distinct passes and years per mission over an area. A res-5 index cell counts when its centre lies within the
    area grown by ~one res-5 edge (9 km), so a small radius still reaches the hex it sits in."""
    w, s, e, n = bbox
    lat0 = (s + n) / 2
    g = [w - 9 / (111.32 * max(math.cos(math.radians(lat0)), 0.05)), s - 9 / 111.32,
         e + 9 / (111.32 * max(math.cos(math.radians(lat0)), 0.05)), n + 9 / 111.32]
    out = {}
    for key in MISSIONS:
        grans, years, cells = set(), set(), set()
        for cell, granule, ym in _rows(key):
            lat, lon = h3.cell_to_latlng(h3.int_to_str(int(cell)))
            if _in_bbox(lat, lon, g):
                grans.add(granule); cells.add(cell)
                if ym:
                    years.add(int(str(ym)[:4]))
        out[key] = {**_mission_row(grans, years), "hexes": len(cells)}
    return out
```

- [ ] **Step 4: Run the tests.** `uv run pytest tests/test_survey.py -q` → 6 PASS. Prove the claim test can fail: make
  `claimed` always `True`, rerun, see `test_claim_is_per_mission...` FAIL, revert.

- [ ] **Step 5: Wire HTTP, the app tool and the adapter.**
  - In `server.py`, change the import line to `from . import api, atl03, cache, coverage, geom, regions, scene, stream, survey, uibuild`.
  - In `do_GET`, add before the `if u.path == "/api/claims":` line:

```python
        if u.path == "/api/coverage_hexes":
            try:
                return self._json(200, survey.coverage_hexes(int(qs.get("res", ["5"])[0]),
                                                             json.loads(qs["bbox"][0]) if "bbox" in qs else None))
            except Exception as e:
                return self._json(400, {"error": f"{type(e).__name__}: {e}"})
```

  - After `ui_claims`, add:

```python
@apps.tool(name="ui_coverage_hexes", **_APP)
def ui_coverage_hexes(res: int = 5, bbox: list[float] | None = None) -> dict:
    return survey.coverage_hexes(res, bbox)
```

  - In `adapter.js`, add `coverageHexes` to `fetchApi` after `claims`:

```js
    coverageHexes: (res, bbox) => j('/api/coverage_hexes?res=' + res + (bbox ? '&bbox=' + encodeURIComponent(JSON.stringify(bbox)) : '')),
```

  - Add it to `appApi`'s return object after `claims`:

```js
      coverageHexes: (res, bbox) => call('ui_coverage_hexes', bbox ? {res, bbox} : {res}),
```

- [ ] **Step 6: Measure on the real demo store.**

  Run: `AICESAT_DATA_DIR=$PWD/data-uwg uv run python -c "import time; from aicesat import survey; t=time.time(); o=survey.coverage_hexes(5); print(len(o['hexes']), sum(h['claimed'] for h in o['hexes']), round(time.time()-t,2),'s')"`

  Expected: hundreds of hexes, of which the West Greenland ones are claimed once ATL06 finishes (GLAS and IceBridge
  are done). Under ~3 s. If it is slower, memoize on the three manifests' mtimes before moving on.

- [ ] **Step 7: Suite + commit.**

```bash
set -o pipefail; uv run pytest -q 2>&1 | tail -3
git add src/aicesat/survey.py src/aicesat/server.py src/aicesat/ui/adapter.js tests/test_survey.py
git commit -m "feat(survey): per-hex mission coverage from the index manifests, for the globe"
```

---

### Task 3: `elevation_change` and the three-tool MCP surface

**Files:**
- Modify: `src/aicesat/api.py` (new section after `timeseries_cell`)
- Modify: `src/aicesat/server.py` (new tools, hide the rest, instructions)
- Modify: `scripts/e2e_apps.py:34` (`open_ui` → `elevation_change`)
- Create: `scripts/e2e_demo.py`
- Test: `tests/test_demo_tools.py`

**Interfaces:**
- Consumes: `survey.area_bbox`, `survey.area_summary`, `survey.LABELS` (Task 2).
- Produces:
  - `api.CHANGE_REF = ["ATL06"]`, `api.REGION_FLAGS`.
  - `api.region_hex(lat, lon, radius_km=10.0) -> str`.
  - `api.region_question(hx) -> "H3 <hx>"`.
  - `api.hex_polygon(hx) -> [[lon, lat] × 6]`.
  - `api.region_scene(hx) -> {"scene_id", "job_id", "status"}`.
  - `api.elevation_change(lat, lon, radius_km=10.0, h3_res=8, limit=10, wait_s=45.0) -> dict`.
  - MCP tools `survey_coverage`, `elevation_change`, `show_timeseries`. Results carry `view` / `query` / `open_url` for
    `app.js` routing (Task 7).

- [ ] **Step 1: Write the failing tests** (`tests/test_demo_tools.py`)

```python
"""The demo's change level and its three-tool MCP surface."""
import h3
import pytest

from aicesat import api, server, survey

STORY = (69.1752, -49.3276)
CANDS = [
    {"h3": "a", "lat": 69.17, "lon": -49.33, "level": "medium", "confidence": 0.5, "span_years": 21.4, "n_bins": 18,
     "trend_cm_yr": -414.0, "why": "w",
     "series": [{"year": 2005.1, "value_m": 0.0, "missions": ["GLAS"]}, {"year": 2026.5, "value_m": -87.0, "missions": ["ATL06"]}]},
    {"h3": "b", "lat": 69.16, "lon": -49.35, "level": "high", "confidence": 0.8, "span_years": 6.0, "n_bins": 7,
     "trend_cm_yr": -20.0, "why": "w",
     "series": [{"year": 2020.2, "value_m": 0.0, "missions": ["ATL06"]}, {"year": 2026.2, "value_m": -1.2, "missions": ["ATL06"]}]},
    {"h3": "c", "lat": 69.02, "lon": -49.46, "level": "low", "confidence": 0.34, "span_years": 21.0, "n_bins": 10,
     "trend_cm_yr": -26361.0, "why": "Low confidence — gated",
     "series": [{"year": 2004.2, "value_m": 7384.7, "missions": ["GLAS"]}, {"year": 2019.2, "value_m": 0.0, "missions": ["ATL06"]}]},
]
NOTES = "no inter-campaign/inter-sensor bias adjustment and no GIA correction applied"


def test_small_radius_resolves_to_a_res5_hex_and_large_to_res4():
    assert h3.get_resolution(api.region_hex(*STORY, radius_km=10)) == 5
    assert h3.get_resolution(api.region_hex(*STORY, radius_km=30)) == 4


def test_hex_polygon_is_the_hex_boundary_in_lon_lat():
    poly = api.hex_polygon(api.region_hex(*STORY))
    assert len(poly) == 6
    assert min(p[1] for p in poly) < STORY[0] < max(p[1] for p in poly)
    assert min(p[0] for p in poly) < STORY[1] < max(p[0] for p in poly)


def test_region_scene_reuses_a_ready_scene_for_the_same_hex(monkeypatch):
    hx = api.region_hex(*STORY)
    monkeypatch.setattr(api, "scenes", lambda: [{"scene_id": "abc", "question": api.region_question(hx), "status": "ready"}])
    monkeypatch.setattr(api, "start_job", lambda *a, **k: pytest.fail("a ready area must not be rebuilt"))
    assert api.region_scene(hx) == {"scene_id": "abc", "job_id": None, "status": "ready"}


def test_region_scene_starts_exactly_one_build_with_the_region_collections(monkeypatch):
    hx, seen = api.region_hex(*STORY), []
    monkeypatch.setattr(api, "scenes", lambda: [])
    monkeypatch.setattr(api, "_region_jobs", {})
    monkeypatch.setattr(api, "start_job", lambda p, kind="scene": seen.append(p) or {"id": "j9", "scene_id": "new"})
    assert api.region_scene(hx)["scene_id"] == api.region_scene(hx)["scene_id"] == "new"
    assert len(seen) == 1
    assert seen[0]["with_atl03"] is False and seen[0]["with_atl06"] is True and seen[0]["question"] == api.region_question(hx)


@pytest.fixture
def ready(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "region_scene", lambda hx: {"scene_id": "s1", "job_id": None, "status": "ready"})
    monkeypatch.setattr(api, "scene_candidates", lambda sid, **kw: seen.update(kw) or
                        {"params": {"ref_missions": kw.get("ref_missions"), "notes": NOTES}, "candidates": CANDS})
    return seen


def test_low_confidence_cells_are_counted_not_listed(ready):
    out = api.elevation_change(*STORY)
    assert (out["n_cells"], out["n_reliable"], out["n_low_confidence"]) == (3, 2, 1)
    assert [c["h3"] for c in out["cells"]] == ["a", "b"]           # longest record first


def test_each_cell_reports_total_change_and_contributing_missions(ready):
    a = api.elevation_change(*STORY)["cells"][0]
    assert a["change_m"] == -87.0 and (a["first_year"], a["last_year"]) == (2005, 2026)
    assert a["missions"] == ["ATL06", "GLAS"] and a["trend_m_per_yr"] == -4.14


def test_the_change_level_uses_the_single_era_reference(ready):
    api.elevation_change(*STORY)
    assert ready["ref_missions"] == ["ATL06"] and ready["h3_res"] == 8


def test_caveats_travel_with_the_answer(ready):
    assert "GIA" in api.elevation_change(*STORY)["caveats"]


def test_a_build_still_running_says_so_and_how_to_wait(monkeypatch):
    monkeypatch.setattr(api, "region_scene", lambda hx: {"scene_id": "s2", "job_id": "j", "status": "loading"})
    out = api.elevation_change(*STORY, wait_s=0)
    assert out["status"] == "building" and "same arguments" in out["message"]


def test_elevation_change_tool_routes_the_app_to_the_region_level(ready):
    out = server.elevation_change(*STORY)
    assert out["view"] == "scene" and out["query"] == "level=region"
    assert out["open_url"].endswith("/#scene/s1?level=region")


def test_show_timeseries_defaults_to_the_change_levels_search(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "timeseries_cell", lambda sid, h, **kw: seen.update(kw) or {"scene_id": sid, "h3": h})
    out = server.show_timeseries("s1", "a")
    assert seen["h3_res"] == 8 and seen["ref_missions"] == ["ATL06"] and out["res"] == 8


def test_survey_coverage_summarises_per_mission_and_opens_the_globe(monkeypatch):
    monkeypatch.setattr(survey, "area_summary", lambda bbox: {k: {"passes": 3, "year_min": 2004, "year_max": 2026,
                                                                  "n_years": 3, "hexes": 1} for k in survey.MISSIONS})
    out = server.survey_coverage(*STORY, radius_km=20)
    assert out["view"] == "survey" and len(out["bbox"]) == 4
    assert set(out["missions"]) == set(survey.LABELS.values()) and out["indexed"] is True and out["long_record"] is True
```

- [ ] **Step 2: Run to verify they fail.** `uv run pytest tests/test_demo_tools.py -q` → FAIL (`AttributeError: module 'aicesat.api' has no attribute 'region_hex'`).

- [ ] **Step 3: Implement in `api.py`.** Add `import h3` to the imports if absent. Append after `timeseries_cell`:

```python
# --- the demo ladder's change level ---------------------------------------------------------------------------
# The change map's reference plane is ONE era's: fitted to all missions jointly, the plane mistakes 20 years of
# thinning for slope wherever the missions sampled different parts of a cell (story cell 8806f21187fffff: 6.2 deg
# joint vs ~2 deg for each mission alone; 2026 read -57 m instead of -87 m). ATL06 alone agrees with GLAS alone to
# ~1 m there and covers 855 cells at res 8 against GLAS's 112.
CHANGE_REF = ["ATL06"]
REGION_FLAGS = {"with_glas": True, "with_icessn": True, "with_atl06": True, "with_atl03": False,
                "with_gedi": False, "with_gpstruth": False, "with_coreg": False}
_region_jobs: dict[str, dict] = {}
_region_lock = threading.Lock()


def region_hex(lat: float, lon: float, radius_km: float = 10.0) -> str:
    """The hex an area request resolves to: res 5 (~17 km across) up to 12 km radius, else res 4 (~45 km). The globe's
    click and the model's lat/lon land on the same hex, so they share one scene."""
    return h3.latlng_to_cell(float(lat), float(lon), 5 if radius_km <= 12 else 4)


def region_question(hx: str) -> str:
    return f"H3 {hx}"


def hex_polygon(hx: str) -> list[list[float]]:
    return [[round(lon, 6), round(lat, 6)] for lat, lon in h3.cell_to_boundary(hx)]


def region_scene(hx: str) -> dict:
    """Find the change-level scene for `hx`, or start its build (once): {"scene_id", "job_id", "status"}."""
    q = region_question(hx)
    with _region_lock:
        for r in scenes():
            if r.get("question") == q and r.get("status") in ("ready", "loading"):
                return {"scene_id": r["scene_id"], "job_id": r.get("job_id"), "status": r["status"]}
        memo = _region_jobs.get(hx)
        if memo:
            j = job(memo["job_id"])
            if not (j and j.get("status") == "error"):
                return {**memo, "status": "ready" if j and j.get("status") == "done" else "loading"}
        jb = start_job({"polygon": hex_polygon(hx), "question": q, **REGION_FLAGS})
        _region_jobs[hx] = {"scene_id": jb["scene_id"], "job_id": jb["id"]}
        return {**_region_jobs[hx], "status": "loading"}


def _change_row(c: dict) -> dict:
    s = c["series"]
    return {"h3": c["h3"], "lat": round(c["lat"], 4), "lon": round(c["lon"], 4),
            "change_m": round(s[-1]["value_m"] - s[0]["value_m"], 1),
            "first_year": int(s[0]["year"]), "last_year": int(s[-1]["year"]),
            "trend_m_per_yr": round(c["trend_cm_yr"] / 100, 2), "span_years": round(c["span_years"], 1),
            "missions": sorted({m for p in s for m in p["missions"]}), "level": c["level"], "why": c["why"]}


def elevation_change(lat: float, lon: float, radius_km: float = 10.0, h3_res: int = 8, limit: int = 10,
                     wait_s: float = 45.0) -> dict:
    """The change level for a place: build (or reuse) the scene over its hex, then rank the cells whose record is
    reliable. Low-confidence cells are counted, never listed."""
    hx = region_hex(lat, lon, radius_km)
    r = region_scene(hx)
    t0 = time.time()
    while r["status"] == "loading" and time.time() - t0 < wait_s:
        time.sleep(1.0)
        rec = next((s for s in scenes() if s["scene_id"] == r["scene_id"]), None)
        r = {**r, "status": rec["status"] if rec else "loading"}
    area = {"h3": hx, "hex_res": h3.get_resolution(hx), "lat": float(lat), "lon": float(lon)}
    if r["status"] == "loading":
        return {"status": "building", "scene_id": r["scene_id"], "area": area,
                "message": "The area is still being fetched from NASA. Call elevation_change again with the same "
                           "arguments; it waits on the same build."}
    if r["status"] != "ready":
        raise ValueError(f"the build for hex {hx} failed; its scene is {r['scene_id']}")
    out = scene_candidates(r["scene_id"], h3_res=int(h3_res), ref_missions=CHANGE_REF)
    cands = out["candidates"]
    reliable = [c for c in cands if c["level"] != "low"]
    ranked = sorted(reliable, key=lambda c: (-c["span_years"], -c["confidence"]))
    summary = None
    if reliable:
        tr = sorted(c["trend_cm_yr"] for c in reliable)
        summary = {"median_trend_m_per_yr": round(tr[len(tr) // 2] / 100, 2),
                   "most_thinning": _change_row(min(reliable, key=lambda c: c["trend_cm_yr"])),
                   "most_thickening": _change_row(max(reliable, key=lambda c: c["trend_cm_yr"])),
                   "n_record_15yr_plus": sum(c["span_years"] >= 15 for c in reliable)}
    return {"status": "ready", "scene_id": r["scene_id"], "area": area, "h3_res": int(h3_res),
            "n_cells": len(cands), "n_reliable": len(reliable), "n_low_confidence": len(cands) - len(reliable),
            "returned": min(len(ranked), max(1, int(limit))), "summary": summary,
            "cells": [_change_row(c) for c in ranked[: max(1, int(limit))]],
            "reference": out["params"]["ref_missions"], "caveats": out["params"]["notes"]}
```

- [ ] **Step 4: Implement the MCP surface in `server.py`.**

  **(a) Hide from the model.** Delete the decorator line only, keeping the functions (tests call them directly):
  - `@apps.tool(resource_uri=UI_URI, name="show_photons")`, `...name="open_ui")`, `...name="add_glas")`,
    `...name="coregister")` and `...name="find_timeseries_candidates")`.
  - The six `@mcp.tool()` lines above `list_regions`, `list_scenes`, `lake_status`, `lake_load_cells`, `job_status`
    and `check_coverage`.

  **(b) Add the two new tools and a URL helper** after `ts_url`:

```python
def scene_level_url(scene_id: str, level: str) -> str:
    return f"{base_url()}/#scene/{scene_id}?level={level}"


@apps.tool(resource_uri=UI_URI, name="survey_coverage")
def survey_coverage(lat: float, lon: float, radius_km: float = 50.0) -> dict:
    """Which laser-altimetry missions have measured a place, how often, and in which years -- before fetching anything.

    Covers ICESat (GLAS, 2003-09), Operation IceBridge ATM (2009-19) and ICESat-2 ATL06 land ice (2018-). Reads only
    the pre-built index, so it is instant. `indexed` false means no index covers the area: say that, never "no data".
    `long_record` true means all three missions overlap there, so a 20-year elevation record is possible.
    Opens the globe on the area, each hex coloured by how many missions saw it."""
    bbox = survey.area_bbox(lat, lon, radius_km)
    s = survey.area_summary(bbox)
    return {"area": {"lat": lat, "lon": lon, "radius_km": radius_km}, "bbox": bbox,
            "missions": {survey.LABELS[k]: v for k, v in s.items()},
            "indexed": any(v["hexes"] for v in s.values()),
            "long_record": all(v["passes"] for v in s.values()),
            "view": "survey"}


@apps.tool(resource_uri=UI_URI, name="elevation_change")
def elevation_change(lat: float, lon: float, radius_km: float = 10.0, h3_res: int = 8, limit: int = 10) -> dict:
    """How the ice or land surface height has changed at a place, across ICESat, IceBridge and ICESat-2 (2003-now),
    and where the record is long enough to tell.

    Fetches every mission's measurements over the H3 hex containing (lat, lon) -- res 5 (~17 km across) for
    radius_km <= 12, else res 4 (~45 km) -- bins them into res-`h3_res` cells (8 ~ 530 m edge) and one-year windows,
    and removes each cell's surface slope with a plane fitted to ICESat-2 alone (one era, so change is never mistaken
    for slope). Returns the reliable cells ranked by record length: total change in metres, first/last year, rate,
    contributing missions and a confidence reason. Low-confidence cells are counted, not listed.
    status "building" means the area is still being fetched: call again with the same arguments.
    Quote every rate with its caveats (no inter-mission bias correction, no GIA). Use show_timeseries for one cell."""
    out = _anticipated(api.elevation_change, lat, lon, radius_km=radius_km, h3_res=h3_res, limit=limit)
    return {**out, "view": "scene", "query": "level=region", "open_url": scene_level_url(out["scene_id"], "region")}
```

  **(c) Rework `show_timeseries`.** Change its signature defaults and body:

```python
@apps.tool(resource_uri=UI_URI, name="show_timeseries")
def show_timeseries(scene_id: str, h3: str, h3_res: int = 8, delta_t: float = 1.0,
                    ref_missions: list[str] | None = None, min_bins: int = 3) -> dict:
    """Show one cell's elevation time series and open the chart on it, with the cell outlined on satellite imagery.

    `scene_id` and `h3` come from elevation_change; keep its h3_res (default 8) -- a cell id only exists within the
    search that produced it. Returns each one-year window's median height relative to the cell's ICESat-2 reference
    plane, with its MAD error bar and the missions that contributed, plus the confidence breakdown and the
    least-squares trend in cm/yr. The trend is uncorrected for inter-mission bias and GIA -- relay that with it."""
    ref = ref_missions or api.CHANGE_REF
    out = _anticipated(api.timeseries_cell, scene_id, h3, h3_res=h3_res, delta_t=delta_t,
                       ref_missions=ref, min_bins=min_bins)
    return {**out, "view": "ts", "select": h3, "res": h3_res, "url": ts_url(scene_id, h3),
            "open_url": scene_level_url(scene_id, "region")}
```

  **(d) Replace `MCPServer(... instructions=...)`** with:

```python
    instructions=(
        "Cross-mission laser altimetry: ICESat (2003-09), Operation IceBridge (2009-19) and ICESat-2 (2018-), as one "
        "record per place. Three tools, one per question: survey_coverage (which missions measured here, and when -- "
        "instant, from the index), elevation_change (how the surface height changed, cell by cell, and where the "
        "record is long enough to tell), show_timeseries (one cell's full record, charted). Resolve place names to "
        "lat/lon yourself. Every rate is uncorrected for inter-mission bias and GIA: quote it with that caveat and "
        "with the cell's confidence and `why`, never as a bare number. Say how many cells were low confidence, not "
        "just the ones you were shown."
    ),
```

  **(e)** Also update the `add_html_resource` `description=` to "Altimetry by place: coverage globe, change map, study
  view" (it is shown to the host).

- [ ] **Step 5: Run the tests.** `uv run pytest tests/test_demo_tools.py -q` → 12 PASS. Prove the reference test can fail:
  set `CHANGE_REF = None`, rerun, see `test_the_change_level_uses_the_single_era_reference` FAIL, revert.

- [ ] **Step 6: Update the e2e scripts.**
  - In `scripts/e2e_apps.py`, replace `bound.get("open_ui", {})` with `bound.get("elevation_change", {})`.
  - Create `scripts/e2e_demo.py`:

```python
"""The UWG demo's MCP surface, over stdio exactly as Claude Desktop sees it: which tools the MODEL can see, then the
three-level question on the Jakobshavn story cell.
usage: AICESAT_PORT=8793 AICESAT_DATA_DIR=$PWD/data-uwg uv run scripts/e2e_demo.py
"""
import asyncio, json, os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LAT, LON, STORY = 69.1752, -49.3276, "8806f21187fffff"
EXT = {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}


def payload(res):
    return json.loads("\n".join(c.text for c in res.content if getattr(c, "text", None)))


async def main():
    params = StdioServerParameters(command="uv", args=["run", "aicesat-server"], env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, extensions=EXT) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            vis = lambda t: ((t.meta or {}).get("ui") or {}).get("visibility")
            model = sorted(t.name for t in tools if not vis(t) or "model" in vis(t))
            print("model-visible tools:", model)
            assert model == ["elevation_change", "show_timeseries", "survey_coverage"], model
            sv = payload(await s.call_tool("survey_coverage", {"lat": LAT, "lon": LON, "radius_km": 50}))
            print("survey_coverage:", json.dumps({k: sv[k] for k in ("missions", "indexed", "long_record")}, indent=1))
            ch = payload(await s.call_tool("elevation_change", {"lat": LAT, "lon": LON}))
            while ch.get("status") == "building":
                print("  building…")
                ch = payload(await s.call_tool("elevation_change", {"lat": LAT, "lon": LON}))
            print("elevation_change:", json.dumps({k: ch.get(k) for k in ("status", "scene_id", "n_cells", "n_reliable",
                                                                          "n_low_confidence", "summary")}, indent=1))
            ts = payload(await s.call_tool("show_timeseries", {"scene_id": ch["scene_id"], "h3": STORY}))
            b = ts["series"][0]["value_m"]
            print("show_timeseries:", STORY, ts["level"], ts["trend_cm_yr"],
                  [(round(p["year"], 1), round(p["value_m"] - b, 1)) for p in ts["series"]])
            print("OK")

asyncio.run(main())
```

- [ ] **Step 7: Suite + commit.** Run `set -o pipefail; uv run pytest -q 2>&1 | tail -3`. Tests that assert the old tool
  descriptions or `find_timeseries_candidates` routing still call plain functions and should pass. Fix any that
  enumerated model-visible tools, stating the reason in the commit message.

```bash
git add src/aicesat/api.py src/aicesat/server.py scripts/e2e_apps.py scripts/e2e_demo.py tests/test_demo_tools.py
git commit -m "feat(mcp): three model tools, one per ladder level; elevation_change on a single-era reference"
```

---

### Task 3b: Sample geometry: why the positions and slopes matter (added 2026-09-29 at Kevin's request)

`show_timeseries` also returns what the series would read if the analysis ignored where each mission sampled the
cell, so the agent can explain it with real numbers. Story cell: GLAS and IceBridge centroids ~400 m east of ATL06;
a joint plane reads −57 m against −87.7 m.

**Files:**
- Modify: `src/aicesat/timeseries.py` (`_cells_of` helper shared with `candidates`; `_load_all` carries
  `sn_slope`/`we_slope`; new `sample_geometry`)
- Modify: `src/aicesat/api.py` (`cell_geometry`), `src/aicesat/server.py` (`show_timeseries` merges it)
- Test: `tests/test_sample_geometry.py`

**Interfaces:**
- Produces: `timeseries.sample_geometry(doc, h3_cell, reported_series, delta_t=1.0, common_epoch=2005.0) -> {"missions":
  {M: {"n", "centroid_offset_m": [dx, dy], "fitted_slope": {...} | None}}, "measured_slope": {M: {...}},
  "change_m": {"reported", "ignoring_positions", "one_plane_across_all_eras"}, "explanation": str}`, and
  `api.cell_geometry(scene_id, h3, h3_res=8, delta_t=1.0, ref_missions=None)`.

- [ ] **Step 1: Failing tests** (`tests/test_sample_geometry.py`). The scene is a synthetic 3 %-sloped cell. GLAS (2005)
  sits 400 m east on the uphill side, and ATL06 (2020–22) is centred and 20 m lower from real thinning. The truth is
  −20 m. Differencing raw heights reads about −32 m, and one plane across both eras absorbs the change into slope
  (about 0 m).

```python
import h3
import numpy as np

from aicesat import scene as scene_mod
from aicesat import timeseries

FRAME = scene_mod.local_frame((-49.6, 69.1, -49.0, 69.3))
DOC = {"frame": FRAME, "z0": 0.0}
CELL = h3.latlng_to_cell(69.2, -49.3, 8)


def _recs(with_slopes=False):
    clat, clon = h3.cell_to_latlng(CELL)
    cx, cy = scene_mod.to_local(FRAME, np.array([clon]), np.array([clat]))
    rng = np.random.default_rng(1)
    out = []
    for mission, xs, years, dh in (("GLAS", (350, 450), [2005.1], 0.0), ("ATL06", (-100, 100), [2020.5, 2021.5, 2022.5], -20.0)):
        n = 60 * len(years)
        x = cx[0] + rng.uniform(*xs, n); y = cy[0] + rng.uniform(-150, 150, n)
        h = 1000.0 + 0.03 * (x - cx[0]) + dh + rng.normal(0, 0.05, n)
        lon, lat = scene_mod.to_lonlat(FRAME, x, y) if hasattr(scene_mod, "to_lonlat") else (None, None)
        rec = {"mission": mission, "x": x, "y": y, "h": h, "lat": lat, "lon": lon,
               "yr": np.repeat(np.asarray(years, "f8"), 60), "propagated": True}
        if with_slopes and mission == "ATL06":
            rec["sn"] = np.zeros(n); rec["we"] = np.full(n, 0.03)
        out.append(rec)
    return out


def _geom(monkeypatch, **kw):
    recs = _recs(**kw)
    monkeypatch.setattr(timeseries, "_load_all", lambda doc, epoch: recs)
    monkeypatch.setattr(timeseries, "_cells_of", lambda lat, lon, res: np.full(len(lat) if lat is not None else 0, h3.str_to_int(CELL), "u8"))
    rep = [{"year": 2005.1, "value_m": 0.0}, {"year": 2022.5, "value_m": -20.0}]
    return timeseries.sample_geometry(DOC, CELL, rep)


def test_it_reports_what_ignoring_positions_would_read(monkeypatch):
    g = _geom(monkeypatch)
    assert g["change_m"]["reported"] == -20.0
    assert abs(g["change_m"]["ignoring_positions"] - (-32.0)) < 1.5          # 0.03 m/m x 400 m of uphill offset
    assert abs(g["change_m"]["one_plane_across_all_eras"]) < 5.0              # the change vanished into the slope


def test_it_says_where_each_mission_sampled(monkeypatch):
    g = _geom(monkeypatch)
    assert g["missions"]["GLAS"]["centroid_offset_m"][0] > 250                # east of the cell's sample centroid
    assert abs(g["missions"]["ATL06"]["fitted_slope"]["dh_dx_m_per_km"] - 30) < 3
    assert "400" in g["explanation"] or "m apart" in g["explanation"]


def test_a_measured_product_slope_is_reported_when_the_data_carries_one(monkeypatch):
    g = _geom(monkeypatch, with_slopes=True)
    m = g["measured_slope"]["ATL06"]
    assert m["n"] == 180 and abs(m["slope_deg"] - 1.72) < 0.05
```

  The `_recs` helper needs lat/lon only for `_cells_of`, which the test replaces. If `scene_mod` has no `to_lonlat`,
  pass `lat=np.zeros(n)` instead. **Ruling if needed:** keep the test independent of the inverse projection.

- [ ] **Step 2: Run** `uv run pytest tests/test_sample_geometry.py -q` → FAIL (`AttributeError: ... sample_geometry`).

- [ ] **Step 3: Implement.**
  - **`timeseries.py`:** extract the h3ronpy/fallback block in `candidates` into
    `_cells_of(lat, lon, res) -> np.ndarray[u8]` and call it there.
  - **`_load_all`:** add `"sn": arrays.get("sn_slope"), "we": arrays.get("we_slope")` to each rec (None when absent).
  - **`sample_geometry(doc, h3_cell, reported_series, delta_t=1.0, common_epoch=2005.0)`:**
    1. Select the cell's points per mission.
    2. Centroid of all of them.
    3. Per mission: n, centroid offset, and an lstsq plane when n ≥ 10.
    4. Measured slope per mission carrying `sn`/`we`: the median `sn`, `we` rotated into local x/y with
       `frame["east_xy"]` / `frame["north_xy"]`, plus slope_deg and n.
    5. Window index `floor((yr - t0) / delta_t)`, where `t0` = min year across the whole doc.
    6. `ignoring_positions` = median raw h in the last window minus the first.
    7. `one_plane_across_all_eras` = the same on residuals about one plane fitted to all points.
    8. `reported` = the last minus the first `value_m` of `reported_series`.
    9. `explanation` = one sentence with the max centroid separation, the cell's slope, and the three numbers.
  - **`api.cell_geometry(scene_id, h3, h3_res=8, delta_t=1.0, ref_missions=None)`:** load the doc, get the reported
    series from `timeseries_cell(..., ref_missions=ref_missions or CHANGE_REF)`, and return
    `timeseries.sample_geometry(...)`. Raise KeyError for an unknown scene.
  - **`server.show_timeseries`:** add `"sample_geometry": api.cell_geometry(scene_id, h3, h3_res, delta_t, ref)`
    inside a `try`. Geometry is an explanation, so its failure must not lose the chart: on exception, set
    `{"error": str(e)}`. Append to the docstring: *"`sample_geometry` says where each mission sampled the cell, the
    slopes the data implies and measures, and what the change would read if those positions were ignored. Use it to
    explain why the answer needs the geometry."*

- [ ] **Step 4: Run the tests, prove one can fail, run the suite, then commit.**
  - Run `uv run pytest tests/test_sample_geometry.py -q` → 3 PASS.
  - Prove it can fail: make `ignoring_positions` use residuals instead of raw heights, and see the first test FAIL.
    Revert.
  - Suite: `set -o pipefail; uv run pytest -q 2>&1 | tail -3`.
  - Real check: `api.cell_geometry(<715f3d2d3a>, "8806f21187fffff")` should show GLAS about +400 m east, a joint-plane
    change near −57 m, and the reported change near −87.7 m.
  - Commit:

```bash
git add src/aicesat/timeseries.py src/aicesat/api.py src/aicesat/server.py tests/test_sample_geometry.py
git commit -m "feat(timeseries): show_timeseries explains what ignoring sample positions and slopes would read"
```

---

### Task 4: Timeline strip + change colours (shared UI helpers)

**Files:**
- Create: `src/aicesat/ui/timeline.js`
- Modify: `src/aicesat/uibuild.py:12` (`SOURCES`)
- Modify: `src/aicesat/ui/shell.css` (append)
- Test: `tests/test_timeline.js`

**Interfaces:**
- Produces `AICESAT.timeline`:
  - `mount(host, onToggle(missionKey)) -> {update(missions: string[], visible: {key: bool})}`.
  - `place(m) -> {left, width}` (percent).
  - `trendColor(trendCmYr, level, lim) -> [r, g, b, a]`.
  - `trendLimit(cands) -> cm/yr`.
  - `hullOfCells(h3ids) -> [[lon, lat], ...]`.

- [ ] **Step 1: Write the failing test** (`tests/test_timeline.js`)

```js
/* Pure helpers behind the timeline strip and the change map. Run: node tests/test_timeline.js */
const fs = require('fs'), path = require('path'), assert = require('assert');
global.window = global; global.AICESAT = {};
global.h3 = require(path.join(__dirname, '..', 'src', 'aicesat', 'widget', 'vendor', 'h3-js-4.5.0.umd.js'));
eval(fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'timeline.js'), 'utf8'));
const T = AICESAT.timeline;

// low confidence is grey whatever the trend: the colour must never carry a number the data does not support
assert.deepStrictEqual(T.trendColor(-500, 'low', 100), T.trendColor(500, 'low', 100));
const red = T.trendColor(-100, 'medium', 100), blue = T.trendColor(100, 'medium', 100);
assert.ok(red[0] > red[2], 'thinning reads red'); assert.ok(blue[2] > blue[0], 'thickening reads blue');
assert.deepStrictEqual(T.trendColor(-1000, 'high', 100), T.trendColor(-100, 'high', 100), 'clipped at the limit');
// the limit ignores gated cells, so a -26361 cm/yr artefact cannot wash out the map
const cands = [{trend_cm_yr: -26361, level: 'low'}].concat(Array.from({length: 50}, (_, i) => ({trend_cm_yr: -10 * i, level: 'medium'})));
assert.ok(T.trendLimit(cands) <= 490, 'limit ' + T.trendLimit(cands));
assert.strictEqual(T.trendLimit([]), 1);
// placement on the 2003-2027 axis
assert.strictEqual(T.place('GLAS').left, 0);
assert.ok(T.place('ATL06').left + T.place('ATL06').width <= 100);
// hull of one hex is that hex; hull of neighbours contains every vertex's longitude range
const c = h3.latLngToCell(69.175, -49.328, 8);
assert.strictEqual(T.hullOfCells([c]).length, 6);
const ring = h3.gridDisk(c, 1), hull = T.hullOfCells(ring);
const lons = ring.flatMap(x => h3.cellToBoundary(x).map(p => p[1]));
assert.ok(Math.min(...hull.map(p => p[0])) <= Math.min(...lons) + 1e-6 && Math.max(...hull.map(p => p[0])) >= Math.max(...lons) - 1e-6);
console.log('timeline ok');
```

- [ ] **Step 2: Run to verify it fails.** `node tests/test_timeline.js` → `ENOENT ... timeline.js`.

- [ ] **Step 3: Implement `src/aicesat/ui/timeline.js`**

```js
/* The mission timeline strip -- legend AND show/hide control at every level of the demo ladder -- plus the pure
   helpers the change map colours with. Each mission is a bar on one 2003-2027 axis, so the strip also says WHEN it
   flew. Pure helpers are tested by tests/test_timeline.js. */
window.AICESAT = window.AICESAT || {};
(function () {
  const T0 = 2003, T1 = 2027;
  const SPANS = {GLAS: [2003, 2009.8], ICESSN: [2009, 2019.9], ATL06: [2018.8, 2026.9], ICESAT2: [2018.8, 2026.9],
                 GEDI: [2019.3, 2026.9], GPSTRUTH: [2006.6, 2025.9]};
  const SHORT = {GLAS: 'ICESat', ICESSN: 'IceBridge', ATL06: 'ICESat-2 · land ice', ICESAT2: 'ICESat-2 · photons',
                 GEDI: 'GEDI', GPSTRUTH: 'GPS traverse'};
  const place = m => { const s = SPANS[m] || [T0, T1]; return {left: (s[0] - T0) / (T1 - T0) * 100, width: (s[1] - s[0]) / (T1 - T0) * 100}; };

  // Diverging change colour: red = surface fell, blue = surface rose, near-white = no change, clipped at +-lim.
  // Low confidence is grey whatever its trend (the confidence gate's verdict is the colour's too).
  const GREY = [150, 150, 158, 90];
  function trendColor(trendCmYr, level, lim) {
    if (level === 'low' || !Number.isFinite(trendCmYr)) return GREY;
    const t = Math.max(-1, Math.min(1, trendCmYr / (lim || 1))), a = level === 'high' ? 215 : 170;
    return t < 0 ? [Math.round(245 - 35 * -t), Math.round(245 - 185 * -t), Math.round(245 - 205 * -t), a]
                 : [Math.round(245 - 195 * t), Math.round(245 - 120 * t), Math.round(245 - 15 * t), a];
  }
  // Symmetric colour limit: the 98th percentile of |trend| over cells that passed the gate.
  function trendLimit(cands) {
    const v = (cands || []).filter(c => c.level !== 'low' && Number.isFinite(c.trend_cm_yr))
      .map(c => Math.abs(c.trend_cm_yr)).sort((a, b) => a - b);
    return v.length ? Math.max(1, v[Math.floor(0.98 * (v.length - 1))]) : 1;
  }
  // Convex hull (Andrew's monotone chain) of a set of hexes' vertices, as [[lon, lat], ...]: a Study area's polygon.
  function hullOfCells(cells) {
    const pts = [];
    for (const c of cells) for (const [lat, lon] of h3.cellToBoundary(c)) pts.push([lon, lat]);
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const half = src => { const h = []; for (const p of src) { while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], p) <= 0) h.pop(); h.push(p); } return h; };
    const lo = half(pts), up = half(pts.slice().reverse());
    return lo.slice(0, -1).concat(up.slice(0, -1)).map(p => [+p[0].toFixed(6), +p[1].toFixed(6)]);
  }
  function mount(host, onToggle) {
    host.classList.add('timeline');
    return {update(missions, visible) {
      const M = AICESAT.missions;
      const ticks = [2003, 2009, 2018, 2026].map(y => `<span class="tl-tick" style="left:${(y - T0) / (T1 - T0) * 100}%">${y}</span>`).join('');
      host.style.height = (26 + missions.length * 22) + 'px';
      host.innerHTML = `<div class="tl-axis">${ticks}</div>` + missions.map((m, i) => {
        const p = place(m), c = M.colorOf(m, null).join(','), on = visible[m] !== false;
        return `<button class="tl-chip${on ? '' : ' off'}" data-m="${m}" title="${((M.MISSIONS[m] || {}).gloss || m)} — click to show/hide"` +
          ` style="left:${p.left}%;width:${p.width}%;top:${24 + i * 22}px;--c:rgb(${c})">${SHORT[m] || m}</button>`;
      }).join('');
      host.querySelectorAll('.tl-chip').forEach(b => b.onclick = () => onToggle(b.dataset.m));
    }};
  }
  AICESAT.timeline = {mount, place, trendColor, trendLimit, hullOfCells, SPANS, T0, T1};
})();
```

- [ ] **Step 4: Register it and style it.**
  - In `uibuild.py`, set `SOURCES = ["util.js", "geo.js", "adapter.js", "map.js", "explore.js", "lake.js", "tspanel.js", "timeline.js", "survey.js", "scene.js", "ts.js", "app.js"]`.
  - Create a placeholder `src/aicesat/ui/survey.js` containing only `window.AICESAT = window.AICESAT || {};` so the build
    passes until Task 6.
  - Append to `shell.css`:

```css
/* ---- demo ladder (demo/uwg-ladder): timeline strip, change legend, study bar, analysis tabs, survey title ---- */
.timeline{position:absolute;left:50%;transform:translateX(-50%);bottom:14px;width:min(640px,calc(100% - 440px));background:rgba(14,18,26,.84);border:1px solid rgba(150,190,230,.18);border-radius:10px;padding:0 12px;z-index:5;backdrop-filter:blur(6px);box-sizing:border-box}
.tl-axis{position:relative;height:20px;border-bottom:1px solid rgba(255,255,255,.12)}
.tl-tick{position:absolute;top:4px;transform:translateX(-50%);font-size:10.5px;color:var(--muted)}
.tl-chip{position:absolute;height:18px;border-radius:9px;border:0;background:var(--c);color:#0b0f16;font:600 11px ui-sans-serif,system-ui;cursor:pointer;overflow:hidden;white-space:nowrap;padding:0 8px;text-align:left}
.tl-chip.off{background:transparent;color:var(--muted);outline:1px dashed var(--c)}
.chg-legend{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:5;display:flex;gap:8px;align-items:center;font-size:11.5px;background:rgba(14,18,26,.84);padding:5px 12px;border-radius:6px}
.chg-ramp{display:inline-block;width:120px;height:8px;border-radius:4px;background:linear-gradient(90deg,rgb(210,60,40),rgb(245,245,245),rgb(50,125,230))}
.study-bar{position:absolute;left:50%;transform:translateX(-50%);bottom:120px;z-index:6;display:flex;gap:8px;align-items:center;background:rgba(14,18,26,.94);border:1px solid rgba(255,255,255,.3);border-radius:8px;padding:6px 10px;font-size:12.5px}
.ana-tabs{position:absolute;top:12px;left:12px;z-index:6;display:flex;gap:6px;align-items:center}
.ana-tabs button.on{background:#2f6fed;color:#fff}
.level-region #controls,.level-study #controls,.level-region #bench,.level-study #bench,.level-region #navhint{display:none!important}
.level-region #tsFind,.level-study #tsFind,.level-region .tsrefrow,.level-study .tsrefrow,.level-region .tsintro,.level-region #tsList{display:none!important}
#view-survey .sv-title{position:absolute;top:18px;left:20px;z-index:5;max-width:470px;pointer-events:none}
#view-survey .sv-title h1{font:600 21px ui-sans-serif,system-ui;margin:0 0 6px;color:#eef2f8}
#view-survey .sv-title p{margin:0;color:#b8c2d0;font-size:13px;line-height:1.45}
.tsctx{display:flex;gap:10px;align-items:flex-start;margin-top:8px}
.tsctx canvas{border-radius:6px;border:1px solid rgba(255,255,255,.15)}
```

- [ ] **Step 5: Run.** `node tests/test_timeline.js` → `timeline ok`. `uv run scripts/build_ui.py` → builds. Prove the
  gate-colour check can fail: make `trendColor` ignore `level`, rerun, see the first assertion FAIL, revert.

- [ ] **Step 6: Commit**

```bash
git add src/aicesat/ui/timeline.js src/aicesat/ui/survey.js src/aicesat/uibuild.py src/aicesat/ui/shell.css tests/test_timeline.js src/aicesat/widget/dist/aicesat.html
git commit -m "feat(ui): mission timeline strip and change-map colour helpers"
```

---

### Task 5: Scene view levels (change map + study)

**Files:**
- Modify: `src/aicesat/ui/scene.js` (edits listed below, by anchor)
- Modify: `src/aicesat/ui/ts.js` (default reference only)

**Interfaces:**
- Consumes: `AICESAT.timeline.*` (Task 4); `api.extract`, `api.candidates` (existing).
- Produces: hash `#scene/<id>?level=region|study[&sel=<h3>][&parent=<id>]`.

- [ ] **Step 1: Add the level chrome to the view HTML.** In the `root.innerHTML` string on line 3, immediately after
  `<div id="deck" class="deck"></div>\n`, insert:

```
<div id="timeline"></div>\n<div id="chgLegend" class="chg-legend" hidden></div>\n<div id="studyBar" class="study-bar" hidden><span id="studyMsg"></span><button id="studyGo">Study →</button><button id="studyClear">clear</button></div>\n<div id="anaTabs" class="ana-tabs" hidden><button data-t="ts" class="on">Time series</button><button data-t="dh">Δh between missions</button><button id="toRegion">← region</button><span id="dhNote" class="small"></span></div>\n
```

- [ ] **Step 2: Add level state.** After `const colorOf = m => AICESAT.missions.colorOf(m, scene);` (line 55) insert:

```js
// ---- demo ladder: the level this scene was opened at (#scene/<id>?level=region|study)
let LEVEL = null;             // 'region' = change map, 'study' = 3-D study; null = the classic scene view
let CHANGE_LIM = 1;           // change-map colour limit (cm/yr), from the gated candidates
const STUDY = new Set();      // H3 cells shift-clicked for a Study build (region level)
let STUDY_VER = 0, ANA_TAB = 'ts', imageryAuto = false;
const camAngles = () => LEVEL === 'region' ? {rotationX: 90, rotationOrbit: 0} : {rotationX: 35, rotationOrbit: -25};
const strip = AICESAT.timeline.mount($('timeline'), m => { visible[m] = visible[m] === false; render(); updateLabels(); });
```

- [ ] **Step 3: Dim points under the change map.** In `cloudLayers`, change the main `ScatterplotLayer`'s
  `getFillColor: colorOf(m),` to:

```js
      getFillColor: (LEVEL === 'region' && candidates.length) ? colorOf(m).slice(0, 3).concat(110) : colorOf(m),
```

  and its `updateTriggers: {getRadius: PT_SCALE},` to `updateTriggers: {getRadius: PT_SCALE, getFillColor: [LEVEL, candidates.length]},`.

- [ ] **Step 4: Camera.**
  - In `fitView`, replace `rotationX: 35, rotationOrbit: -25,` with `...camAngles(),`.
  - After `fitView` add:

```js
// The MCP App transport carries no points, so there is nothing for fitView to frame: frame the DEM instead.
function fitSurface() {
  const b = surfaceExtent(); if (!b) return false;
  const span = Math.max(b.maxx - b.minx, b.maxy - b.miny) || 1, px = Math.min(root.clientWidth, root.clientHeight);
  const zoom = Math.log2(px / (span * 1.1)); if (!Number.isFinite(zoom)) return false;
  curZoom = zoom; bounds = b;
  deckgl.setProps({initialViewState: {target: [(b.minx + b.maxx) / 2, (b.miny + b.maxy) / 2, 0], ...camAngles(), zoom, minZoom: zoom - 6, maxZoom: zoom + 8}});
  return true;
}
```

  - In `applyDoc`, replace `if (!didFit && hasPositions && fitView()) didFit = true;` with
    `if (!didFit && (hasPositions ? fitView() : (!api.sceneStreamRun && fitSurface()))) didFit = true;`.
  - **Verify in the browser (Task 8) that `rotationX: 90` is top-down.** If it renders edge-on, use `rotationX: 0`
    and fix the comment.

- [ ] **Step 5: ICESat-2 reference by default** (must match `api.CHANGE_REF`).
  - In `initTimeSeries`, replace the `defRef` line with
    `const defRef = present.includes('ATL06') ? ['ATL06'] : present.includes('GLAS') ? ['GLAS'] : present;   // one era: see api.CHANGE_REF`.
  - Make the same change to the `defRef` line in `ts.js` `open()`.

- [ ] **Step 6: Auto change map.** Replace `finishLoad` with:

```js
function finishLoad() {
  console.log('[aicesat] scene loaded', Object.entries(scene.series).map(([m, s]) => m + ':' + s.n).join(' '), 'surface', scene.surface ? scene.surface.n_cells_observed : 'none', 'meshOk', meshOk);
  if (LEVEL && !candidates.length) { $('tsRes').value = LEVEL === 'region' ? 8 : 9; tsLabels(); findCandidates(); }
}
```

  In `findCandidates`, replace the line starting `if (candidates.length) selectCand(0); else {` with:

```js
    CHANGE_LIM = AICESAT.timeline.trendLimit(candidates); renderChgLegend();
    const want = params.get('sel'), wi = want ? candidates.findIndex(c => c.h3 === want) : -1;
    if (candidates.length && (!LEVEL || wi >= 0)) selectCand(Math.max(0, wi));
    else if (candidates.length) { $('tsChart').hidden = true; renderConf(null); $('tsReadout').textContent = 'click a hex to see its record · shift-click hexes to study them'; }
    else { $('tsChart').hidden = true; renderConf(null); $('tsReadout').textContent = 'no cells with 3+ time windows — try a larger cell size or a wider time window'; }
```

  Add after `findCandidates`:

```js
function renderChgLegend() {
  const el = $('chgLegend'); el.hidden = !LEVEL || !candidates.length; if (el.hidden) return;
  const low = candidates.filter(c => c.level === 'low').length;
  el.innerHTML = `<span>surface fell</span><i class="chg-ramp"></i><span>rose</span><b>±${(CHANGE_LIM / 100).toFixed(1)} m/yr</b>` +
    `<span class="small">${candidates.length - low} cells · ${low} low-confidence in grey</span>`;
}
```

- [ ] **Step 7: Colour, pick and tooltip the change hexes.**
  - In `candidateLayers`, replace the `const rings = ...` line and the `PolygonLayer` props object with:

```js
  const TL = AICESAT.timeline;
  const rings = candidates.map((c, i) => ({poly: c.xy.map(xy => [xy[0], xy[1], onGround(xy[0], xy[1], c)]), sel: i === candSel, study: STUDY.has(c.h3), c}));
  const layers = [new deck.PolygonLayer({id: 'cands', data: rings, getPolygon: d => d.poly, filled: true, stroked: true, pickable: true,
    getFillColor: d => LEVEL ? TL.trendColor(d.c.trend_cm_yr, d.c.level, CHANGE_LIM) : (d.sel ? [120, 225, 255, 20] : [200, 214, 245, 24]),
    getLineColor: d => d.sel ? [150, 235, 255, 255] : d.study ? [255, 255, 255, 255] : LEVEL ? [20, 20, 26, 110] : [200, 214, 245, 180],
    lineWidthUnits: 'pixels', getLineWidth: d => (d.sel || d.study) ? 3 : LEVEL ? 0.7 : 1.8, lineWidthMinPixels: 0.5,
    onClick: (info, ev) => {
      if (!info || info.index == null || info.index < 0) return;
      if (LEVEL === 'region' && ev && ev.srcEvent && ev.srcEvent.shiftKey) toggleStudy(candidates[info.index].h3);
      else selectCand(info.index);
    },
    updateTriggers: {getFillColor: [candSel, CHANGE_LIM, LEVEL], getLineColor: [candSel, STUDY_VER, LEVEL], getLineWidth: [candSel, STUDY_VER, LEVEL], getPolygon: Z_EXAG}})];
```

  - In `sceneTooltip`, add before the `} else if (id.startsWith('pc-')` branch:

```js
  } else if (id === 'cands' && info.object && info.object.c) {
    const c = info.object.c;
    text = `H3 ${c.h3}\n${(c.trend_cm_yr / 100).toFixed(2)} m/yr over ${Math.round(c.span_years)} yr · ${c.level} confidence` +
           (LEVEL === 'region' ? '\nclick: its record · shift-click: add to study' : '');
```

- [ ] **Step 8: Study selection and build.** After `renderChgLegend` add:

```js
function toggleStudy(h) { if (STUDY.has(h)) STUDY.delete(h); else STUDY.add(h); STUDY_VER++; syncStudyBar(); render(); }
function syncStudyBar() {
  const n = STUDY.size; $('studyBar').hidden = LEVEL !== 'region' || !n;
  $('studyMsg').textContent = `${n} cell${n === 1 ? '' : 's'} selected — study with ICESat-2 photons and Δh`;
}
async function startStudy() {
  const cells = [...STUDY]; if (!cells.length) return;
  $('studyGo').disabled = true; AICESAT.clearError();
  try {
    const d = await api.extract({polygon: AICESAT.timeline.hullOfCells(cells), question: `study: ${cells.length} cells of ${scene.question || sceneId}`,
      with_glas: true, with_icessn: true, with_atl06: true, with_atl03: true, with_coreg: true, with_gedi: false, with_gpstruth: false});
    location.hash = `#scene/${d.scene_id}?level=study&parent=${sceneId}`;
  } catch (e) { AICESAT.showError(e); }
  $('studyGo').disabled = false;
}
$('studyGo').onclick = startStudy;
$('studyClear').onclick = () => { STUDY.clear(); STUDY_VER++; syncStudyBar(); render(); };
function syncTabs() {
  const study = LEVEL === 'study';
  $('anaTabs').hidden = !study;
  $('tspanel').hidden = study && ANA_TAB !== 'ts';
  $('anaTabs').querySelectorAll('button[data-t]').forEach(b => b.classList.toggle('on', b.dataset.t === ANA_TAB));
  updateStats();
}
$('anaTabs').querySelectorAll('button[data-t]').forEach(b => b.onclick = () => { ANA_TAB = b.dataset.t; syncTabs(); });
$('toRegion').onclick = () => { const p = params.get('parent'); if (p) location.hash = '#scene/' + p + '?level=region'; };
```

  In `updateStats`, replace its first line with:

```js
  const dhTabOff = LEVEL === 'study' && ANA_TAB !== 'dh';
  const note = $('dhNote'); if (note) note.textContent = (LEVEL === 'study' && ANA_TAB === 'dh' && !coreg) ? 'no Δh here: it needs ICESat-1 shots and ICESat-2 photons in the study area' : '';
  if (!coreg || dhTabOff) { $('stats').hidden = true; return; }
```

- [ ] **Step 9: Strip + imagery on by default in level mode.** At the end of `updateLabels`, add:

```js
  strip.update(MISSION_ORDER.filter(m => scene.series[m]), visible);
  if (LEVEL && scene.imagery && !imageryAuto) { imageryAuto = true; SHOW_IMAGERY = true; $('imagery').checked = true; render(); }
```

- [ ] **Step 10: Parse the level on open.** In `this.open`, after `STREAM_BUDGET = ...;` add:

```js
  LEVEL = params.get('level') || null;
  root.classList.toggle('level-region', LEVEL === 'region'); root.classList.toggle('level-study', LEVEL === 'study');
  ANA_TAB = 'ts'; STUDY.clear(); STUDY_VER++; imageryAuto = false; syncStudyBar(); syncTabs();
```

- [ ] **Step 11: Build and smoke-test.** Run `uv run scripts/build_ui.py`. Start the private server with the sandbox
  off: `AICESAT_PORT=8791 AICESAT_DATA_DIR=$PWD/data-uwg uv run scripts/serve.py`.

  Open `http://127.0.0.1:8791/?v=1#scene/715f3d2d3a?level=region` in Chrome (claude-in-chrome), and check:
  1. The camera is top-down over imagery.
  2. The strip toggles missions.
  3. Change hexes appear, red along the trunk.
  4. Typing `8806f21187fffff` into go-to (visible in the drawer) and clicking shows the chart reading about −87 m.
  5. Shift-clicking 2 hexes shows the study bar.
  6. The console has no errors.

- [ ] **Step 12: Commit**

```bash
git add src/aicesat/ui/scene.js src/aicesat/ui/ts.js src/aicesat/widget/dist/aicesat.html
git commit -m "feat(scene): region and study levels - change map, study selection, analysis tabs"
```

---

### Task 6: The globe (level 1 UI)

**Files:**
- Modify: `src/aicesat/ui/map.js` (imagery, coverage layer, hover, click)
- Replace: `src/aicesat/ui/survey.js`
- Modify: `src/aicesat/ui/app.js` (the `survey` view and default route), `src/aicesat/ui/util.js:99` (default view),
  `src/aicesat/ui/shell.html` (top bar, view div, help)

**Interfaces:**
- Consumes: `api.coverageHexes` (Task 2), `AICESAT.timeline.mount` (Task 4).
- Produces:
  - `MapView` options `{imagery: [{url, bounds}]}`.
  - `MapView#setCoverage(byRes, visible)`.
  - `MapView#onHexClick(hex)`.
  - `AICESAT.SurveyView(root, api, openScene(sceneId, hex))` with `show(arg)` / `hide()`.
  - Hash `#survey[/W,S,E,N]`.

- [ ] **Step 1: `map.js`.**
  - In the constructor, after `this.onSelect = () => {}; ...`, add:

```js
    this.imagery = this.opts.imagery || []; this.coverage = null; this._covKey = 0; this.onHexClick = () => {};
```

  - In `render()`, directly after the `if (window.__NE_LAND) layers.push(new GeoJsonLayer({id: 'land', ...}));` line, add:

```js
    // Imagery as whole-image BitmapLayers in lon/lat: tiled raster layers did not index on the globe (see the note above),
    // but one image per region with _imageCoordinateSystem LNGLAT is tessellated onto the sphere.
    this.imagery.forEach((im, i) => layers.push(new BitmapLayer({id: 'img-' + i, image: im.url, bounds: im.bounds,
      _imageCoordinateSystem: deck.COORDINATE_SYSTEM.LNGLAT})));
    if (this.coverage) layers.push(...this.coverageLayers(H3HexagonLayer));
```

  - Add these methods to the class:

```js
  setCoverage(byRes, visible) { this.coverage = {byRes, visible}; this._covKey++; this.render(); }
  coverageRes() { const z = this.state.viewState.zoom; return z < 3 ? 3 : z < 5.8 ? 4 : 5; }
  coverageLayers(H3HexagonLayer) {
    const res = this.coverageRes(), key = res + '|' + this._covKey;
    if (this._covMemo && this._covMemo.key === key) return this._covMemo.layers;
    const {byRes, visible} = this.coverage, on = Object.keys(visible).filter(k => visible[k] !== false);
    const data = (byRes[res] || []).map(h => ({...h, seen: on.filter(k => h.missions[k])})).filter(h => h.seen.length);
    const C = AICESAT.missions.MISSION_COLORS;
    const fill = h => {
      if (on.length === 1) { const c = C[on[0]], p = h.missions[on[0]].passes;
        return [c[0], c[1], c[2], Math.round(60 + 150 * Math.min(1, Math.log10(1 + p) / 2.3))]; }
      const n = h.seen.length;   // where all three overlap is where a 20-year record exists
      return n >= 3 ? [255, 214, 102, 190] : n === 2 ? [120, 200, 220, 130] : [120, 140, 170, 70];
    };
    const layers = [new H3HexagonLayer({id: 'coverage', data, getHexagon: d => d.h3, highPrecision: 'auto', filled: true,
      stroked: true, extruded: false, pickable: true, getFillColor: fill, lineWidthMinPixels: 1,
      getLineColor: d => d.claimed ? [255, 255, 255, 150] : [255, 255, 255, 35]})];
    this._covMemo = {key, layers};
    return layers;
  }
  coverageTip(h) {
    const L = {GLAS: 'ICESat', ICESSN: 'IceBridge', ATL06: 'ICESat-2'}, res = h3.getResolution(h.h3);
    const rows = ['GLAS', 'ICESSN', 'ATL06'].filter(k => h.missions[k]).map(k => { const m = h.missions[k];
      return `<b>${L[k]}</b> ${m.passes} pass${m.passes === 1 ? '' : 'es'} · ${m.year_min}–${m.year_max}`; });
    const act = res < 4 ? 'click to zoom in' : h.claimed ? 'click to see how the surface changed here' : 'not fully indexed — pick a bright-edged hex';
    return `${rows.join('<br>')}<br><i>${act}</i>`;
  }
```

  - In `click(info)`, make the first statement:
    `if (info.layer && info.layer.id === 'coverage' && info.object) { this.onHexClick(info.object); return; }`
  - In `hover(info)`, add a branch before the `claims` branch:
    `else if (info.layer && info.layer.id === 'coverage' && info.object) html = this.coverageTip(info.object);`

- [ ] **Step 2: Write `survey.js`** (replacing the placeholder):

```js
/* Level 1 of the demo ladder: the globe, each hex coloured by which missions measured it (index only -- nothing is
   fetched). Click a res-4/5 hex to open its change map; the build behind it is implicit and shared with the model's
   elevation_change (same "H3 <hex>" question). */
window.AICESAT = window.AICESAT || {};
AICESAT.SurveyView = class {
  constructor(root, api, openScene) {
    root.innerHTML = `<div class="map" id="svMap"></div>
      <div class="sv-title"><h1>Where has the surface been measured — and what changed?</h1>
        <p>Every hex shows which laser altimeters measured it: ICESat (2003–09), IceBridge (2009–19), ICESat-2 (2018–).
        Gold = all three, a 20-year record. Toggle missions below. Click a hex to see how the surface changed there.</p></div>
      <div id="svStrip"></div>
      <div id="attrib">Imagery: Sentinel-2 cloudless 2020 by EOX IT Services GmbH (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id);
    this.api = api; this.root = root; this.openScene = openScene;
    this.visible = {GLAS: true, ICESSN: true, ATL06: true}; this.byRes = {};
    const W = 'https://tiles.maps.eox.at/wms?service=WMS&request=GetMap&version=1.1.1&layers=s2cloudless-2020&styles=&srs=EPSG:4326&format=image/jpeg';
    const img = (b, w, h) => ({url: `${W}&bbox=${b.join(',')}&width=${w}&height=${h}`, bounds: b});
    this.map = new AICESAT.MapView($('svMap'), {grid: false, draw: false, footprints: false,
      imagery: [img([-180, -90, 180, 90], 2048, 1024), img([-56, 66, -42, 72], 2048, 878)]});
    this.map.onHexClick = h => this.openHex(h);
    this.strip = AICESAT.timeline.mount($('svStrip'), m => { this.visible[m] = this.visible[m] === false; this.paint(); });
    this.strip.update(['GLAS', 'ICESSN', 'ATL06'], this.visible);
  }
  async load() {
    try { for (const r of [3, 4, 5]) this.byRes[r] = (await this.api.coverageHexes(r)).hexes; }
    catch (e) { AICESAT.showError(e); return; }
    this.paint();
  }
  paint() { this.map.setCoverage(this.byRes, this.visible); this.strip.update(['GLAS', 'ICESSN', 'ATL06'], this.visible); }
  async openHex(hx) {
    const res = h3.getResolution(hx.h3);
    const ring = h3.cellToBoundary(hx.h3), lats = ring.map(p => p[0]), lons = ring.map(p => p[1]);
    if (res < 4) { this.map.flyTo([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)]); return; }
    if (!hx.claimed) { AICESAT.showError('Not every mission is indexed over all of this hex yet — pick one with a bright edge.'); return; }
    const question = 'H3 ' + hx.h3;   // == api.region_question: the model's elevation_change reuses this scene
    try {
      const hit = ((await this.api.scenes()) || []).find(s => s.question === question && (s.status === 'ready' || s.status === 'loading'));
      if (hit) return this.openScene(hit.scene_id, hx.h3);
      const d = await this.api.extract({polygon: ring.map(([lat, lon]) => [+lon.toFixed(6), +lat.toFixed(6)]), question,
        with_glas: true, with_icessn: true, with_atl06: true, with_atl03: false, with_gedi: false, with_gpstruth: false, with_coreg: false});
      this.openScene(d.scene_id, hx.h3);
    } catch (e) { AICESAT.showError(e); }
  }
  show(arg) {
    this.root.classList.add('on');
    if (!this._loaded) { this._loaded = true; this.load(); }
    const b = (arg || '').split(',').map(Number);
    this.map.flyTo(b.length === 4 && b.every(Number.isFinite) ? b : [-51.5, 68.6, -47.5, 70.0]);
  }
  hide() { this.root.classList.remove('on'); }
};
```

- [ ] **Step 3: Routing and shell.**
  - `util.js:99`: change `view: view || 'explore'` to `view: view || 'survey'`.
  - In `app.js` `get(name)`, add
    `if (name === 'survey') views[name] = new AICESAT.SurveyView(root, api, (id, hx) => { location.hash = '#scene/' + id + '?level=region&hex=' + hx; });`.
  - In `route()`, change the name line to
    `const name = ['survey', 'explore', 'lake', 'scene', 'ts'].includes(r.view) ? r.view : 'survey';`.
  - Change `if (name === 'explore' || name === 'lake') lastList = name;` to include `|| name === 'survey'`.
  - Change `else v.show();` to `else v.show(r.arg);`.
  - Change `let current = null, lastList = 'explore';` to `'survey'`.
  - In `shell.html`:
    - Replace the brand/tagline with `<span class="brand">Altimetry, by place</span><span class="tagline">ICESat · IceBridge · ICESat-2</span>`.
    - Delete the two `data-view` tab buttons (Explore and Data Lake stay reachable at `#explore` and `#lake`).
    - Add `<div class="view" id="view-survey"></div>` as the first child of `#views`.
    - Replace the help panel's `<ol>` with three items: **Coverage** (the globe: which missions measured each hex),
      **Change** (click a hex: each cell's trend and its 20-year record), **Study** (shift-click cells: ICESat-2
      photons and the height difference between missions).

- [ ] **Step 4: Build and check in the browser.** Run `uv run scripts/build_ui.py`, then hard-reload
  `http://127.0.0.1:8791/?v=2#survey`. Check:
  1. The globe shows imagery. If the BitmapLayer does not tessellate on the globe, delete the world image and keep the
     regional one; if that fails too, drop `imagery` and keep Natural Earth. Record which in the commit message.
  2. Gold hexes over West Greenland, and Nepal/Summit elsewhere.
  3. The strip recolours per mission.
  4. Hover shows passes and years.
  5. Clicking the hex containing 69.175, −49.328 (zoom in until res 5) opens `#scene/<id>?level=region&hex=...`, and the
     scene streams in.

- [ ] **Step 5: Commit**

```bash
git add src/aicesat/ui/map.js src/aicesat/ui/survey.js src/aicesat/ui/app.js src/aicesat/ui/util.js src/aicesat/ui/shell.html src/aicesat/widget/dist/aicesat.html
git commit -m "feat(ui): coverage globe - hexes by mission, EOX imagery, click a hex to open its change map"
```

---

### Task 7: Claude Desktop embed (chart + context + Open in 3D) and result routing

**Files:**
- Modify: `src/aicesat/ui/app.js` (`onResult`), `src/aicesat/ui/ts.js`, `src/aicesat/ui/adapter.js`

**Interfaces:**
- Consumes: tool results `{view, scene_id, query, select, res, open_url, bbox}` (Task 3).
- Produces: `api.imageryDataUrl(id) -> Promise<string|null>`; `AICESAT.lastOpenUrl`.

- [ ] **Step 1: Route every tool result.** In `app.js`, replace `const onResult = r => {...};` with:

```js
    const onResult = r => {
      const sc = AICESAT.toolPayload(r); if (!sc) return;
      if (sc.open_url) AICESAT.lastOpenUrl = sc.open_url;     // the ts view's "Open in 3D" (the host origin is not ours)
      if (sc.view === 'ts' && sc.scene_id) location.hash = '#ts/' + sc.scene_id + '?' + [sc.select && 'sel=' + sc.select, sc.res && 'res=' + sc.res].filter(Boolean).join('&');
      else if (sc.view === 'survey') location.hash = '#survey/' + (sc.bbox || []).join(',');
      else if (sc.scene_id) location.hash = '#scene/' + sc.scene_id + (sc.query ? '?' + sc.query : '');
      else if (sc.view) location.hash = '#' + sc.view;
      else if (!location.hash) location.hash = '#survey';
      route();
    };
```

- [ ] **Step 2: Imagery for the context map.**
  - In `adapter.js` `fetchApi`, add `imageryDataUrl: id => Promise.resolve('/api/scene/' + id + '/imagery.jpg'),`.
  - In `appApi`'s object, add
    `imageryDataUrl: async id => { const b64 = await chunkedBytes(id, 'imagery'); return b64 ? 'data:image/jpeg;base64,' + b64 : null; },`.

- [ ] **Step 3: Context map + Open in 3D in `ts.js`.**
  - In the HTML, after the `tscontrols` div's closing `</div>` (inside `tshead`), insert:

```
'    <div class="tsctx"><canvas id="tsCtx" hidden></canvas><div><button id="tsOpen3d">Open in 3D ↗</button><div class="small" id="tsCtxNote"></div></div></div>\n' +
```

  - Add after `const select = i => {...};`:

```js
    let meta = null;
    // Where the cell is: the scene's own imagery, every candidate outlined faintly, the selected one bright.
    const drawCtx = async () => {
      const cv = $('tsCtx');
      if (!meta || !meta.imagery || !meta.frame) { cv.hidden = true; return; }
      const src = await api.imageryDataUrl(sceneId); if (!src) { cv.hidden = true; return; }
      const im = new Image();
      im.onload = () => {
        const Wc = 220, Hc = Math.round(Wc * im.height / im.width), d = devicePixelRatio || 1;
        cv.width = Wc * d; cv.height = Hc * d; cv.style.width = Wc + 'px'; cv.style.height = Hc + 'px'; cv.hidden = false;
        const ctx = cv.getContext('2d'); ctx.drawImage(im, 0, 0, cv.width, cv.height);
        const ex = meta.imagery, px = (x, y) => [(x - ex.x0) / (ex.x1 - ex.x0) * cv.width, (1 - (y - ex.y0) / (ex.y1 - ex.y0)) * cv.height];
        const ring = (c, style, lw) => { ctx.beginPath(); c.xy.forEach((p, k) => { const q = px(p[0], p[1]); if (k) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
          ctx.closePath(); ctx.strokeStyle = style; ctx.lineWidth = lw * d; ctx.stroke(); };
        candidates.forEach(c => ring(c, 'rgba(255,255,255,0.35)', 0.8));
        if (sel >= 0) ring(candidates[sel], 'rgb(150,235,255)', 2.5);
      };
      im.src = src;
    };
    $('tsOpen3d').onclick = () => api.openLink(AICESAT.lastOpenUrl || (location.origin + '/#scene/' + sceneId + '?level=region'));
```

  - In `select`, add `drawCtx();` as its last statement.
  - In `open()`, change `const meta = await api.sceneMeta(id);` to `meta = await api.sceneMeta(id);`.

- [ ] **Step 4: Build + check both transports.**
  - Run `uv run scripts/build_ui.py`.
  - Browser: `http://127.0.0.1:8791/?v=3#ts/<region scene id>?sel=8806f21187fffff&res=8`. The chart reads about −87 m,
    the context map shows the hex outlined, and Open in 3D opens the region scene in a new tab.
  - App transport: `AICESAT_PORT=8793 AICESAT_DATA_DIR=$PWD/data-uwg APPS=1 uv run scripts/e2e_apps.py` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/aicesat/ui/app.js src/aicesat/ui/ts.js src/aicesat/ui/adapter.js src/aicesat/widget/dist/aicesat.html
git commit -m "feat(ui): the embedded time series carries its place - context map and Open in 3D"
```

---

### Task 8: Pre-warm, end-to-end, dry run

**Files:**
- Create: `scripts/prewarm_demo.py`
- Create: `deploy/claude-desktop-demo.json`

- [ ] **Step 1: Confirm the indexes finished.** Run `tail -2 data-uwg/uwg_atl06_index.log data-uwg/uwg_atl03_index.log`
  and expect `DONE ... exit=0`.
  - If ATL06 is still running, **do not** pre-warm yet: a scene built now is refused (the claim is stamped only at the
    end). Work on anything that doesn't need it.
  - Then run `AICESAT_DATA_DIR=$PWD/data-uwg uv run python -c "from aicesat import survey; o=survey.coverage_hexes(5); print(sum(h['claimed'] for h in o['hexes']))"`
    and expect more than 50 claimed hexes.

- [ ] **Step 2: Write `scripts/prewarm_demo.py`**

```python
"""Fetch the demo's change-level area once, through the same path the globe and elevation_change use (the "H3 <hex>"
scene), so the recording is a lake hit. Run it ONLY after the ATL06 index has finished.
usage: AICESAT_DATA_DIR=$PWD/data-uwg uv run scripts/prewarm_demo.py [lat lon]
"""
import sys
import time

import h3

from aicesat import api

STORY = "8806f21187fffff"
lat, lon = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (69.1752, -49.3276)
hx = api.region_hex(lat, lon)
if len(sys.argv) <= 2 and h3.cell_to_parent(STORY, 5) != hx:
    sys.exit(f"story cell's res-5 parent {h3.cell_to_parent(STORY, 5)} != the hex its centre falls in ({hx}); "
             "the globe click and elevation_change would build different scenes")
if any(s.get("question") == api.region_question(hx) and s.get("status") == "ready" for s in api.scenes()):
    sys.exit(f"{hx}: already built")
t0 = time.time()
doc = api.build_scene(polygon=api.hex_polygon(hx), question=api.region_question(hx), wait_for_imagery=True,
                      log_fn=print, **api.REGION_FLAGS)
print(f"{hx}: scene {doc['scene_id']} in {time.time() - t0:.0f}s", {m: s["n"] for m, s in doc["series"].items()})
```

- [ ] **Step 3: Pre-warm (sandbox off; long).** Run `AICESAT_DATA_DIR=$PWD/data-uwg uv run scripts/prewarm_demo.py`,
  and expect a scene id plus per-mission counts (ATL06 about 500k–1M). Then check the story cell:

  `AICESAT_DATA_DIR=$PWD/data-uwg uv run python -c "from aicesat import api; print(api.timeseries_cell('<scene id>', '8806f21187fffff', h3_res=8, ref_missions=['ATL06'])['trend_cm_yr'])"`

  Expected about −414. **If the story cell reads materially differently from −87 m 2005→2026, stop and report.** The
  hex scene clips to the hex polygon, not the old box, so edge effects are possible.

- [ ] **Step 4: MCP end-to-end.** Run `AICESAT_PORT=8793 AICESAT_DATA_DIR=$PWD/data-uwg uv run scripts/e2e_demo.py`.
  Expected:
  - Model-visible tools are the three.
  - `survey_coverage` shows all three missions with passes.
  - `elevation_change` returns `ready`, reusing the pre-warmed scene (no build).
  - `show_timeseries` returns the story cell's series. Then `OK`.

- [ ] **Step 5: Browser walk-through on 8791.** Globe → click the story hex → region scene (a lake hit, fast) → click the
  story cell → chart. Then shift-click 2–3 red cells next to it → Study → a photon build over the ATL03 index box
  (−49.9, 69.1, −49.1, 69.25) → 3-D with photons, Δh tab populated. The Study build is the demo's second pre-warm:
  keep that scene. Record a GIF with `gif_creator` (`uwg_ladder_dryrun.gif`).

- [ ] **Step 6: Claude Desktop config for Kevin** (`deploy/claude-desktop-demo.json`)

```json
{
  "_comment": "UWG demo: local stdio server on the ISOLATED demo store. Merge into ~/Library/Application Support/Claude/claude_desktop_config.json and restart Claude Desktop. AICESAT_PORT 8792 keeps it off Kevin's 8765 and the test 8791; Open in 3D uses it.",
  "mcpServers": {
    "aicesat-demo": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/Users/kebe6994/projects/hackathon/aicesat", "run", "aicesat-server"],
      "env": {"AICESAT_DATA_DIR": "/Users/kebe6994/projects/hackathon/aicesat/data-uwg", "AICESAT_PORT": "8792",
              "AICESAT_EDL_FILE": "/Users/kebe6994/.edl/token.prod"}
    }
  }
}
```

- [ ] **Step 7: Full suite, JS tests, commit.**

```bash
set -o pipefail; uv run pytest -q 2>&1 | tail -3
for t in tests/*.js; do node "$t" || exit 1; done
git add scripts/prewarm_demo.py deploy/claude-desktop-demo.json
git commit -m "chore(demo): pre-warm script and Claude Desktop config for the isolated demo store"
```

- [ ] **Step 8: Hand Kevin the demo script:**
  - Globe (toggle missions) → click the Jakobshavn hex → change map → story cell chart → Study.
  - Claude Desktop prompt: *"Has the ice near Jakobshavn Isbræ changed since ICESat? Where is the record long enough
    to tell?"*
  - Include the one honest-framing sentence from the spec.
