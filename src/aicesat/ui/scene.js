AICESAT.SceneView = class {
  constructor(root, api, back) {
    root.innerHTML = '<div id="deck" class="deck"></div>\n<div id="progress" class="panel" data-title="build progress" hidden>\n  <div class="sl-head"><span id="slSpin" class="spinner"></span><span id="slTitle">Building scene…</span><span id="slElapsed" class="sl-elapsed"></span></div>\n  <div id="progRows" class="prog-rows"></div>\n  <div id="slNow" class="prog-now"></div>\n</div>\n<div id="navhint">drag to orbit · scroll to zoom</div>\n<div id="exagWarn" class="exag-badge" hidden></div>\n<div id="controls" class="panel" data-title="controls">\n  <div class="ctl-group">\n    <div class="ctl-head">Missions <span class="ctl-note">show / hide</span></div>\n    <div id="missionToggles" class="misrows"></div>\n  </div>\n  <div class="ctl-group">\n    <label class="ctl-row"><input id="demOn" type="checkbox" checked> DEM base surface</label>\n    <label class="ctl-row"><input id="gratOn" type="checkbox" checked> Lat/lon grid on terrain</label>\n    <label class="ctl-row"><input id="hexOn" type="checkbox"> H3 cell grid</label>\n    <label class="ctl-row"><span class="ctl-lbl">Cell size</span><input id="hexRes" type="range" min="5" max="11" step="1" value="8" class="ctl-range" disabled><b id="hexResLbl" class="ctl-val"></b></label>\n    <label class="ctl-row"><input id="imagery" type="checkbox" disabled> Show satellite imagery</label>\n    <div id="imageryStatus" class="ctl-info"></div>\n  </div>\n  <div class="ctl-group">\n    <label class="ctl-row"><span class="ctl-lbl">Vertical ×<b id="zexagVal">1</b></span><input id="zexag" type="range" min="1" max="10" step="1" value="1" class="ctl-range"></label>\n    <label class="ctl-row"><span class="ctl-lbl">Points ×<b id="ptSizeVal">1</b></span><input id="ptSize" type="range" min="0.4" max="3" step="0.1" value="1" class="ctl-range"></label>\n    <label class="ctl-row"><span class="ctl-lbl">Terrain <b id="terrAlphaVal">solid</b></span><input id="terrAlpha" type="range" min="0.3" max="1" step="0.05" value="1" class="ctl-range"></label>\n  </div>\n  <button id="benchBtn" hidden>How the data got here</button>\n</div>\n<div id="attrib" style="position:absolute; bottom:4px; right:396px; font-size:10px; color:var(--muted)"></div>\n<div id="bench" class="panel" data-title="access comparison" hidden style="top:112px; left:12px; width:440px; max-height:calc(100% - 200px); overflow:auto">\n  <h2 style="font-size:13px;margin:0 0 4px">How the data got here — access-method comparison</h2>\n  <div class="small" id="benchMeta"></div>\n  <table id="benchTable" style="width:100%;border-collapse:collapse;font-size:11.5px;margin-top:6px"></table>\n  <div class="small" style="margin-top:6px">Measured on the same area, granules, and photons across every method. The real wins are how many files get opened and parsed — not just bytes moved.</div>\n  <button id="benchClose" style="margin-top:6px">hide</button>\n</div>\n<div id="stats" class="panel" data-title="Δh panels" hidden>\n  <h2>Height difference Δh — ICESat-2 minus ICESat-1</h2>\n  <canvas class="hist" id="histDh"></canvas>\n  <div class="hist-cap">← lower · Δh (metres) · higher → · bar height = number of co-located pairs · dashed line = 0</div>\n  <div class="readout" id="readout1"></div>\n  <h2 style="margin-top:10px">Effect of the plate-motion correction on Δh</h2>\n  <canvas class="hist" id="histArt"></canvas>\n  <div class="hist-cap">how much re-aligning the footprints changes each pair (metres)</div>\n  <div class="readout" id="readout2"></div>\n  <div id="unresolved"></div>\n</div>\n<div id="tspanel" class="panel" data-title="time series">\n  <h2>Elevation time series</h2>\n  <div class="small tsintro">Cells observed across time; height plotted there as a residual about a local reference plane (so surface slope is removed, not mistaken for change).</div>\n  <label class="ctl-row"><span class="ctl-lbl">Cell size</span><input id="tsRes" type="range" min="7" max="11" step="1" value="9" class="ctl-range"><b id="tsResLbl" class="ctl-val"></b></label>\n  <label class="ctl-row"><span class="ctl-lbl">Time window</span><input id="tsDt" type="range" min="0.25" max="3" step="0.25" value="1" class="ctl-range"><b id="tsDtLbl" class="ctl-val"></b></label>\n  <div class="ctl-row tsrefrow"><span class="ctl-lbl">Reference</span><span id="tsRef" class="tsref"></span></div>\n  <div class="row"><button id="tsFind">Find candidates</button><span id="tsStatus" class="small"></span></div>\n  <div id="tsList" class="tslist"></div>\n  <canvas id="tsChart" class="tschart" hidden></canvas>\n  <div id="tsReadout" class="small"></div>\n  <div id="tsConf" class="small"></div>\n  <div id="tsCaveat" class="small tscaveat" hidden>No inter-campaign / inter-sensor bias adjustment yet (coming later).</div>\n</div>';
/* Demo B widget: two point clouds, OFF/ON co-registration toggle, Δh histograms, honesty labels,
   plus visual cues: DEM surface, paired-shot highlighting.
   Corrections (plate motion, …) are applied to the Δh computation via checkboxes; the true positional shift is
   sub-pixel, so the 3-D clouds do not visibly move (no exaggeration, no animated snap, no shift arrow). */
const {Deck, OrbitView, PointCloudLayer, PathLayer, TextLayer, SimpleMeshLayer, LightingEffect, AmbientLight, DirectionalLight} = deck;
let params = new URLSearchParams(); let sceneId = null;
let Z_EXAG = 1;
// 1 = solid. The DEM used to be drawn translucent AND with depth writing off, which is what actually lets a point
// behind a ridge draw in front of it. That is right for a near-flat ice sheet, where the surface is a reference and
// the points are the subject; in 5,500 m of Himalayan relief it reads as seeing through mountains. Solid is the
// default now, and the old behaviour is a drag of the Terrain slider away.
let TERRAIN_ALPHA = 1;

function surfaceAppearance(img) {
  const a = Math.round(255 * TERRAIN_ALPHA);
  const props = {getColor: img ? [255, 255, 255, a] : [76, 84, 100, a],   // charcoal hillshade so mission colours pop
                 // Satellite imagery already CONTAINS the sun: the 2025-12-15 Sentinel-2 scene over Langtang was
                 // acquired at solar azimuth 162 / elevation 36, and those shadows are in the pixels. Adding the
                 // synthetic hillshade on top rendered two suns 27 deg apart, close enough to look plausible while
                 // making the east/west contrast partly an artefact. Draped mesh is unlit; a BARE DEM keeps the
                 // hillshade, because there it is the only relief cue there is.
                 material: img ? false : {ambient: 0.5, diffuse: 0.85, shininess: 12, specularColor: [30, 30, 30]},
                 updateTriggers: {getPosition: Z_EXAG, getColor: TERRAIN_ALPHA}};
  if (TERRAIN_ALPHA < 0.99) props.parameters = {depthWriteEnabled: false};
  return props;
}
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
const adj = {plate_motion: true, gia: true};   // corrections the Δh readout applies: always both (db499a3 removed the toggles)
const $ = id => root.querySelector('#' + id);
const PAIR_RING = [220, 200, 150, 180];
let SHOW_PAIRS = true;
// Mission identity and the display palette live in tspanel.js, shared with the standalone #ts view. Aliased here
// so everything below reads exactly as it did when they were declared in this constructor.
const {MISSIONS, MISSION_ORDER, MISSION_COLORS} = AICESAT.missions;
const visible = {};   // mission key -> shown; initialised per scene (all on)
const colorOf = m => AICESAT.missions.colorOf(m, scene);

const deckgl = new Deck({
  parent: $('deck'),
  width: '100%', height: '100%',
  onError: e => { console.error('[aicesat] deck error', e && e.message); if (/mesh/i.test(String(e && e.message))) { meshOk = false; render(); } },
  onLoad: () => console.log('[aicesat] deck loaded'),
  views: new OrbitView({orbitAxis: 'Z', fovy: 45}),
  // Low-angle hillshade from the NORTH-WEST (azimuth 315, the cartographic convention — lighting from the south-east
  // instead makes ridges read as valleys). deck.gl's `direction` is the direction light TRAVELS: the shader uses
  // `-directionalLight.direction` as the vector toward the light, so a NW source travels east-and-south. It used to
  // be [-1, 1, -0.6], which is azimuth 135 (SE) — the opposite of what the comment claimed, and the opposite of the
  // LIGHT vector the ICESSN platelets shade with, so a scene showing both lit them from opposite sides.
  effects: [new LightingEffect({ambient: new AmbientLight({color: [255, 255, 255], intensity: 0.9}),
                                sun: new DirectionalLight({color: [255, 250, 235], intensity: 1.6, direction: [1, -1, -0.6]})})],
  initialViewState: {target: [0, 0, 0], rotationX: 35, rotationOrbit: -25, zoom: -6, minZoom: -12, maxZoom: 6},
  controller: true,
  getTooltip: sceneTooltip,
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
const FOOTPRINT_M = {GLAS: 35, ICESSN: 12, ATL06: 16, ICESAT2: 8, GEDI: 25, GPSTRUTH: 2};   // GEDI's footprint is a real 25 m; a GPS epoch is a point
// ATL03 photons stay points at every zoom, deliberately: each is a single detection with no footprint or slope at
// scene scale, so a point is the honest primitive (#21). Missions whose product IS a fitted surface get facets.

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

// Layer-data memos. deck.gl compares props.data / props.mesh BY IDENTITY, so handing it a fresh object literal on
// every render invalidates every attribute and re-uploads the whole buffer to the GPU — tens of MB for a 2.7M-point
// scene. render() runs on any poll tick (imagery polls every 2.5s until it resolves), so without these the scene
// visibly rebuilt itself after it had finished loading. Keyed on exactly the inputs the buffers depend on.
const _cloudData = new Map();      // mission -> {src, data}
let _meshMemo = null;              // {z, zexag, imgKey, mesh}
function clearLayerMemos() { _cloudData.clear(); _meshMemo = null; }

function cloudData(m, src) {
  const prev = _cloudData.get(m);
  if (prev && prev.src === src) return prev.data;
  const data = {length: src.length / 3, attributes: {getPosition: {value: src, size: 3}}};
  _cloudData.set(m, {src, data});
  return data;
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
      data: cloudData(m, src),
      modelMatrix: zExagMatrix(),
      getFillColor: colorOf(m), getRadius: (FOOTPRINT_M[m] || 14) * PT_SCALE, radiusUnits: 'meters',
      radiusMinPixels: 1, radiusMaxPixels: 6, billboard: true, pickable: true,
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
  // The mesh bakes Z_EXAG into its vertices and texCoords into the imagery extent, so those two join `z` in the key.
  const imgKey = img ? `${img.x0},${img.y0},${img.x1},${img.y1}` : '';
  const memoHit = _meshMemo && _meshMemo.z === z && _meshMemo.zexag === Z_EXAG && _meshMemo.imgKey === imgKey;
  if (meshOk && memoHit) {
    const meshProps = {
      id: 'surface-mesh' + (img ? '-img' : ''), data: [{}], mesh: _meshMemo.mesh,
      getPosition: () => [0, 0, 0],
      ...surfaceAppearance(img),
    };
    if (img) meshProps.texture = api.imageryUrl(sceneId, IMG_VER);
    return [new SimpleMeshLayer(meshProps)];
  }
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
      _meshMemo = {z, zexag: Z_EXAG, imgKey, mesh: {attributes: attrs, indices: {value: new Uint32Array(idx)}}};
      const meshProps = {
        id: 'surface-mesh' + (img ? '-img' : ''), data: [{}],
        mesh: _meshMemo.mesh,
        getPosition: () => [0, 0, 0],
        ...surfaceAppearance(img),
      };
      if (img) meshProps.texture = api.imageryUrl(sceneId, IMG_VER);   // omit the key entirely when not draping; IMG_VER busts the cache after a source change
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
// ---- local metres <-> lon/lat ---------------------------------------------------------------------------------
// The scene renders in a local metric frame (aeqd centred on the bbox, or polar stereographic above 55 deg), so
// nothing on screen carries a coordinate. These convert, using the frame's own orthonormal east/north unit
// vectors -- the same basis plateletLayer uses -- so they hold for the rotated polar frames too. A flat-Earth
// scaling around the bbox centre is sub-metre over a scene-sized box and is not meant for anything larger.
const M_PER_DEG_LAT = 110574;
function frameCentre(fr) { const b = fr.bbox; return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]; }
function mPerDegLon(clat) { return 111320 * Math.cos(clat * Math.PI / 180); }

function localToLonLat(fr, x, y) {
  const E = fr.east_xy || [1, 0], N = fr.north_xy || [0, 1];
  const [clon, clat] = frameCentre(fr);
  // SOLVE [E N][de dn]' = [x y]', do not project. E and N come from a finite difference at the bbox centre rounded
  // to 6 decimals, so they are only APPROXIMATELY orthonormal and a dot-product inverse drifts with distance from
  // the centre -- 0.4 m at the corner of this scene, and it grows with the box. A 2x2 solve is exact and no dearer.
  const det = E[0] * N[1] - N[0] * E[1];
  if (!det) return [clon, clat];                     // degenerate basis: refuse to invent a coordinate
  const de = (N[1] * x - N[0] * y) / det, dn = (E[0] * y - E[1] * x) / det;
  return [clon + de / mPerDegLon(clat), clat + dn / M_PER_DEG_LAT];
}

function lonLatToLocal(fr, lon, lat) {
  const E = fr.east_xy || [1, 0], N = fr.north_xy || [0, 1];
  const [clon, clat] = frameCentre(fr);
  const de = (lon - clon) * mPerDegLon(clat), dn = (lat - clat) * M_PER_DEG_LAT;
  return [de * E[0] + dn * N[0], de * E[1] + dn * N[1]];
}

const fmtLat = v => `${Math.abs(v).toFixed(3)}\u00b0${v >= 0 ? 'N' : 'S'}`;
const fmtLon = v => `${Math.abs(v).toFixed(3)}\u00b0${v >= 0 ? 'E' : 'W'}`;

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
  // The axes sit ON the scene's own corner and run the FULL edge, so they frame the block of ground you are looking
  // at instead of floating beside it as a gnomon. They used to start 0.22 of the span outside the surface and run
  // niceStep(span/4)*2 -- roughly half an edge -- which put every tick coordinate off the terrain it labelled.
  // One DEM cell of outward clearance stops the lines z-fighting the mesh they now lie along; nudging beats
  // disabling depthTest, which would show them straight through the terrain from every orbit angle.
  const gs = scene && scene.surface;
  const pad = (gs && gs.cell) ? gs.cell : 0.004 * span;
  const o = [minx - pad, miny - pad, minz * Z_EXAG];
  const Lx = Math.max(maxx - minx, 1), Ly = Math.max(maxy - miny, 1), zTrue = Math.max(maxz - minz, 1);
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
      texts.push({position: [t1[0] + tdir[0] * tk * 1.2, t1[1] + tdir[1] * tk * 1.2, t1[2]], text: fmt(v, pt), color, size: 11,
                  anchor: tdir[0] < 0 ? 'end' : 'middle'});
    }
    const lab = [end[0] + dir[0] * 0.02 * span + (dir[2] ? -tk * 2.5 : 0), end[1] + dir[1] * 0.02 * span, end[2] + (dir[2] ? 0.02 * span : 0)];
    texts.push({position: lab, text: label, color, size: 13, anchor: dir[2] ? 'end' : (dir[0] ? 'start' : 'middle')});
  };
  // Ticks read in DEGREES, not metres from an arbitrary corner: a scene is located by coordinate, and "6 km" from
  // an origin the viewer cannot see locates nothing. The axis NAME keeps the metric span, so scale is not lost.
  const fr = scene.frame;
  const km = v => `${(v / 1000).toFixed(v >= 1000 ? 0 : 1)} km`;
  const lonAt = (v, pt) => fr ? fmtLon(localToLonLat(fr, pt[0], pt[1])[0]) : km(v);
  const latAt = (v, pt) => fr ? fmtLat(localToLonLat(fr, pt[0], pt[1])[1]) : km(v);
  // Each axis gets its OWN tick step now that each runs its own true edge length: one shared step sized off the
  // larger edge left the shorter axis with one or two ticks. niceStep(L) lands near L/4, i.e. about four per edge.
  axis([1, 0, 0], Lx, [235, 120, 120], fr ? `lon \u2192 (${km(Lx)})` : 'x', niceStep(Lx), lonAt, 1);
  axis([0, 1, 0], Ly, [120, 220, 140], fr ? `lat \u2192 (${km(Ly)})` : 'y', niceStep(Ly), latAt, 1);
  axis([0, 0, 1], zTrue, [140, 170, 255], 'z', niceStep(zTrue), v => `${v.toFixed(0)} m`, Z_EXAG);
  return [
    new PathLayer({id: 'axes', data: paths, getPath: d => d.p, getColor: d => d.c, getWidth: d => d.w, widthUnits: 'pixels', updateTriggers: {getPath: Z_EXAG}}),
    new TextLayer({id: 'axes-text', data: texts, getPosition: d => d.position, getText: d => d.text, getColor: d => d.color, getSize: d => d.size, getTextAnchor: d => d.anchor || 'middle',
      sizeUnits: 'pixels', billboard: true, fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto',
      background: true, getBackgroundColor: [20, 20, 26, 170], backgroundPadding: [3, 1], updateTriggers: {getPosition: Z_EXAG}}),
  ];
}


// ---------------------------------------------------------------- lat/lon graticule, draped on the terrain
// A scene renders in local metres, so without this the only coordinates on screen are the axis ticks along its edge.
// Draping the graticule over the DEM carries them THROUGH the scene, so a ridge, a valley floor or a single shot can
// be read off a coordinate where it actually sits instead of by eye against a distant axis.
let GRAT_ON = true;
const GRAT_LIFT_M = 8;          // lift above the mesh so a line reads on top of the terrain instead of z-fighting it
const GRAT_MAX_SAMPLES = 400;   // cap the walk: at one sample per DEM cell a finer mesh would grow this linearly

// A degree step off the 1/2/5 ladder. niceStep's shape with the target written out: a graticule wants about six
// lines across the span, where niceStep's implicit target is four.
function graticuleStep(spanDeg) {
  const t = Math.max(spanDeg, 1e-12) / 6, p = Math.pow(10, Math.floor(Math.log10(t)));
  return [1, 2, 5, 10].map(m => m * p).reduce((a, b) => Math.abs(b - t) < Math.abs(a - t) ? b : a);
}

// The multiples of `step` inside [lo, hi]. Graticule lines must fall on ROUND coordinates: numbered from the range
// start instead (28.1766, 28.2266, ...) they are unreadable, which is the whole reason not to just linspace the span.
function ticksIn(lo, hi, step) {
  const out = [];
  for (let k = Math.ceil(lo / step - 1e-9); k * step <= hi + 1e-9; k++) {
    const v = k * step;
    if (v >= lo - 1e-9) out.push(Math.abs(v) < 1e-12 ? 0 : +v.toFixed(10));
  }
  return out;
}

// Split a walked line into the runs where the DEM actually has ground. surfaceHeightAt returns null over a hole and
// its contract is that the caller must not invent ground there -- HMA has real holes on the steep faces of this very
// scene, so a line bridged across one would draw terrain that does not exist. A run of a single sample is not a line.
function drapeSegments(samples) {
  const out = [];
  let run = [];
  for (const smp of samples) {
    const h = smp[2];
    if (h == null || !isFinite(h)) { if (run.length > 1) out.push(run); run = []; continue; }
    run.push(smp);
  }
  if (run.length > 1) out.push(run);
  return out;
}

// lon/lat envelope of the surface, from its four CORNERS: a polar frame is rotated, so the extreme longitude can sit
// at a corner rather than on an edge midpoint.
function surfaceLonLatBounds(fr, b) {
  let loMin = Infinity, loMax = -Infinity, laMin = Infinity, laMax = -Infinity;
  for (const c of [[b.minx, b.miny], [b.maxx, b.miny], [b.minx, b.maxy], [b.maxx, b.maxy]]) {
    const ll = localToLonLat(fr, c[0], c[1]);
    loMin = Math.min(loMin, ll[0]); loMax = Math.max(loMax, ll[0]);
    laMin = Math.min(laMin, ll[1]); laMax = Math.max(laMax, ll[1]);
  }
  return {loMin, loMax, laMin, laMax};
}

// Where a graticule line's label goes: the run END furthest from the scene centre, i.e. out at the perimeter.
// A line crossing DEM holes has many run ends and most are hole edges in the MIDDLE of the terrain -- labelling the
// longest run's far end (which this did at first) stranded coordinates mid-scene, as the rendered scene showed.
function edgeMostEnd(segs, cx, cy) {
  let best = null, bestD = -1;
  for (const seg of segs) {
    for (const e of [seg[0], seg[seg.length - 1]]) {
      const d = (e[0] - cx) * (e[0] - cx) + (e[1] - cy) * (e[1] - cy);
      if (d > bestD) { bestD = d; best = e; }
    }
  }
  return best;
}

function graticuleLayers() {
  if (!GRAT_ON || !scene || !scene.frame) return [];
  const b = surfaceExtent(); if (!b) return [];
  const fr = scene.frame;
  const {loMin, loMax, laMin, laMax} = surfaceLonLatBounds(fr, b);
  const step = graticuleStep(Math.max(loMax - loMin, laMax - laMin));   // one step for both axes -> a square graticule
  const g = scene.surface;
  const cell = (g && g.cell) || 200;
  const paths = [], texts = [];
  const cx = (b.minx + b.maxx) / 2, cy = (b.miny + b.maxy) / 2;
  const LON_C = [235, 120, 120, 150], LAT_C = [120, 220, 140, 150];   // the axis hues, muted: same meaning, less shout
  const walk = (kind, v) => {
    const a0 = kind === 'lat' ? loMin : laMin, a1 = kind === 'lat' ? loMax : laMax;
    const edge = kind === 'lat' ? b.maxx - b.minx : b.maxy - b.miny;
    const n = Math.max(32, Math.min(GRAT_MAX_SAMPLES, Math.round(edge / cell)));
    const samples = [];
    for (let i = 0; i <= n; i++) {
      const u = a0 + (a1 - a0) * (i / n);
      const xy = lonLatToLocal(fr, kind === 'lat' ? u : v, kind === 'lat' ? v : u);
      samples.push([xy[0], xy[1], surfaceHeightAt(xy[0], xy[1])]);
    }
    const segs = drapeSegments(samples);
    for (const seg of segs) paths.push({seg, kind, value: v});
    const e = edgeMostEnd(segs, cx, cy);
    if (e) {
      texts.push({position: [e[0], e[1], (e[2] + GRAT_LIFT_M * 3) * Z_EXAG],
                  text: kind === 'lat' ? fmtLat(v) : fmtLon(v), color: kind === 'lat' ? LAT_C : LON_C});
    }
  };
  for (const v of ticksIn(laMin, laMax, step)) walk('lat', v);
  for (const v of ticksIn(loMin, loMax, step)) walk('lon', v);
  if (!paths.length) return [];
  return [
    new PathLayer({id: 'graticule', data: paths, pickable: true,
      getPath: d => d.seg.map(s2 => [s2[0], s2[1], (s2[2] + GRAT_LIFT_M) * Z_EXAG]),
      getColor: d => d.kind === 'lat' ? LAT_C : LON_C, getWidth: 1.4, widthUnits: 'pixels',
      updateTriggers: {getPath: Z_EXAG}}),
    new TextLayer({id: 'graticule-text', data: texts, getPosition: d => d.position, getText: d => d.text,
      getColor: d => d.color, getSize: 10, sizeUnits: 'pixels', billboard: true,
      fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto',
      background: true, getBackgroundColor: [20, 20, 26, 150], backgroundPadding: [2, 1],
      updateTriggers: {getPosition: Z_EXAG}}),
  ];
}

// ---------------------------------------------------------------- H3 grid overlay
// The addressing grid the whole store is keyed on, drawn on the scene it addresses. h3-js is vendored into the bundle
// (map.js already uses it), so cells, rings and per-mission counts are all computed here -- no server round trip and
// nothing to memoize on the server. The binning IS O(points), so it is done once per (scene, res) and cached.
let HEX_ON = false, HEX_RES = 8;
let hexCache = {key: null, cells: null};

// The memo key has to include HOW MANY POINTS HAVE ARRIVED, not just the scene and resolution. Point arrays fill
// progressively over the stream, so a grid switched on mid-build binned a partial cloud -- and keyed on
// (scene, res) alone it then served those partial counts forever. Observed as a cell reading "ATL06 27" against 48
// in the finished scene. Lengths are the cheapest thing that changes exactly when the binning would.
function hexCacheKey(sceneId, res, series) {
  const lens = Object.keys(series || {}).sort()
    .map(m => m + ':' + ((series[m] && series[m].positions) ? series[m].positions.length : 0));
  return sceneId + '|' + res + '|' + lens.join(',');
}

function hexGrid(res) {
  const key = hexCacheKey(scene && scene.scene_id, res, scene && scene.series);
  if (hexCache.key === key) return hexCache.cells;
  const fr = scene.frame, b = surfaceExtent();
  if (!fr || !b) return [];
  const counts = new Map();                       // h3 cell -> {mission: n}
  for (const [m, sr] of Object.entries(scene.series)) {
    const src = sr.positions; if (!src || !src.length) continue;
    for (let i = 0; i < src.length; i += 3) {
      const ll = localToLonLat(fr, src[i], src[i + 1]);
      const c = h3.latLngToCell(ll[1], ll[0], res);
      let e = counts.get(c); if (!e) counts.set(c, e = {});
      e[m] = (e[m] || 0) + 1;
    }
  }
  // Every cell the SURFACE covers, so the grid is the scene's footprint and not merely where points happen to fall.
  const {loMin, loMax, laMin, laMax} = surfaceLonLatBounds(fr, b);
  const ring = [[laMin, loMin], [laMin, loMax], [laMax, loMax], [laMax, loMin], [laMin, loMin]];
  let ids = [];
  try { ids = h3.polygonToCells(ring, res); } catch (e) { ids = [...counts.keys()]; }
  const seen = new Set(ids);
  for (const c of counts.keys()) if (!seen.has(c)) { ids.push(c); seen.add(c); }
  const edge = h3.getHexagonEdgeLengthAvg ? h3.getHexagonEdgeLengthAvg(res, 'm') : null;
  const cells = ids.map(c => ({cell: c, res, edge_m: edge, counts: counts.get(c) || {},
                               ring: h3.cellToBoundary(c).map(ll => lonLatToLocal(fr, ll[1], ll[0]))}));
  hexCache = {key, cells};
  return cells;
}

function hexGridLayers() {
  if (!HEX_ON || !scene || !scene.frame) return [];
  const cells = hexGrid(HEX_RES);
  if (!cells.length) return [];
  // Draped: each ring vertex is lifted onto the terrain, so a cell follows the ground it addresses rather than
  // floating on a plane through it. A vertex over a DEM hole falls back to the scene's base height.
  const base = (surfaceExtent() || {minz: 0}).minz;
  return [new deck.PolygonLayer({
    id: 'hexgrid', data: cells, pickable: true, stroked: true, filled: true, extruded: false,
    getPolygon: d => d.ring.map(xy => {
      const h = surfaceHeightAt(xy[0], xy[1]);
      return [xy[0], xy[1], ((h == null || !isFinite(h) ? base : h) + GRAT_LIFT_M) * Z_EXAG];
    }),
    getFillColor: d => Object.keys(d.counts).length ? [120, 225, 255, 12] : [0, 0, 0, 0],
    getLineColor: [150, 190, 230, 110], lineWidthUnits: 'pixels', getLineWidth: 1,
    updateTriggers: {getPolygon: Z_EXAG},
  })];
}

// H3 edge length per resolution comes from h3 itself, so the label cannot drift from the grid it describes.
function hexResLabel() {
  const e = h3.getHexagonEdgeLengthAvg ? h3.getHexagonEdgeLengthAvg(HEX_RES, 'm') : null;
  const el = $('hexResLbl'); if (!el) return;
  el.textContent = 'res ' + HEX_RES + (e ? ' \u00b7 ~' + Math.round(e) + ' m' : '');
}

// ---------------------------------------------------------------- hover readouts
// One formatter per pickable thing. Kept as pure string builders so they are unit-testable without a GPU: the only
// job of the deck getTooltip below is to route an `info` to the right one.
const nfmt = n => n.toLocaleString('en-US');
// 5 decimals, not fmtLat/fmtLon's 3: a degree's third decimal is ~100 m, so at 3 two points a footprint apart print
// the SAME coordinate. The axis ticks keep 3 (they label a whole edge); a point readout locates one measurement.
const fmtLat5 = v => `${Math.abs(v).toFixed(5)}\u00b0${v >= 0 ? 'N' : 'S'}`;
const fmtLon5 = v => `${Math.abs(v).toFixed(5)}\u00b0${v >= 0 ? 'E' : 'W'}`;
const pointTip = (mission, lon, lat, hTrue) => `${mission}\n${fmtLat5(lat)}  ${fmtLon5(lon)}\n${nfmt(Math.round(hTrue))} m (WGS84 ellipsoid)`;
function gridTip(cell, res, edgeM, counts) {
  const rows = Object.entries(counts).sort();
  const body = rows.length ? rows.map(e => `${e[0]} ${nfmt(e[1])}`).join('  ') : 'no points in this cell';
  const size = edgeM ? `  ~${nfmt(Math.round(edgeM))} m edge` : '';
  return `H3 res ${res}  ${cell}${size}\n${body}`;
}
const lineTip = (kind, v) => (kind === 'lat' ? fmtLat(v) : fmtLon(v)) + (kind === 'lat' ? '  (parallel)' : '  (meridian)');

// Route a deck pick to a readout. Points carry no date: the position sidecar holds x/y/z only, so inventing one here
// would mean guessing. Height is un-exaggerated and put back on the absolute datum before it is shown.
function sceneTooltip(info) {
  if (!info || !info.layer || info.index == null || info.index < 0) return null;
  const id = info.layer.id, fr = scene && scene.frame;
  let text = null;
  if (id === 'graticule' && info.object) {
    text = lineTip(info.object.kind, info.object.value);
  } else if (id === 'hexgrid' && info.object) {
    text = gridTip(info.object.cell, info.object.res, info.object.edge_m, info.object.counts);
  } else if (id.startsWith('pc-') || id.startsWith('pl-') || id.startsWith('paired-')) {
    const m = id.slice(id.indexOf('-') + 1);
    const sr = scene && scene.series && scene.series[m];
    const src = sr && sr.positions;
    if (!src || !fr) return null;
    const i = info.index;
    if (3 * i + 2 >= src.length) return null;
    const ll = localToLonLat(fr, src[3 * i], src[3 * i + 1]);
    text = pointTip(m, ll[0], ll[1], src[3 * i + 2] + (scene.z0 || 0));
  }
  if (!text) return null;
  return {text, style: {backgroundColor: 'rgba(14,18,26,0.94)', color: '#e8ecf4', fontSize: '11.5px',
                        padding: '5px 7px', borderRadius: '4px', whiteSpace: 'pre',
                        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                        border: '1px solid rgba(150,190,230,0.35)'}};
}


// Terrain height (TRUE metres, pre-exaggeration) under a local x/y, bilinear over the DEM grid. null outside the
// grid or over nodata — the caller must not invent ground where the DEM has none.
function surfaceHeightAt(x, y) {
  const g = scene && scene.surface;
  if (!g || g.z == null) return null;
  const {x0, y0, cell, nx, ny, z} = g;
  const fi = (x - x0) / cell, fj = (y - y0) / cell;
  if (!(fi >= 0 && fj >= 0 && fi <= nx - 1 && fj <= ny - 1)) return null;
  const i0 = Math.floor(fi), j0 = Math.floor(fj);
  const i1 = Math.min(i0 + 1, nx - 1), j1 = Math.min(j0 + 1, ny - 1);
  const tx = fi - i0, ty = fj - j0;
  const q = [z[j0 * nx + i0], z[j0 * nx + i1], z[j1 * nx + i0], z[j1 * nx + i1]];
  if (q.some(v => v == null || !isFinite(v))) return null;   // a hole in the DEM: say so, do not average around it
  return (q[0] * (1 - tx) + q[1] * tx) * (1 - ty) + (q[2] * (1 - tx) + q[3] * tx) * ty;
}

// ---- markers: "look HERE" -------------------------------------------------------------------------------------
// Axis ticks orient you; they do not point at anything. A marker is a named coordinate -- a lake, an avalanche
// source, a gauge -- drawn as a pin PLANTED ON THE TERRAIN: the stick starts at the DEM height under the point and
// rises a short way above it. An earlier version spanned the scene's whole vertical extent, which drove the stick
// down through the imagery and out below the ground, reading as an artefact rather than a location.
// scene.markers is [{lon, lat, label}]; absent or empty renders nothing.
// The label sits above the HIGHEST terrain in the scene, not a fixed distance above its own ground: a pin in a
// valley had its label swallowed by the ridge behind it from most camera angles. The stick still starts on the
// ground, so the pin stays planted and you can see which point the label belongs to — it just grows to reach clear
// air. Tall sticks in deep valleys are the intended look.
const MARKER_HEADROOM_FRAC = 0.07;   // clearance above max terrain, as a fraction of the scene's true relief
const MARKER_HEADROOM_MIN_M = 150;   // ...but never so little that the label grazes the summit
function markerLayers() {
  const ms = (scene && scene.markers) || [];
  const fr = scene && scene.frame;
  const b = surfaceExtent() || bounds;
  if (!ms.length || !fr || !b) return [];
  const relief = Math.max(b.maxz - b.minz, 1);
  const topZ = b.maxz + Math.max(relief * MARKER_HEADROOM_FRAC, MARKER_HEADROOM_MIN_M);
  const span = Math.max(b.maxx - b.minx, b.maxy - b.miny);
  const sticks = [], dots = [], labels = [];
  ms.forEach(m => {
    const [x, y] = lonLatToLocal(fr, m.lon, m.lat);
    // Off-scene markers are dropped rather than clamped to the edge: a pin on the boundary pointing at something
    // outside it is worse than no pin, because it reads as a location.
    if (x < b.minx - 0.02 * span || x > b.maxx + 0.02 * span || y < b.miny - 0.02 * span || y > b.maxy + 0.02 * span) return;
    // Plant on the terrain. With no DEM under the point, sit on the scene floor rather than guessing a height --
    // and the label still carries the coordinate, which is the part that has to be right.
    const ground = surfaceHeightAt(x, y);
    const z0 = (ground == null ? b.minz : ground) * Z_EXAG;
    const z1 = topZ * Z_EXAG;                     // every label at the same height, clear of all terrain
    sticks.push({s: [x, y, z0], t: [x, y, z1]});
    dots.push({p: [x, y, z1]});
    labels.push({position: [x, y, z1], text: m.label || `${fmtLat(m.lat)} ${fmtLon(m.lon)}`});
  });
  if (!sticks.length) return [];
  return [
    new deck.LineLayer({id: 'marker-halo', data: sticks, getSourcePosition: d => d.s, getTargetPosition: d => d.t,
      getColor: [10, 10, 14, 210], getWidth: 5, widthUnits: 'pixels', updateTriggers: {getSourcePosition: Z_EXAG, getTargetPosition: Z_EXAG}}),
    new deck.LineLayer({id: 'marker-stick', data: sticks, getSourcePosition: d => d.s, getTargetPosition: d => d.t,
      getColor: [255, 190, 60, 240], getWidth: 2, widthUnits: 'pixels', updateTriggers: {getSourcePosition: Z_EXAG, getTargetPosition: Z_EXAG}}),
    new deck.ScatterplotLayer({id: 'marker-dot', data: dots, getPosition: d => d.p, getFillColor: [255, 190, 60, 255],
      getRadius: 4.5, radiusUnits: 'pixels', stroked: true, getLineColor: [10, 10, 14, 220], lineWidthUnits: 'pixels',
      getLineWidth: 1.5, updateTriggers: {getPosition: Z_EXAG}}),
    new TextLayer({id: 'marker-label', data: labels, getPosition: d => d.position, getText: d => d.text,
      getColor: [255, 215, 130], getSize: 13, sizeUnits: 'pixels', billboard: true, getPixelOffset: [0, -14],
      fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto', background: true,
      getBackgroundColor: [20, 20, 26, 200], backgroundPadding: [4, 2], updateTriggers: {getPosition: Z_EXAG}}),
  ];
}


// ---------------------------------------------------------------- render / view
function render() {
  if (!scene) return;
  // Order is PICKING PRECEDENCE as well as draw order, most specific last: a point beats the graticule line it sits
  // on, and the line beats the hex cell under it. With the graticule below the hex grid (as it first was) a line
  // could not be hovered at all while the grid was on.
  deckgl.setProps({layers: [...surfaceLayers(), ...hexGridLayers(), ...graticuleLayers(), ...cloudLayers(),
                            ...candidateLayers(), ...axesLayers(), ...markerLayers()]});
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
  $('terrAlpha').value = TERRAIN_ALPHA; $('terrAlphaVal').textContent = terrLabel();
  $('demOn').checked = SHOW_SURFACE;
  $('gratOn').checked = GRAT_ON;
  $('hexOn').checked = HEX_ON; $('hexRes').value = HEX_RES; $('hexRes').disabled = !HEX_ON; hexResLabel();
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
const terrLabel = () => TERRAIN_ALPHA >= 0.99 ? 'solid' : `${Math.round(TERRAIN_ALPHA * 100)}%`;
$('terrAlpha').oninput = e => { TERRAIN_ALPHA = parseFloat(e.target.value); $('terrAlphaVal').textContent = terrLabel(); render(); };
$('ptSize').oninput = e => { PT_SCALE = parseFloat(e.target.value); $('ptSizeVal').textContent = PT_SCALE; render(); };
$('demOn').onchange = e => { SHOW_SURFACE = e.target.checked; render(); };
$('gratOn').onchange = e => { GRAT_ON = e.target.checked; render(); };
$('hexOn').onchange = e => { HEX_ON = e.target.checked; $('hexRes').disabled = !HEX_ON; render(); };
// oninput, not onchange: the binning is O(points) but memoized per (scene, res), so dragging re-bins once per stop.
$('hexRes').oninput = e => { HEX_RES = parseInt(e.target.value, 10); hexResLabel(); render(); };
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
const H3_EDGE_M = AICESAT.ts.H3_EDGE_M;
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
function renderCandList() { AICESAT.ts.renderCandList($('tsList'), candidates, candSel, selectCand); }
function selectCand(i) { candSel = i; renderCandList(); drawChart(); renderConf(candidates[i]); render(); }
function renderConf(c) { AICESAT.ts.renderConf($('tsConf'), c); }
function drawChart() { AICESAT.ts.drawChart($('tsChart'), candSel < 0 ? null : candidates[candSel], colorOf, $('tsReadout')); }
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
    stopPoll(); stopStream(); clearLayerMemos();
    sceneId = id; scene = null; coreg = null; bounds = null; deckgl.setProps({layers: []});
    // Candidate cells belong to the scene that produced them. initTimeSeries() clears them, but it needs
    // scene.series so it cannot run until the doc lands — which left the PREVIOUS scene's cells painted over the
    // new one for the whole load. Cleared here, before the wait, not after it.
    candidates = []; candSel = -1;
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
