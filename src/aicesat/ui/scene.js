AICESAT.SceneView = class {
  constructor(root, api, back) {
    root.innerHTML = '<div id="deck" class="deck"></div>\n<div id="progress" class="panel" data-title="build progress" hidden>\n  <div class="sl-head"><span id="slSpin" class="spinner"></span><span id="slTitle">Building scene…</span><span id="slElapsed" class="sl-elapsed"></span></div>\n  <div id="progRows" class="prog-rows"></div>\n  <div id="slNow" class="prog-now"></div>\n</div>\n<div id="navhint">drag to orbit · scroll to zoom</div>\n<div id="exagWarn" class="exag-badge" hidden></div>\n<div id="controls" class="panel" data-title="controls">\n  <div class="ctl-group">\n    <div class="ctl-head">Missions <span class="ctl-note">show / hide</span></div>\n    <div id="missionToggles" class="misrows"></div>\n  </div>\n  <div class="ctl-group">\n    <label class="ctl-row"><input id="demOn" type="checkbox" checked> DEM base surface</label>\n    <label class="ctl-row"><input id="imagery" type="checkbox" disabled> Show satellite imagery</label>\n    <div id="imageryStatus" class="ctl-info"></div>\n  </div>\n  <div class="ctl-group">\n    <label class="ctl-row"><span class="ctl-lbl">Vertical ×<b id="zexagVal">1</b></span><input id="zexag" type="range" min="1" max="10" step="1" value="1" class="ctl-range"></label>\n    <label class="ctl-row"><span class="ctl-lbl">Points ×<b id="ptSizeVal">1</b></span><input id="ptSize" type="range" min="0.4" max="3" step="0.1" value="1" class="ctl-range"></label>\n  </div>\n  <button id="benchBtn" hidden>How the data got here</button>\n</div>\n<div id="attrib" style="position:absolute; bottom:4px; right:396px; font-size:10px; color:var(--muted)"></div>\n<div id="bench" class="panel" data-title="access comparison" hidden style="top:112px; left:12px; width:440px; max-height:calc(100% - 200px); overflow:auto">\n  <h2 style="font-size:13px;margin:0 0 4px">How the data got here — access-method comparison</h2>\n  <div class="small" id="benchMeta"></div>\n  <table id="benchTable" style="width:100%;border-collapse:collapse;font-size:11.5px;margin-top:6px"></table>\n  <div class="small" style="margin-top:6px">Measured on the same area, granules, and photons across every method. The real wins are how many files get opened and parsed — not just bytes moved.</div>\n  <button id="benchClose" style="margin-top:6px">hide</button>\n</div>\n<div id="stats" class="panel" data-title="Δh panels" hidden>\n  <h2>Height difference Δh — ICESat-2 minus ICESat-1</h2>\n  <canvas class="hist" id="histDh"></canvas>\n  <div class="hist-cap">← lower · Δh (metres) · higher → · bar height = number of co-located pairs · dashed line = 0</div>\n  <div class="readout" id="readout1"></div>\n  <h2 style="margin-top:10px">Effect of the plate-motion correction on Δh</h2>\n  <canvas class="hist" id="histArt"></canvas>\n  <div class="hist-cap">how much re-aligning the footprints changes each pair (metres)</div>\n  <div class="readout" id="readout2"></div>\n  <div id="unresolved"></div>\n</div>\n<div id="tspanel" class="panel" data-title="time series">\n  <h2>Elevation time series</h2>\n  <div class="small tsintro">Cells observed across time; height plotted there as a residual about a local reference plane (so surface slope is removed, not mistaken for change).</div>\n  <label class="ctl-row"><span class="ctl-lbl">Cell size</span><input id="tsRes" type="range" min="7" max="11" step="1" value="9" class="ctl-range"><b id="tsResLbl" class="ctl-val"></b></label>\n  <label class="ctl-row"><span class="ctl-lbl">Time window</span><input id="tsDt" type="range" min="0.25" max="3" step="0.25" value="1" class="ctl-range"><b id="tsDtLbl" class="ctl-val"></b></label>\n  <div class="ctl-row tsrefrow"><span class="ctl-lbl">Reference</span><span id="tsRef" class="tsref"></span></div>\n  <div class="row"><button id="tsFind">Find candidates</button><span id="tsStatus" class="small"></span></div>\n  <div id="tsList" class="tslist"></div>\n  <canvas id="tsChart" class="tschart" hidden></canvas>\n  <div id="tsReadout" class="small"></div>\n  <div id="tsConf" class="small"></div>\n  <div id="tsCaveat" class="small tscaveat" hidden>No inter-campaign / inter-sensor bias adjustment yet (coming later).</div>\n</div>';
/* Demo B widget: two point clouds, OFF/ON co-registration toggle, Δh histograms, honesty labels,
   plus visual cues: DEM surface, paired-shot highlighting.
   Corrections (plate motion, …) are applied to the Δh computation via checkboxes; the true positional shift is
   sub-pixel, so the 3-D clouds do not visibly move (no exaggeration, no animated snap, no shift arrow). */
const {Deck, OrbitView, PointCloudLayer, PathLayer, TextLayer, SimpleMeshLayer, LightingEffect, AmbientLight, DirectionalLight} = deck;
let params = new URLSearchParams(); let sceneId = null;
let Z_EXAG = 1;
let SHOW_IMAGERY = false;
let SHOW_SURFACE = true;   // DEM base surface on/off (scene controls)
let IMG_VER = 0;           // bumps on an imagery re-fetch so the draped texture URL changes and reloads

// ICESSN dots<->platelets level-of-detail scalars (declared before the Deck so its onViewStateChange closure is safe).
// curZoom tracks the OrbitView zoom (2^zoom ≈ pixels/metre); a facet spans PLATELET_M · PT_SCALE · 2^zoom screen px.
let PT_SCALE = 1;          // user "Points ×" multiplier (scales both dots and platelet facets)
const PLATELET_M = 42;     // drawn facet side (m); ICESSN nadir platelets are ~tens of m along/across track
const PX_PLATELET = 6;     // switch dots -> platelets once a facet spans at least this many screen px
let curZoom = -6;
const plateletsNear = () => PLATELET_M * PT_SCALE * Math.pow(2, curZoom) >= PX_PLATELET;

let scene = null, coreg = null, bounds = null, meshOk = true;
const adj = {plate_motion: true, gia: true};   // which corrections are applied (toggled in the controls)
const $ = id => root.querySelector('#' + id);
const PAIR_RING = [220, 200, 150, 180];
let SHOW_PAIRS = true;
// The three missions, as users know them (the legend doubles as show/hide controls). Keyed by the internal series
// name; ICESat-2 appears as two products (ATL03 photons + ATL06 land ice).
const MISSIONS = {
  GLAS:    {name: 'ICESat-1 (GLAS)',          epoch: '2003–2009', gloss: 'ICESat / GLAS laser-altimeter surface heights'},
  ICESSN:  {name: 'IceBridge (ATM)',          epoch: '2009–2019', gloss: 'Operation IceBridge airborne ATM elevations (ICESSN)'},
  ICESAT2: {name: 'ICESat-2 photons (ATL03)', epoch: '2018–',     gloss: 'ICESat-2 ATL03 individual signal photons'},
  ATL06:   {name: 'ICESat-2 land ice (ATL06)', epoch: '2018–',    gloss: 'ICESat-2 ATL06 land-ice height segments'},
};
const MISSION_ORDER = ['GLAS', 'ICESSN', 'ICESAT2', 'ATL06'];   // chronological
const visible = {};   // mission key -> shown; initialised per scene (all on)
// Display palette (Okabe-Ito subset): distinct, colour-blind-friendly, and high-contrast against the grey-blue DEM.
// Applied everywhere (clouds, legend swatches, time-series points) so it also recolours scenes built before this palette.
// Okabe-Ito blue/yellow/green (colour-blind-safe by construction): the dense ATL06 becomes a receding blue base,
// GLAS a bright yellow, IceBridge green; ATL03 (rarely shown) a distinct vermillion.
// Punchy trio on the (now charcoal) DEM: GLAS yellow, IceBridge vermillion, ATL06 blue — high contrast + colour-blind
// distinct (yellow/blue is the safe axis; vermillion is Okabe-Ito's CVD-safe red). ATL03 (rare) takes green.
const MISSION_COLORS = {GLAS: [240, 228, 66], ICESSN: [230, 75, 60], ATL06: [40, 140, 225], ICESAT2: [40, 200, 120]};
const colorOf = m => MISSION_COLORS[m] || (scene && scene.series[m] && scene.series[m].color) || [200, 200, 210];

const deckgl = new Deck({
  parent: $('deck'),
  width: '100%', height: '100%',
  onError: e => { console.error('[aicesat] deck error', e && e.message); if (/mesh/i.test(String(e && e.message))) { meshOk = false; render(); } },
  onLoad: () => console.log('[aicesat] deck loaded'),
  views: new OrbitView({orbitAxis: 'Z', fovy: 45}),
  // low-angle directional light from the north-west so relief reads as shading (hillshade-like)
  effects: [new LightingEffect({ambient: new AmbientLight({color: [255, 255, 255], intensity: 0.9}),
                                sun: new DirectionalLight({color: [255, 250, 235], intensity: 1.6, direction: [-1, 1, -0.6]})})],
  initialViewState: {target: [0, 0, 0], rotationX: 35, rotationOrbit: -25, zoom: -6, minZoom: -12, maxZoom: 6},
  controller: true,
  // track zoom for the ICESSN dots<->platelets level-of-detail; re-render only when the threshold flips (not every tick)
  onViewStateChange: ({viewState}) => {
    if (typeof viewState.zoom === 'number') { const was = plateletsNear(); curZoom = viewState.zoom; if (plateletsNear() !== was) render(); }
  },
  layers: [],
});

const indices = n => { const a = new Array(n); for (let i = 0; i < n; i++) a[i] = i; return a; };
const ease = t => t < .5 ? 2 * t * t : -1 + (4 - 2 * t) * t;

// ---------------------------------------------------------------- point clouds
function cloudLayer(id, flat, color, size, opts = {}) {
  return new PointCloudLayer(Object.assign({
    id, data: indices(flat.length / 3),
    getPosition: i => [flat[3 * i], flat[3 * i + 1], flat[3 * i + 2] * Z_EXAG],
    getColor: color, pointSize: size, sizeUnits: 'pixels', updateTriggers: {getPosition: Z_EXAG},
  }, opts));
}

// Rough ground-footprint radius per mission (metres). Points are drawn in WORLD units and clamped in pixels, so at
// scene overview they're crisp small dots (not blobs) and grow toward the true footprint as you zoom in — a physical
// cue, not a precise footprint. The pixel floor stops them vanishing when zoomed out; the cap stops fat blobs.
const FOOTPRINT_M = {GLAS: 35, ICESSN: 12, ATL06: 16, ICESAT2: 8};

// --- ICESSN platelets: the ILATM2 nadir product IS a plane fit per short along-track segment, so each measurement
// carries its own surface slope. We draw it as the geometric primitive it is — a small facet tilted to its fitted
// plane — but only when it's near enough to read as a tilted quad (plateletsNear, above); at overview zoom a facet is
// sub-pixel, so we fall back to the same clamped dots every other mission uses.
const LIGHT = (() => { const v = [-1, 1, 2], l = Math.hypot(...v); return v.map(c => c / l); })();  // NW-and-above, for facet shading
const usePlatelets = (m, s) => m === 'ICESSN' && s.slopes && s.slopes.length && plateletsNear();
// Vertical exaggeration as a GPU model matrix (column-major): scales z only, so changing it never re-walks or
// re-uploads a multi-million-point position buffer.
const zExagMatrix = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, Z_EXAG, 0, 0, 0, 0, 1];

function plateletLayer(m, s) {
  const src = s.positions, sl = s.slopes, base = colorOf(m);
  const fr = scene.frame, E = fr.east_xy || [1, 0], N = fr.north_xy || [0, 1];
  const half = PLATELET_M * PT_SCALE / 2;
  const corners = [[half, half], [half, -half], [-half, -half], [-half, half]];   // (east, north) offsets, CCW
  return new deck.SolidPolygonLayer({
    id: 'plat-' + m, data: indices(src.length / 3),
    modelMatrix: zExagMatrix(),   // z scaling on the GPU (matches the point layers); vertices stay in true metres
    getPolygon: i => {
      const cx = src[3 * i], cy = src[3 * i + 1], cz = src[3 * i + 2], sn = sl[2 * i], we = sl[2 * i + 1];
      return corners.map(([de, dn]) => {
        const dx = de * E[0] + dn * N[0], dy = de * E[1] + dn * N[1], dz = we * de + sn * dn;   // the platelet's fitted plane
        return [cx + dx, cy + dy, cz + dz];
      });
    },
    // manual hillshade so the tilt reads even where SolidPolygonLayer's flat faces get uniform lighting: brightness
    // from the facet normal (-we, -sn, 1) against a fixed NW-above light.
    getFillColor: i => {
      const we = sl[2 * i], sn = sl[2 * i + 1], nl = Math.hypot(we, sn, 1);
      const b = Math.max(0.4, Math.min(1, 0.62 + 0.5 * ((-we * LIGHT[0] - sn * LIGHT[1] + LIGHT[2]) / nl)));
      return [base[0] * b, base[1] * b, base[2] * b, 235];
    },
    _normalize: false,   // simple convex quads
    updateTriggers: {getPolygon: PT_SCALE, getFillColor: base},   // Z_EXAG now rides the model matrix, no re-tessellation
  });
}

function cloudLayers() {
  const out = [];
  for (const [m, s] of Object.entries(scene.series)) {
    if (visible[m] === false) continue;   // per-mission show/hide (legend toggles)
    const src = s.positions;   // measured photons/shots as delivered; corrections are sub-pixel here (see Δh panel)
    if (!src || !src.length) continue;    // announced by the metadata poll, not yet delivered by the stream
    const paired = (coreg && coreg.pair_display_indices && coreg.pair_display_indices[m]) ? new Set(coreg.pair_display_indices[m]) : null;
    if (paired && SHOW_PAIRS) {  // co-located shots: a thin pale ring UNDER the point (subtle marker, not a blob)
      out.push(new deck.ScatterplotLayer({
        id: 'paired-' + m, data: [...paired],
        getPosition: i => [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG],
        getRadius: (FOOTPRINT_M[m] || 12) * 1.7, radiusUnits: 'meters', radiusMinPixels: 3, radiusMaxPixels: 12,
        stroked: true, filled: false, getLineColor: PAIR_RING, lineWidthUnits: 'pixels', getLineWidth: 1.2,
        billboard: true, updateTriggers: {getPosition: Z_EXAG},
      }));
    }
    if (usePlatelets(m, s)) { out.push(plateletLayer(m, s)); continue; }   // near enough -> tilted facets, not dots
    // Binary attribute path: hand deck.gl the Float32Array directly instead of {data: indices(n), getPosition: fn}.
    // The accessor form allocated an n-element index array AND called a JS closure per point on every render — for a
    // ~2M-point mission that dominated the frame. Vertical exaggeration is applied on the GPU via a model matrix, so
    // changing it costs no re-upload and no re-walk of the buffer.
    out.push(new deck.ScatterplotLayer({
      id: 'pc-' + m,
      data: {length: src.length / 3, attributes: {getPosition: {value: src, size: 3}}},
      modelMatrix: zExagMatrix(),
      getFillColor: colorOf(m), getRadius: (FOOTPRINT_M[m] || 14) * PT_SCALE, radiusUnits: 'meters',
      radiusMinPixels: 1, radiusMaxPixels: 6, billboard: true,
      updateTriggers: {getRadius: PT_SCALE},
    }));
  }
  return out;
}

// ---------------------------------------------------------------- surface (depth cue)
function surfaceLayers() {
  // `z` arrives on the stream, but the grid metadata arrives on the metadata poll — so there is a window where the
  // surface exists and its values do not. surfaceExtent() has always guarded this; this did not, and indexing the
  // absent z threw inside render() and killed the poll loop. The scene doc is now assembled from two sources with
  // different arrival times: every consumer of a bulk array must tolerate it not being there yet.
  const g = scene.surface; if (!g || !g.z) return [];
  const img = SHOW_IMAGERY && scene.imagery;
  if (!SHOW_SURFACE && !img) return [];   // nothing to draw — imagery needs the mesh to drape on, so it keeps the mesh alive
  const {x0, y0, cell, nx, ny, z} = g;
  const P = (i, j) => [x0 + i * cell, y0 + j * cell, z[j * nx + i] * Z_EXAG];
  const layers = [];
  if (meshOk) {
    const vid = new Int32Array(nx * ny).fill(-1); const pos = []; let nv = 0;
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) if (z[j * nx + i] != null) { vid[j * nx + i] = nv++; pos.push(...P(i, j)); }
    const idx = [];
    for (let j = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++) {
      const a = vid[j * nx + i], b = vid[j * nx + i + 1], c = vid[(j + 1) * nx + i], d = vid[(j + 1) * nx + i + 1];
      if (a >= 0 && b >= 0 && c >= 0 && d >= 0) idx.push(a, b, c, b, d, c);
    }
    if (idx.length) {
      const positions = new Float32Array(pos), normals = new Float32Array(pos.length);
      for (let k = 0; k < idx.length; k += 3) {           // accumulate face normals -> smooth shading
        const [a, b, c] = [idx[k], idx[k + 1], idx[k + 2]];
        const ax = positions[3*a], ay = positions[3*a+1], az = positions[3*a+2];
        const ux = positions[3*b]-ax, uy = positions[3*b+1]-ay, uz = positions[3*b+2]-az;
        const vx = positions[3*c]-ax, vy = positions[3*c+1]-ay, vz = positions[3*c+2]-az;
        const n = [uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx];
        for (const q of [a, b, c]) { normals[3*q] += n[0]; normals[3*q+1] += n[1]; normals[3*q+2] += n[2]; }
      }
      for (let q = 0; q < normals.length; q += 3) { const l = Math.hypot(normals[q], normals[q+1], normals[q+2]) || 1; normals[q] /= l; normals[q+1] /= l; normals[q+2] /= l; }
      // texCoords must ALWAYS exist (SimpleMeshLayer reads the attribute even with no texture); map to the
      // imagery extent when draping, else to the surface extent (unused, but a valid attribute).
      const ex = img ? img : {x0, y0, x1: x0 + nx * cell, y1: y0 + ny * cell};
      const texCoords = new Float32Array((pos.length / 3) * 2);
      for (let q = 0, t = 0; q < pos.length; q += 3, t += 2) {
        texCoords[t] = (pos[q] - ex.x0) / (ex.x1 - ex.x0);
        texCoords[t + 1] = 1 - (pos[q + 1] - ex.y0) / (ex.y1 - ex.y0);
      }
      const attrs = {positions: {value: positions, size: 3}, normals: {value: normals, size: 3}, texCoords: {value: texCoords, size: 2}};
      const meshProps = {
        id: 'surface-mesh' + (img ? '-img' : ''), data: [{}],
        mesh: {attributes: attrs, indices: {value: new Uint32Array(idx)}},
        getPosition: () => [0, 0, 0], getColor: img ? [255, 255, 255, 235] : [76, 84, 100, 205],   // charcoal hillshade so the mission colours pop (was light blue-grey)
        material: {ambient: 0.5, diffuse: 0.85, shininess: 12, specularColor: [30, 30, 30]},
        updateTriggers: {getPosition: Z_EXAG},
      };
      if (img) meshProps.texture = api.imageryUrl(sceneId, IMG_VER);   // omit the key entirely when not draping; IMG_VER busts the cache after a source change
      else meshProps.parameters = {depthWriteEnabled: false};    // translucent surface: don't occlude points behind it
      layers.push(new SimpleMeshLayer(meshProps));
    }
  }
  // faint wireframe (rows + columns) — part of the DEM base look and the fallback if the mesh fails; only with DEM on
  if (SHOW_SURFACE) {
    const paths = [];
    const run = (len, other, at) => { let cur = []; for (let k = 0; k < len; k++) { const [i, j] = at(k); if (z[j * nx + i] == null) { if (cur.length > 1) paths.push(cur); cur = []; } else cur.push(P(i, j)); } if (cur.length > 1) paths.push(cur); };
    for (let j = 0; j < ny; j += 2) run(nx, j, i => [i, j]);
    for (let i = 0; i < nx; i += 2) run(ny, i, j => [i, j]);
    layers.push(new PathLayer({id: 'surface-wire', data: paths, getPath: d => d, getColor: [200, 205, 220, 35], getWidth: 1, widthUnits: 'pixels'}));
  }
  return layers;
}

