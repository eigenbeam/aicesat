# Cross-Mission Altimetry MCP Server — Design Spec

**Status:** draft / hackweek scope — **Appendix D (Slices 1–3) built and verified on real data 2026-08-25; the §5–§6 index + byte-range access built for ATL03 and the Appendix C comparison measured (C.5b)**; build notes are marked *Build note (2026-08-25)* throughout, measured results in B.10 and C.5b.
**One-line:** An MCP server that answers cross-mission elevation questions over GLAS, IceBridge, and ICESat-2 — co-registering samples to a common ITRF frame and epoch so plate motion doesn't misalign footprints, with per-sample provenance — by fetching only the data chunks a question needs, accumulating them into a local persistent Parquet lake, and computing answers locally with DuckDB.

---

## 0. Scope and non-goals

### In scope (hackweek)
- Local, single-user MCP server exposing a small set of **question-shaped** tools.
- A **persistent, growing Parquet lake** on local disk, accumulated as questions are asked.
- A **temporal-geospatial chunk index** that records which source chunks have been ingested (and, as a stretch, which regions are covered) so fetches can eventually be skipped.
- **Compute layer** over the lake in DuckDB: coverage overlap, cross-mission co-location, elevation deltas between epochs, comparability flags.
- **ITRF/epoch co-registration** materialized post-ingest to a fixed common epoch, with native coordinates preserved.

### Explicit non-goals
- Not a data-return API. Tools return **computed, structured answers with provenance and caveats**, not raw sample dumps or prose narration. The agent phrases; the server computes.
- Not a vertical-datum reconciler. ITRF co-registration fixes the **horizontal** terrestrial frame + epoch. Geoid, tides, and GIA (vertical bedrock motion) are **out of scope** and must be surfaced as unresolved in every comparability output. See §7.
- Not an ice-dynamics tool. Co-registration handles **plate motion**, not **ice flow**. Cross-epoch comparisons over flowing ice are flagged, not corrected. See §7.
- Not multi-user / not a service. Local process, local disk, one user.
- Not the fetch mechanism itself. Per-mission chunk retrieval from S3 is behind a `SourceAdapter` interface and is **deferred** (see §6, §11).

---

## 1. Architecture overview

```
                    ┌─────────────────────────────────────────────┐
   agent question   │                 MCP Server                  │
  ───────────────▶  │   (question-shaped tools, §3)               │
                    │                                             │
                    │   ┌──────────────┐     ┌──────────────────┐ │
                    │   │ Query planner│────▶│  Chunk Index     │ │
                    │   │  (§4)        │     │  (§5)            │ │
                    │   └──────┬───────┘     └────────┬─────────┘ │
                    │          │  coverage gap?        │          │
                    │          ▼                       ▼          │
                    │   ┌──────────────┐     ┌──────────────────┐ │
                    │   │ Compute layer│     │ Ingest pipeline  │ │
                    │   │ DuckDB (§8)  │     │  (§6)            │ │
                    │   └──────┬───────┘     └────────┬─────────┘ │
                    │          │                       │          │
                    │          ▼                       ▼          │
                    │   ┌─────────────────────────────────────┐  │
                    │   │   Persistent Parquet Lake (§7)      │  │
                    │   └─────────────────────────────────────┘  │
                    └───────────────────┬─────────────────────────┘
                                        │ SourceAdapter.fetch_chunk() (§6, deferred)
                                        ▼
                              data files on S3 / other source
```

**Request lifecycle:**
1. Agent calls a question-shaped tool with a bbox + time range (+ tool-specific args).
2. Query planner resolves the request to a set of **chunk keys** (mission × spatial cell × time window) via the chunk index.
3. For any chunk key not marked present, the ingest pipeline fetches it (via `SourceAdapter`), normalizes it to the common schema, co-registers it, upserts it into the lake, and marks the chunk present in the index.
4. Compute layer runs the tool's DuckDB query over the now-sufficient lake.
5. Tool returns a structured result with provenance + comparability caveats.

---

## 2. Design decisions locked in (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Server returns | Computed answers, not data | The reconciliation *is* the product; leaking it to the agent yields a naive wrapper (§0). |
| Lake persistence | Persistent, growing | User intent. Cost is the coverage index + dedup, not storage (§5, §7). |
| Fetch strategy (hackweek) | **Dumb-but-correct**: always fetch requested region, upsert, query | Correct regardless of index quality; coverage-index-driven skipping is a stretch (§5.4). |
| Row identity | Per-mission natural tuple → uniform **surrogate hash** | Two missions have great keys, IceBridge has none; hash unifies upsert logic (§7.3). |
| Spatial cell | H3 at fixed resolution (TBD to match source chunk size) | Integer set ops for coverage; natural partition key. **Cell = coverage unit, NOT row identity** (§5.2). |
| ITRF transform timing | Post-ingest **materialization** to fixed common epoch; native coords retained | Avoids per-query pyproj cost; avoids freezing epoch into stored raw data; re-epoching is a re-materialize, not a migration (§7.4). |
| Vertical datum | Out of scope, surfaced as caveat | Honest scoping; conflating frame with datum is the credibility-killer (§0, §7). |

---

## 3. MCP tool surface (question-shaped)

Tools return structured JSON. None return raw sample arrays as the primary payload; a sample-level export tool exists but is explicitly a debug/escape hatch.

### 3.1 `coverage_overlap`
> "Which missions observed this box in this time range, and how much?"

**Input:** `bbox` (lon/lat), `time_range` (start/end, nullable = all), `missions` (optional filter).
**Output:** per mission: list of covering campaigns/cycles with date ranges, spatial coverage fraction within bbox (optimistic — "cell touched"; see §5.3), sample count, native frame/epoch. Explicit `triple_overlap: bool` and `pairwise_overlaps: [...]`.
**Note:** GLAS coverage is intermittent (campaign-based); IceBridge is sparse flight lines. Coverage is reported at **campaign granularity**, never assumed continuous.

### 3.2 `elevation_change_between_epochs`
> "How did surface elevation change here between mission A (epoch t1) and mission B (epoch t2)?"

**Input:** `bbox`, `mission_a` + `time_window_a`, `mission_b` + `time_window_b`, `colocation_radius` (default TBD).
**Output:** co-located sample pairs aggregated to a delta statistic (median dh, spread, n_pairs), the co-location radius used, **and** a `comparability` block (§7.5): surface slope estimate, plate-motion correction applied, and the unresolved list (geoid/tide/GIA, ice-flow flag). Returns *nulls with reasons* when overlap is empty.

### 3.3 `is_comparable_here`
> "If I compared these two missions here, what would corrupt the result?"

**Input:** `bbox`, `mission_a`, `mission_b`, optional `time_windows`.
**Output:** a diagnostic, not a number: coverage coincidence yes/no, estimated surface slope (→ horizontal-misregistration-to-vertical sensitivity), whether the region is likely dynamic ice (ice-flow flag), which corrections are and aren't applied. This tool exists to make the caveats first-class rather than buried in §3.2's output.

### 3.4 `cross_mission_profile`
> "Give me co-located elevations along this transect across whatever missions saw it."

**Input:** `bbox` or `linestring`, `time_range`, `missions`.
**Output:** binned along-track profile with one series per mission, each carrying native frame/epoch and co-registration applied. For visualization by the agent.

### 3.5 `export_samples` (escape hatch, not the point)
Raw co-registered samples for a small bbox, with all provenance columns. Rate/size limited. Documented as debug/inspection, not the primary interface — keeps the "answers questions, doesn't return data" contract honest while allowing verification.

---

## 4. Query planner

Responsibilities:
1. **Resolve request → cells.** Given (bbox, time_range, missions), compute the set of `(mission, cell, time_bucket)` keys intersecting the request. Time bucketing is per-mission (§5.1).
2. **Consult the index** (§5) for each cell. The index serves two roles: (a) *coverage* — is this cell already materialized to GeoParquet? (b) *addressing* — for cells not yet materialized, which source byte-ranges must be read to build them?
3. **Trigger ingest** for cells missing from the warm GeoParquet tier, via the index's byte-range references (§6). Hackweek: fetch the requested region's cells; stretch: only genuinely-missing.
4. **Dispatch compute** (§8) once the warm tier covers the request.

The planner is the only component that knows about both the index and the ingest pipeline; compute and tools stay ignorant of fetch state.

---

## 5. Temporal-geospatial index (the addressing scheme)

This is the architectural centerpiece, and it is more than a coverage map. **The index imposes the spatial organization the source files lack.** Native ATL03 chunking is along-track and by-variable — geographically arbitrary — so a bbox's photons are scattered across many granules and across many chunks within each. Rather than accept the file's topology (the kerchunk/DMR++ approach, which makes *native* chunks addressable but does not re-organize space), we build a map from **the world** to **source byte-ranges**, and treat granules as mere backing storage. Query a cell → get the byte-ranges across whatever granules contribute to it → fetch and decode only those. For ATL03, **this index *is* the sub-granule and cross-granule spatial indexing scheme.**

### 5.1 The index has two roles
1. **Addressing** (the new, primary role): `(mission, cell, time_bucket) → [chunk_ref…]`, where each `chunk_ref = (url, byte_offset, length, dtype, shape, filter_pipeline, within_chunk_predicate)`. This is what lets the read path (§6) fetch source bytes for a cell without opening or re-parsing the file.
2. **Coverage** (the original role, retained): is this cell already materialized to the warm GeoParquet tier (§7), so we can skip re-reading source entirely?

