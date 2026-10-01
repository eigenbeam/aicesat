# Land the UWG demo branch, then separate aicesat into parts

Written 2026-10-01 to pick up later. Decided with Kevin: land `demo/uwg-ladder` on `main` by **fast-forward plus
restore commits** (not cherry-picks), and `main` adopts the **branch's time-series defaults** (ATL06 reference, res 8,
fixed-effects plane, 1 m slope-removal gate). The evidence comes from three read-only code maps and an adversarial
review. Claims marked *verified* were re-checked by grep.

## Where it stands (pick up here)

**Branch:** `land/uwg-demo`. It's the demo tip plus S0 and S1. `main` (`efc5e10`) is still an ancestor, so landing is
a pure fast-forward. The local tag `uwg-demo-2026-09-30` marks the recorded demo at `fad1893`; it isn't pushed yet.

| Commit | Stage | What |
|---|---|---|
| `537492f` | S0 | `tests/test_golden.py` + `tests/golden/uwg.json`: the demo store's numbers, pinned |
| `9b1accd` | S1 | MCP: the 11 de-registered model tools are back (14 total); `AICESAT_PROFILE=demo` gives the 3-tool demo |
| `ac998ce` | S1 | UI: Coverage / Explore / Data Lake tabs and help restored; the classic TS controls and detailed readout restored; demo look = profile |
| `eefe759` | S1 | Time-series defaults ATL06 + res 8 everywhere; before/after in `docs/science/2026-10-01-timeseries-defaults.md` |
| `1602f09` | S1 | `scripts/e2e_demo.py` runs in the demo profile |

**Verified at `1602f09`:**
- `uv run pytest -q` gives 734 passed, 4 skipped. Run it outside the sandbox; the access-pool, serve and stream-http
  tests need sockets and the EDL token.
- The golden suite (9) passes unchanged.
- All `node tests/*.js` pass.
- `scripts/e2e_demo.py` passes in the demo profile and `scripts/e2e_apps.py` in the full profile.

**Not done yet:**
1. **Landing (outward-facing, Kevin decides):**
   - **(a)** `git push origin land/uwg-demo:main` plus `git push origin uwg-demo-2026-09-30` keeps the reviewed commits.
   - **(b)** A PR for review. GitHub's merge buttons rewrite the commits, so after review push the fast-forward by hand.
2. **`scripts/e2e_mcp.py` not run.** It builds a new scene from NASA (`show_photons` / `add_glas` / `coregister`).
   Run it once before or after landing: `AICESAT_PORT=8766 uv run scripts/e2e_mcp.py`.
