# aicesat — cross-mission altimetry: the co-registration reveal

Hackweek build of **Demo B** from `cross-mission-altimetry-mcp-spec.md` (Appendix D): an MCP server for Claude
Desktop that pulls real ICESat-2 **ATL03 v007** photons and ICESat/GLAS **GLAH06 r34** shots over a Greenland
box, renders them as a 3D point cloud (deck.gl), and — on a toggle — runs a *live* ITRF2014 + epoch
co-registration (plate motion, ITRF2014-PMM / NOAM) with pyproj, animating the snap and the Δh shift.

What the co-registration does: removes **plate motion** between epochs (horizontal, ~2 cm/yr at Greenland).
What it does **not** do, and says so on screen: ice flow, GIA, geoid/tide, firn compaction, vertical datum.
GLAS heights are converted TOPEX/Poseidon → WGS84 ellipsoid using the product's `d_deltaEllip` (~0.70 m)
and the saturation correction `d_satElevCorr` is applied; that is recorded in every comparability block.

## Setup

```bash
uv sync                       # Python 3.13; deps: mcp>=2, earthaccess, h5py, numpy, scipy, pyproj
uv run pytest                 # offline unit tests (plate-motion magnitude, silent-identity trap, ellipsoid, colocation)
```

Earthdata Login: a bearer token is read from `~/.edl/token.prod` (override with `AICESAT_EDL_FILE`, or set
`EARTHDATA_TOKEN`). The server never prints to stdout (stdio MCP transport); logs go to stderr.

## Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (use absolute paths):

```json
{
  "mcpServers": {
    "aicesat": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/Users/kebe6994/projects/hackathon/aicesat", "run", "aicesat-server"]
    }
  }
}
```

Restart Claude Desktop. The server also serves the widget at `http://127.0.0.1:8765/?scene=<id>`
(port via `AICESAT_PORT`).

## Demo prompts (three slices)

1. **Slice 1** — "Show me ICESat-2 photons over the EGIG west flank region." → `show_photons` → open the widget URL.
2. **Slice 2** — "Add the ICESat-1 GLAS shots to that scene." → `add_glas` (native coordinates; do not "fix" the offset).
3. **Slice 3** — "Is the elevation difference between them real, or a registration artifact? Co-register them." →
   `coregister` → toggle OFF/ON in the widget. Rehearse once so the pyproj result is cached (B.7).

Tools: `list_regions`, `check_coverage`, `show_photons` (region, bbox or polygon), `add_glas`, `coregister`, `open_area_selector`.

## Index + byte-range access + Parquet lake (spec §4–§8, Stage 0)

The ATL03 path no longer opens HDF5 files at query time. `scripts/ingest.py` runs the planner:

1. **Index build, once per granule** (`index.py`): h5py walks each beam's chunk B-trees once and records, for every
   100 000-photon chunk of `lat_ph / lon_ph / h_ph / signal_conf_ph / delta_time`, its byte offset, size, filter
   pipeline and per-chunk `filter_mask`, plus the **H3 res-6 cells** the chunk touches (from the 20 m segment
   geolocation). Stored as Parquet in `data/index/atl03/<granule>.parquet`, schema-versioned.
2. **Tier-1 read** (`access.py`): bbox → overlapping cells → distinct chunk refs (DuckDB over the index) minus chunks
   already materialized (`data/index/meta.duckdb` coverage table) → concurrent HTTPS `Range` GETs (EDL bearer token →
   presigned CloudFront URL, reused) → zlib + byte-unshuffle in numpy (validated byte-identical to h5py).
3. **Materialize** (`lake.py`): every decoded photon goes to the Parquet file of its *own* cell,
   `data/lake/mission=ICESAT2/h3_cell=<id>/<granule>__<beam>.parquet`, with a deterministic `row_id`, provenance
   (granule, beam, photon index, chunk index) and `coreg_lon/lat` materialized at the common epoch.
4. **Query** (`lake.py`): DuckDB `read_parquet(..., hive_partitioning)` with cell + bbox + confidence predicates — the
   residual filter that removes neighbours' photons from straddling chunks.

