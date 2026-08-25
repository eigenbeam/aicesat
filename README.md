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

## Scripts (no MCP transport needed)

```bash
uv run scripts/check_coverage.py --region egig_west_flank        # granule counts by month / laser campaign
uv run scripts/make_scene.py egig_west_flank --max-granules 8 --glas --coreg   # full pipeline, prewarm
uv run scripts/serve.py                                          # widget server only, for local testing
uv run scripts/probe_glas.py                                     # dump GLAH06 40 Hz dataset layout
uv run scripts/ingest.py egig_west_flank 8 [--force]             # index + byte-range ingest + lake query, prints access stats
uv run scripts/probe_atl03_chunks.py                             # ATL03 chunk layout / filters / chunk-info throughput
uv run scripts/probe_range_get.py                                # range GET + decode vs h5py, byte-identical check
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
