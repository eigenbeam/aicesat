# Mining `laser_intermission` — findings & actions for aicesat

**Source:** `../laser_intermission/` — Ben Smith's (SmithB; ICESat-2 ATL06 algorithm lead,
author of `pointCollection`) project. Goal: multi-sensor elevation **time series over Greenland**
fusing the same three missions we use — ICESat-1/GLAS (GLAH06), IceBridge ATM (ICESSN/ILATM2),
ICESat-2/ATL06 — to find sites with decadal variability.
**Maturity caveat:** only the discovery/curation phases are *implemented*; the richest fusion &
inter-sensor-bias phases exist as **prose spec only**. Treat spec-only numbers as leads to validate,
not settled constants.

Captured 2026-08-27 from a mining subagent. Items tagged **[VALIDATE w/ Ben]** rest on
spec prose or thresholds we haven't reproduced against a real granule.

---

## Architectural validation (no code change, but shapes future work)

**The fusion graph is ICESat-1 → ATM → ICESat-2, never direct.** ICESat-1 and ICESat-2 reference
tracks barely overlap, so direct ICESat-1↔ICESat-2 crossovers are sparse. **ATM/IceBridge is the
hub** that bridges the two eras. This backs our decision to carry IceBridge as a first-class mission,
and means any *multi-mission* Δh (beyond a single pair) should tie through ATM rather than expecting
dense ICESat-1↔ICESat-2 pairs. → Card F.

**Divergences we should NOT copy:** he uses SlideRule for ATL06 (we use byte-range ATL03 photons);
he does **no ITRF/epoch or GIA** correction (fits a *local* reference surface within <2 km instead —
a genuinely different method from our plate-motion + ICE-6G_C machinery); no DEM draping; auth via
netrc not our bearer token. None of these conflict with our verified facts.

---

## Findings & actions

### B — Adopt ICESSN slope + RMS (per-platelet σ and surface slope)  [obviously actionable]
Each ICESSN/ILATM2 platelet already carries **along-track slope (SN_slope), across-track slope
(WE_slope), and a plane-fit RMS residual (RMS, in cm)** — columns we already parse in `icessn.py`
(`seconds, lat, lon, elev, SN_slope, WE_slope, RMS(cm), npt_used, npt_edit, distance, track`).
- Ben uses **σ = RMS × 0.01** (cm→m) as the per-measurement uncertainty for inverse-variance weighting.
- He carries the slope fields through the fit.
- **Why it matters to us:** directly retires our "median estimator is slope-blind" concern
  (see memory `aicesat-region-slope-concern`): ICESSN hands us slope per platelet for free, and RMS
  gives a real per-point σ instead of an unweighted median. Today `icessn.extract` reads RMS only to
  *filter* (`rms_cm < MAX_RMS_CM`) and drops slope entirely.
- **Action:** surface `SN_slope`/`WE_slope` and expose `sigma = rms_cm*0.01` from `icessn.extract`;
  use σ for inverse-variance weighting in the per-cell estimator; optionally use slope to de-bias the
  median across a sloped cell. Verify field order/units against a real ILATM2 granule first.

### A — Repair ATM/ICESSN CMR footprints before trusting them  [VALIDATE w/ Ben]
CMR GPolygon footprints for ATM products massively **over-claim spatial extent** — Ben measured
over-claims up to **~990×** true swath area; naively indexing them yields **~16% phantom H3 cells**
(cells the aircraft never overflew).
- **Discriminant:** implied swath width = polygon_area / along-track_span; ATM swath is only a few
  hundred metres, so an implied width **> ~2 km** flags a bogus footprint.
- **Repair:** buffer the actual measurement points **~400 m** perpendicular to the flight leg,
  splitting the track wherever consecutive points jump **> 3 km** (gaps between survey lines).
- **Our exposure:** our final ATM *data* is point-filtered to the bbox so scenes are correct, but any
  coverage count/map derived from CMR polygons is inflated. We currently bin actual points into H3, so
  we largely dodge the phantom-cell problem in *scenes* — but `coverage.py` granule counts still trust CMR.
