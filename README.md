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

Tools: `list_regions`, `check_coverage`, `show_photons`, `add_glas`, `coregister`.

## Scripts (no MCP transport needed)

```bash
uv run scripts/check_coverage.py --region egig_west_flank        # granule counts by month / laser campaign
uv run scripts/make_scene.py egig_west_flank --max-granules 8 --glas --coreg   # full pipeline, prewarm
uv run scripts/serve.py                                          # widget server only, for local testing
uv run scripts/probe_glas.py                                     # dump GLAH06 40 Hz dataset layout
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

## Honesty requirements baked into the widget
- Persistent label: horizontal offset exaggerated ×N; true displacement in cm; readout numbers un-exaggerated.
- `unresolved` list visible in both OFF and ON states.
- ON-state wording: "plate-motion artifact removed" — never "missions agree".
- Region selection (slope / coverage / slow flow) is the collaborator's call; `regions.py` holds placeholders.