### 5.2 Grain
A **cell key** = `(mission, spatial_cell, time_bucket)`.
- `spatial_cell` — **H3 at fixed resolution R for v1** (see §5.6 on H3 vs space-filling curves). R chosen so a cell is a practical addressing unit relative to native chunk size (empirical, may differ per mission).
- `time_bucket` is mission-specific: GLAS → laser campaign ID (Laser 1, 2A, …), intermittent, never uniform-calendar; ICESat-2 → cycle (or RGT×cycle), or month as a coarse default; IceBridge → campaign (season+year+pole).

### 5.3 The chunk-vs-cell granularity fact (and why addressing is "ranges + predicate", not "exact bytes")
A byte-range is independently fetchable only at the source's **chunk** boundary (for HDF5, the atom of the b-tree; if the chunk is compressed you must fetch the whole compressed chunk to decode any of it). When a spatial cell is finer than a chunk's footprint, the chunk holding the cell's photons also holds neighbors'. Therefore:
- The index maps cell → *chunks that contain the cell's data*, **plus a `within_chunk_predicate`** (the spatial/temporal filter to apply after decode). "The index gives exact bytes" would overclaim; it gives the minimal set of chunks plus the residual filter. The residual filter is **not optional** — omitting it silently returns neighbors' photons.
- The **same chunk appears under many cells** (chunks straddle cell boundaries). This is the identical structural fact as row-identity dedup (§5.4 / §7.3): overlap is the rule, not the exception.

### 5.4 Cell addressing vs row identity — DO NOT CONFLATE
- **Cell key (this section):** "which source bytes hold this *region*, and is the region materialized?" — governs fetching and skipping. Coarse.
- **Row identity (§7.3):** "have I already stored this *measurement*?" — governs dedup. Fine, per shot/photon/footprint.

A cell holds thousands of measurements; chunks don't align to cells. Cell-present does not imply any specific row is present. **Idempotency comes from the row surrogate key on upsert (§7.3), never from the cell key.** Conflating them corrupts aggregations at cell boundaries in a way that mimics real spatial structure and won't be caught by eye.

### 5.5 Coverage is optimistic; fetch-skipping is a stretch
Because chunks (and IceBridge flight lines) don't align to cells, a cell marked *materialized* from one chunk may not be spatially complete. Hackweek: treat coverage as boolean **and always pair with the always-fetch fallback** so optimism never causes a silent hole; the addressing role is still exercised (it's how the fetch happens), only the *skip* decision is deferred. Stretch: store coverage fraction / the set of contributing chunk-refs per cell and skip only when coverage ≥ threshold. Build order: always-fetch-correct first, skip-optimization second — the reverse risks a week with nothing answering a question.

### 5.6 Index key: H3 for v1; H3-vs-SFC as a gated, time-boxed benchmark
Two distinct jobs, which need not use the same scheme:
- **Discrete addressable cell** (coverage + the `(mission, cell, time_bucket)` key): **H3**. Discrete hierarchy, integer set ops, clean parent/child rollups. This is the v1 choice for both roles.
- **Row-group sort order inside GeoParquet** (locality — do spatially-adjacent records land in contiguous, skippable row groups?): candidate for a **space-filling curve (Hilbert or S2)**, which can group neighbors into contiguous ranges better than H3's hierarchy.

**Benchmark, scoped so it cannot become its own project.** Materialize the same cell(s) two ways — H3-sorted vs Hilbert-sorted row groups — run the Stage-1 query set, compare row-groups-skipped and wall-clock. An afternoon, not a research effort. **Default if the afternoon doesn't happen: H3 in both roles.** Honest limit: results are dataset- and query-shape-dependent (dense polar tracks vs. sparse mid-latitude; bbox vs. transect), so this decides *for the prototype's query mix*, not in general — revisit if the workload changes. Do not sink the week into curve theory.

### 5.7 Index storage
The index is itself GeoParquet (or a DuckDB table) so the query engine reads it with the same fast path as the data: keyed by `(mission, spatial_cell, time_bucket)`, with the `chunk_ref` list, a `materialized bool`, `ingested_at`, and `source_etag/version` (staleness — a source reprocess invalidates the addressing *and* any materialized GeoParquet built from it). The addressing rows are built once per granule at index-build time (§6.1); the coverage flags update as cells materialize.

---

## 6. Index-build and two-tier access

Two phases with different lifetimes: **index-build** runs once per granule (parses source structure into byte-range references); **access** runs per query (reads those references, materializes to warm GeoParquet). The performance thesis is that all file-structure parsing happens in index-build and is amortized — the query path never re-parses a file.

### 6.1 Index-build (once per granule)
For each source granule, populate the addressing index (§5.1):
- **HDF5 missions (ATL03, GLAS GLAH06):** read the chunk index — offset, length, dtype, shape, and filter pipeline per chunk — and the geolocation needed to assign chunks to spatial cells. Use whatever reads the b-tree fastest (see §6.3); `h5py`'s chunk-info API returns exactly this without reimplementing the b-tree walk. Bake `(url, offset, length, dtype, shape, filter_pipeline)` + cell assignment + `within_chunk_predicate` basis into the index.
- **IceBridge ILATM2 V2 (CSV):** no internal chunk index exists — byte-range addressing does not apply (§6.2). Index-build here is just recording the granule's spatial/temporal extent for coverage; the data is handled entirely by materialize-once.
- Pin **ILATM2 V2 specifically**; Pre-IceBridge (BLATM2) is a separate overlapping collection, **out of scope**.

### 6.2 Access — two tiers
**Tier 1 — reference read (ingest read path, NOT a query path).** Given a cell's `chunk_ref`s from the index: issue HTTP range reads for exactly those byte spans, decode (§6.3), apply the `within_chunk_predicate` to drop neighbors. This produces the cell's raw records. Tier 1 exists *to build Tier 2*; queries never hit it directly.

**Tier 2 — materialize-to-GeoParquet (the query path).** Normalize the Tier-1 records into the common schema (§7.1), mint row keys (§7.3), assign cells, upsert into **persisted, spatially-sorted GeoParquet** with row-group statistics (§5.6 sort order). **Queries always hit Tier 2.** First query for a cell pays Tier-1 read + materialization; subsequent queries hit warm GeoParquet. This is the persistent lake (§7), fed by the index rather than by granule dumps.

For CSV IceBridge there is no Tier 1: convert the whole granule to GeoParquet once (materialize-only), then it is byte-range/row-group addressable *as Parquet* — the same fast path as everything else, just reached by a one-time conversion instead of reference-in-place.

Rationale for custom GeoParquet over consuming DMR++/kerchunk references directly: a prototype found the custom index smaller and faster, because DMR++ describes the *native* HDF5 layout generically (all variables, along-track chunking) whereas our GeoParquet carries only needed columns, spatially sorted, with row-group stats tuned to our query shape.

### 6.3 Decode: fastest available primitive, no ideological constraint
The goal is **fastest read/decode at both index-build and query time — not dependency-free.** Libraries are welcome; the win is architectural, not from reimplementing HDF5.

- **The architectural win:** query-time decode works from the **pre-baked chunk manifest** (offset, length, dtype, shape, filter). No file-structure re-parsing happens per query, regardless of which library supplies the bytes or the codec. The b-tree parse is paid once, in index-build, and amortized across all future reads. This — not hand-rolling — is what makes it faster than opening the file per query.
- **Codecs from libraries, for free:** decompression calls the same optimized C codec (zlib / szip / shuffle via `numcodecs`/`imagecodecs`) that any HDF5 library would call — using a library for the codec costs nothing at runtime and buys correctness for the filter pipeline (shuffle, scale-offset, fill values) that hand-rolling would put at risk. Decode path: range-read bytes → library codec → `np.frombuffer(dtype)` → Arrow.
- **Index-build parsing from libraries too:** `h5py` chunk-info (or a faster binding if a benchmark shows it) yields the manifest; no need to reimplement the b-tree to obtain its output.
- **Hand-rolling is a profile-guided last resort,** applied only to a specific hot primitive if profiling shows a library call dominating — never a blanket constraint, and never for filter pipelines (correctness risk).

### 6.4 Silent-corruption ordering
Steps that can silently corrupt — the `within_chunk_predicate` (drop neighbors, §5.3), row-key mint + upsert (§7.3) — must be correct before anything else matters. Steps that only cost time (cell assignment, ITRF materialization §7.4) can be optimized later. Any hand-rolled decode primitive must be **validated byte-identical against the reference library on a sample** before it is trusted (§9).

---

## 7. Persistent Parquet lake

### 7.1 Common schema (one row per measurement, all missions)
| Column | Type | Notes |
|---|---|---|
| `row_id` | bytes/hash | **surrogate primary key**, §7.3 |
| `mission` | enum | GLAS / ICESAT2 / ICEBRIDGE |
| `native_lon`, `native_lat` | double | as delivered |
| `native_height` | double | ellipsoidal, as delivered |
| `height_ref` | enum | source vertical reference (e.g. WGS84 ellipsoid) — **not reconciled**, recorded |
| `native_frame` | enum | e.g. ITRF2014 (ICESat-2), **ITRF2008 (GLAS release 34; NSIDC lists EPSG:5332)**, campaign-specific (IceBridge) |
| `native_epoch` | date | observation date — drives co-registration |
| `t` | timestamp | observation time (UTC) |
| `coreg_lon`, `coreg_lat` | double | **materialized** ITRF@common-epoch, §7.4 (nullable until materialized) |
| `coreg_epoch` | date | the fixed common epoch used |
| `slope` | double | native if provided (IceBridge), else null / derived downstream |
| `quality` | struct/flags | mission-specific quality, normalized to a common pass/fail + native detail |
| `h3_cell` | uint64 | partition + index key |
| `source_chunk_id` | string | provenance: which fetched chunk this came from |
| `source_granule` | string | provenance: native file/granule |
| `native_shot_ref` | struct | provenance: the native identity tuple (§7.3) |

