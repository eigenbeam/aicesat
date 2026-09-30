# UWG demo: the coverage → change → study ladder

**Date:** 2026-09-29 · **Branch:** `demo/uwg-ladder` (from `main` efc5e10; may be thrown away) · **Demo:** NSIDC User
Working Group, 2026-09-30, **recorded** (waits are clipped; live-fetch risk is not a design constraint).

## Goal

Show the UWG a new, thought-provoking way to look at altimetry: **place-centric, not track-centric**. Pick a place and
see every mission over it, ICESat (2003–09), IceBridge (2009–19) and ICESat-2 (2018–), then drill down from "where is
there a record" to "what changed" to "study this". Close in Claude Desktop, where the MCP server gives the agent
the same three levels as high-level tools, so the agent never has to work out granules, variables or corrections itself.

Not smoke-and-mirrors: every number on screen is computed live from ingested points by the production code path.
The region is pre-indexed and the demo areas were fetched once beforehand; say so.

## The ladder: each UI level shows what the system actually knows at that level

| Level | Shows | Data source | Cost |
|---|---|---|---|
| 1 Coverage (globe) | which missions passed over each hex, how often, which years | index Parquet (no NASA calls) | instant |
| 2 Change (region) | per-hex trend + multi-mission time series | a scene built implicitly over the rough area | one fetch (pre-warmed) |
| 3 Study (sub-area) | 3-D points incl. ATL03 photons, fine hexes, Δh between missions | a scene over the sub-area, `with_atl03` + `with_coreg` | lake hits + photons |

**Region:** box A = (-51.5, 68.6, -47.5, 70.0), ~160×155 km of West Greenland around Jakobshavn. CMR: GLAS 235,
IceBridge 2,274, ATL06 1,098 granules. ATL03 is indexed only over the study box (-49.9, 69.1, -49.3, 69.25), 215
granules. Box A was chosen over the smaller box B (-50.5, 68.8, -48.0, 69.6) after measuring the index rate: IceBridge
~4.6 granules/s and GLAS ~0.5/s, so both finish in minutes.

**Data isolation:** the demo runs on its own store, `data-uwg/` (gitignored), an APFS copy-on-write clone of `data/`
made 2026-09-29 ~22:50 and selected with `AICESAT_DATA_DIR`. Every index build, pre-warm, demo server and the demo's
Claude Desktop config point at it; `data/` and Kevin's 8765 server are never written. Reason: re-indexing a granule for
new ground filters its rows to the *new* build's cells, so a granule shared by two claims can silently lose the older
claim's rows. It did not happen here (checked: 0 of the 57 rewritten GLAS/ATL06 granules serve the Nepal or Summit
claims), but that latent bug is worth an issue after the demo.

## UI

**Chrome:** one top bar (title, breadcrumb `West Greenland › area › study`, `?`). No tabs; Data Lake stays reachable
at `#lake` for dev only. A **mission timeline strip** at the bottom of every level is both legend and toggle
(each mission alone or together).

**Level 1: globe.** EOX Sentinel-2 cloudless tiles on the globe (deck.gl `TileLayer` on `GlobeView`; fall back to
Natural Earth if that misbehaves). Coverage hexes at res 3/4/5 by zoom, only inside index claims. All missions on:
color = number of missions present. One mission on: that mission's color, opacity = number of passes. Hover shows per
mission: passes, year range. **Click a res-4/5 hex → it becomes the rough area** (polygon clipped to the claim with
the existing `fit_to_coverage`), and the camera flies into level 2. Clicking a res-3 hex just zooms in. Box drag is a
secondary gesture. No build button, no collections list, no Fit, no claim outlines, no scenes list.

**Level 2: region.** The existing scene viewer opened **top-down** over imagery + DEM. The implicit build runs
`build_scene(polygon, GLAS + ICESSN + ATL06, no ATL03)` and points stream in by mission color. When it is ready,
**change hexes** (res 8 default, slider 7–9) fade in: diverging blue–red by `trend_cm_yr`, symmetric, clipped at the
2nd–98th percentile of reliable cells, opacity by confidence, **low-confidence cells grey**. Click a hex → **time-series
drawer** (existing `ts.js` chart) with the cell's multi-mission series. Shift-click hexes or drag a box → **Study**.