Measured on the EGIG box, 8 granules: index 110 s (one-time); 120 chunks → 608 requests, 138 MB, 27 s; lake 12 M
photons / 476 MB; query 5.36 M photons in 2.3 s; second run 0 fetches, 2.4 s. The old `earthaccess.open + h5py`
path is kept as `atl03.extract_legacy` for the access-method comparison.

## Techniques adopted from NSIDC's `open-altimetry` ATL24 chunk-map spike

Reviewed `nsidc/open-altimetry@worktree-atl24-sampling-spike` (`tools/atl24-sample`, `docs/plans/atl24-on-demand-reads.md`,
`docs/reference/chunkmap-architecture.tex`). Same problem — HDF5 chunk map captured once, ranged HTTPS via the EDL→303→
CloudFront chain, no HDF5 library on the read path — with a careful nine-arm benchmark. What transferred:
- **Range coalescing with a gap threshold, not one giant GET** (`chunkmap.plan`; their sweep found a 256 KB gap optimum
  and that a single merged request is *slower*): `access.coalesce`, gap 256 KB, span cap 64 MB, gap bytes counted.
- **Concurrency is the bigger lever than coalescing** (their ATL24 arm: 7.3× from 1→32 in flight; coalescing ≤ 25 %):
  groups of (granule, beam) are fetched concurrently on top of per-span threads.
- **Per-chunk bounding boxes for fetch selection** (their precision sweep: two boxes 0.98, H3 r7 0.53, r5 0.12): the
  index now stores each chunk's segment box; the planner prunes chunks whose box misses the query even when their H3
  cell overlaps it. Cells remain the partition key.
- **Retry policy** for NSIDC/CloudFront transient 5xx/429 with `Retry-After` (they measured ~0.27 % transient 500s),
  a 200-instead-of-206 guard, presign cache keyed per granule with re-resolve on 403, one presign per granule under
  concurrency.
- **Correctness gates at index time**: refuse filter ids other than deflate/shuffle and any chunking that splits the
  trailing dimension (they do both); we additionally keep the per-chunk `filter_mask`, which their map omits.