// ---------------------------------------------------------------- orientation cues
function niceStep(len) { const t = len / 4, p = Math.pow(10, Math.floor(Math.log10(t))); return [1, 2, 5, 10].map(m => m * p).reduce((a, b) => Math.abs(b - t) < Math.abs(a - t) ? b : a); }
// Local-metre bounds of the DEM base surface, so the axes anchor to the surface's corner (a stable frame that covers
// the whole scene) rather than wherever the point cloud happens to fall. Falls back to the data bounds when no DEM.
function surfaceExtent() {
  const g = scene && scene.surface; if (!g || g.z == null) return null;
  let minz = Infinity, maxz = -Infinity;
  for (const v of g.z) if (v != null) { if (v < minz) minz = v; if (v > maxz) maxz = v; }
  if (minz > maxz) { minz = 0; maxz = 0; }
  return {minx: g.x0, maxx: g.x0 + (g.nx - 1) * g.cell, miny: g.y0, maxy: g.y0 + (g.ny - 1) * g.cell, minz, maxz};
}
function axesLayers() {
  const b = surfaceExtent() || bounds;
  if (!b) return [];
  const {minx, maxx, miny, maxy, minz, maxz} = b;
  const span = Math.max(maxx - minx, maxy - miny);
  const o = [minx - 0.22 * span, miny - 0.06 * span, minz * Z_EXAG];       // corner: west of the data, level with the cue row
  const stepXY = niceStep(span / 4), Lxy = stepXY * 2, zTrue = Math.max(maxz - minz, 1), Lz = niceStep(zTrue) * 2;
  const paths = [], texts = [];
  const axis = (dir, len, color, label, tickStep, fmt, scale) => {
    const end = [o[0] + dir[0] * len * scale, o[1] + dir[1] * len * scale, o[2] + dir[2] * len * scale];
    paths.push({p: [o, end], c: color, w: 2.5});
    const tk = 0.012 * span;
    // tick direction: perpendicular to the axis, pointing away from the other axes (west for y and z, south for x)
    const tdir = dir[2] ? [-1, 0, 0] : (dir[0] ? [0, -1, 0] : [-1, 0, 0]);
    for (let v = tickStep; v <= len + 1e-9; v += tickStep) {
      const pt = [o[0] + dir[0] * v * scale, o[1] + dir[1] * v * scale, o[2] + dir[2] * v * scale];
      const t1 = [pt[0] + tdir[0] * tk, pt[1] + tdir[1] * tk, pt[2]];
      paths.push({p: [pt, t1], c: color, w: 1.5});
      texts.push({position: [t1[0] + tdir[0] * tk * 1.2, t1[1] + tdir[1] * tk * 1.2, t1[2]], text: fmt(v), color, size: 11,
                  anchor: tdir[0] < 0 ? 'end' : 'middle'});
    }
    const lab = [end[0] + dir[0] * 0.02 * span + (dir[2] ? -tk * 2.5 : 0), end[1] + dir[1] * 0.02 * span, end[2] + (dir[2] ? 0.02 * span : 0)];
    texts.push({position: lab, text: label, color, size: 13, anchor: dir[2] ? 'end' : (dir[0] ? 'start' : 'middle')});
  };
  const km = v => `${(v / 1000).toFixed(v >= 1000 ? 0 : 1)} km`;
  axis([1, 0, 0], Lxy, [235, 120, 120], 'x', stepXY, km, 1);
  axis([0, 1, 0], Lxy, [120, 220, 140], 'y', stepXY, km, 1);
  axis([0, 0, 1], Lz, [140, 170, 255], 'z', niceStep(zTrue), v => `${v.toFixed(0)} m`, Z_EXAG);
  return [
    new PathLayer({id: 'axes', data: paths, getPath: d => d.p, getColor: d => d.c, getWidth: d => d.w, widthUnits: 'pixels', updateTriggers: {getPath: Z_EXAG}}),
    new TextLayer({id: 'axes-text', data: texts, getPosition: d => d.position, getText: d => d.text, getColor: d => d.color, getSize: d => d.size, getTextAnchor: d => d.anchor || 'middle',
      sizeUnits: 'pixels', billboard: true, fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto',
      background: true, getBackgroundColor: [20, 20, 26, 170], backgroundPadding: [3, 1], updateTriggers: {getPosition: Z_EXAG}}),
  ];
}

