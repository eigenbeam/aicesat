# Task: carry PRs #15 and #18 forward onto main, and reconcile the board with the code

**Status (2026-09-24): resolved into tickets.** Assessed 2026-09-10, re-verified 2026-09-17. On
2026-09-24 every remaining part of both PRs became a ticket, the comments to Ben were posted on #15 and
#18, and the board was triaged. See "Where it landed" below. #18 closes once Ben has seen its comment;
#15 closes when the port (#40) merges. The analysis below is kept as the record of why.

## Where it landed (2026-09-24)

| From | Now |
|---|---|
| PR #18 Part 1, native slopes | #35 (`slope_deg_median`, co-authored), #36 (ATL06 `fit_statistics` chunk-layout spike), #19 (ATL06 slopes + ribbons), #38 (ICESSN σ) |
| PR #18 Part 2, six beams | already on main; the yaw-flip "unknown" label is #28 |
| PR #18 Part 3, ITRF | #8 (frame generalisation, per-row `itrf_year`, loud failure, plate-motion constants pinned to PROJ) |
| PR #15, GPS traverse | #40 (port onto the index contract, Ben as co-author), after #30, #34 and #8's frame work |
| #14 (Ben) | epic; children #27 (time-series plane conditioning) and #37 (coreg across-track offset) |
| The estimator finding below | #27 |
| Registration surface below | #34; the `hasPlan` and `logbuf` gaps were fixed in #29 |
| Board reconciliation below | done: #1–#5, #22, #24, #25 closed; #6, #7, #11 closed with reasons; #9, #10, #12, #13, #19, #20, #23 rewritten |

Baseline: `uv run pytest` → **547 passed, 1 skipped**. Run it with the sandbox disabled; the
sandbox blocks server binds and produces 9 spurious failures + 4 errors.

## Why

Ben Smith opened two PRs on 2026-08-27 and 2026-08-28: #15 (the IS2TGPSSS Summit GPS traverse as a
ground-truth collection) and #18 (reading what the products actually provide: native slopes, all six
ATL06 beams, ATM's ITRF frame). Both were written and tested against `7e60ff0`. Main has since moved
**215 commits** and changed shape in ways neither PR could have anticipated: the transport redesign
(`a0c8a8b`) deleted the pull path, the sub-granule H3 index became a **precondition** rather than an
optimisation, and GEDI landed as a 5th collection (`58a28dc`). The conflicts come from those changes
on main, so the question for each PR is how to carry its work onto main's current shape.

Separately, the board has drifted from the code in both directions — items shipped but still in
Backlog, and cards whose premise no longer matches the source.

---

## PR #18 — "Ingest what the products actually provide"

Three separable parts. Conflicts with main: `atl06.py` (4 hunks), `icessn.py` (3),
`test_new_missions.py` (1), `coreg.py` (1, a bare `import re`).

**Decided: carry the parts still needed forward as small PRs against main, each crediting Ben as
co-author, and close #18 with a link to where each part landed.**

### Part 2 — all six ATL06 beams: already on main

Main reached the same result by a different route while the PR was open. `src/aicesat/atl06.py` is
now 66 lines; `_strong_beams` and `_extract_granule` are gone, replaced by
`index_atl06.fetch_bbox(..., strong_only=False)` (`atl06.py:53-55`). The index carries a per-row
`strong` flag and a beam **name**, so the PR's `GT_BEAMS` re-indexing and `beam_strong` encoding have
nothing to attach to — `extract` returns only `lon/lat/h/t`. No port needed. The PR's Summit
measurement — weak beams over dry snow return slightly *more* good segments than their strong
partners (15,445 vs 15,382) — is the evidence that all-six is the right default.

One piece of the PR is still ahead of main here. PR #18 handled the yaw-flip case (`sc_orient == 2`)
explicitly: it read those granules and marked the beam label **unknown** (`beam_strong = -1`). Main's
index also reads them, but `index.strong_beams(2)` returns an empty set (`index.py:127-128`), so every
beam is labelled `strong=False` rather than unknown. No data is dropped, but the label is wrong. The
PR's tri-state label is the model for the fix. File as an issue.