3. **Running setups:** the 8791 server and Claude Desktop run this worktree's code, and the default profile is now
   `full`. Add `AICESAT_PROFILE=demo` to both to keep the recorded demo (the script's Setup section shows the command).
   `deploy/claude-desktop-demo.json` is now a template.
4. **Deferred from S0 and S1:**
   - The FakeReader fetch matrix for GEDI/GPSTRUTH moves to just before S4, which changes fetch.
   - Removing the dead Study code moves to S5 with the other dead code.
5. **Tracking:** S2–S6 are not yet filed on GitHub Project eigenbeam #2.

**Golden tests run against an APFS clone,** not `data-uwg`. The read path is not read-only: on the first run
`index_status` rebuilt GPSTRUTH's stale coverage manifest through `coverage._ensure_manifest`.
```
cp -c -R data-uwg data-golden                      # from the repo root; instant, shares blocks
AICESAT_DATA_DIR=$PWD/data-golden uv run pytest -m golden
AICESAT_GOLDEN_UPDATE=1 ...                        # only after an INTENDED change; say why in the commit
```

## 1. The demo branch against main

- `main` is the merge base (`efc5e10`). The branch was 44 commits ahead, and a read-only `merge-tree` shows
  **0 conflicts**.
- **What main gains:**
  - 4 general bug fixes:
    - `serve.py` `__main__` guard (5438215). Without it, ATL03 builds from the web UI hang.
    - coreg `stride` KeyError (e43aef5).
    - The browser loads co-registration (275a513).
    - Platelets drew 0 triangles (e711402).
  - `ui_candidates` chunking (6a87d7d).
  - The fixed-effects plane, the slope-removal gate and `sample_geometry`.
  - Survey coverage hexes and day counts, GeoNames names, the coverage globe, the change map, CSV download, and
    Sources.
- **What main would have lost, merged as-is (now restored on `land/uwg-demo`):**
  - 11 of 12 MCP model tools (40c04cb).
  - The Explore and Data Lake tabs and help (1709c5f, 6abd996).
  - The classic scene view's TS sliders and status (1dc3bf5).
  - The TS readout detail.
  - On-map credits in every view.
  - The silent `show_timeseries` default change.
- **The generated bundle:** 27 of 44 commits touch `src/aicesat/widget/dist/aicesat.html` (*verified*). Resolve it by
  rebuilding (`uv run scripts/build_ui.py`), never by hand.

## 2. Separating the system: verdict

The proposal was four parts: 1 indexing engine, 2 query and search engine (index → data from S3), 3 visualization
site + MCP server, 4 analytical engine + interface. Four parts is the right number, but two of the boundaries are in
the wrong place.

| Proposed | Verdict | Why (evidence) |
|---|---|---|
| 1 Indexing / 2 Query | **One part with two roles**, not two engines | Index rows *are* byte-range plans that `access.decode_chunk` consumes, so the format couples them by design. The role line already exists as `tests/test_index_only.py`: the builder may search CMR and is the only writer; the reader uses only the index. |
| 3 Viz site + MCP server | Right, as **delivery** | HTTP + stream, MCP, TUI and the browser are all delivery. The browser has to stop duplicating server policy: mission colours, year spans, default reference, region identity. |
| 4 Analytical engine + interface | The engine is right; "interface" is the **application API** | The analysis maths is already pure (`timeseries._fit_cell`, `_confidence`, `coreg.propagate`, `gia`, `lakelevel`), but every entry point takes a *scene doc*. `api.py` is the de facto application API, mixing four concerns under one `_lock`. |

**Recommended parts**, on a small **shared core** (H3 cells, metric frame, geodesy, collection registry):
- **Data:** index build + index-driven access + the lake.
- **Analysis:** pure science.
- **Application API:** `api.py` split into scenes/jobs, analysis and admin.
- **Delivery:** HTTP, stream, MCP, TUI, browser.

Two seams matter as much as the layers:
- **Per-collection plugins.**
  - Collection knowledge is hard-coded in about 10 places: `coverage._index_for`, `api.LEGS`, `lake.PRODUCTS`,
    `scene.COLORS`, `timeseries.MISSION_LABEL`, `survey.MISSIONS`, the region windows, and the JS.
  - Build orchestration is copied 6 times and `fetch_bbox` 5 times.
- **The scene hub.**
  - A scene is four things at once: a query cache handle, a presentation artifact, an analysis input, and a result
    and memo store.
  - It should become a presentation artifact with a stable identity (`region_hex`), not a question string.

What's tangled today:
- Counting function-local imports, there's one 13-module cycle across all layers. It comes mostly from shared H3
  helpers living in `planner.py`, plus `planner → coreg → scene`.
- `coverage.py` is registry, gate and presentation all at once.
- Lake state lives in `data/index/`.
- 16 modules build paths from `cache.DATA_DIR`.

Not worth doing now:
- A separate service layer. Split `api.py` instead.
- A `MeasurementSet` class. The tests already use plain `{mission: (arrays, meta)}` plus a frame.
- Subpackage moves. There are about 70 test monkeypatch sites.
- import-linter. There's no CI.
- A single Collection protocol. Fetch has three shapes, so use two templates (chunk-unit for ATL06/GLAS/GEDI, span-unit
  for ICESSN/GPSTRUTH) and leave ATL03 alone.

Two corrections from the review (*verified*):
- `indexed_<c>_granules` is called only by builders, not by read paths.
- `coverage._ensure_manifest`'s lazy rebuild is load-bearing. The ATL03 builder (`scripts/build_index.py`) never calls
  `coverage.build_manifest`, so don't make readers read-only before every builder rolls up.

## 3. Stages

Each stage is its own branch and PR off `main`. The golden numbers must be unchanged at every stage.

**S0, golden tests: done** (`537492f`). The FakeReader fetch matrix for GEDI/GPSTRUTH (cold, warm, evicted, force,
polygon in `tests/test_lake_cache.py`) is deferred to just before S4.

**S1, land the branch: done on `land/uwg-demo` except the push.**
- Tag, fast-forward `main`, and the restore commits listed above.
- Pulling out the dead Study code (`toggleStudy`, `startStudy`, `#studyBar`, `#anaTabs`) is deferred to S5.

**S2, break the cycle.** Move pure symbols and leave re-export shims:
- `cells.py`: the H3 helpers from `planner.py` (`cells_for_bbox`, `_cells_vectorized`, `coverage_cells`,
  `addressing_cells`, `search_polygon`, `cells_bbox`, `claim_res`, `COVERAGE_RES`).
- `frame.py`: `frame_crs`, `local_frame`, `to_local` from `scene.py`.
- `geodesy.py`: `propagate`, `decimal_year` from `coreg.py`.
- `indexstore.py`: `granule_name`, `index_files_for_cells`, `read_parquet_src` and the manifest code from `coverage.py`.
- `precache_adjacent` → `lake.py`.

Retarget the `planner.*` monkeypatches in the same commit; shims would make them silently stop patching. Add a
cycle-ratchet test using the AST pattern of `test_index_only.py`: the cycle may only shrink.

**S3, a data-only collection registry.**
- Grow it from `coverage.collections()` / `index_module` (`coverage.py:147-194`).
- Fields: key, mission, res, index dir, ym expression, product/version, window, flag, label, colour, default, and an
  explicit `build_priority`. LEGS order decides z0 and series order, and it differs from `collections()` order.
- Replace `_index_for`, `LEGS`, `COLORS`, `MISSION_LABEL`, `PRODUCTS` and the windows.
- The UI reads colours, spans and the default reference from `/api/collections`.

**S4, a generic build, then fetch templates.**
- Generalize `build_atl06.build_bbox` (`build_atl06.py:35-115`) into one orchestrator. It is the only CMR caller and
  always rolls up, which fixes the ATL03 rollup.
- Delete `build_gedi.py` and the script copies (`scripts/build_{glas,icessn,gpstruth}_index.py`, `build_index.py`).
- Then add two `fetch_bbox` templates and remove four of the five `_fetch_direct` copies.

**S5, analysis takes data; split `api.py`.**
- `_load_all(series, frame)` / `_reload_arrays` behind a 3-line doc adapter.
- Split `api.py` into scenes/jobs, analysis (its own lock and memo; today `scene_candidates` waits on the build
  `_lock`) and admin.
- Scene identity becomes a `region_hex` registry field, replacing `'H3 <hex>'` strings in Python and JS.
- HTTP routes for the 3 model tools. HTTP stops bypassing api for coverage, coverage_hexes and bench.
- `stream.py` stops reading `api._jobs` / `_registry`.
- Delete `server.add_glas`, `adapter.sceneDoc` and the dead Study code.

**S6, optional, each needing a stated reason.**
- Move the lake store out of `data/index` (`lake.py:31,293-294`), with a move-if-missing shim.
- Make the index read-only on the read path, after S4 makes every builder roll up.
- Stop materializing ATL03 `coreg_lon/lat`. Only `lake.py`, `planner.py` and two tests reference them (*verified*).
- import-linter, once CI exists.

## 4. Verification (every stage)

- `uv run pytest -q` outside the sandbox, plus `uv run pytest -m golden` against `data-golden`. The golden numbers must
  be unchanged.
- All `node tests/*.js` pass. `uv run scripts/build_ui.py` rebuilds the bundle with no diff beyond the stage's intent.
- `scripts/e2e_demo.py` (demo profile), `scripts/e2e_apps.py` and `scripts/e2e_mcp.py` pass.
- After S1 lands:
  - Claude Desktop in the full profile lists 14 tools.
  - In the demo profile, the UWG prompt reproduces 204 / 233.
  - The `docs/demo/uwg-demo-script.md` walk-through matches its numbers.
- S2: the cycle-ratchet test passes.
- S4: index → claim → rollup on a small box for every collection, with `coverage_hexes` unchanged.