// ---------------------------------------------------------------- render / view
function render() {
  if (!scene) return;
  deckgl.setProps({layers: [...surfaceLayers(), ...cloudLayers(), ...candidateLayers(), ...axesLayers()]});
}

function fitView() {
  // Scan each mission's buffer in place — positions are Float32Arrays (incremental transport), and flat-mapping them
  // into one JS array would copy millions of floats per call.
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9, minz = 1e9, maxz = -1e9, n = 0;
  for (const s of Object.values(scene.series)) {
    const p = s.positions; if (!p || !p.length) continue;
    n += p.length;
    for (let i = 0; i < p.length; i += 3) { minx = Math.min(minx, p[i]); maxx = Math.max(maxx, p[i]); miny = Math.min(miny, p[i + 1]); maxy = Math.max(maxy, p[i + 1]); minz = Math.min(minz, p[i + 2]); maxz = Math.max(maxz, p[i + 2]); }
  }
  if (!n) return false;
  // Check the BOUNDS, not just the derived zoom. `span || 1` launders a NaN into a plausible 1 (NaN is falsy), so a
  // finite zoom proves nothing — the NaN then rides in on `target` and deck.gl reports only
  // "@math.gl/web-mercator: assertion failed", pointing nowhere near the cause.
  if (![minx, maxx, miny, maxy, minz, maxz].every(Number.isFinite)) return false;
  bounds = {minx, maxx, miny, maxy, minz, maxz};
  const span = Math.max(maxx - minx, maxy - miny) || 1;
  const px = Math.min(root.clientWidth, root.clientHeight);
  const zoom = Math.log2(px / (span * 1.25));
  // The stream paints within ~100 ms of the view opening, which can be before the canvas has been laid out. px of 0
  // makes zoom -Infinity, and deck.gl answers that with an opaque "@math.gl/web-mercator: assertion failed". Report
  // the miss so the caller leaves didFit alone and tries again on the next frame of data.
  if (!Number.isFinite(zoom)) return false;
  curZoom = zoom;   // seed the LOD zoom so the first render picks dots-vs-platelets correctly before any interaction
  deckgl.setProps({initialViewState: {target: [(minx + maxx) / 2, (miny + maxy) / 2, 0], rotationX: 35, rotationOrbit: -25, zoom, minZoom: zoom - 6, maxZoom: zoom + 8}});
  return true;
}