### Part 1 — native surface slope: half on main, the other half still needed

Main now reads ICESSN `sn_slope`/`we_slope` — the platelet-rendering work (`b39be1f`) arrived at them
independently, for rendering. They flow `index_icessn.py:64-65,226,266,298,321` → `icessn.py:49-50` →
`scene.py:136-142` → the tilted-facet renderer.

The PR's argument holds in full for analysis: **nothing in the analysis path reads them yet.**
`timeseries._load_all` (`timeseries.py:62`) and `coreg.coregister_scene` (`coreg.py:322-324`) take
only `lon/lat/h/t`. `coreg.comparability_block` still reports
`"surface_slope_method": "least-squares plane over all ICESat-2 photons in the bbox"`
(`coreg.py:263`), and `geom.py` has no slope reduction. The PR's `geom.slope_deg_median` is the
missing piece, and it is cheaper to land now than when written, since the ICESSN slopes are already
in hand.

The ATL06 half needs more on main than it did against the PR's base: `ATL06_DATASETS`
(`index_atl06.py:34`) has no `fit_statistics/`. Adding `dh_fit_dx`/`dh_fit_dy`/`seg_azimuth` is an
index schema change **plus a full re-index of 2333+ granules**, and the builder hard-raises on
`"chunking differs from latitude"` (`index_atl06.py:123`) — unverified for the `fit_statistics`
sub-group. That makes it a natural fit for issue #19; see "Convergence" below. The PR's live Summit
check (20,000/20,000 segments quality-good with finite `dh_fit_dx/dy`) is the reference result a port
should reproduce.

### Part 3 — ITRF realization from the ATM header: needed, and it addresses a silent fallback on main

The `coreg._frame_pipeline` change applies clean: main's version (`coreg.py:61-75`) is byte-identical
to the PR's base. The `icessn.py` half needs porting, because main's ICESSN path is now index-based
(see "Fetch-time vs index-time fields" below). `icessn.py:53` still emits
`native_frame: "ITRF (campaign-dependent)"`.

Verified behaviour on main:

```
'ITRF (campaign-dependent)' -> ValueError: unsupported native frame
'ITRF2005'                  -> ValueError: unsupported native frame   <- PR #18's pipeline accepts this
'ITRF2008'                  -> OK
```

`timeseries.py:68-72` calls `propagate(..., native)` inside a broad
`except Exception: log.warning(...)` → **"using raw positions"**. So on main every IceBridge point in
an elevation time series skips co-registration while GLAS and ICESat-2 get propagated, and says so
only in a log line. PR #18 supplies the input half of the fix: an exact realization per granule, a
per-row `itrf_year`, and a pipeline that accepts any `ITRFyyyy` PROJ knows.

