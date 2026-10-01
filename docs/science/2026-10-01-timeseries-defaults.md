# Time-series defaults on main (2026-10-01)

**Decision (Kevin):** main adopts the UWG demo branch's time-series science everywhere (API, MCP tools, web UI):
a fixed-effects reference plane (one level per (window, mission) group, one shared slope from the spread within each
group), zeroed at the **ATL06** level by default, **res 8** cells (~530 m), and a cell is low confidence when its
slope-removal error exceeds 1 m. Before: one plane fitted to the reference points (GLAS by default), res 9 (~200 m).

## Before / after on the demo store

Every 'H3 <hex>' scene of the UWG demo store (Jakobshavn Isbrae, West Greenland), one-year windows, min 3 windows.
"main" is `efc5e10`'s own code at its defaults; "new" is `land/uwg-demo` at the new defaults. Run with
`AICESAT_DATA_DIR=data-golden` (an APFS clone of `data-uwg`).

| Scene | Hex | main: ref, res | cells | vouched | low | median m/yr | new: ref, res | cells | vouched | low | median m/yr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1b9136681a | `8506f257fffffff` | GLAS, 9 | 74 | 60 | 14 | -1.5 | ATL06, 8 | 231 | 193 | 38 | -1.19 |
| 63b9c8c660 | `8506f20bfffffff` | GLAS, 9 | 28 | 22 | 6 | -0.12 | ATL06, 8 | 213 | 174 | 39 | -0.03 |
| 69606ee845 | `8506f253fffffff` | GLAS, 9 | 71 | 66 | 5 | -1.17 | ATL06, 8 | 213 | 182 | 31 | -1.08 |
| 804df356f6 | `8506f21bfffffff` | GLAS, 9 | 21 | 17 | 4 | -3.38 | ATL06, 8 | 223 | 203 | 20 | -1.22 |
| ab8ba0e4c3 | `8506f29bfffffff` | GLAS, 9 | 127 | 127 | 0 | -1.94 | ATL06, 8 | 238 | 235 | 3 | -0.91 |
| cabf237539 | `8506f28ffffffff` | GLAS, 9 | 114 | 100 | 14 | -3.6 | ATL06, 8 | 245 | 232 | 13 | -1.97 |
| d570962c26 | `8506f2cffffffff` | GLAS, 9 | 61 | 60 | 1 | -1.21 | ATL06, 8 | 213 | 203 | 10 | -0.73 |
| e9f22ff4c2 | `8506f203fffffff` | GLAS, 9 | 36 | 33 | 3 | -4.85 | ATL06, 8 | 209 | 139 | 70 | -2.6 |
| f66beabec8 | `8506f213fffffff` | GLAS, 9 | 43 | 29 | 14 | -4.23 | ATL06, 8 | 233 | 204 | 29 | -2.01 |
| f671a3d024 | `8506f283fffffff` | GLAS, 9 | 126 | 124 | 2 | -2.99 | ATL06, 8 | 246 | 240 | 6 | -1.96 |
| fb51f2a278 | `8506f247fffffff` | GLAS, 9 | 36 | 21 | 15 | 0.01 | ATL06, 8 | 263 | 187 | 76 | 0.02 |
| fe2b66237c | `8506f293fffffff` | GLAS, 9 | 0 | 0 | 0 | None | ATL06, 8 | 205 | 205 | 0 | -0.92 |

## How to read it

- **Which cells exist changed, not the measured thinning.** Under main a cell qualified only with enough GLAS points
  (it needs at least `_MIN_REF_PTS` reference points), so only cells on ICESat tracks had a series: 0-127 per scene,
  none in `8506f293`. With the ATL06 reference a cell qualifies wherever ICESat-2 measured: about 205-263 per scene.
- **The medians are not comparable across the two columns.** The new population mixes 20-year records (ICESat
  through ICESat-2) with short ones (IceBridge and ICESat-2 only, or ICESat-2 only), so its median trend is smaller in
  magnitude (story hex: -4.23 -> -2.01 m/yr). The old population held only long, GLAS-anchored records. Quote a
  hex's median only with the record lengths behind it; the change map's colour is per cell and does not have this
  problem.
- **More low-confidence cells in some scenes** (e.g. `8506f203`: 3 -> 70, `8506f247`: 15 -> 76) are cells the old
  default never produced. Checked on those two: most are gated by the slope-removal error (59 of 70; 68 of 76),
  with mixed missions (ATL06 alone, ATL06+GLAS, ATL06+ICESSN, all three) and median record spans of 7.5 and 15
  years, so not only short ICESat-2 records. They are grey on the map; whether 1 m is the right gate for them is
  a question for the science review, not settled here.
- **The demo's story cell** (`8806f21187fffff`: -4.17 m/yr, 19 windows, 0.45 medium) is pinned by
  `tests/test_golden.py` and unchanged by this commit, which only moves defaults (explicit parameters are unaffected).