// ---------------------------------------------------------------- histograms / readouts
function drawHist(canvas, values, {color, refLine = 0, range} = {}) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * devicePixelRatio, H = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  if (!values || !values.length) { ctx.fillStyle = '#777'; ctx.font = `${12 * devicePixelRatio}px sans-serif`; ctx.fillText('no pairs', 10, 20); return; }
  const [lo, hi] = range, nb = 40, counts = new Array(nb).fill(0);
  for (const v of values) { const b = Math.floor((v - lo) / (hi - lo) * nb); if (b >= 0 && b < nb) counts[b]++; }
  const max = Math.max(...counts) || 1, bw = W / nb;
  ctx.fillStyle = color;
  counts.forEach((c, i) => { const h = c / max * (H - 18 * devicePixelRatio); ctx.fillRect(i * bw + 1, H - h - 14 * devicePixelRatio, bw - 2, h); });
  const x0 = (refLine - lo) / (hi - lo) * W;
  ctx.strokeStyle = '#ddd'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x0, 0); ctx.lineTo(x0, H - 14 * devicePixelRatio); ctx.stroke(); ctx.setLineDash([]);
  if (refLine >= lo && refLine <= hi) { ctx.fillStyle = '#d6d6d6'; ctx.font = `${11 * devicePixelRatio}px sans-serif`; ctx.fillText('0', x0 + 3 * devicePixelRatio, 12 * devicePixelRatio); }
  ctx.fillStyle = '#aaa'; ctx.font = `${12 * devicePixelRatio}px sans-serif`;
  ctx.fillText(`${lo.toFixed(2)} m`, 2, H - 2); const t = `${hi.toFixed(2)} m`; ctx.fillText(t, W - ctx.measureText(t).width - 2, H - 2);
}

function updateStats() {
  if (!coreg) { $('stats').hidden = true; return; }
  $('stats').hidden = false;
  const on = adj.plate_motion;
  const g = (adj.gia && coreg.gia) ? coreg.gia.dh_shift_m : 0;   // GIA is an additive Δh shift (near-constant over a scene)
  const base = on ? coreg.dh_coreg : coreg.dh_native, st0 = on ? coreg.stats.coreg : coreg.stats.native;
  const dh = g ? base.map(v => v + g) : base;
  const st = g ? {...st0, median: st0.median + g, mean: st0.mean + g} : st0;   // shift only recentres; MAD unchanged
  drawHist($('histDh'), dh, {color: on ? '#378ADD' : '#D85A30', range: coreg.stats.dh_range});
  $('readout1').innerHTML = `median Δh = <b>${(st.median * 100).toFixed(1)} cm</b> (MAD ${(st.mad * 100).toFixed(1)} cm, n = ${st.n}) — ` +
    (on ? 'plate motion applied; remaining Δh is real change + unresolved terms' : 'plate motion off; includes the registration artifact') +
    (g ? ` · GIA applied: ${coreg.gia.uplift_rate_mm_per_yr.toFixed(2)} mm/yr × ${coreg.gia.years_apart_signed.toFixed(1)} yr = ${(g * 100).toFixed(1)} cm` : '');
  drawHist($('histArt'), coreg.artifact, {color: '#E0A030', range: coreg.stats.artifact_range});
  const a = coreg.stats.artifact, c = coreg.comparability;
  $('readout2').innerHTML = `plate-motion effect on Δh: median <b>${(a.median * 100).toFixed(2)} cm</b> (MAD ${(a.mad * 100).toFixed(2)} cm) from a <b>${(coreg.displacement_m * 100).toFixed(1)} cm</b> shift over ${coreg.years_apart.toFixed(1)} yr — ` +
    `sub-pixel in the scene; ` +
    `slope ${c.surface_slope_deg.toFixed(2)}° regional` + (coreg.along_track_slope_deg != null ? ` / ${coreg.along_track_slope_deg.toFixed(2)}° along-beam` : '') + (coreg.dem_slope_deg != null ? ` / ${coreg.dem_slope_deg.toFixed(2)}° DEM` : '') +
    ` <details class="small" style="display:inline"><summary style="display:inline;cursor:pointer">more</summary>${coreg.dh_estimator}; only the along-beam component of the shift is observable; ${coreg.n_pairs.gross_outliers_dropped.native} gross pairs > ${coreg.n_pairs.gross_outliers_dropped.threshold_m} m dropped</details>`;
  const unres = g ? c.unresolved.filter(x => x !== coreg.gia.unresolved_key) : c.unresolved;
  $('unresolved').innerHTML = `<b>Unresolved (not corrected):</b> ${unres.join(', ')}` +
    (c.dynamic_ice_flag === true ? '<br><b style="color:#D85A30">dynamic ice — ice flow is NOT corrected</b>' : c.dynamic_ice_flag === null ? '<br>Dynamic ice: <b>unknown</b> (no velocity field)' : '') +
    `<details class="small"><summary style="cursor:pointer">corrections</summary>plate motion (ITRF2014-PMM, ${coreg.common_frame} @ ${coreg.common_epoch}); ${c.ellipsoid_correction_applied}; ` +
    `frame step ${coreg.native_frames.GLAS}→${coreg.common_frame} shifts GLAS heights by ${(coreg.frame_vertical_shift_m.GLAS * 1000).toFixed(1)} mm` +
    (g ? `; GIA ${coreg.gia.uplift_rate_mm_per_yr.toFixed(2)} mm/yr (${coreg.gia.model}, ${coreg.gia.citation})` : '') +
    (c.dynamic_ice_flag === null ? `; ${c.dynamic_ice_note}` : '') + `</details>`;
}