Partitioned by `mission` and `h3_cell` (Hive-style) for DuckDB predicate pushdown.

### 7.2 Provenance is per-row and non-optional
Every row carries: native frame, native epoch, source granule, source chunk, native identity tuple, and the co-registration applied. This is what makes tool outputs auditable and is the "with per-sample provenance" clause of the pitch. Provenance columns are never dropped.

### 7.3 Row identity — per-mission tuple → uniform surrogate
Missions differ in identity quality; unify by hashing the mission-appropriate tuple into a single `row_id` so upsert/dedup logic is identical everywhere.

| Mission | Product | Natural identity tuple | Quality |
|---|---|---|---|
| ICESat-2 | ATL03 | `(granule, beam, segment_id, photon_index)` | Excellent — spec-clean, hierarchical |
| GLAS | GLAH06 (40 Hz) | `(granule, i_rec_ndx, i_shot_count)` | Excellent — `i_rec_ndx`+`i_shot_count` documented as uniquely identifying a laser shot |
| IceBridge | ILATM2 V2 | `(source_file, gps_time, lat, lon)` | **Weak — natural key, no surrogate in product.** Fragile to reprocessing/coordinate jitter |

`row_id = hash(mission, native_identity_tuple)`. Upsert on `row_id`.

**IceBridge idempotency caveat (the one that bites):** its hash is only as stable as filename + coordinates. Reprocessing (V1→V2 already happened) or the same flight appearing in both ILATM2 and BLATM2 can present the "same" measurement with jittered coords/different filename → hash miss → duplicate → corrupted aggregates. Mitigations: pin ILATM2 V2 only; exclude Pre-IceBridge; record `source_etag/version` and treat a version bump as explicit re-ingest, not silent double-add. **Open item:** confirm whether ILATM2 V2's CSV header exposes any per-record sequence number that would harden this key (§11).

### 7.4 ITRF co-registration as post-ingest materialization
Runs inside the Tier-2 materialization step (§6.2): as decoded records are written to warm GeoParquet, `coreg_*` is computed and persisted alongside native coords. "Post-ingest" = after the Tier-1 read, not a separate later pass.
- **Store native coords always** (§7.1) — the epoch decision is never frozen into raw stored data.
- **Materialize** `coreg_lon/lat` at a **fixed common epoch** (choice TBD; a natural anchor is ICESat-2's frame/epoch since it's ITRF2014 and matches the day-job — or a neutral common frame if true symmetry is wanted). Materialization = compute once, persist, not per-query.
- Transform uses `pyproj` frame+epoch propagation. DuckDB can't do it in SQL → done in the Python materialization pass over newly-added rows, or via a UDF, but **persisted** either way.
- Re-epoching to a different common epoch = re-run the materialization pass, not a data migration. Native coords make this lossless.

**What co-registration does and does not do (state in every comparability output):**
- ✅ Handles **plate motion** — the rigid horizontal drift of the tectonic plate (1–2 cm/yr; 15–30 cm over the GLAS↔ICESat-2 span). This is the correction that clears the noise floor on any sloped surface.
- ❌ Does **not** handle **ice flow** — the ice on top of the plate moves independently, hundreds of m to km/yr on outlet glaciers. No coordinate transform recovers this.
- ❌ Does **not** handle **vertical land motion (GIA)** — mm/yr bedrock uplift/subsidence that aliases *directly* into elevation change.
- ❌ Does **not** unify the **vertical datum** — geoid/tide reconciliation is separate; ITRF fixes the terrestrial (horizontal) frame only.

### 7.5 Comparability block (attached to every cross-mission answer)
```
comparability: {
  coverage_coincides: bool,
  colocation_radius_m: float,
  surface_slope_deg: float | null,          # drives horizontal→vertical error sensitivity
  horizontal_to_vertical_sensitivity: str,  # e.g. "20cm horiz @ 5° slope → ~1.7cm vertical"
  plate_motion_corrected: true,
  unresolved: ["ice_flow?", "GIA", "geoid/tide", "vertical_datum"],
  dynamic_ice_flag: bool                     # is this region likely flowing ice?
}
```
The point of surfacing this per-answer is that the corrections *not* applied are exactly the ones that dominate where the science is interesting (dynamic margins). Hiding them would make the tool confidently wrong.

---

## 8. Compute layer (DuckDB over the lake)

- Reads partitioned Parquet directly (`read_parquet` with Hive partitioning); predicate pushdown on `mission` + `h3_cell` + time.
- Question-shaped tools compile to parameterized SQL over `coreg_lon/lat` (co-located joins) with native coords available for audit.
- **Co-location** = spatial join within `colocation_radius` on co-registered coordinates, optionally within matched time windows. H3 cell equality (or k-ring) as the coarse join filter, exact distance as the refinement.
- **Elevation delta** = aggregate (median/robust) of paired height differences; report spread and n.
- **Slope** from IceBridge native where available; derived from ICESat-2/GLAS along-track where not — feeds the comparability sensitivity.
- pyproj-dependent work (any on-the-fly re-epoch) is a pre-query Python pass, not in-SQL. Default path touches only pre-materialized `coreg_*`.

---

## 9. Failure modes to design against (summary)
1. **Chunk-key used as row identity** → boundary-region double-count/holes. Guard: surrogate key on upsert (§7.3), never the cell.
2. **IceBridge hash instability** → silent duplicates. Guard: pin ILATM2 V2, exclude BLATM2, version-aware re-ingest (§7.3).
3. **"Materialized" cell treated as "spatially complete"** → silent holes, esp. IceBridge flight lines. Guard: optimistic-coverage + always-fetch fallback for MVP; coverage fraction for stretch (§5.5).
3b. **`within_chunk_predicate` omitted** → neighbors' photons returned as if in-cell (chunks are coarser than cells, §5.3). Guard: residual filter is mandatory on every Tier-1 read.
4. **Frame co-registration mistaken for vertical comparability** → confidently wrong deltas. Guard: comparability block on every answer, `unresolved` list always populated (§7.5).
5. **Assuming continuous coverage** for GLAS/IceBridge → false overlap reports. Guard: campaign-granularity time buckets (§5.1).
6. **Query-time ITRF transform** → slow, per-question pyproj cost on a growing lake. Guard: post-ingest materialization (§7.4).
7. **Double-correction** if a native product is already at a common epoch. Guard: trust `native_frame/native_epoch` as delivered; verify per product before materializing.
8. **Hand-rolled decode diverges from reference** on some filter pipeline → silently corrupt values that look like science. Guard: use library codecs by default; any hand-rolled primitive validated byte-identical against the reference library before trust (§6.3–6.4).
9. **Stale source reprocess** → index byte-ranges point at superseded bytes. Guard: `source_etag/version` on index rows; a version bump invalidates addressing *and* any GeoParquet built from it (§5.7).
10. *Build note (2026-08-25)* — **Order-statistic Δh estimator is blind to the misregistration it is meant to reveal.** A median of the ~10³ photons within the co-location disc moves by zero or one rank step when the disc shifts 30 cm, so the per-pair artifact comes out exactly 0.0 regardless of slope. Guard: estimate the surface height *at the footprint centre* with a local along-track linear fit (continuous in position; its response to a shift `ds` is `slope_along × ds`, which is the effect being measured). ATL06 already ships this quantity.
11. *Build note (2026-08-25)* — **Frame-step vertical component leaks into the "slope artifact".** The ITRF2008→ITRF2014 Helmert has a ~2 mm translation whose vertical projection at 70°N is ~2 mm; taken through the per-pair difference it produced a tight, non-zero "artifact" (−0.23 cm, MAD 0.03 cm) that was entirely this term — plausible-looking and wrong. Guard: compute the artifact from co-registered *positions* with *native* heights, and report the frame's vertical shift as its own number (`frame_vertical_shift_m`). Treat any tight non-zero artifact with suspicion until decomposed.

---

## 10. Capability stages

Staged so each tier delivers a coherent, demonstrable capability rather than a half-built everything. Each stage is defined by the **appendix questions (Appendix A)** it satisfies. Earlier stages carry lower risk and prove the architecture; later stages add the high-risk IceBridge leg.

### Stage 0 — spine (prerequisite, not a demo)
Row identity + common schema + idempotent upsert (§7.3), ATL03 first. The addressing index (§5) and Tier-1→Tier-2 access (§6) proven on **one** ICESat-2 granule: byte-range read → decode → materialize to GeoParquet → one DuckDB query returns a real number. Nothing downstream is correct without idempotent upsert and a working read path; this is the architectural bet (spatial-index-as-access-scheme) reduced to its smallest honest test.

### Stage 1 — 🟢 A1 + A6, on ICESat-2 + GLAS only
The first *capability* release. Adds GLAS (clean key, §7.3), ITRF materialization + comparability block (§7.4–7.5), the coverage role of the index (§5.1), campaign time-buckets (§5.2), and the tool surface `coverage_overlap`, `is_comparable_here`, `elevation_change_between_epochs`.
- **Satisfies A6** (feasibility triage — the orchestration thesis) and **A1** (intercampaign bias over stable interior — the co-registration/compute thesis).
- **Reachable without IceBridge**, so it carries none of that ingest risk. This is the defensible minimum that proves *both halves* of the project's value.
- Planner uses always-fetch-correct (§5.5); no skip optimization yet.

### Stage 2 — 🟢 A2, adds IceBridge (ILATM2 V2)
Adds the airborne leg: IceBridge ingest via **materialize-only** (CSV → GeoParquet once, no byte-range tier, §6.2), the weak-key mitigations (§7.3), and the ICESat-2 ∩ IceBridge overlap-window logic.
- **Satisfies A2** (ICESat-2 vs ATM cross-validation).
- Highest-risk ingest of the project; de-risk by confirming an ILATM2 V2 fetch + header on **day one** of this stage.

