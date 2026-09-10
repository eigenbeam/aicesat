# Task: adaptive DEM / imagery resolution as the camera zooms

**Status:** scoped, not started (2026-09-09). Depends on the footprint change below, which is written but uncommitted.

## Why

The scene draws its terrain and imagery over the box the user drew, but the point cloud covers the whole H3 cells
the lake was addressed by. Measured on scene `0941a429aa` (Langtang, the 26 Aug 2026 flood source):

| | area | share of returned points |
|---|---|---|
| drawn bbox `85.35 28.20 85.62 28.38` | 529 km² | 28.1% (82,899 of 294,646) |
| res-5 cell footprint actually read | 2,861 km² (5.4×) | 100% |

So 211,747 points hang past the edge of their own terrain. Extending the DEM and imagery to the cell footprint
fixes that, but `dem._grid_local` coarsens the mesh to stay under `MAX_CELLS = 120_000`:

```
bbox            26.5 × 19.9 km   at 100 m →  53,200 cells   fits        → 100 m mesh
cell footprint  56.5 × 50.7 km   at 100 m → 287,528 cells   over cap
                                 at 150 m → 128,029 cells   over cap
                                 at 225 m →  57,061 cells   fits        → 225 m mesh
```

One footprint at 225 m, or two footprints at 100 m. Adaptive resolution is the way to have both: draw the whole
footprint coarse, and refine what the camera is actually looking at.

## What the camera is

Not a free-flying camera. `scene.js` uses deck.gl's **`OrbitView`**:

```js
views: new OrbitView({orbitAxis: 'Z', fovy: 45}),
initialViewState: {target: [cx, cy, 0], rotationX: 35, rotationOrbit: -25, zoom, minZoom: zoom-6, maxZoom: zoom+8},
controller: true,
```

The view state is a **`target`** (a point in local metres), a yaw/pitch pair *around* that target, and a **`zoom`**.
Dragging orbits; scrolling zooms; panning moves the target. So "what am I looking at" is fully determined by
`target` + `zoom`, with `2^zoom ≈ pixels/metre`. There is no view frustum to intersect and no flight path to
predict — the visible ground extent is two numbers and a division.

**There is already a working precedent for camera-driven LOD in this file.** ICESSN swaps dots ↔ platelets on a
zoom threshold:

```js
onViewStateChange: ({viewState}) => {
  if (typeof viewState.zoom === 'number') {
    const was = plateletsNear(); curZoom = viewState.zoom;
    if (plateletsNear() !== was) render();   // re-render only when the threshold FLIPS, not every tick
  }
}
```

That hook, and that discipline — act on threshold crossings, not on every camera event — is what this feature
extends.

## The measurement that shapes the design

**The DEM has 12–28× of unused headroom. The imagery has about 1.4×.** They are not the same problem and should
not get the same solution.

### DEM: real headroom

HMA source is 8 m. `surface_for_frame(frame, extent, z0, cell_m)` already takes an arbitrary extent and target cell
size, and `_grid_local` only ever *coarsens* — so a smaller extent does not refine on its own, the caller must ask
for a finer `cell_m`. Asking gives a genuine ladder:

| view extent | request `cell_m` | actual mesh | vs the 8 m source |
|---|---|---|---|
| 56.5 km (whole footprint) | 100 | 225 m | 28× coarser |
| 14.1 km (quarter) | 25 | 56 m | 7× coarser |
| 7.0 km (eighth) | 25 | 25 m | 3× coarser |
| 3.5 km | 12 | 12 m | 1.5× coarser |

Four levels, each carrying information the one above does not.

Measured cost of a detail fetch, scene `0941a429aa`:

- 14.1 × 12.7 km at `cell_m=100` → **10.2 s** (cold: HMA tiles not yet in GDAL's cache)
- same extent at `cell_m=25` → **1.1 s** (warm)

So the first detail request in a session is slow and the rest are about a second. Acceptable behind a spinner;
not smooth enough to fire on every camera tick, which is why threshold-crossing matters.

### Imagery: almost none

Both sources bottom out at Sentinel-2's native 10 m true colour, so there is far less to win than it looks.

```
EOX WMTS m/px at 28.285 N          DEM/imagery currently rendered at
  z12  33.66                         full footprint  13.80 m/px  (eox z13)
  z13  16.83   ← our MAX_ZOOM cap    drawn bbox       6.47 m/px  (s2 COGs)
  z14   8.41   ← native limit
  z15   4.21   upsampled by EOX
  z16   2.10   upsampled by EOX
```

**`MAX_ZOOM = 13` is our own cap, not EOX's** — I fetched z14/z15/z16 tiles directly and all returned 200 with real
JPEGs. Raising it to 14 buys one genuine doubling. Beyond that EOX is interpolating, and so are we: the current
6.47 m/px `s2` render is *already* upsampled from 10 m source.

I measured eox at a zoomed quarter as "3.45 m/px" and briefly took that as a win. It is not — both fetches came
back at **z13**, so the smaller one is the same tiles stretched into a 4096 px raster. Output raster size is not
resolution. Any imagery LOD work has to be checked against the source zoom level, not the reported `m_per_px`.

`s2` (Sentinel-2 L2A COGs) is worse for this purpose regardless: **274 s** for one 4096 px render of the bbox
extent, out-of-region. That is not a camera response under any design.

## Design

### 1. A quadtree over the scene footprint, for the DEM only

Node id `L/r/c` — level `L`, row, column — over `scene.data_extent(frame)`. Level 0 is the whole footprint; each
level halves the extent and halves the requested `cell_m`. Cap at the level where `cell_m` reaches ~12 m, since
below that the 8 m source stops giving anything back.

Quadtree rather than "extent under the camera" because the node id is the cache key. Free-form extents never
repeat, so nothing is ever a cache hit, and panning re-fetches continuously.

### 2. Server: one endpoint, no new machinery

`GET /api/scene/<id>/detail?level=<L>&row=<r>&col=<c>` → the same surface dict `scene_part(part='surface')`
returns, for that node's extent at that node's `cell_m`.

`surface_for_frame` already does the work; the endpoint computes the node's extent from `data_extent` and calls it.
The existing `DEM_DIR` npz cache keys on the bbox and grid shape, so node results persist across restarts for free.

### 3. Client: threshold-driven, one node at a time

In `onViewStateChange`, debounce ~250 ms after the camera settles, then:

1. visible ground extent ≈ canvas px ÷ `2^zoom`, centred on `target`
2. pick the deepest node whose extent is ≥ ~1.5× the visible extent (hysteresis, so a nudge across a boundary does
   not thrash)
3. if that node id differs from the one displayed, fetch it (cache first) and add a second `SimpleMeshLayer` on top
   of the base mesh, id `surface-detail`
4. drop the detail layer when the camera zooms back out past the level-0 threshold

The base mesh stays loaded throughout, so there is never a hole while a detail node is in flight.

### 4. Imagery: raise the cap, do not build a pyramid

Change `MAX_ZOOM` from 13 to 14 and let the existing `m_per_px → z` calculation do its job. That is the whole of
the available win. Explicitly **not** doing: per-node imagery fetches, `s2` detail tiles, or anything that treats
output raster size as resolution.

If high-resolution Sentinel-2 over a sub-region is wanted later, it should be a deliberate "load detail here"
action with a progress bar, not a camera response — 274 s cannot hide behind a debounce.

## Risks and what would make me stop

- **Two meshes, two datums, one seam.** The detail node samples the same DEM as the base, so heights agree; but the
  detail node's edge cells interpolate differently from the coarse mesh underneath. Expect a visible lip at node
  boundaries. Mitigation: draw the detail mesh with a small vertical bias toward the camera, or fade its edges. If
  the seam reads as terrain, the feature is worse than the problem it solves.
- **The first fetch is 10 s.** If that lands mid-orbit it will feel broken. The spinner has to be obvious, and the
  base mesh must stay up.
- **`MAX_CELLS` interacts with this.** A detail node at level 3 requesting 12 m over 7 km is 340k cells and will be
  silently coarsened back. Either the endpoint sizes nodes so the cap is never hit, or the cap moves — and if it
  moves, the scene file grows (287,528 vertices ≈ 2.5 MB, from 472 KB today).
- **Nothing here helps a scene with no DEM.** Over ocean or outside HMA/Copernicus the base surface is already
  absent; adaptive resolution of nothing is still nothing.

## Phasing

1. **Land the footprint change first** (below). Without it there is nothing to zoom into — the point cloud and the
   terrain still disagree.
2. Raise `MAX_ZOOM` to 14. One line, one measurable doubling, independent of everything else.
3. DEM quadtree: endpoint + cache, driven by a hand-written test that requests known node ids. No UI yet.
4. Client: `onViewStateChange` → node selection → detail layer. This is where the seam risk shows up.
5. Decide on `MAX_CELLS` once real node sizes exist, not before.

## Prerequisite, already written but uncommitted

`scene.data_extent(frame, polygon, res=5)` computes the local extent of the res-5 cells a query actually reads, and
`add_imagery` / `set_surface` now call it instead of `bbox_extent`. Effect on scene `0941a429aa`: footprint
26.5 × 19.9 km → 56.5 × 50.7 km, DEM mesh 100 m → 225 m, imagery 6.47 → 13.80 m/px.

That trade — one footprint but coarser — is the whole reason this task exists. It is worth landing on its own,
because a point cloud that extends past its terrain is a worse fault than a coarse mesh.

## Verification

- **Node coverage:** every node id at level `L` tiles `data_extent` exactly, no gaps or overlaps. Pure arithmetic,
  testable without a DEM.
- **Refinement is real:** a level-2 node's mesh must resolve features the level-0 mesh does not. Compare the two at
  the same points against ATL06 — the detail node should show a lower RMSE, the same way HMA showed 11.5 m against
  Copernicus's 29.8 m. If it does not, the level is not buying anything and the ladder should stop shallower.
- **Cache actually hits:** the second request for a node id issues no network read. Assert on the read count, not
  on wall time.
- **The camera does not thrash:** orbiting at a fixed zoom must not change the selected node. A test on the node
  selection function with a synthetic sequence of view states, no browser needed.
- **In the browser:** the canvas amber-pixel technique used for the markers works here too — sample the rendered
  canvas before and after a zoom and confirm the detail layer's vertices actually changed the surface.