function updateLabels() {
  // Mission show/hide rows now live in the unified controls box: checkbox + colour swatch + friendly name + count · epoch.
  const rows = MISSION_ORDER.filter(m => scene.series[m]).map(m => {
    const s = scene.series[m], info = MISSIONS[m] || {name: m, epoch: '', gloss: ''}, on = visible[m] !== false, col = colorOf(m);
    return `<label class="misrow${on ? '' : ' off'}" title="${info.gloss}"><input type="checkbox" data-m="${m}" ${on ? 'checked' : ''}>` +
      `<span class="dot" style="background:rgb(${col.join(',')})"></span><span class="misname">${info.name}</span>` +
      `<span class="mismeta">${ptsLabel(s)} · ${info.epoch}</span></label>`;
  }).join('');
  $('missionToggles').innerHTML = rows;
  $('missionToggles').querySelectorAll('input[data-m]').forEach(i => i.onchange = e => { visible[e.target.dataset.m] = e.target.checked; render(); updateLabels(); });
  if (scene.surface && scene.surface.attribution) $('attrib').dataset.dem = scene.surface.attribution;
  // Satellite-imagery toggle: only enabled once the area's imagery has been fetched
  // Imagery no longer blocks the build, so it can land AFTER the scene is ready — the server reports where it is via
  // imagery_status (pending | ready | unavailable) rather than us inferring it from the build state.
  const imgc = $('imagery'), ist = $('imageryStatus');
  if (imgc) {
    const st = scene.imagery_status;
    if (scene.imagery) { imgc.disabled = false; if (ist) ist.textContent = ''; }
    else if (st === 'pending' || (!sceneReady && st !== 'unavailable')) { imgc.disabled = true; if (ist) ist.textContent = 'fetching imagery…'; }
    else { imgc.disabled = true; if (ist) ist.textContent = 'imagery unavailable for this area'; }
  }
  $('attrib').textContent = (scene.imagery ? `Imagery: ${scene.imagery.attribution}` : '') + (scene.surface && scene.surface.attribution ? ` · DEM: ${scene.surface.attribution}` : '');
}

// ---- progressive load: open the shell instantly, then poll the growing scene doc and paint each new series /
// surface / imagery as it lands. The server persists the doc after every build leg (frame first, then per collection,
// then surface, then imagery), so each poll returns a little more. Deck.gl diffs layers by id, so simply re-running
// render() on each poll natively adds only the new layers — no bespoke diffing needed here.
let pollTimer = null, sceneReady = false, didFit = false, lastSeriesSig = '', schemaRefreshed = false;
function stopPoll() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

// ---------------------------------------------------------------- push transport (src/aicesat/stream.py)
// The only transport for point arrays and the DEM surface. One long-lived response carries both as raw f32 frames as
// they land; the ordinary poll still brings metadata, progress, imagery and coreg, but no bulk data at all. The
// chunked/base64 pull it replaced is deleted, not disabled.
//
// `?budget=N` caps the stream at N points per mission, DECLARED to the server via ?limit= — stopping the read does
// not stop the server, because a proxy keeps draining the origin. Omit it to load the whole scene.
let STREAM_BUDGET = 0, streamHandle = null, streamStats = null, streamSurface = null;
const streamed = new Map();                       // mission -> the series object the stream last produced

function stopStream() {
  if (streamHandle) { streamHandle.stop(); streamHandle = null; }
  streamed.clear(); streamStats = null; streamSurface = null;
}

/** Graft the streamed arrays onto a polled doc. Returns a NEW doc; never mutates the one passed in. */
function mergeStreamed(doc) {
  if (!doc) return doc;
  const out = {...doc};
  if (streamed.size) {
    const series = {...doc.series};
    for (const [m, s] of streamed) {
      // `n` stays the server's true count from meta, so the legend still reads "shown of total" honestly while the
      // scene fills in; with no budget set the two converge.
      const base = series[m] || {mission: m, color: s.color, n: s.n, meta: {}, granules: []};
      series[m] = {...base, positions: s.positions, slopes: s.slopes || null, n_shown: s.n_shown};
    }
    out.series = series;
  }
  if (streamSurface) out.surface = streamSurface;
  return out;
}

function startStream(id) {
  stopStream();
  if (!api.sceneStreamRun) return;                // MCP App transport: metadata + DEM surface only, no point clouds
  streamHandle = api.sceneStreamRun(id, (state, stats) => {
    if (sceneId !== id) return;                   // navigated away mid-stream
    for (const [m, s] of Object.entries(state.series)) streamed.set(m, s);
    if (state.surface) streamSurface = state.surface;
    streamStats = stats;
    if (scene) applyDoc(mergeStreamed(scene));    // repaint with what has landed so far
  }, {paintMs: 120, limit: STREAM_BUDGET || undefined});
  streamHandle.done
    .then(st => console.log('[aicesat] stream done', {ms: Math.round(st.tDone), MB: (st.bytes / 1e6).toFixed(2),
                                                     frames: st.frames, resets: st.resets}))
    .catch(e => { if (e.name !== 'AbortError') console.warn('[aicesat] stream failed', e); });
}

function applyDoc(doc) {
  if (!doc) return;
  scene = doc;
  coreg = scene.coreg;   // still read from disk (Δh panel / pair markers) but no longer user-triggered
  const keys = Object.keys(scene.series);
  keys.forEach(m => { if (!(m in visible)) visible[m] = true; });   // each mission defaults on as it appears
  $('zexag').value = Z_EXAG; $('zexagVal').textContent = Z_EXAG; syncExag();
  $('demOn').checked = SHOW_SURFACE;
  $('imagery').checked = SHOW_IMAGERY;
  const hasPositions = Object.values(scene.series).some(s => s.positions && s.positions.length);
  if (!didFit && hasPositions && fitView()) didFit = true;   // frame the data once, on the first series to arrive
  render(); updateLabels(); updateStats();
  const sig = keys.slice().sort().join(',');
  if (sig !== lastSeriesSig) { lastSeriesSig = sig; if (keys.length) initTimeSeries(); }   // reset TS UI only when the mission set changes
}

// Build progress: show the server's own per-leg log rather than an opaque spinner. A build can be slow for honest
// reasons (a cold NASA fetch), and the point cloud is a poor progress bar — data served from the lake arrives all at
// once at finalize, so the map can sit still while real work is happening.
let buildStart = 0, lastLog = [];
// Once the "Scene ready" confirmation has been dismissed, the panel STAYS dismissed for this scene. renderProgress
// unconditionally cleared `hidden`, and the ready branch keeps polling every 2.5 s while imagery is still in flight
// — so every poll re-showed the panel and the 1.4 s timer hid it again, giving a show/hide cycle per poll. That is
// the panel "disappearing and reappearing": one cycle per scenes + part?part=meta pair in the network log.
let progressDismissed = false;

// Build progress lives in the drawer beside Controls and Time Series, not over the canvas. It used to be a centred
// overlay with a backdrop blur, which fought the progressive streaming it exists to announce — the point of streaming
// points is to watch them land.
// One row element per mission, updated IN PLACE. Rebuilding progRows.innerHTML each poll destroyed and recreated
// every node ~2.4 times a second, which is what made the panel look like it kept appearing and disappearing: the
// indeterminate bar's 1.5 s progslide animation restarted from its beginning on every redraw (~3.7 restarts per
// cycle), and `transition: width .3s` never ran because the node it was transitioning was already gone.
const progRowEls = new Map();