**Level 3: study.** `build_scene(sub-area, all missions + ATL03 + coreg)`. Only the ATL03 part needs new fetching.
Outside the ATL03 study box, Study builds without photons and says so. The camera tilts to 3-D, change hexes at res 9–10, and
one **Analyses** drawer has two tabs: *Time series* and *Δh between missions* (today's co-registration panel). Back
returns to level 2.

Hidden (code kept): Explore's 3-step panel, panels dropdown, vertical-exaggeration slider (fixed default), plate-motion
checkbox (moves into the Δh tab), "How the data got here" (moves under `?`).

## Backend

1. **`coverage_hexes(bbox, res)`** (api + HTTP route + app-only tool): DuckDB over the index Parquet of GLAS/ICESSN/ATL06
   → per hex per mission `{passes, year_min, year_max, n_years}`. Rows are keyed at res 5; res 3/4 roll up to parents.
   Dates from `gdate` / granule names. Restricted to claimed cells.
2. **Implicit build** = existing `extract` / `build_scene` with a polygon. No new build machinery.
3. **Change map:** `candidates(doc, h3_res, ref_missions=all present)`. All missions as the reference gives 855–890 cells at res 8
   on the Jakobshavn scene against 112 for GLAS-only. **Verify** that the story cell `8806f21187fffff` still reads −87 m
   2005→2026 with the 2017–19 rebound under this reference before relying on it.
4. **Confidence gate (science fix, required):** in `timeseries._confidence`, force `level = "low"` when the roughness
   score is 0 (within-window scatter ≥ 1.5 m) or `n_ref < 10`. Gate on the *quality of the evidence*, never on the size
   of the answer. Motivating case: `8806f200d3fffff` at res 8 / ref ATL06 reports −263 m/yr at "medium" (8 ref points,
   708 m scatter). The test must fail with the gate removed.

## MCP (Claude Desktop close)

The model sees one tool per ladder level (area = `lat, lon, radius_km` or bbox; Claude resolves place names):

- **`survey_coverage(area)`**: per mission passes and years over the area, plus whether it is indexed. Index only,
  instant. Embed: the globe flown to the area with coverage hexes.
- **`elevation_change(area, h3_res=8, limit=10)`**: reuse a ready scene for the area or build one; wait up to ~50 s,
  else return `{status: "building", job_id}`. Returns the reliable cells ranked, with trend, span, missions, lat/lon and
  `n_total`, plus the caveats (no inter-sensor bias correction, no GIA). Embed: the change map.
- **`show_timeseries(scene, cell)`** (exists): embed = the chart + the cell outlined on the scene's imagery (context) + an
  **Open in 3D** button that opens the full scene in the browser from the same local server (MCP Apps `openLink`; verify
  the vendored bridge supports it).

Everything else becomes model-invisible on this branch (`show_photons`, `add_glas`, `coregister`, `open_ui`,
`lake_*`, `list_regions`, `job_status`; a repeated `elevation_change` on the same area waits on the same build, so the
model never needs a job id). No "Slice N" / "Greenland demo regions"
wording in any model-visible description.

Demo prompt (approx.): *"Has the ice near Jakobshavn Isbræ changed since ICESat? Where is the record long enough to
tell?"* → `survey_coverage` → `elevation_change` → `show_timeseries` on the story cell.

## Build order (stop anywhere and still demo)

1. Index builds into `data-uwg/` (restarted 22:51 over box A). 2. Confidence gate + test. 3. Level 2 (top-down, change hexes, drawer,
timeline strip, decluttered chrome). 4. Level 1 (coverage endpoint, globe imagery, click-to-fly). 5. Level 3 (study,
analyses drawer). 6. MCP tools + embed. 7. Pre-warm the demo areas; dry run.

## Testing

pytest for `coverage_hexes`, the confidence gate (proven able to fail), and the MCP tool responses; node tests for pure
UI helpers (color scale, hex→area) following `tests/test_coverage_gate.js`; Chrome end-to-end on a private server
(`AICESAT_PORT=8791 AICESAT_DATA_DIR=data-uwg`, never Kevin's 8765); `scripts/e2e_apps.py` for the App path. Kevin does the final Claude Desktop
check (Claude cannot drive Desktop).

## Risks

`TileLayer` on `GlobeView` unverified · ATL06 index rate over box A unmeasured (1,098 granules) · Claude Desktop MCP App
rendering is reportedly flaky · a res-4 rough area is ~4–5M ATL06 points (streams have handled 2.8M; candidates ran
at ~2 s per 1M) · the all-missions reference plane is unverified on the story cell.

## Out of scope

A single continuous globe→3-D canvas, viewport-driven loading, the EC2 box, merging to `main`, fixing the
shared-granule re-index bug (issue only).