Main still needs its own half. `propagate` takes a single frame string per mission, and a multi-year
ICESSN series spans several realizations (ITRF05 for 2011, ITRF08 for 2012–2016, ITRF14 from 2017,
per the PR's header table). The PR deliberately labels such extracts `"ITRF (mixed: …)"` so they fail
loudly rather than transform with the wrong realization — the right call for a change scoped to
ingest, and the PR says it stops there. Completing the fix therefore means propagating per row,
grouped by `itrf_year`, and making the `timeseries` fallback visible. Without both, a multi-campaign
time series still falls back to raw positions.

Calibration, measured not assumed: for a 2011→2020 propagation at Summit this is **18.2 cm
horizontal, ~2 mm vertical**. On 0.3° terrain the Δh consequence is sub-millimetre. A
correctness-and-provenance fix, not a numbers fix — which matches the PR's own statement ("No Δh
number changes in this PR").

Maps to **issue #8** (validate ITRF epoch / plate motion transforms), currently in Backlog.

---

## PR #15 — IS2TGPSSS Summit GPS traverse — OPEN

`gpstruth.py` (290 L) and `tests/test_gpstruth.py` merge clean. The other 9 files conflict, each
where main has changed underneath them.

**The domain work is the core of the PR and cannot be rebuilt from main:** the era-aware ARP
constant (1.785 vs 1.797, catching a 12 mm systematic in exactly the sparse early record ≈ 0.6 mm/yr
of spurious trend over 19 years), three metadata key forms taking the match rate 258/271 → 271/271,
the two ARP-to-snow-surface surveys that would double-count sinkage, `invalid_raise=False` for three
truncated final rows, and the median-sinkage default with a per-row `track_depth_known` flag. This is
careful work against a product whose user guide understates what the files need, and nothing main
has done since affects any of it.

**What changed underneath the integration.** The PR followed main's collection conventions as they
were when it was written; three of those conventions have since moved:

1. **The collection contract.** Every collection is now
   `extract(bbox, window, polygon=None, on_granule=None, on_plan=None)`, index-only. The PR modelled
   itself on `icessn.py`, which at the time used `coverage.search()` + `earthaccess.download()`;
   main has since removed that path, and two guard tests enforce the removal:
   `tests/test_index_only.py:53` and `tests/test_no_caps.py:91`. Porting means writing an
   `index_gpstruth` alongside the unchanged parser.
2. **Four new registration points**, added after the PR: `timeseries.MISSION_LABEL`,
   `coverage._index_for`, `coverage.FOOTPRINTS`, `ui/explore.js` `flagOf`.
   `test_collection_registration.py` parametrizes over `collections()`, so these need GPSTRUTH
   entries or 4 tests fail.
3. **Explore.** The PR notes that Explore needed no change because its checkboxes were driven off
   `coverage.collections()`. That was accurate then; main has since added its own hardcoded map at
   `explore.js:156`, so that is one more place to register.

`summit_traverse` (regions.py) stands on its own and can land independently — `summit` survives in
`REGIONS`, and the PR's argument for the tighter bounds holds (~15% of epochs are snowmobile transit,
invisible to quality filtering at `SDHGT_95` 0.077 vs 0.078 m).

---

## Board reconciliation

| Item | Board says | Code says | Evidence |
|---|---|---|---|
| #22 coverage check slow | Backlog | **DONE** | `coverage.py:195-275` `_coverage/manifest.parquet`; 43.5 s → 0.35 s over 33,064 files |
| #24 remove caps/subsamples | Backlog | **DONE** | zero functional hits for `max_granules`/`stride`/`linspace(` in `src/`; `8028ca6` |
| #25 positions to binary sidecar | Backlog | **DONE** | `cache.scene_array_*` `.f32`; `d5c7dba` |
| #5 deploy MCP server on AWS | Ready | **DONE** | deployed us-west-2; see `docs/tasks/aws-us-west-2-deploy.md` |
| #1 #2 #3 #4 | In review | shipped long ago (vertical shift, ellipsoid, ICESSN, ATL06) | need triage, not review |
| #19 ATL06 ribbons | Backlog | NOT STARTED | `scene.js:98` gates facets to ICESSN only; no `dh_fit*` in `src/` |
| #20 GLAS footprint discs | Backlog | NOT STARTED | billboard `ScatterplotLayer` `scene.js:167-173`; no footprint geometry in `GLAS_DATASETS` |
| #21 ATL03 confidence | Backlog | **PARTIAL** | points already the right primitive; `conf` extracted `atl03.py:70` then dropped before the scene |
| #23 lake compaction | Backlog | **PARTIAL** | `scripts/bench_lake_layout.py` + `bench_lake_params.py` exist with results; no compaction in `lake.py` (only row-group `relayout`) |

Ready-column card *"ICESSN: surface slope + use RMS as per-point σ (retire slope-blind estimator)"*
is partly out of date: its premise ("columns we parse but discard") no longer holds **for slope**
(slope ships) and **still holds for RMS**. "Slope-blind" is also the wrong diagnosis — see below. The
card cites `docs/notes/laser_intermission_findings.md` §B, which is still the right reference for
the σ half.