function progRowFor(m, host) {
  let e = progRowEls.get(m);
  if (e && e.root.isConnected) return e;
  const root = document.createElement('div');
  root.className = 'prog-row';
  root.innerHTML = '<div class="prog-name"><span class="dot"></span><span class="prog-label"></span>'
                 + '<span class="prog-phase"></span></div>'
                 + '<div class="prog-barwrap"><div class="prog-bar"></div></div>'
                 + '<div class="prog-meta"><span class="prog-pct"></span><span class="prog-bits"></span></div>';
  host.appendChild(root);
  e = {root, dot: root.querySelector('.dot'), label: root.querySelector('.prog-label'),
       phase: root.querySelector('.prog-phase'), bar: root.querySelector('.prog-bar'),
       pct: root.querySelector('.prog-pct'), bits: root.querySelector('.prog-bits')};
  progRowEls.set(m, e);
  return e;
}

function setText(el, v) { if (el && el.textContent !== v) el.textContent = v; }

function renderProgress(loading, log) {
  const box = $('progress'); if (!box) return;
  if (!progressDismissed) box.hidden = false;
  const spin = $('slSpin'); if (spin) spin.classList.toggle('sl-idle', !loading);
  setText($('slTitle'), loading ? 'Building scene…' : 'Scene ready');
  const el = $('slElapsed');
  if (el) setText(el, buildStart ? ((Date.now() - buildStart) / 1000).toFixed(0) + 's' : '');
  const now = $('slNow');
  if (now) setText(now, loading ? ((log && log.length) ? log[log.length - 1] : '') : '');
  const prog = (scene && scene.progress) || {};
  const rows = MISSION_ORDER.filter(m => prog[m] || (scene && scene.series && scene.series[m]));
  const host = $('progRows');

  for (const [m, e] of [...progRowEls]) {            // a mission that went away (scene switch) loses its row
    if (!rows.includes(m)) { e.root.remove(); progRowEls.delete(m); }
  }
  if (!rows.length) {
    if (host.dataset.state !== 'empty') { host.innerHTML = '<div class="prog-now">waiting for the first collection…</div>'; host.dataset.state = 'empty'; }
    return;
  }
  if (host.dataset.state === 'empty') { host.innerHTML = ''; host.dataset.state = 'rows'; }

  for (const m of rows) {
    const p = prog[m] || {}, name = (MISSIONS[m] || {}).name || m;
    const total = p.total || 0, done = p.done || 0;
    const known = total > 0;                    // no denominator yet => indeterminate bar, not a fake 0%
    const pct = known ? Math.max(0, Math.min(100, Math.round(done / total * 100))) : 0;
    const phase = p.phase || 'queued';
    const bits = [];
    if (p.points) bits.push(p.points.toLocaleString() + ' pts');
    if (known && phase === 'fetching') bits.push(`${done}/${total} granules`);
    if (p.cached_chunks) bits.push(`${p.cached_chunks.toLocaleString()} chunks cached`);
    const rgb = colorOf(m).join(',');
    const e = progRowFor(m, host);
    const dotBg = `rgb(${rgb})`;
    if (e.dot.style.background !== dotBg) e.dot.style.background = dotBg;
    setText(e.label, name);
    setText(e.phase, phase);
    const cls = 'prog-phase prog-' + String(phase).replace(/[^a-z]/g, '');
    if (e.phase.className !== cls) e.phase.className = cls;
    const w = (known ? pct : 24) + '%';
    if (e.bar.style.width !== w) e.bar.style.width = w;
    if (e.bar.style.background !== dotBg) e.bar.style.background = dotBg;
    e.bar.classList.toggle('prog-indet', !known);
    setText(e.pct, known ? pct + '%' : '—');
    setText(e.bits, bits.join(' · '));
  }
}

// The scene holds every extracted point; the viewer draws a sample of them. Say so rather than showing a count
// that is not what is on screen.
function ptsLabel(s) {
  const shown = s.n_shown != null ? s.n_shown : (s.positions ? s.positions.length / 3 : s.n);
  const total = s.n || shown;
  return shown < total ? `${shown.toLocaleString()} of ${total.toLocaleString()} pts`
                       : `${total.toLocaleString()} pts`;
}

function esc(v) { return String(v == null ? '' : v).replace(/[<>&]/g, c => ({'<': '&lt;', '>': '&gt;', '&': '&amp;'}[c])); }

async function pollUntilReady() {
  const myId = sceneId;
  let status = 'loading', jobId = null;
  try {
    const list = await api.scenes(); const rec = (list || []).find(s => s.scene_id === myId);
    status = rec ? rec.status : 'loading'; jobId = rec && rec.job_id;
  } catch (e) {}
  if (jobId) {                       // the job carries the per-leg log the overlay shows
    try { const j = await api.job(jobId); if (j && j.log) lastLog = j.log; } catch (e) {}
  }
  if (sceneId !== myId) return;   // navigated to another scene while awaiting
  let doc = null;
  // Incremental: fetch the small `meta` part and only the position/slope chunks that actually grew, appending onto the
  // buffers we already hold. Re-fetching the whole doc each tick re-shipped millions of floats per poll.
  // Metadata only: one small JSON request. Points and surface are on the stream.
  try { const up = await api.sceneUpdate(scene, myId); doc = up.doc; } catch (e) { doc = null; }   // 404 in the first instant, before the shell is persisted
  if (sceneId !== myId) return;
  if (doc) applyDoc(mergeStreamed(doc));
  const ld = $('progress');
  if (status === 'loading') {
    if (!buildStart) buildStart = Date.now();
    renderProgress(true, lastLog);
    pollTimer = setTimeout(pollUntilReady, 400);   // fast poll while loading so the per-granule stream reads as continuous
  } else {
    sceneReady = true;
    if (!doc) { stopPoll(); if (ld) ld.hidden = true; AICESAT.showError(`scene ${myId}: not available`); return; }
    // brief "ready" confirmation so a fast build doesn't just flicker, then get out of the way
    renderProgress(false, lastLog);
    setTimeout(() => { if (sceneReady && ld) { ld.hidden = true; progressDismissed = true; } }, 1400);
    // The build no longer waits on imagery, so it can still be in flight after the scene is ready. Keep a slow poll
    // alive until it resolves — the meta part is small, so this is cheap, and it stops as soon as it lands or fails.
    if (doc.imagery_status === 'pending' && !doc.imagery) pollTimer = setTimeout(pollUntilReady, 2500);
    else stopPoll();
    finishLoad();
  }
}

function finishLoad() {
  // co-registration compute stays server-side; the Δh panel shows only if a scene already carries coreg on disk.
  void schemaRefreshed;
  console.log('[aicesat] scene loaded', Object.entries(scene.series).map(([m, s]) => m + ':' + s.n).join(' '), 'surface', scene.surface ? scene.surface.n_cells_observed : 'none', 'meshOk', meshOk);
}

const syncExag = () => { const w = $('exagWarn'); if (w) { w.hidden = Z_EXAG <= 1; w.textContent = 'Heights exaggerated \u00d7' + Z_EXAG + ' \u2014 vertical only'; } };
$('zexag').oninput = e => { Z_EXAG = parseFloat(e.target.value); $('zexagVal').textContent = Z_EXAG; syncExag(); render(); updateLabels(); };
$('ptSize').oninput = e => { PT_SCALE = parseFloat(e.target.value); $('ptSizeVal').textContent = PT_SCALE; render(); };
$('demOn').onchange = e => { SHOW_SURFACE = e.target.checked; render(); };
$('imagery').onchange = e => { SHOW_IMAGERY = e.target.checked; render(); };