### Stage 3 — 🟢 A3, three-mission trajectory
All three missions co-registered along an outlet-glacier transect; `cross_mission_profile` + `dynamic_ice_flag`.
- **Satisfies A3**, and showcases the "computes the answer *and its limits*" property (flowing ice → ice-flow flagged, not corrected).

### Folded in / out
- **A4 (firn boundary)** — folded in wherever elevation-change lands (Stage 1+); it is a labeled `unresolved` hand-off, not new machinery.
- **A5 (sea-ice freeboard)** — out of scope; future only.

### Stretch (any stage with time to spare)
Coverage-fraction index + fetch-skipping (§5.5); H3-vs-SFC row-group benchmark (§5.6); re-epoch flexibility (§7.4); profile-guided hand-rolled decode primitive (§6.3).

### Degrade path
If the week tightens, ship **Stage 1 correctly on two clean-key missions** and demote Stage 2/3 to "demonstrated over a prepared region." Two questions (A1, A6) answered trustworthily prove the bridge; three answered shakily prove nothing a reviewer would trust. Note this inverts the naive intuition that IceBridge is the centerpiece — the two clean-key missions with a real ITRF-epoch gap between them are the tighter proof of concept.

---

## 11. Decisions made and open items

### Decided (recorded so they aren't re-litigated)
- **Access model** — two-tier: index-driven byte-range read (Tier 1, ingest read path) feeding persisted spatially-sorted GeoParquet (Tier 2, the only query path). Custom GeoParquet over DMR++/kerchunk references, per prototype (smaller + faster for our query shape). §6.
- **The index is the spatial scheme** — for ATL03 the index *is* the sub/cross-granule spatial indexing, mapping world → source byte-ranges; granules are backing storage. §5.
- **Decode** — fastest available primitive at each stage, libraries welcome; the win is the pre-baked chunk manifest (no per-query file parsing), not dependency-freeness. Library codecs by default; hand-roll only profile-guided, never filter pipelines. §6.3.
- **Index key v1** — H3 for both the addressable cell and (v1) the row-group sort. SFC (Hilbert/S2) for row-group ordering is a gated, time-boxed benchmark with H3 as the fallback. §5.6.
- **IceBridge** — ILATM2 V2 only, materialize-only (no byte-range tier); BLATM2 out. §6.1–6.2.
- *Build note (2026-08-25)* — **Common epoch / anchor frame:** ITRF2014 at a fixed common epoch, **default 2005.0** (configurable per call; re-epoching is a recompute). Chosen so the ICESat-2 cloud is the one that moves (B.3). Every measurement is propagated from its *own* observation epoch with the ITRF2014-PMM NOAM Euler vector (PROJ `helmert` with rate terms, position-vector convention); GLAS is first moved ITRF2008→ITRF2014 as a separate, inverted 4-D step evaluated at the observation epoch (PROJ's `ITRF2014:ITRF2008` init entry is 2014→2008 *forward*).
- *Build note (2026-08-25)* — **Colocation radius default: 35 m** (half the GLAS footprint), with Δh taken from a local along-track fit of ATL03 photons evaluated at the GLAS footprint centre (§9 item 10). Per-surface-type tuning remains open; footprint-mismatch note stands.
- *Build note (2026-08-25)* — **Products pinned:** ATL03 **v007** (v006 is no longer cloud-hosted under NSIDC_CPRD), GLAH06 **v034** (cloud-hosted, ~4 MB granules → bulk download beats remote open). Toolchain: Python 3.13 (earthaccess 0.18 has no 3.14 wheels), `mcp` 2.x (`MCPServer`; the `FastMCP` import is gone), pyproj 3.7.2 / PROJ 9.5, deck.gl 9.3 via script tag.
- *Build note (2026-08-25)* — **GLAS vertical handling:** heights converted TOPEX/Poseidon→WGS84 with the product's own `d_deltaEllip` (0.712 m at the EGIG box) and the saturation correction `d_satElevCorr` applied (it is *not* applied in `d_elev`). Recorded in every comparability block as `ellipsoid_correction_applied`; geoid/tide/GIA/firn and GLAS intercampaign bias remain `unresolved`. `dynamic_ice_flag` is `null` with a note until a velocity field exists — `false` would be an overclaim.

### Open
- **SourceAdapter fetch mechanism** — per-mission S3 range-read + auth (earthaccess/S3 direct). Still the concrete piece behind Tier-1; spec codes against the interface. The index's addressing role *is* most of what a fetch layer needs, but auth/credentialing and the actual range-GET client are unwritten.
- **Chunk-index extraction cost per mission** — index-build (§6.1) must read HDF5 chunk-info efficiently at scale; confirm `h5py` chunk-info throughput is acceptable over S3, or whether a faster path (bulk b-tree read, C binding) is needed. This is the one place a library *could* be the bottleneck and hand-rolling *might* pay — settle by benchmark, not assumption.
- **H3 resolution R** — empirical, per-mission; trades addressing granularity (fetch waste when cell ≪ chunk) against index size (when cell ≫ chunk).
- ~~**Common epoch / anchor frame**~~ — decided, see above (ITRF2014 @ 2005.0, configurable).
- **Colocation radius default** — 35 m for the demo (decided above); per-surface-type tuning and the IceBridge swath case still open.
- **ILATM2 V2 CSV header** — confirm whether it exposes a per-record sequence number that would harden the IceBridge row key (§7.3).
- **Whether NASA reference files exist for the target products** — if DMR++/kerchunk sidecars exist for ATL03/GLAS at the versions wanted, index-build could *validate against* them even if we don't consume them; worth a check before writing the chunk-info extractor.
- **GIA / geoid / tide** — out of scope now; comparability block reserves space to surface and later apply them.

---

## Appendix A — Researcher questions the server should answer

Six questions grounded in the published cross-mission altimetry literature. Each entry gives the question, who actually asks it, how the outlined server answers it, and an honest read on whether the server beats an agent simply writing a one-off script.

**Legend**
- 🟢 **BUILD** — target capability; the MVP tool surface (§3) should satisfy this by end of hackweek.
- 🟡 **BUILD (boundary)** — build the answer, but the point is that the server states what it *cannot* resolve. Credibility feature.
- ⚪ **FUTURE** — real science, poor fit for the elevation-centric MVP; note as future scope only.

An honest overall framing, stated up front because a reviewer will see through a blanket claim: for one-off *computational* questions (A1–A3) a capable agent writing a script could do the same math. The server's advantage there is **modest and specific** — the co-registration and comparability logic is written once and applied consistently instead of re-derived (and re-bugged) per script; the persistent lake makes repeat questions over a region instant and their coverage already known; the comparability block enforces the caveats every time instead of relying on the analyst to remember GIA exists. The advantage is **large and qualitative** only on the orchestration/meta questions (A6, and the feasibility half of A2): discovery across three heterogeneous, campaign-intermittent archives, re-asked constantly, is genuinely awkward to script. The appendix claims superiority where it's real and declines to where it isn't.

---

### 🟢 A1. Intercampaign / intermission bias over a stable interior
**Question.** "What is the elevation bias between GLAS and ICESat-2 over the East Antarctic interior, where true elevation change is near zero?"

**Why researchers ask it.** Every multi-mission mass-balance record must remove an intermission bias, conventionally estimated from observations over overlapping (or geophysically-null) epochs; ICESat's own intercampaign bias is a standard correction in Greenland/Antarctica mass-loss work. Getting this offset wrong propagates directly into reported mass change.

**How the server answers.** `elevation_change_between_epochs` over a low-slope interior box chosen so the true geophysical signal is minimal; the residual median difference *is* the bias estimate. Co-location runs on co-registered coordinates; the result carries `surface_slope_deg` (confirming the box is flat enough to trust) and an `unresolved` list flagging that firn compaction and GIA are not removed.

**vs. a script (modest advantage).** The math is scriptable, but the correctness hinges on exactly the machinery the server centralizes: ITRF+epoch co-registration so footprints align, slope-screening to select a valid stable region, and the comparability block to keep firn/GIA visible. A one-off script commonly skips co-registration and silently folds plate-motion misregistration into the "bias." The server makes the right thing the default thing.

**Flagship case** — the single best demonstration that the co-registration layer earns its place.

---

### 🟢 A2. Cross-validation of ICESat-2 against IceBridge ATM
**Question.** "Over this Greenland firn region, what is the ICESat-2 − ATM elevation difference, and does it vary with surface condition?"

**Why researchers ask it.** ATM is the airborne transfer standard used to validate ICESat-2; ICESat-2 biases of several cm over melting, coarse-grained, water-saturated firn — assessed against ATM — are an active, contested topic (subsurface/volumetric scattering at 532 nm).

**How the server answers.** `elevation_change_between_epochs` (or `cross_mission_profile`) restricted to the ~14-month ICESat-2 ∩ IceBridge overlap (late 2018 – Nov 2019). ATM's native `slope` feeds the comparability sensitivity; co-location uses the footprint-mismatch-aware radius. Coverage/feasibility half of the question is answered by `coverage_overlap` first ("is there any co-flown data here?").

**vs. a script (mixed).** The *feasibility* half — does co-flown data exist in the narrow overlap window, and where — is orchestration the server does well. The *compute* half is scriptable; the server's edge is consistent temporal-overlap handling and the slope-aware caveat applied every time.

**Caveat for the build.** This is the entry that most stresses the weak IceBridge row key (§7.3); it is a reason to get ILATM2 V2 identity right, not a reason to trust it blindly.

---

### 🟢 A3. Three-mission dynamic-thinning trajectory of one outlet glacier
**Question.** "Trace surface elevation on Jakobshavn (or Thwaites/Pine Island) across GLAS (2003–09), IceBridge (2009–19), and ICESat-2 (2018–)."

**Why researchers ask it.** This is the literal bridge use case; accelerated thinning of Jakobshavn was published by combining ICESat and IceBridge, and outlet-glacier dynamic thinning is the headline signal of the whole multi-mission record.

**How the server answers.** `cross_mission_profile` over the glacier trunk, one series per mission on co-registered coordinates, each carrying native frame/epoch. **Crucially, `dynamic_ice_flag` fires** and the comparability block states plainly: plate motion corrected, **ice flow not corrected**, firn/GIA unresolved.

**vs. a script (the honest catch is the value).** This is flowing ice: the surface parcel physically translates hundreds of m to km between missions. A naive script co-locates points on ice that moved and reports a clean trajectory that is partly an artifact. The server's contribution is *refusing to hide that* — it computes the trajectory and marks exactly why it may mislead. Best illustration of "computes the answer and its limits."

---

### 🟡 A4. Firn-compaction vs. dynamic signal separation (boundary case)
**Question.** "How much of the observed multi-year elevation change here is firn compaction vs. ice dynamics?"

**Why researchers ask it.** Central to attribution — Totten Glacier thinning was assigned a dynamical origin only after firn-compaction anomalies were accounted for. Surface elevation change is not mass change until firn is removed.

**How the server answers — and where it stops.** The server delivers the co-registered, bias-aware elevation-change field and flags firn as `unresolved`. It **does not** model firn compaction: that requires a regional climate / firn densification model (e.g. HIRHAM5/ERA forcing), explicitly out of scope (§0). Included here precisely as a stated boundary — the server hands off cleanly to a firn model rather than pretending to be one.

**vs. a script.** Neither a script nor the server can answer the attribution without the firn model; the server's virtue is saying so in-band instead of returning an elevation change the analyst might over-interpret as mass change.

---

### ⚪ A5. Sea-ice freeboard cross-mission consistency (future scope)
**Question.** "Are GLAS and ICESat-2 surface-height / freeboard distributions over Arctic sea ice consistent in their lead-vs-floe separation?"

**Why researchers ask it.** Freeboard → thickness is a primary cryosphere product; ICESat-2's small footprint was designed for sea-surface height in narrow leads.

**Why it's future scope, not MVP.** Freeboard needs a local sea-surface reference derived from leads — retrieval machinery the elevation-centric L2 lake does not natively support — and GLAS ↔ ICESat-2 sea ice has no temporal overlap, so consistency is distributional, not co-located. Real science, wrong shape for the hackweek server. Listed so the omission is deliberate, not an oversight.

---

### 🟢 A6. Coverage / comparability feasibility triage
**Question.** "Before I design a study of region X, which missions actually observed it, when, and are they comparable enough to be worth it?"

**Why researchers ask it.** It is the question that precedes every other one here, and it is genuinely painful today: the three archives (ICESat/GLAS, IceBridge, ICESat-2) don't answer "who saw this spot, when, and how comparably" in one place, and coverage is campaign-intermittent (GLAS) and flight-line-sparse (IceBridge), so the honest answer is not "yes/no" but "these campaigns, this fraction, comparable subject to these caveats."

**How the server answers.** `coverage_overlap` + `is_comparable_here` directly — campaign-granularity coverage per mission, optimistic spatial fraction, pairwise/triple overlap flags, and the slope/dynamic-ice/unresolved-corrections diagnostic, with no elevation math required.

**vs. a script (large, qualitative advantage).** This is pure orchestration across three heterogeneous archives, re-asked constantly with different inputs — exactly what an MCP tool is shaped for and exactly what one would *not* write a bespoke script for. The least glamorous entry and the strongest "MCP-native, not a script" case.

---

### What to build so these are satisfied

The 🟢 entries define the MVP acceptance test. Mapping to the tool surface (§3) and build order (§10):

| Question | Tools exercised | Depends on |
|---|---|---|
| A1 bias-over-stable | `elevation_change_between_epochs` | ITRF materialization (§7.4), slope screening, comparability block (§7.5) |
| A2 ICESat-2 vs ATM | `coverage_overlap`, `elevation_change_between_epochs` | IceBridge ILATM2 V2 ingest + row key (§7.3), overlap-window logic |
| A3 outlet-glacier trajectory | `cross_mission_profile` | all three missions ingested, `dynamic_ice_flag` |
| A6 feasibility triage | `coverage_overlap`, `is_comparable_here` | chunk index (§5), campaign time-buckets (§5.1) |

Observed dependency: **A1 and A6 are reachable with ICESat-2 + GLAS alone** (the two clean-key missions) and together prove both halves of the thesis — A1 the co-registration/compute half, A6 the orchestration half. **A2 and A3 require the IceBridge leg**, which is the highest-risk ingest (§10). Therefore, if the week tightens, the defensible cut is: ship A1 + A6 on two missions correctly, and demote A2/A3 to "demonstrated over a prepared region" rather than shipping three missions with a half-working IceBridge key. Two questions answered trustworthily prove the bridge; three answered shakily prove nothing a reviewer would trust.

---

## Appendix B — Killer demo: "The co-registration reveal"

The demo makes the architecture's least-visible, most-defensible novelty **visceral**: that horizontal misregistration between missions masquerades as elevation change, and that ITRF+epoch co-registration removes it. The visual *is* the scientific point — you watch a fake elevation signal appear and disappear.

Chosen over a glacier fly-through because a fly-through of pre-staged points proves only that you have XYZ points (anyone with the granules has those); it shows none of the index, co-registration, or honesty layers. This demo shows the one thing nobody else's visualization shows, and — critically — it runs on **Stage 1 (ICESat-2 + GLAS)**, so the best moment of the week does not depend on the riskiest ingest (IceBridge). It is also the seed of the Stage-3 fly-through (Appendix B.7): C is this, grown a camera and a third mission.

### B.1 The question it answers (real, from Appendix A1)
User asks the agent, in plain language:
> "Over [sloped region near an outlet-glacier margin], GLAS saw ~2004 and ICESat-2 sees now. Is the elevation difference between them real, or an artifact of how the two missions are registered?"

The answer is not a number in prose — it renders as an interactive 3D scene with one control. That is the demo.

### B.2 What renders
A 3D scene in the agent UI:
- Two point clouds over the region: **GLAS** footprints (~2004, one color) and **ICESat-2** footprints (now, second color), on the sloped surface.
- A side **elevation-difference histogram** of co-located GLAS−ICESat-2 pairs.
- One primary control: **`Co-registration: OFF | ON`**.
- Orbit / zoom / pan (standard 3D camera).
- A persistent, unmissable **exaggeration label** (see B.5).

### B.3 The interaction — "the snap"
- **OFF:** the two clouds sit horizontally offset by the plate-motion displacement between the two epochs (exaggerated, B.5). Because the surface is sloped, that horizontal offset produces a **systematic vertical difference** at co-located points — the histogram is wide and visibly **biased away from zero**. A readout says e.g. "median Δh = 14 cm (artifact)".
- **Toggle to ON:** the ICESat-2 cloud **animates** into alignment (the plate-motion vector removed by the ITRF+epoch transform, §7.4) over ~1 s. As it moves, the histogram **collapses toward zero** in the same animation. Readout updates: "median Δh ≈ 1 cm (residual)".
- The *motion* is the payload: the viewer sees plate motion being removed and a fake elevation signal vanishing with it. Slope is what converts the horizontal snap into the vertical histogram collapse — so the demo also teaches *why* co-registration matters exactly where the science is (sloped, dynamic margins).

> *Build note (2026-08-25)* — **The histogram does not collapse on real data, and cannot.** The plate-motion artifact is `slope × (along-beam component of the 30 cm shift)`: sub-millimetre at 0.2°, ~1 cm at 2°, ~3 cm at 5° — against decimetre photon noise and metre-scale real change over 15 years. The built widget therefore shows the OFF/ON Δh histograms *and* a separate per-pair artifact panel (Δh at co-registered positions minus Δh at native positions, native heights), which is a tight peak whose offset from zero *is* the artifact. The on-screen wording is "plate-motion artifact removed: X cm", never "missions agree". See B.10 for measured numbers.

### B.4 Why this proves the architecture (not just "we have points")
- **Co-registration (§7.4)** is the visible actor — OFF/ON is literally native vs. `coreg_*` coordinates.
- **The comparability block (§7.5)** renders as the histogram + readout + the honest labels; the "artifact" vs "residual" language is the `unresolved`/`plate_motion_corrected` fields made visual.
- **The compute path (A1)** is exercised end to end: the scene is the rendered output of `elevation_change_between_epochs`, not a bespoke plot.
A reviewer sees the difference between the naive comparison (script that skips co-registration → the biased histogram) and the server's answer (the collapsed one) *in a single toggle*.

### B.5 The exaggeration must be labeled — non-negotiable
Plate motion is 15-30 cm over the GLAS↔ICESat-2 span: real, above the noise floor (§7.4), but invisible at glacier scale without exaggeration. The horizontal offset (and possibly vertical) is **exaggerated to be seen**, and the scene must carry a persistent on-screen label: e.g. "Horizontal offset exaggerated ×500 for visibility; true displacement ≈ 22 cm." 

*Build note (2026-08-25):* at a 76 km scene, ×500 (150 m) is sub-pixel; the widget auto-picks the factor that makes the shift ~3 % of the scene span (×7000 on the EGIG box), rounds it to one significant figure, and prints it in the label together with the true displacement.

This is a hard requirement, not polish: the demo whose entire thesis is "don't let a visual mislead you about elevation" **must not itself cheat**. An unlabeled exaggeration hands a sharp reviewer the exact weapon the comparability ethos exists to remove. Labeled, the demo is unimpeachable; unlabeled, it refutes its own point. The true (un-exaggerated) numbers appear in the readout alongside the exaggerated geometry.

### B.6 Data path (tool call → render)
1. Agent calls `elevation_change_between_epochs` (bbox, GLAS window, ICESat-2 window, colocation radius).
2. Server resolves cells → byte-ranges (§5), Tier-1 reads + Tier-2 materializes if cold (§6), or hits warm GeoParquet.
3. Returns **both** native and `coreg_*` co-located pairs, the two Δh distributions (native → biased, coreg → residual), `surface_slope_deg`, the true displacement magnitude, and the comparability block (§7.5).
4. The UI widget renders the two clouds, wires OFF/ON to native vs coreg coordinates, and animates between the two returned distributions.

The server returns **structured data with both states**; the widget does the phrasing/animation (consistent with §0 — server computes, client renders). No prose narration from the server.

### B.7 Cold-cell caveat for the live demo
First touch of a cold cell pays the full Tier-1 read + materialize synchronously (§6.2, §7.4 note). **Pre-warm the demo region** before presenting — run the query once so the GeoParquet is hot — so the on-stage interaction is instant. Do not optimize this away; just warm it. (If you *want* to show the byte-range machinery, do it as a deliberate, separate "first fetch" beat, not during the co-registration reveal.)

### B.8 Build cost and reuse
- Reuses the Stage-1 compute path wholesale; the new work is the 3D widget (two point clouds, a histogram, one animated toggle) and having the tool return native+coreg pairs together (a small addition to the §3.2 output).
- 3D: a lightweight web GL scene (points + camera controls) is enough; no terrain mesh required for the core effect, though a faint surface aids depth perception.
- **Grows into Demo C (Stage 3):** add a fly-through camera along a glacier trunk and the third mission, and reuse §7.5's `dynamic_ice_flag` to shade untrustworthy (fast-flowing) segments in-scene. Same widget, same honesty layer, more camera and one more mission — the two demos are one build at different stages, not two.

### B.9 Failure mode for the demo itself
The one way this demo lies: if the "residual" (ON) histogram is near-zero because the region was *chosen* to make it so, it could imply co-registration fixes everything — when in truth firn/GIA/geoid remain (§7.5). Guard: keep the `unresolved` list visible even in the ON state, and pick a region where the residual is small **because co-registration genuinely dominates the artifact there**, not because all signals happen to cancel. The demo shows co-registration removing *the plate-motion artifact*, not "making the missions agree" — the on-screen language must say the former.

### B.10 Measured on real data (build note, 2026-08-25)
Placeholder region: EGIG west flank box (−45, 69.8, −43, 70.2); ATL03 v007, 8 granules, 12–21 March 2020, strong beams, land-ice confidence ≥ 3 → 5.36 M signal photons (298 k rendered); GLAH06 v034, all 278 granules over the box, 19 campaigns L1A–L2F → 38.4 k usable shots (38 cloud returns dropped by a cross-campaign neighbour test).

| Quantity | Value |
|---|---|
| Plate-motion displacement, ICESat-2 2020.22 → 2005.0 | 32.2 cm (SE; independently checked against ω × r with the NOAM Euler vector: 2.13 cm/yr toward NW) |
| GLAS 2005.87 → 2005.0 | 3.0 cm |
| Relative shift between clouds | **30.5 cm**, vector (+0.22 E, −0.21 N) in EPSG:3413 local |
| Regional slope (plane over all photons) / along-beam slope at pairs | 0.23° / 0.13° |
| Co-located pairs (35 m) | 572 native, 568 co-registered, 0 gross (> 50 m) |
| Δh ICESat-2 − GLAS, median (MAD) | **−1.31 m** (0.23 m) — real change + unresolved terms |
| Per-pair plate-motion artifact, median (MAD) | **+0.03 cm** (0.06 cm) — consistent with 0.13° × along-beam shift |
| ITRF2008→ITRF2014 vertical shift of GLAS heights | −1.7 mm (reported separately, not in the artifact) |
| Live co-registration compute (5.4 M photons, pyproj) | 6–8 s; cached thereafter |

Reading: the artifact is ~4000× smaller than the real signal on this box. That is the honest physics for a 0.2° interior flank, not a bug — the collaborator's region choice (several degrees of slope, slow flow, GLAS coverage) is what makes the reveal visible. Two wrong numbers were produced and caught on the way here (§9 items 10–11): a disc-median estimator that returned exactly zero artifact, and a −0.23 cm "artifact" that was the frame step's vertical component.

---

## Appendix C — Technical demo: "The world is the index" (byte-range access)

A second, independent demo for a different audience. Where Appendix B convinces the **science** audience ("your cross-mission numbers are trustworthy"), this convinces the **engineering/architecture** audience ("we access these archives in a way nobody else does"). Kept separate on purpose: fusing them would dilute B's single-message snap and bury this demo's novelty as a loading animation under it. Each demo survives a tight week on its own, and this one needs **only ATL03** (Stage 0/1) — the mission whose native along-track chunking is worst and whose spatial re-indexing therefore wins most.

### C.1 The claim it proves — stated honestly
Not "we move orders of magnitude fewer bytes." That would beat a strawman. The competent status-quo path already subsets. The honest, defensible claim is:

> Given that the competent path already subsets, the world-as-index eliminates the **granule-boundary tax** and the **per-query structure-parse tax**: N granule-opens-parse-and-scans collapse into **one spatial lookup that already knows the byte-ranges**, across granule boundaries, with zero per-query HDF5 structure parsing.

The win is in **granules touched, files opened, structure re-parsed, and round-trips** — not primarily raw bytes. This is the counter a reviewer who knows h5coro cannot wave away.

### C.2 The baseline must be strong, not naive
The left side is **not** "download whole HDF5 files" — nobody who knows the data does that, and racing it is theater. The baseline is the current **best practice**: CMR spatial query → cloud-native client-side subsetting (**h5coro**) → read only needed beams/variables, spatially clipped. h5coro is chosen deliberately because it is the respected tool for ICESat-2 cloud access; beating best practice is the point, beating a slow server (Harmony) would look like a rigged race.

What the strong baseline still pays, and the demo makes visible:
1. **Granule-boundary tax** — bbox photons scatter across N granules (orbital, not geographic boundaries), so N files are opened, authenticated against, and spatially scanned for one small box.
2. **Structure-parse tax** — each of the N opens re-parses that granule's b-tree/metadata to decide what to read. This is exactly the parse the index amortizes to build-time (§6.1).
3. **Round-trip fan-out** — per-granule request patterns vs. batched byte-range GETs.

### C.3 Honest scoreboard (what the split-screen counts)
Led by the metrics where the win is real; bytes shown honestly including where it is *close*.
| Metric | Competent baseline (h5coro) | World-as-index | Honest read |
|---|---|---|---|
| **Granules opened / auth'd** | N | ~0 at query time (hit index, not files) | **large, real win** |
| **Structure parses (b-tree walks)** | N per query | 0 at query time (amortized to build, §6.1) | **large, real win** |
| **Round-trips / requests** | per-granule fan-out | batched byte-range GETs | **real win** |
| **Bytes of non-target data read** | whole compressed chunks (chunk is the atom) | whole compressed chunks (§5.3) | **often CLOSE — show this honestly** |
| **Cold-cache wall-clock** | measured | measured | win, driven by the above |

The fourth row is the demo's self-honesty check: at the compressed-chunk atom, both paths over-read similarly (§5.3). The index does **not** claim a magic byte reduction. Claiming a giant byte win would be cheating; claiming a giant *granule-touch / parse / round-trip* win is the truth, and it is still striking — N files-and-parses collapsing to one lookup is a visible asymmetry.

### C.4 What renders — split-screen, 2.5D globe
Deliberately **2.5D, not forced 3D**: a globe with granule ground-tracks on the sphere and byte-range slivers lighting up along them. Spatial enough to be beautiful, honest about being a data-motion visualization rather than a landscape. Forcing full 3D here would be decoration.

One bbox query (photons in a small box over a glacier), run on both sides simultaneously:
- **Left — competent baseline:** the query region lights up; N granule ground-tracks (long orbital swaths, most of each irrelevant to the box) illuminate and are **opened and scanned**; per-granule parse + request activity animates; scoreboard ticks up granules/parses/round-trips/bytes/time.
- **Right — world-as-index:** the same query resolves to **H3 cells** → the **index** lights up the specific byte-ranges scattered across those same granules → thin slivers stream from a dozen files' interiors → the box fills. Scoreboard ticks: ~0 granule-opens, 0 parses, batched GETs.

The payload is the **asymmetry in motion**: left hauls and scans whole orbital swaths; right sips byte-slivers from their guts; both answer the identical question; the granule/parse/round-trip counters end orders of magnitude apart while the byte counter ends *close* — which is the honest, more interesting result. Granules visibly become mere containers the index reaches inside.

### C.5 Honesty tier of the baseline: measured-and-replayed
The left side is **really run once and replayed** (not live, not modeled):
- **Really run:** an actual CMR query + h5coro subsetted fetch over the demo bbox, capturing true granules-touched, parses, round-trips, bytes, and wall-clock.
- **Replayed:** the animation is driven from those captured real numbers, so on-stage it is instant and robust, not fragile live integration on the losing side.
- **Labeled:** "baseline: h5coro, measured [date], replayed" on screen. Not live (too fragile), not modeled (too soft, a reviewer trusts it least). This is the C-appendix analogue of B.5's exaggeration label — the demo about honest access must itself be honest about its baseline.

### C.5b Measured scoreboard (build note, 2026-08-25)
The index (§5–§6, ATL03 only) was built and the comparison run for real — not the split-screen demo of C.4, but the
numbers it would be driven by. Same bbox (EGIG west flank), same 8 ATL03 v007 granules, same target photon subset.

| Method | Granules touched (client) | HDF5 structure parses at query time | HTTP requests | MB transferred | Wall-clock s | Photons returned |
|---|---|---|---|---|---|---|
| H3 chunk index + byte-range GETs + Parquet lake, first touch | 8 | 0 (8 at index build, once) | 608 | 138 | 156.4 (110 index build + 27 fetch + 2 query) | 5,363,896 |
| same, second query (lake warm) | 0 | 0 | 0 | 0 | 3.22 | 5,363,896 |
| earthaccess.open + h5py over fsspec block cache | 8 | 8 | 201 | 3,372 | 154.9 | 5,363,095 |
| download whole granules (8 threads) + local h5py | 8 | 8 | 16 | 22,272 | 556.1 | 5,363,095 |
| SlideRule atl03x (h5coro, public cluster, us-west-2) | 8 | 8 (server-side, opaque) | 1 | 99 | 13.4 | 4,400,711 |
| NSIDC Harmony trajectory subsetter (async) + download | 8 | 8 (server-side, opaque) | 10 | 721 | 127.5 (first run 215, queue variance) | 5,363,095 |

What the numbers say, against C.3's predictions: the granule-open and structure-parse rows behave exactly as predicted
(0 at query time vs 8 per query everywhere else). The byte row is *not* "close" as C.3 conservatively expected — the
byte-range path moves 24× less than remote h5py because fsspec's block cache over-reads at 4–16 MB granularity, while
whole-compressed-chunk reads (§5.3) are ~300 kB each. Round-trips are not a win here (608 chunk GETs vs 201 block
reads); batching adjacent chunks is the obvious next step. The strong baseline (C.2) is SlideRule, which wins
wall-clock outright (13 s) by running next to the data; the index's answer to that is the warm path — 3 s, zero
traffic, no server — and an exact subset. Harmony's cost is queue latency and all-variable output.

### C.6 Data path (tool call → render)
1. Agent calls a query that resolves cells → byte-ranges (§5) and reads via Tier-1 (§6.2) — the right side is the **real** system, run live (it is fast and robust; it is the winning side).
2. The left side plays the pre-captured h5coro measurement (C.5).
3. Both drive the same scoreboard component; the globe animates granule-tracks (left) vs. byte-slivers (right).
The right side is genuinely the production path (§5–§6), not an animation — only the losing baseline is replayed, and only because running h5coro live on stage is needlessly fragile.

### C.7 Scope, cost, relationship to other demos
- **ATL03-only, Stage 0/1.** Needs the addressing index (§5) and Tier-1 read (§6) on one mission — the architectural spine. No GLAS, no co-registration, no IceBridge. Independent of Appendix B; the two demos share no dependency and hit different audiences.
- **New work:** the split-screen globe widget + scoreboard, and the one-time h5coro baseline capture (C.5). The right side reuses the real index/read path.
- **Two independent killer demos, two audiences, each tight-week-survivable:** B (co-registration, science) on ICESat-2+GLAS; C (byte-range index, engineering) on ICESat-2 alone.

### C.8 Failure modes for this demo
1. **Strawman baseline** → a knowledgeable reviewer rejects the whole thing. Guard: strong baseline is h5coro best-practice (C.2), really measured (C.5).
2. **Overclaiming bytes** → the one row where the paths are close (C.3 row 4) gets hidden, and the demo cheats exactly where the project's ethos says not to. Guard: show the close byte row explicitly; lead with granules/parses/round-trips, which are the real wins.
3. **Modeled numbers passed as measured** → soft, and if discovered, fatal to credibility. Guard: measured-and-replayed with an on-screen label (C.5).
4. **Forced 3D** → decoration that invites "why is this 3D?" Guard: 2.5D globe, honest about being data-motion (C.4).

---

## How to use this document (orientation for the implementer)

Most of this spec (§0–§11, Appendix A) describes the **full proposed system** — the persistent lake, the byte-range index, the staged capabilities. That is the *proposal this hackweek justifies*, not the four-half-day build. **The build target is Appendix D**: three vertical slices for Demo B (Appendix B), scoped to what fits four half-days with one or two people. Treat §0–§11 and Appendices A–B as reference/context; treat Appendix D as the work order. Where D omits something in the larger spec (the MCP server internals, the index, Demo C, IceBridge), that omission is deliberate — do not build ahead into it.

---

## Appendix D — Execution plan: three vertical slices (implementation brief)

**Budget:** four half-days (~16 person-hours solo, ~28 with a science collaborator). **Demo:** B only (co-registration reveal). **Not in scope:** the MCP server internals, the persistent lake, the byte-range index (§5–§6), Demo C, IceBridge, any mission beyond ICESat-2 + GLAS. Building any of these fails the week.

**Principle: each slice ends in something you would be willing to show.** If time runs out mid-project, the last completed slice is still a demo. Ship each slice before starting the next; do not build all three half-done.

**Library choices (pinned — do not re-decide):**
- Data access: `earthaccess` (NASA Earthdata auth + granule search + S3/https fetch).
- HDF5 read: `h5py`.
- Transform: `pyproj` (ITRF frame + epoch propagation).
- Widget: **deck.gl** (`PointCloudLayer`/`ScatterplotLayer`, 3D orbit view), with a **fallback to plotly 3D scatter** if deck.gl is not rendering real points by the end of Slice 1's widget block. The two-mission story matters more than the renderer — do not let deck.gl ramp consume Slice 2.
- ICESat-2 product: **ATL03 v006** (or latest available; pin whichever is used). GLAS product: **GLAH06** (40 Hz group).
  *Build note (2026-08-25):* pinned **ATL03 v007** (v006 not cloud-hosted) and **GLAH06 v034**; Python 3.13, `mcp` 2.x; widget is deck.gl (plotly fallback not needed).

---

### Slice 1 — One mission on screen (ICESat-2)

**Deliverable:** real ATL03 photons over one Greenland region, rendered as a 3D point cloud with an orbit camera, triggered by a plain-language question in the agent UI. Fetch → extract → clip → render, wired to a prompt. The "server" behind it may be a thin shim.

**This slice deliberately has little in the middle.** Its purpose is to de-risk the two ends — real ATL03 extraction and 3D rendering — while you have the most time to recover. Expect the deck.gl ramp and the Earthdata auth to be the time sinks; neither is science.

**Sub-task 1a — choose and validate the ATL03 photon subset (first-class work, not setup).**
The subset is a scientific decision with real visual consequences; the wrong subset renders noise instead of a surface. Decide and validate:
- **Beams:** strong beams only for the demo (higher photon rate, cleaner surface). Record which (gt1l/gt2l/gt3l depend on spacecraft orientation — read it from the granule, don't assume).
- **Signal confidence:** filter on `signal_conf_ph` (per-photon confidence, per surface type) to signal photons — start at medium+high confidence for the land-ice surface type; drop background photons or the surface won't read.
- **Height:** use the geolocated photon height `h_ph` with `lon_ph`/`lat_ph`.
- **Validate:** the acceptance test is visual — does a recognizable surface emerge, not a noise cloud? This is where the science collaborator contributes judgment.

**Acceptance criteria (Slice 1 done when all true):**
1. A named question in the agent UI ("show me ICESat-2 photons over [region]") returns a rendered 3D point cloud.
2. The points are real ATL03 signal photons (subset per 1a), clipped to the region bbox.
3. Orbit/zoom/pan works; a recognizable ice surface is visible.

**Sharp edges:** Earthdata Login auth via `earthaccess` (do this first — it blocks everything); strong-beam identity depends on `sc_orient`; `signal_conf_ph` is a 2D array indexed by surface type — select the land-ice column; ATL03 photon volume is large — clip early, before render.

**Non-goals this slice:** no GLAS, no co-registration, no index, no real MCP server, no histogram.

**Status (2026-08-25): done.** Photon slices are located from the 20 m segment index (`ph_index_beg`/`segment_ph_cnt`) so only in-bbox bytes are read (~18 s per beam-granule over HTTPS). Real MCP server exists after all (`show_photons`, `add_glas`, `coregister`, `check_coverage`, `list_regions`) since Claude Desktop is the agent UI; the stdio transport was verified with a scripted client.

---

### Slice 2 — Add ICESat-1 (GLAS)

**Deliverable:** a second point cloud (GLAS, ~2003–2009, distinct color) in the same scene, same region, same camera. The render side is free (built in Slice 1); this slice is **all ingest**, and GLAS is a genuinely different product from ATL03 — not a copy of the Slice-1 loader.

**Acceptance criteria:**
1. GLAS GLAH06 elevations over the same region render as a second, distinctly colored cloud in the same scene.
2. Both missions visible together, correctly geolocated, in **native coordinates**.

**Do NOT "fix" any visible offset between the clouds.** In native coordinates over a sloped region the two clouds may sit subtly offset — that offset is the plate-motion artifact Slice 3 exists to reveal. Render honest native coordinates and leave it.

**Sharp edges:** GLAS is HDF5 but the **40 Hz group** holds the shot-level data, with different variable names than ATL03 (`i_rec_ndx`, `i_shot_count` for identity; separate lat/lon/elev vars); GLAS coverage is **campaign-intermittent** — confirm the chosen region actually has GLAS shots before committing (this is part of region validation, below); watch the 1 Hz vs 40 Hz group structure.

**Non-goals this slice:** no transform yet, no histogram, no third mission.

**Status (2026-08-25): done.** Additional sharp edges found: r34 is ITRF2008; `d_lon` is 0–360; `d_satElevCorr` is *not* applied in `d_elev`; `d_deltaEllip` gives the T/P→WGS84 offset directly; usable-shot filter `elev_use_flg == 0 && sat_corr_flg <= 2`; granules are ~4 MB, so bulk download (parallel) beats per-file remote opens; cloud returns appear as vertical stacks and are removed by a cross-campaign neighbour-median test (no plane extrapolation — that wrongly drops real shots near curved margins).

---

### Slice 3 — The computation (ITRF + epoch co-registration) and the toggle

**Deliverable:** the demo itself. A question / toggle triggers a **live** co-registration computation (server-side pyproj), the result is **cached** ("live-then-stored"), and the scene animates the snap: native ↔ coreg coordinates, with the Δh histogram collapsing alongside. This shows the agent *doing* something useful, not swapping precomputed states.

**Live-then-stored:** first co-registration call runs pyproj for real and stores the result; subsequent toggles hit the cache. **Rehearse a warm-cache run before presenting** so the first (live) call's correctness is proven off-stage — you are not debugging pyproj in front of people.

**Acceptance criteria:**
1. **OFF state:** two clouds in native coords, visibly offset (exaggerated, see below); co-located Δh histogram wide and biased away from zero; readout shows true (un-exaggerated) median Δh labeled "artifact".
2. **ON state:** toggling triggers a real pyproj ITRF+epoch transform of the ICESat-2 (or GLAS) cloud to a common epoch; clouds animate into alignment; histogram collapses toward zero; readout shows residual labeled accordingly.
3. Result is cached after first compute.

**Hard requirements — non-negotiable even under time pressure (these cost minutes, not hours):**
- **Exaggeration label (B.5):** the horizontal offset is exaggerated to be visible (true displacement ≈ 15–30 cm, invisible at scene scale). A persistent on-screen label must state the exaggeration factor and the true magnitude. The demo about not letting visuals mislead must not itself mislead.
- **Region honesty (B.9):** the residual must collapse *because co-registration removes the plate-motion artifact there*, not because the region's signals happen to cancel. Keep the `unresolved` list (firn/GIA/geoid) visible even in the ON state. On-screen language: co-registration removes *the plate-motion artifact*, not "makes the missions agree."

**Sharp edges:** the pyproj transform must **propagate to a common epoch**, not merely swap frame labels — epoch propagation (plate motion) is the entire effect; a frame-only swap does nothing visible and silently defeats the demo. Confirm ATL03's native frame/epoch (ITRF2014) and GLAS's as-delivered frame before transforming; do not double-correct (§7.4, §9).

**Non-goals:** no IceBridge, no third mission, no real index/lake behind the compute — a shim that runs pyproj and caches is sufficient.

**Status (2026-08-25): done; measured results in B.10.** Additional sharp edges found: (1) PROJ's `helmert` evaluates rates as `dP × (t − t_epoch)` with `t` from the 4th coordinate — **omit the time argument and the transform is a silent identity**, no error; the code asserts non-zero displacement and a test proves the trap. (2) `Transformer.from_crs` never infers plate motion, even with the `ITRF2014@2005.0` syntax — the pipeline must be written by hand (`cart → helmert +drx +dry +drz +t_epoch=<obs epoch> +convention=position_vector → inv cart`, 4th coordinate = target epoch). (3) The Δh estimator must be a local fit, not a disc median (§9 item 10), and the frame step's vertical component must be kept out of the artifact (§9 item 11).

---

### Candidate demo regions (Greenland) — TO BE VALIDATED BY SCIENCE COLLABORATOR

**Selection criteria (the collaborator should validate against these, not just the picks):**
- **Moderate slope** — enough that horizontal misregistration converts to a *visible* vertical Δh, but not steep-margin terrain where retrieval itself is hard (the SE basin shows the largest cross-mission differences precisely because steep slopes complicate retrieval — avoid that confound).
- **Slow flow / stable elevation** — so the 2003→now Δh is dominated by the *plate-motion artifact* you're isolating, not by real dynamic thinning (which would contaminate the "artifact" and break B.9). Avoid fast outlet-glacier trunks.
- **Confirmed GLAS + ICESat-2 coverage** — GLAS is campaign-intermittent; presence over the box must be checked, not assumed.
- **Bonus: published cross-mission ground truth** — gives the collaborator a sanity anchor.

| # | Candidate region | Why it fits | Validation flags for collaborator |
|---|---|---|---|
| 1 | **Near Summit Station** (~72.6°N, 38.5°W, ~3250 m) | Gentle divide-region topography; unmatched ground-truth pedigree (continuous kinematic GPS since 2007, sub-1 cm ICESat-2 bias established); interior = minimal true elevation change | Slope may be *too* gentle to show artifact without heavy exaggeration — check slope is non-trivial over the box; confirm GLAS campaign coverage |
| 2 | **EGIG line, central-west traverse** (~70°N, transecting the west flank) | Classic glaciological traverse; spans interior-to-flank so slope is tunable by box placement; historical measurement density | Pick a box on the moderate-slope flank, not the steep margin; confirm both missions cross it. *Measured (2026-08-25) for the placeholder box (−45, 69.8, −43, 70.2): coverage excellent (38 ATL03 granules Mar–May 2020; GLAS in all 19 campaigns) but slope 0.23° → artifact 0.03 cm vs Δh −1.31 m: **too flat**; move the box downslope (west) toward the margin.* |
| 3 | **K-transect / west flank near Kangerlussuaq region** (~67°N, west margin, *upstream* of the fast zone) | Well-studied ablation/percolation transect; real slope; extensive literature | Stay upstream of dynamic/ablation-dominated zone or real thinning contaminates the artifact; verify flow is slow enough |
| 4 | **NE interior flank, upstream of NEGIS** (~75–77°N, interior side) | N/NE basins show best cross-mission agreement (simpler terrain, well-sampled) — clean artifact isolation; some slope on the flank | Stay well upstream of NEGIS fast flow; confirm slope is enough to show effect |
| 5 | **North-central interior flank** (~76°N, NW-NO basin) | NW/NO basins are the best-agreeing, best-sampled, simplest-terrain regions — lowest confound risk | Lowest confound but possibly lowest slope — validate slope is sufficient; confirm GLAS coverage |

**Recommended default if the collaborator is unavailable:** candidate 2 or 3 (tunable slope on a studied west-flank transect), with a Summit-region box (1) as the "clean interior" fallback if the flank regions show too much real change. **Do not finalize without slope + coverage + slow-flow validation** — a wrong region silently breaks B.9 (the residual collapses for the wrong reason) or B.3 (no visible artifact to remove).

---

### Half-day allocation (guide, not contract)
- **HD1:** Slice 1 — auth + ATL03 extraction + subset validation (1a) + first deck.gl render. Gate: real photons on screen. Fall back to plotly here if deck.gl isn't rendering.
- **HD2:** Slice 2 — GLAS extraction into the same scene. Gate: two clouds, native coords, honest offset visible.
- **HD3:** Slice 3 — live pyproj co-registration + toggle + histogram + labels. Gate: the snap + collapse works.
- **HD4:** Agent-UI wiring, warm-cache rehearsal, honesty-label polish, and **≥ half reserved as buffer** — one of HD1–3 will overrun. If nothing overran, add a second region or improve the animation.

**Collaborator division (if two people):** collaborator owns region selection/validation and Slice-1 subset judgment (1a) and Slice-3 honesty check (B.9) — highest-judgment, lowest-code parts. You own extraction, transform, and widget.

---

## Appendix E — Visual companion

The three stage mockups live in a standalone HTML companion file (`demo-visual-companion.html`), presented alongside this spec. They are interactive (the Slice 3 co-registration toggle animates the snap), which is why they are a separate HTML file rather than static images embedded here.

Contents of the companion:
- **Slice 1 mockup** — one ICESat-2 point cloud; what "real surface reads, not noise" looks like.
- **Slice 2 mockup** — ICESat-2 + GLAS in native coordinates, with the honest offset visible (do not fix).
- **Slice 3 mockup** — the co-registration reveal: the OFF/ON toggle, the snap, the histogram collapse, and the persistent exaggeration label and unresolved-corrections line.

The mockups are 2D schematics standing in for the real 3D deck.gl scene, and their numbers (median Δh, exaggeration factor) are illustrative placeholders — the real values come from the chosen region and the actual pyproj transform. These visuals are for shared understanding of the target; they are **not** part of the Claude Code execution plan (Appendix D), which stays clean.

### Region reference maps (for collaborator validation, not embedded)
No single published figure marks all five candidates, and published figures carry their source papers' copyright — so rather than embed a map, use these real, labeled maps as references during validation (Appendix D). Recommended base: an ESA/NASA Greenland **ice-velocity** map, because the fast-flow zones are visible and are exactly the confound to avoid. The collaborator marks the candidate boxes on a projected basemap in a real GIS tool — which is the validation step already called for.
- **Summit (1) and EGIG (2):** ESA "Greenland, showing EGIG line" map; "Summit Station location in Greenland" location figures. The EGIG line runs central-west through the Summit region, so one map often shows both.
- **K-transect (3):** "The K-transect in west Greenland at 67°N" (NASA background); station maps with S5/S6/S9/S10. Best-documented for the flow confound — shows where it sits relative to the ablation/fast zone to stay upstream of.
- **NE flank (4):** NEGIS location and ESA "ice velocity on Greenland ice sheet, 2014–2024" maps. The bright fast-flow signature is the thing candidate 4 must sit upstream of.
- **N-central (5):** no published figure marks this — it is a location logic (clean interior flank), not an established study site. Honest signal that it is the least "real" of the five; consider dropping in favor of the four that correspond to labeled features.