---

## Finding not on the board: the estimator's cross-track term is unconstrained

`timeseries._fit_cell` (`timeseries.py:160-219`) fits its reference plane by **unweighted OLS to
the reference mission's points only** (`timeseries.py:170-172`), gated at `_MIN_REF_PTS = 6`, in
res-9 cells (**201 m edge, 0.105 km²**). GLAS shots are ~172 m apart along-track, so six reference
points means roughly three near-repeat passes — **near-parallel by design**. The across-track
coefficient is fit from data that barely constrains it, and `slope_deg` (`timeseries.py:216`) is
reported anyway with **no conditioning check**.

Simulated sweep of across-track spread at GLAS-like 15 cm height noise, n = 6:

| across-track spread | σ(slope) | as vertical across a 200 m cell |
|---|---|---|
| 5 m | 1.30° | 452 cm |
| 15 m | 0.40° | 139 cm |
| 30 m | 0.20° | 71 cm |
| 60 m | 0.10° | 35 cm |
| 120 m | 0.05° | 18 cm |
| 200 m | 0.03° | 11 cm |

This is the failure Ben described in **issue #14**, present in the shipped estimator — confirmation
that the issue identified a real problem rather than a hypothetical one. It also refines the board
card above: the estimator is *not* slope-blind (it removes a plane first); the real failure is that
the plane's cross-track term is unconstrained and unflagged.

The table shows **sensitivity, not incidence**: it does not say how many real candidate cells have
across-track spread this small. Measure that on the store before deciding how far up the queue this
goes.

Fix shape: compute the design matrix's conditioning (or the across-track spread directly), and
either refuse the cross-track component or mark it unconstrained in the output — which is precisely
what issue #14 asks for ("an unconstrained component should be stated as unconstrained rather than
silently absorbed").

Related dead code: `plane_rms` is computed at `timeseries.py:187` and never returned.

---

## Convergence: one ingest change unlocks four items

Adding `dh_fit_dx` (+ bearing) to `ATL06_DATASETS` serves:

- **#19** ATL06 tilted ribbons — the issue body already specifies exactly this work.
- **#14** slope-component constraint gate — ATL06's `dh_fit_dy` *is* the beam-pair-derived
  across-track component, as Ben established in PR #18 and noted on #14.
- **Spec build-note 10** (`docs/cross-mission-altimetry-mcp-spec.md:292`) — "estimate the surface
  height at the footprint centre with a local along-track linear fit… **ATL06 already ships this
  quantity**."
- **Spec §276** — "Slope from IceBridge native where available" — the gap PR #18 part 1 closes.

**Prerequisite spike (needs EDL + network; cannot be done offline):** confirm
`land_ice_segments/fit_statistics/dh_fit_dx` has the same chunk layout as `latitude`, or
`index_atl06.py:123` will raise for every granule. No local ATL06 HDF5 granule exists to check
against. **Do this first; it gates the whole workstream.**

---

## Fetch-time vs index-time fields (do not re-derive this)

For ICESSN these two look similar and are not:

- **`rms_cm` → available at FETCH time, no re-index.** `_parse_span_points`
  (`index_icessn.py:203-226`) re-parses the raw lines and already reads `rms = float(f[6])`
  (`index_icessn.py:74`), using it only as a drop test (`index_icessn.py:217`). Plumbing it out is
  the same one-line pattern as `sn_slope`/`we_slope`: add to the return dict, to `_DIRECT` (`:170`),
  to the lake `extras` (`:266`, `:298`), and to `icessn.py:49-50`. Cached cells degrade to NaN
  exactly as the slopes already do.