// ---------------------------------------------------------------- access-method scoreboard (measured; spec C.3)
async function loadBench() {
  try {
    const b = await api.bench(); if (!b) return;
    const rows = Object.entries(b);
    if (!rows.length) return;
    const first = rows[0][1];
    $('benchMeta').textContent = `${first.region} ${JSON.stringify(first.bbox)}, ${first.n_granules} ATL03 v007 granules, ${first.window.join('..')}; measured ${first.measured_at.slice(0, 10)}`;
    const cols = [['method', r => r.label || r.method], ['granules touched', r => r.granules_touched], ['HDF5 parses at query', r => r.hdf5_opens_at_query_time ?? r.hdf5_opens],
                  ['requests', r => r.requests], ['MB', r => (r.bytes / 1e6).toFixed(0)], ['wall s', r => r.wall_s], ['photons', r => r.photons != null ? r.photons.toLocaleString() : 'n/a']];
    const t = $('benchTable');
    t.innerHTML = '<tr>' + cols.map(c => `<th style="text-align:left;border-bottom:1px solid var(--hair);padding:2px 4px">${c[0]}</th>`).join('') + '</tr>' +
      rows.map(([k, r]) => '<tr>' + cols.map((c, i) => `<td style="padding:2px 4px;border-bottom:1px solid #26262e;${i ? 'text-align:right;font-variant-numeric:tabular-nums' : ''}">${c[1](r)}</td>`).join('') +
        (r.notes ? `</tr><tr><td colspan="7" class="small" style="padding:0 4px 6px">${r.notes}</td>` : '') + '</tr>').join('');
    $('benchBtn').hidden = false;
  } catch (e) { console.warn('[aicesat] bench unavailable', e); }
}
$('benchBtn').onclick = () => { $('bench').hidden = !$('bench').hidden; };
$('benchClose').onclick = () => { $('bench').hidden = true; };


// ---------------------------------------------------------------- closeable panels (shell panel manager)
// opt-in "?" help on the jargon-heaviest label (Δh panel)
{ const U = AICESAT.util, G = U.GLOSSARY;
  const dhH = root.querySelector('#stats h2'); if (dhH) dhH.appendChild(U.help(G.dh)); }