- **Action:** if/where we ever index ATM footprints from CMR polygons, add the area/span credibility
  check + point-buffer repair. **[VALIDATE w/ Ben]** the 400 m / 3 km / 2 km thresholds.

### C — CMR spatial coincidence over-reports GLAS (and ATM) coverage  [obviously actionable + validate]
GLAH06 (ICESat-1/GLAS) has **no true CMR footprint** — CMR returns orbit-backtracked bounding boxes.
Ben found only **13/30** CMR-"coincident" GLAS granules were actually within 2 km of the target.
- **Our exposure:** Explore → "Check coverage" GLAS granule count is an **upper bound**. A user can see
  "N granules", build a scene, and get far fewer/no actual in-bbox GLAS points. `glas.extract`
  point-filters so the *scene* is honest; the *coverage promise* is not.
- **Action:** (1) UI — label GLAS coverage as an upper bound ("granules whose orbit may cross this
  area"), or run a cheap in-bbox point check and report actual counts; (2) longer term, verify GLAS/ATM
  proximity from real ground tracks, not CMR bbox, before reporting counts. **[VALIDATE w/ Ben]** the
  ground-track proximity method.

### D — Guard ICESSN 2-digit-year parsing (pointCollection +2000 bug)  [obviously actionable]
`pointCollection` hardcodes **+2000** on 2-digit ATM years, so `BLATM2_93…` (1993) silently becomes
**2093** and the pre-2000 baseline vanishes. Correct rule: **yy + 1900 if yy ≥ 90 else + 2000**.
- **Our status:** SAFE — `icessn._parse_file` dates via the full 8-digit `%Y%m%d` from the filename,
  not a 2-digit year. But the failure mode is silent and catastrophic for a decadal baseline.
- **Action:** add a regression test asserting a pre-2000 ATM filename dates to 199x (not 209x); if we
  ever adopt a `pointCollection` path or a 2-digit parser, apply the yy≥90 rule.

### E — earthaccess `version=` silent-zero trap for ATM/GLAS  [possibly actionable]
Ben reports passing `version=` to earthaccess for BLATM2/ILATM2/GLAH06 can **silently return 0**
granules.
- **Our status:** we DO pass `version=` and it works (verified 2026: ILATM2 v2, GLAH06 034, ATL06 007
  all return granules), so the trap is version/library-specific — but it's a silent-zero failure mode.
- **Action:** add a smoke test asserting `coverage.search` returns >0 for a known-good ATM/GLAS
  bbox+time window, so a future earthaccess upgrade that reintroduces the trap fails loudly.

### F — Inter-sensor bias at an epoch boundary is the top Δh risk  [reinforces existing work; validate approach]
Ben's spec flags the single biggest error in any decadal cross-mission Δh as an **inter-sensor bias
that lands on an epoch boundary** (e.g. the GLAS G–C inter-campaign bias, or an ATM↔ICESat-2 offset) —
it is indistinguishable from real elevation change.
- **Our exposure:** reinforces our still-open **GLAS inter-campaign (G–C) bias** correction card. Also:
  because ICESat-1↔ICESat-2 overlap is thin, the reliable tie path is ICESat-1 → ATM → ICESat-2.
- **Action:** keep/raise priority on the G–C bias correction; apply it before reporting Δh; surface a
  UI caveat when a Δh spans a known bias boundary; consider ATM as the inter-mission tie.
  **[VALIDATE w/ Ben]** which biases he treats as first-order and his tie method.

---

## Impact summary (UI / MCP tools / interaction)
- **UI:** C and F imply user-visible caveats — a GLAS coverage "upper bound" note (Explore, step 2),
  and a Δh "spans a bias boundary" caveat (Scene). B (slope/RMS) could add a per-cell σ readout.
- **MCP server tools:** B changes `icessn.extract`'s output (add slope + σ) and the per-cell estimator;
  C/A touch `coverage.py`; D/E add tests. No tool *signatures* need change for B/C if we keep additions
  backward-compatible (extra arrays/fields).
- **Interaction:** C and F change what we *promise* the user — honest coverage counts and honest Δh
  caveats — which is squarely in scope for the current "unclear meaning & feedback" UX work.