- **Process pool for the index build** (h5py's global lock: threads gave them 0×, processes 5–8×; opens ~2 s dominate).
- **Cross-arm checksum agreement before timing** (`lat_sum`, `h_sum` per arm in `results.json`).

Not adopted (yet): compressed-chunk LRU keyed (granule, dataset, chunk) — our lake persists decoded photons instead;
per-chunk "interest" counts (signal ≥ 4) to answer "nothing here" with zero I/O; NDJSON streaming with display
strides. Postgres bytea/TOAST and varint-delta findings do not transfer (Parquet encodings cover it).

## Performance work (2026-08-25, after the benchmark)
Three changes, each aimed at a measured phase of the cold touch (index build 47 s, fetch 7 s network + 16 s CPU, query 2 s):
1. **Materialize phase → worker processes.** Each (granule, beam) group is fetched, decoded, cell-assigned (`h3ronpy`,
   vectorised), co-registered and written in its own process (`planner._process_group`); the parent presigns all
   granules concurrently first (per-URL locks) and is the single DuckDB writer for coverage marks. The plate-motion
   step has a numpy implementation (`coreg.propagate_numpy`, same maths as PROJ's helmert-with-rates, validated to
   < 0.1 mm against pyproj in tests).
2. **Index build.** 8 worker processes; the HDF5 metadata walk uses a 1 MB block open (earthaccess' default 16 MB blocks
   pulled ~10× the bytes), and the geolocation arrays are read through *their own* chunk map with coalesced range
   GETs instead of the block cache (the NSIDC spike's technique). `scripts/build_index.py` pre-builds the index for a
   whole area/season offline so no query waits on it (spec §6.1: index-build is amortized, not on the query path).
3. **Warm path.** CMR granule lists are cached on disk for 24 h (`coverage.search`; the search was ~1 s of every warm
   query); lake files are written with 64k-row row groups in along-track order so DuckDB prunes row groups by lat/lon
   statistics (`lake.ROW_GROUP_ROWS`; `scripts/relayout_lake.py` rewrites an existing lake once).
Also from the throughput probe: the coalescing gap defaults to 2 MB (`AICESAT_COALESCE_GAP`) because from a remote
link requests under the bandwidth-delay product (~6 MB at 40 MB/s × 150 ms) are latency-bound; in-region the spike's
256 KB is right.

## Access-method comparison (measured 2026-08-25, spec Appendix C.3)

Same bbox (egig_west_flank [-45.0, 69.8, -43.0, 70.2]), the same 8 ATL03 v007 granules (2020-03-01..2020-05-31), the same
target subset (strong beams, land-ice confidence ≥ 3, clipped to the box). Run with `scripts/bench_access.py`; the widget's
*How the data got here* panel shows the same table from `data/bench/results.json`. Measured, not modelled.

| Method | Granules touched (client) | HDF5 structure parses at query time | HTTP requests | MB transferred | Wall-clock s | Photons returned |
|---|---|---|---|---|---|---|
| H3 chunk index + byte-range GETs + Parquet lake, first touch | 7 | 0 (8 at index build, once) | 97 | 100 | 28.9 (15.8 index build + 12.3 fetch/materialize + 0.75 query) | 5,363,896 |
| same, second query (lake warm) | 0 | 0 | 0 | 0 | 0.75 | 5,363,896 |
| earthaccess.open + h5py over fsspec block cache | 8 | 8 | 201 | 3,372 | 154.9 | 5,363,095 |
| download whole granules (8 threads) + local h5py | 8 | 8 | 16 | 22,272 | 556.1 | 5,363,095 |
| SlideRule atl03x (h5coro, public cluster, us-west-2) | 8 | 8 (server-side, opaque) | 1 | 99 | 13.4 | 4,400,711 |
| NSIDC Harmony trajectory subsetter (async) + download | 8 | 8 (server-side, opaque) | 10 | 721 | 127.5 (first run 215, queue variance) | 5,363,095 |

Honest reading (spec C.3 / C.8):
- **Granules opened and structure parses at query time** are the real win: the index path opens nothing and parses
  nothing per query; every other path re-opens and re-parses all 8 granules per query (SlideRule and Harmony do it
  server-side, where it is invisible to the client but still paid).
- **Bytes**: the byte-range path moves 99 MB — 34× less than remote h5py through fsspec's block cache (3.4 GB),
  225× less than whole-granule download (22 GB), 7× less than Harmony's spatially-subsetted-but-all-variables files
  (721 MB). SlideRule returns only 99 MB of geoparquet because the *server* reads the source; what h5coro reads from
  S3 is not exposed, so that row is not a byte comparison at all.
- **Requests**: 99 after coalescing adjacent chunks into spans (92 spans, 1.7 MB of gap bytes) and
  pruning 35 of 120 candidate chunks by their own bounding boxes — down from 608 single-chunk GETs in the
  first measurement. The network part of the cold fetch is now 7.0 s; the rest of the fetch phase is decode,
  per-photon cell assignment, co-registration and Parquet writes (CPU).
- **Wall-clock**: SlideRule still wins outright (13 s) — a cluster in us-west-2 next to the data beats any client on
  a laptop over HTTPS. Our cold time is now 28.9 s (156 s in the first measurement): 15.8 s one-time index build with
  8 worker processes and chunk-map geolocation reads, 12.3 s fetch + materialize in worker processes, 0.75 s
  query; the *repeat* query is 0.75 s with zero traffic, which no server-side path offers. Harmony's time is
  queue-dominated and varied 127–215 s.
- **Subset fidelity**: the index path returns 801 more photons than segment-based slicing because it applies the exact
  per-photon predicate to whole chunks; box pruning changed nothing (same 5,363,896). SlideRule's 4.40 M reflects its
  own defaults (`quality_ph`, segment-based clipping) — a different subset, flagged as such.

## Scripts (no MCP transport needed)

```bash
uv run scripts/check_coverage.py --region egig_west_flank        # granule counts by month / laser campaign
uv run scripts/make_scene.py egig_west_flank --max-granules 8 --glas --coreg   # full pipeline, prewarm
uv run scripts/serve.py                                          # widget server only, for local testing
uv run scripts/probe_glas.py                                     # dump GLAH06 40 Hz dataset layout
uv run scripts/ingest.py egig_west_flank 8 [--force]             # index + byte-range ingest + lake query, prints access stats
uv run scripts/probe_atl03_chunks.py                             # ATL03 chunk layout / filters / chunk-info throughput
uv run scripts/probe_range_get.py                                # range GET + decode vs h5py, byte-identical check
uv run scripts/bench_access.py --methods index,legacy,download,sliderule,harmony   # access-method comparison (network, ~20 min)
uv run scripts/build_index.py --region egig_west_flank [--window START END]        # offline index pre-build, uncapped, 8 workers
uv run scripts/relayout_lake.py                                  # one-off: rewrite lake files with 64k-row row groups
```

Data (`data/cache`, `data/raw`, `data/scenes`) is gitignored; delete it to force a re-fetch.

## How Δh is measured (and why not a median)
For each GLAS shot, the ICESat-2 surface height *at the footprint centre* comes from a local along-track linear fit
of the signal photons within the co-location radius (35 m). A median over the disc is an order statistic: moving
the disc by 30 cm swaps a few edge photons and the median moves by zero or one rank step, so it cannot resolve a
sub-cm slope artifact (it did not, in review). The fit is continuous in position: a shift `ds` along the beam
changes it by `slope_along * ds`, which is the misregistration effect being measured. Only the along-beam
component of the plate-motion shift is observable on a single beam; this is stated in the output.
The per-pair artifact panel uses co-registered positions with *native* heights, so the mm-level vertical part of
the ITRF2008→ITRF2014 frame step is reported separately (`frame_vertical_shift_m`) and never counted as slope
artifact.

## Area selection, imagery, axes, relief
- **Area selector** (`/select.html`, MCP tool `open_area_selector`): a 2-D map on Sentinel-2 cloudless imagery with
  the candidate regions outlined and the lake's materialized H3 cells drawn as hexagons. Drag a box, or click polygon
  vertices and press Close/Enter; *Check coverage* queries CMR; *Build scene* starts a background job (`POST
  /api/extract`, polled at `/api/job/<id>`) that runs the planner, drapes imagery, optionally adds GLAS and
  co-registers, and links to the 3-D widget. Polygons are honoured exactly (point-in-polygon after the lake query;
  coverage counts use the polygon's bounding box).
- **Imagery base layer** (`imagery.py`): EOX Sentinel-2 cloudless 2020 WMTS tiles mosaicked and warped into the
  scene's local EPSG:3413 frame (4096 px wide, cached), draped on the surface mesh as a texture. Licence CC BY-NC-SA
  4.0, attribution on screen. Over the interior accumulation zone the mosaic is featureless white — imagery only
  informs near margins.
- **3-D axes** in the south-west corner with tick marks: x/y in km (local EPSG:3413), z in *true* metres while the
  scene is drawn with the vertical exaggeration shown on the slider (1–50×). A low north-west directional light
  makes relief read as shading; the surface now spans the full box (outside the track hull it is an inverse-distance
  blend of the nearest observed cells, counted in the legend).
- `regions.py` gained `jakobshavn_margin` as a relief/imagery showcase — explicitly **not** a Demo-B region (fast
  ice, real thinning; the co-registration numbers there are contaminated, and the widget says so).

## Visual cues in the widget
- **Surface**: a translucent height field with a faint wireframe, gridded (500 m) from the ICESat-2 photons and
  linearly interpolated across track gaps inside their hull — labelled as interpolated; a depth cue only.
- **Paired shots**: the GLAS shots that have an ICESat-2 pair (the only points behind the histograms) are drawn
  larger and bright; unpaired shots are dimmed once co-registration exists.
- **Ghost**: in the ON state the ICESat-2 cloud's *native* position stays as a grey ghost so the snap has a reference.
- **Orientation**: in-scene scale bar, true-north arrow (EPSG:3413 +y is not north away from 45°W), and a blue
  arrow showing the plate-motion shift direction at the same exaggeration as the clouds, labelled with the true cm.
- The widget server sends `Cache-Control: no-store`, so edits to `widget/` show on reload.

## Honesty requirements baked into the widget
- Persistent label: horizontal offset exaggerated ×N; true displacement in cm; readout numbers un-exaggerated.
- `unresolved` list visible in both OFF and ON states.
- ON-state wording: "plate-motion artifact removed" — never "missions agree".
- Region selection (slope / coverage / slow flow) is the collaborator's call; `regions.py` holds placeholders.