AICESAT.util.drawer(root, null);
$('stats').addEventListener('reopen', () => updateStats());
// 3-D navigation hint over the canvas, auto-dismissed on first interaction (or after a few seconds)
{ const nh = $('navhint'); if (nh) { $('deck').addEventListener('pointerdown', () => nh.classList.add('hide'), {once: true}); setTimeout(() => nh.classList.add('hide'), 6000); } }
// ---------------------------------------------------------------- time series over coincident cells
let candidates = [], candSel = -1;
const H3_EDGE_M = {7: 1220, 8: 461, 9: 174, 10: 66, 11: 25};
const missionColor = colorOf;
function tsLabels() { const r = +$('tsRes').value; $('tsResLbl').textContent = 'res ' + r + ' · ~' + (H3_EDGE_M[r] || '?') + ' m'; $('tsDtLbl').textContent = (+$('tsDt').value).toFixed(2) + ' yr'; }
function tsRefMissions() { return [...$('tsRef').querySelectorAll('input:checked')].map(i => i.value); }
function initTimeSeries() {
  candidates = []; candSel = -1;
  const present = Object.keys(scene.series);
  const defRef = present.includes('GLAS') ? ['GLAS'] : present;   // same rule as timeseries._reference_set: earliest-epoch, single-sensor anchor
  $('tsRef').innerHTML = present.map(m => '<label class="tsref-item"><input type="checkbox" value="' + m + '"' + (defRef.includes(m) ? ' checked' : '') + '> ' + ((MISSIONS[m] || {}).name || m) + '</label>').join('');
  $('tsRef').querySelectorAll('input').forEach(i => i.onchange = () => findCandidates());
  tsLabels(); renderCandList(); renderConf(null); $('tsChart').hidden = true; $('tsReadout').textContent = ''; $('tsCaveat').hidden = true; $('tsStatus').textContent = '';
}
async function findCandidates() {
  if (!scene) return;
  $('tsStatus').innerHTML = '<span class="spin-sm"></span>'; $('tsFind').disabled = true; AICESAT.clearError();
  try {
    const d = await api.candidates(sceneId, {h3_res: +$('tsRes').value, delta_t: +$('tsDt').value, ref_missions: tsRefMissions(), min_bins: 3});
    candidates = d.candidates || []; candSel = -1;
    $('tsStatus').textContent = candidates.length + (candidates.length === 1 ? ' cell' : ' cells');
    $('tsCaveat').hidden = !candidates.length;
    renderCandList(); render();
    if (candidates.length) selectCand(0); else { $('tsChart').hidden = true; renderConf(null); $('tsReadout').textContent = 'no cells with 3+ time windows — try a larger cell size or a wider time window'; }
  } catch (e) { $('tsStatus').textContent = 'error'; AICESAT.showError(e); }
  $('tsFind').disabled = false;
}
function renderCandList() {
  $('tsList').innerHTML = candidates.map((c, i) =>
    '<div class="tscand ' + (i === candSel ? 'on' : '') + '" data-i="' + i + '"><span class="conf-badge ' + c.level + '" title="confidence ' + c.confidence + '">' + c.level + '</span> <b>' + c.n_bins + ' epochs</b> · ' + c.span_years + ' yr · ' + c.slope_deg + '° <span class="small">' + c.n_points + ' pts</span></div>').join('');
  $('tsList').querySelectorAll('.tscand').forEach(el => el.onclick = () => selectCand(+el.dataset.i));
}
function selectCand(i) { candSel = i; renderCandList(); drawChart(); renderConf(candidates[i]); render(); }
function compRow(label, val, score) { return '<div class="comp-row"><span class="comp-lbl">' + label + '</span><span class="comp-val">' + val + '</span><span class="comp-bar"><i style="width:' + Math.round((score || 0) * 100) + '%"></i></span></div>'; }
function renderConf(c) {
  const el = $('tsConf'); if (!el) return;
  if (!c) { el.innerHTML = ''; return; }
  const m = c.components, sc = m.scores;
  el.innerHTML = '<div class="conf-why"><span class="conf-badge ' + c.level + '">' + c.level + '</span> ' + c.why + '</div>' +
    '<details class="tscomp"><summary>confidence breakdown (' + c.confidence + ')</summary><div class="tscomp-body">' +
    compRow('within-cell roughness', m.roughness_m + ' m', sc.roughness) +
    compRow('epochs (time windows)', m.epochs, sc.epochs) +
    compRow('baseline', m.span_yr + ' yr', sc.span) +
    compRow('reference points', m.ref_pts, sc.density) +
    '</div></details>';
}
function linfit(x, y) { const n = x.length; if (n < 2) return 0; const mx = x.reduce((a, b) => a + b, 0) / n, my = y.reduce((a, b) => a + b, 0) / n; let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (x[i] - mx) * (y[i] - my); sxx += (x[i] - mx) ** 2; } return sxx ? sxy / sxx : 0; }
function drawChart() {
  const cv = $('tsChart'); if (candSel < 0 || !candidates[candSel]) { cv.hidden = true; return; }
  cv.hidden = false; const c = candidates[candSel], s = c.series, dpr = devicePixelRatio;
  const ctx = cv.getContext('2d'); const W = cv.width = cv.clientWidth * dpr; cv.style.height = '150px'; const H = cv.height = 150 * dpr;
  ctx.clearRect(0, 0, W, H);
  const padL = 44 * dpr, padR = 8 * dpr, padT = 10 * dpr, padB = 20 * dpr;
  const yrs = s.map(p => p.year), vals = s.map(p => p.value_m), mads = s.map(p => p.mad_m);
  const x0 = Math.min(...yrs), x1 = Math.max(...yrs);
  let ymin = Math.min(...vals.map((v, i) => v - mads[i])), ymax = Math.max(...vals.map((v, i) => v + mads[i]));
  const pd = (ymax - ymin) * 0.15 || 0.1; ymin -= pd; ymax += pd;
  const sx = v => padL + (v - x0) / ((x1 - x0) || 1) * (W - padL - padR);
  const sy = v => padT + (ymax - v) / ((ymax - ymin) || 1) * (H - padT - padB);
  ctx.strokeStyle = '#555'; ctx.lineWidth = dpr; ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, H - padB); ctx.lineTo(W - padR, H - padB); ctx.stroke();
  if (ymin <= 0 && ymax >= 0) { ctx.strokeStyle = '#666'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(padL, sy(0)); ctx.lineTo(W - padR, sy(0)); ctx.stroke(); ctx.setLineDash([]); }
  ctx.fillStyle = '#aaa'; ctx.font = (11 * dpr) + 'px sans-serif'; ctx.textAlign = 'right';
  ctx.fillText(ymax.toFixed(2) + ' m', padL - 4 * dpr, sy(ymax) + 8 * dpr); ctx.fillText(ymin.toFixed(2), padL - 4 * dpr, sy(ymin));
  ctx.textAlign = 'center'; ctx.fillText(x0.toFixed(0), sx(x0), H - 5 * dpr); ctx.fillText(x1.toFixed(0), sx(x1), H - 5 * dpr);
  ctx.strokeStyle = '#7a7a86'; ctx.lineWidth = 1.2 * dpr; ctx.beginPath(); s.forEach((p, i) => { const X = sx(p.year), Y = sy(p.value_m); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); }); ctx.stroke();
  s.forEach(p => { const X = sx(p.year), col = missionColor(p.missions[0]);
    ctx.strokeStyle = 'rgba(' + col.join(',') + ',0.55)'; ctx.lineWidth = dpr; ctx.beginPath(); ctx.moveTo(X, sy(p.value_m - p.mad_m)); ctx.lineTo(X, sy(p.value_m + p.mad_m)); ctx.stroke();
    ctx.fillStyle = 'rgb(' + col.join(',') + ')'; ctx.beginPath(); ctx.arc(X, sy(p.value_m), 3.4 * dpr, 0, 7); ctx.fill(); });
  const trend = linfit(yrs, vals) * 100;
  const missions = [...new Set(s.flatMap(p => p.missions))].map(m => (MISSIONS[m] || {}).name || m).join(' → ');
  $('tsReadout').innerHTML = 'trend <b>' + trend.toFixed(1) + ' cm/yr</b> · ' + s.length + ' epochs over ' + c.span_years + ' yr · ' + missions;
}
function candidateLayers() {
  if (!candidates.length) return [];
  const rings = candidates.map((c, i) => ({poly: c.xy.map(xy => [xy[0], xy[1], c.center[2] * Z_EXAG]), sel: i === candSel}));
  const layers = [new deck.PolygonLayer({id: 'cands', data: rings, getPolygon: d => d.poly, filled: true, stroked: true, pickable: true,
    getFillColor: d => d.sel ? [120, 225, 255, 20] : [200, 214, 245, 24],   // faint fills only — no cyan blob on the selected hex; emphasis is the outline + leader marker
    getLineColor: d => d.sel ? [150, 235, 255, 255] : [200, 214, 245, 180],
    lineWidthUnits: 'pixels', getLineWidth: d => d.sel ? 3 : 1.8, lineWidthMinPixels: 1.5,
    onClick: info => { if (info && info.index != null && info.index >= 0) selectCand(info.index); },
    updateTriggers: {getFillColor: candSel, getLineColor: candSel, getLineWidth: candSel, getPolygon: Z_EXAG}})];
  // selected cell: a vertical marker rising from the surface + a floating label, so the current time-series cell is
  // unmistakable in the 3-D scene.
  const c = candidates[candSel];
  if (c && bounds) {
    // Rise above the surface by ~the local vertical relief. (Using the HORIZONTAL span here shot the marker ~6 km
    // into the sky — the x/y extent is kilometres while the relief is tens–hundreds of metres.)
    const span = Math.max(bounds.maxx - bounds.minx, bounds.maxy - bounds.miny) || 1000;
    // Tall enough that the tether reads as a clear leader line: the vertical relief alone is tiny vs the km-wide scene,
    // so scale with the horizontal span — but well short of the ~6 km "orbit" that 0.32*span produced.
    const stickH = Math.max(span * 0.06, (bounds.maxz - bounds.minz) * Z_EXAG * 1.3);
    const base = [c.center[0], c.center[1], c.center[2] * Z_EXAG];
    const top = [c.center[0], c.center[1], c.center[2] * Z_EXAG + stickH];
    const MARK = [150, 235, 255];
    // Tether hex -> label as LineLayer segments (bright core over a dark halo) — LineLayer draws a single segment
    // reliably where a 2-point PathLayer did not. depthTest off so it's ALWAYS visible over the dense points and the
    // terrain. A small anchor dot marks the hex; the label caps the top. No fat top dot / no filled hex (those blobbed).
    const seg = [{s: base, t: top}];
    layers.push(new deck.LineLayer({id: 'cand-stick-halo', data: seg, getSourcePosition: d => d.s, getTargetPosition: d => d.t,
      getColor: [8, 14, 22, 205], getWidth: 7, widthUnits: 'pixels', parameters: {depthTest: false},
      updateTriggers: {getSourcePosition: Z_EXAG, getTargetPosition: Z_EXAG}}));
    layers.push(new deck.LineLayer({id: 'cand-stick', data: seg, getSourcePosition: d => d.s, getTargetPosition: d => d.t,
      getColor: MARK, getWidth: 3, widthUnits: 'pixels', parameters: {depthTest: false},
      updateTriggers: {getSourcePosition: Z_EXAG, getTargetPosition: Z_EXAG}}));
    layers.push(new deck.ScatterplotLayer({id: 'cand-stick-anchor', data: [base], getPosition: d => d, getFillColor: MARK,
      getRadius: 4, radiusUnits: 'pixels', radiusMinPixels: 3, radiusMaxPixels: 5, billboard: true,
      parameters: {depthTest: false}, updateTriggers: {getPosition: Z_EXAG}}));
    layers.push(new TextLayer({id: 'cand-stick-label', data: [{position: top}], getPosition: d => d.position,
      getText: () => 'current time series', getColor: [10, 15, 24], getSize: 12, sizeUnits: 'pixels',
      getPixelOffset: [0, -8], billboard: true, fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto',
      background: true, getBackgroundColor: MARK.concat(255), backgroundPadding: [5, 3],
      getTextAnchor: 'middle', getAlignmentBaseline: 'bottom', updateTriggers: {getPosition: Z_EXAG}}));
  }
  return layers;
}
$('tsRes').oninput = tsLabels; $('tsDt').oninput = tsLabels;
$('tsRes').onchange = () => findCandidates(); $('tsDt').onchange = () => findCandidates();
$('tsFind').onclick = () => findCandidates();

void back;   // Back now lives in the top bar; the legacy in-legend button was removed

// ---------------------------------------------------------------- view API
this.open = async (id, query) => {
  root.classList.add('on');
  params = new URLSearchParams(query || '');
  if (params.get('zexag')) { Z_EXAG = parseFloat(params.get('zexag')); }
  STREAM_BUDGET = parseInt(params.get('budget') || '0', 10) || 0;
  if (id !== sceneId) {
    stopPoll(); stopStream();
    sceneId = id; scene = null; coreg = null; bounds = null; deckgl.setProps({layers: []});
    sceneReady = false; didFit = false; lastSeriesSig = ''; schemaRefreshed = false; progressDismissed = false;
    buildStart = 0; lastLog = [];                 // progress overlay state is per-scene
    progRowEls.clear();                           // rows belong to the scene that created them
    Object.keys(visible).forEach(k => delete visible[k]);   // all missions on by default for the new scene
    const ld = $('progress'); if (ld) ld.hidden = false;
    startStream(id);
    await pollUntilReady();   // paints the shell + whatever is ready now; keeps polling if the build is still running
    loadBench();
  } else if (!sceneReady && !pollTimer) {
    if (!streamHandle) startStream(id);               // came back to a still-building scene -> reopen the stream
    await pollUntilReady();   // came back to a still-building scene -> resume progressive polling
  } else {
    deckgl.redraw && deckgl.redraw(true);
  }
};
this.hide = () => { root.classList.remove('on'); stopPoll(); stopStream(); };

  }
};