- **`itrf_year` → header-only, so it NEEDS an index column and a rebuild.** PR #18 read the header
  from the downloaded file; main's byte-range fetch reads line spans and never sees the `#` header.
  But `build_icessn_index` (`index_icessn.py:96`) does `RangeReader().read_all(...)` — a
  **whole-file** read at build time — so the header is right there, and the PR's `_itrf_year` parser
  (case-insensitive, two-digit years) can run on it unchanged. Cost: an `ICESSN_INDEX_VERSION` bump
  ("2" → "3") + rebuild.

**ICESSN is currently fully evicted** — `data/index/icessn/` and `data/lake/mission=ICESSN/` are
both empty (Nepal-only store since 2026-09-09; ICESSN never flew Nepal, and the footprint gate
refuses it). 418 granules were indexed before eviction. A rebuild has to happen anyway for *any*
ICESSN work, so **`itrf_year` rides along free** — the argument for doing the ITRF work now rather
than later.

> ⚠️ `index_status`'s readdir count is blind to stale schemas (it once reported "100% built" over
> 57/844). Verify a rebuild by row count and schema metadata key, not by file count.

---

## Foundation problem: the registration surface is ~20 points, the test covers 9

`tests/test_collection_registration.py` checks 9. The real surface, traced from GEDI's `58a28dc`,
also includes:

`api.py` — `LEGS` (`:363`), `_ex_*`/`_int_*` (`:300-304`, `:340-344`), `_COLL_FOR_LEG` (`:370`),
error-ordering tuple (`:572`), canonical series order (`:577` — a missing key **silently drops the
series from the doc**), `_index_source` (`:706-722`, a *second* index-dir switch separate from
`coverage._index_for`), `_enforce_lake_limit` (`:235`). `server.py:519-525` `ui_extract` signature.
`ui/scene.js:91` `FOOTPRINT_M`. `ui/tspanel.js:13-22` (three maps). `ui/explore.js:157` `hasPlan`.
`widget/dist/aicesat.html` (committed build artifact — regenerate via `scripts/build_ui.py`).
`tests/test_index_only.py:48,100`. `scripts/reset_data.py:27-33`, `check_index.py:34-43`,
`why_not_covered.py:87`, `diag_collections.py:25,35,37,85`.

**GEDI — the newest collection, added on main — is itself missing from 8 of these:**
`explore.js:157` `hasPlan` (so a GEDI-only build falls back to log-string sniffing),
`api._enforce_lake_limit`, `test_index_only.py:100` parametrize, and all four scripts. A test that
covers the full surface would turn adding any collection — including a port of PR #15 — into
following the test's failures rather than tracing the codebase by hand. That makes it worth doing
before the next collection lands, whoever does the port.

---

## Decisions (resolved 2026-09-24 unless marked open)

1. **PR #15:** ported in-house now (#40), with Ben as co-author on every commit.
2. **Priority:** the #15 port's critical path first (#30 → #34 → #8's frame work → #40), then science
   correctness (#8, #37, #27, #39, #35, #38, #28, #36).
3. **ICESSN rebuild scope:** *still open.* It gates #8's per-row `itrf_year` column (part 2); the rest
   of #8 is offline.
4. **The timeseries conditioning defect:** filed as #27, in Ready. The fix comes first; measuring how
   often it happens on real cells needs a Greenland index and is a follow-up.

---

## Verification (applies to whatever is chosen)

- `uv run pytest` with the sandbox disabled. Baseline to beat: 547 passed, 1 skipped.
- For the ITRF work: a regression test asserting ICESSN's `native_frame` actually survives
  `coreg.propagate` — the current silent fallback is exactly the failure mode that needs a test,
  not just a fix. Assert a **nonzero** displacement; the "silent-identity trap" guard at
  `coreg.py:326-327` is the existing precedent. Include a multi-campaign extract, since that is
  where the fallback would otherwise persist.
- For any index schema change: verify by row count and schema metadata version key, not by
  `index_status`.
- For a new or changed collection: extend `test_collection_registration.py` to the full surface
  first, then let it tell you what is missing — including for GEDI.
- End-to-end: `build_scene` over a region with the affected collection, then the time-series panel,
  since that is where the ICESSN propagation failure actually surfaces.
