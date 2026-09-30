# UWG demo — recording script (2026-09-30)

Everything below runs on the isolated store `data-uwg/` from the `demo/uwg-ladder` worktree.
Honest framing to say once: *"The region was indexed last night and these two areas were fetched once, so the video
isn't three minutes of downloading. Everything you see is computed live from those measurements by the same code."*

## Setup

- Web UI: `AICESAT_PORT=8791 AICESAT_DATA_DIR=/Users/kebe6994/projects/hackathon/aicesat/data-uwg uv run scripts/serve.py`
  (from `.claude/worktrees/uwg-ladder`), open `http://127.0.0.1:8791/`.
- Claude Desktop: merge `deploy/claude-desktop-demo.json` into `claude_desktop_config.json`, restart Desktop.
- Pre-warmed: hex `8506f213fffffff` (lower trunk, scene `f66beabec8`) and hex `8506f20bfffffff` (at the calving
  front, scene `63b9c8c660`).

## 1. Coverage: "where has the surface been measured?" (globe)

- Open on West Greenland. Gold hexes = all three missions measured there: a 20-year record.
- Toggle the strip: ICESat alone (143 passes, 2003–09), IceBridge alone (1,728 flight passes, 2009–19), ICESat-2
  alone (800 passes, 2018–). Point out *when* each flew and *where*: that is the whole idea, place first.
- Hover a hex: passes and years per mission. Zoom in: glaciers, fjords and bays are named (GeoNames).
- Nothing was fetched to draw this: it is the index.

## 2. Change: "what changed here?" (click the lower-trunk hex)

- The change map: every 530 m cell coloured by its trend; grey = the tool will not vouch for it.
  233 cells, 204 reliable, 29 grey; median −2.0 m/yr; 48 cells with a record of 15 years or more.
- Click cell **`8806f2129dfffff`** (69.144°N, 49.002°W). All three missions, 2004–2026:
  ~86 m of thinning through 2016, a ~+18 m rebound 2016–2019 (IceBridge against itself), then ICESat-2 shows
  thinning resume. The rebound matches the published slowdown (Khazendar et al. 2019, *Nature Geoscience*) — cite
  it; the tool does not attribute cause.
- Caveats to say out loud: no inter-mission bias correction, no GIA; the tool says so on every answer.

## 3. The geometry lesson (why the service matters)

- Cell **`8806f21187fffff`** (69.175°N, 49.328°W): the missions sampled different spots inside the cell. Their
  sample centres are up to 448 m apart on ground sloping ~2.4°. Reported change 2004→2026: **−87.6 m**, medium
  confidence (slope-removal error 0.39 m); IceBridge shows **+6.1 m** in 2017→19. Differencing the raw heights would
  say **−105.1 m**; fitting one plane across all eras would say **−53.3 m**. Same data, three answers — the right
  one takes the slope only from each year's own samples, so change between years is never read as slope.
- Next step to mention: the products carry their own slopes (ATL06 `dh_fit_dx/dy`, IceBridge platelet slopes);
  a service that uses them is exactly what an agent should not have to know about. (Not used yet: ATL06's are
  not carried through the index, issue #19.)

## 4. Study (shift-click 2–3 red cells around `8806f21187fffff`, then Study)

- Builds that sub-area with ICESat-2 photons (ATL03) and co-registration; 3-D view; the Δh tab.

## 5. Claude Desktop close

Prompt (anchor the place — the published Jakobshavn coordinate is the calving front, where cells sit on the fjord
walls and read ~0 change, a nice control but not the story):

> *Has the ice on the lower trunk of Jakobshavn Isbræ, around 69.18°N 49.3°W, changed since ICESat? Where is the
> record long enough to tell, and how sure can we be?*

Expected: `survey_coverage` → `elevation_change` (204 reliable of 233; median −2.0 m/yr; the embed opens the change
map) → `show_timeseries` on a cell (chart + where-it-is map + Open in 3D). Follow-up to ask:
*"Why can't I just difference the heights?"* — `show_timeseries` returns `sample_geometry`, so Claude can answer
with this cell's own numbers.
