# UWG demo — script (2026-09-30)

App frozen at `demo/uwg-ladder` `4136554`, isolated store `data-uwg/`. Each beat is **DO** (what to click, what appears) and
**SAY** (a spare draft of the narration; Kevin rewrites it). Every number below was read off the running app at freeze.

Honest framing to say once: *"The region was indexed last night and this area was fetched once, so the video isn't
minutes of downloading. Everything you see is computed live from those measurements by the same code."*

## Pronunciation

English approximations, stressed syllable in caps. The Greenlandic `ll` is a breathy "thl" (like Welsh `ll`); `q` is
a `k` made further back in the throat, so a plain `k` is fine.

| Name | Say | Note |
|---|---|---|
| Jakobshavn Isbræ | YAH-kobs-hahv-en ISS-bray | The glaciology community's usual form. Danish is closer to "YAH-kobs-hown EES-breh" |
| Sermeq Kujalleq | SER-mek koo-YAH-thlek | Official Greenlandic name of Jakobshavn Isbræ (Bjørk et al. 2015) |
| Jakobshavn Isfjord | YAH-kobs-hahv-en ISS-fyord | The fjord in front of the glacier |
| Ilulissat | ee-loo-LEE-saht | The town at the fjord mouth (Jakobshavn is its Danish name) |
| Sermeq Avannarleq | SER-mek ah-VAHN-nahr-lek | The glacier just north |
| ICESat | ICE-sat | Not "I-C-E sat" |
| Khazendar | kah-zen-DAR | If you cite Khazendar et al. 2019 |

## Setup

- Web UI (from `.claude/worktrees/uwg-ladder`), **restart it** so the server matches the frozen code:
  `AICESAT_PORT=8791 AICESAT_DATA_DIR=/Users/kebe6994/projects/hackathon/aicesat/data-uwg uv run scripts/serve.py`,
  then open `http://127.0.0.1:8791/` and hard-reload.
- Claude Desktop: `deploy/claude-desktop-demo.json` merged into `claude_desktop_config.json`; **restart Desktop** so its
  server loads today's code. Not rehearsed since the UI changes: do one dry run of section 5 before recording.
- Hold each view a few seconds longer than feels natural: easier to talk over than to rush.

## Cells used (hex id · centre)

| Where | Hex id | Centre | What it is |
|---|---|---|---|
| Globe, click 1 | `8306f2fffffffff` (res 3) | 69.10°N 49.49°W | West-central Greenland; zooms in |
| Globe, click 2 | `8406f21ffffffff` (res 4) | 69.10°N 49.49°W | The Jakobshavn cluster; zooms in (same centre as click 1) |
| Globe, click 3 | `8506f213fffffff` (res 5) | 69.20°N 49.15°W | Lower trunk of Jakobshavn Isbræ; opens its change map (scene `f66beabec8`) |
| Change map | `8806f21187fffff` (res 8) | 69.1752°N 49.3276°W | Story cell: all three missions, 2004–2026 |
| Change map | `8806f21153fffff` (res 8) | 69.2212°N 49.2212°W | Grey contrast cell: −12 m/yr the tool will not vouch for |

The panels show centres, not ids: hover first, check the centre in the panel, then click.

## 1. The globe: where has the surface been measured?

**DO** Open on the globe (most of the Earth, centred on Greenland). Tick **Hex grid**, then **Coverage shading**.
**SAY** Every hex is a place. Shading is how many granules the three missions left there; nothing was fetched to draw
this, it is the index. Three missions: ICESat 2003–2009, IceBridge 2009–2019, ICESat-2 2018 to now.

**DO** Hover the bright hex on Greenland's west coast: panel reads **69.10°N 49.49°W** (`8306f2fffffffff`), with
GLAH06 / ILATM2 / ATL06 granule counts and *not fully indexed*. Click it.
**SAY** This coast is where all three overlap, so a twenty-year record is possible. ("Not fully indexed": this big hex
reaches past the area indexed for the demo.)

**DO** The view zooms to the Jakobshavn cluster. Optional: untick and re-tick missions in Controls to show each alone.
Click the brightest hex in the middle: panel again **69.10°N 49.49°W** (`8406f21ffffffff`).

**DO** The view zooms to single hexes. Hover the hex **just east (right) of the "Jakobshavn Isbræ" label**: panel reads
**69.20°N 49.15°W** (`8506f213fffffff`). Click it.
**SAY** This is the lower trunk of Jakobshavn Isbræ, officially Sermeq Kujalleq.

## 2. The change map: what changed here?

**DO** The change map opens tilted over shaded terrain (~10 s for the cells). Point at the Legend.
**SAY** Every 530 m cell is coloured by its elevation trend, red down, blue up, clipped at ±4.3 m/yr. Grey means the tool
will not vouch for it: 233 cells, 204 coloured, 29 grey. The lines are the measurements themselves: ICESat footprints in
gold, IceBridge in lavender, ICESat-2 in green.

## 3. The story cell

**DO** Hover cells to show the Time series panel previewing. Hover the cell at the **west edge of the cluster, on the
gold ICESat track**: panel Centre **69.1752°N 49.3276°W** (`8806f21187fffff`). Click it to hold (black-and-white outline).
**SAY** All three missions in one record, 2004 to 2026, 19 one-year windows. ICESat and IceBridge show the surface
falling about 82 m to 2017. IceBridge against itself then shows it rising about 6 m to 2019, which matches the slowdown
Khazendar et al. 2019 reported (the tool does not attribute cause). ICESat-2 then shows thinning resume, about 10 m since 2020. About 88 m in all, −4.2 m/yr.
Confidence medium, 0.45: the breakdown says why, led by how well the slope could be removed (0.39 m).

**SAY** (caveat, once) No inter-mission bias correction and no GIA yet; the panel says so under every record.

## 4. A cell the tool will not vouch for

**DO** Click the story cell again to release it. Hover the grey cell **about 7 km north-east**: Centre
**69.2212°N 49.2212°W** (`8806f21153fffff`). Click to hold.
**SAY** Taken at face value this cell says −12 m a year. But the chart has a 130 m cliff between IceBridge in 2019 and
ICESat-2 in 2020: IceBridge flew over ground inside this cell that ICESat-2 never measured, so removing the slope
between them is uncertain by 37 m. The tool greys it out rather than hand you a number it cannot defend.

**DO** (optional) Hold the story cell again; in **Download**, click **Time series · CSV** (the other formats are marked
planned).

## 5. Claude Desktop close

**DO** Prompt (anchor the place: the published Jakobshavn coordinate is the calving front, where cells sit on the fjord
walls and read ~0 change):

> *Has the ice on the lower trunk of Jakobshavn Isbræ, around 69.18°N 49.3°W, changed since ICESat? Where is the
> record long enough to tell, and how sure can we be?*

Expected: `survey_coverage` (within 50 km: ICESat 143 days, IceBridge 65 flight days, ICESat-2 681 days) →
`elevation_change` (204 reliable of 233; median −2.0 m/yr; the embed opens the change map) → `show_timeseries` on a
cell (chart, where-it-is map, Open in 3D).

**DO** Follow-up: *"Why can't I just difference the heights?"* On the story cell, `show_timeseries` returns
`sample_geometry`: missions sampled spots up to 448 m apart on ground sloping ~2.4°. Differencing raw heights says
**−105.1 m**, one plane across all years says **−53.3 m**, the reported change is **−87.6 m**: same data, three answers.
The right one takes the slope only from each year's own samples, so change between years is never read as slope.
