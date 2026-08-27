AICESAT.SceneView = class {
  constructor(root, api, back) {
    root.innerHTML = '<div id="deck" class="deck"></div>\n<div id="sceneLoading" class="scene-loading" hidden><span class="spinner"></span>Loading scene…</div>\n<div id="navhint">drag to orbit · scroll to zoom</div>\n<div id="exagWarn" class="exag-badge" hidden></div>\n<div id="hud" class="panel" data-title="legend">\n  <h1 id="title">Cross-mission altimetry</h1>\n  <div class="q" id="question"></div>\n  <div class="legend" id="legend"></div>\n  <div class="small" id="framenote"></div>\n</div>\n<div id="controls" class="panel" data-title="controls">\n  <div class="ctl-group">\n    <div class="ctl-head">Corrections <span class="ctl-note">change the numbers</span></div>\n    <button id="btnCoreg">Co-register missions</button>\n    <div id="coregOn" hidden>\n      <div class="ctl-state">Co-registered ✓</div>\n      <label id="lblPlate" class="ctl-row"><input id="adjPlate" type="checkbox" checked> plate motion</label>\n      <label id="lblGia" class="ctl-row"><input id="adjGia" type="checkbox" checked> GIA (bedrock rebound)</label>\n    </div>\n    <div id="status" class="ctl-status small"></div>\n  </div>\n  <div class="ctl-group">\n    <div class="ctl-head">View <span class="ctl-note">change the picture</span></div>\n    <label class="ctl-row"><span class="ctl-lbl">Vertical ×<b id="zexagVal">1</b></span><input id="zexag" type="range" min="1" max="10" step="1" value="1" class="ctl-range"></label>\n    <label class="ctl-row"><input id="imagery" type="checkbox"> satellite imagery</label>\n    <label class="ctl-row"><input id="pairs" type="checkbox" checked> co-location markers</label>\n    <button id="benchBtn" hidden>How the data got here</button>\n  </div>\n</div>\n<div id="attrib" style="position:absolute; bottom:4px; right:396px; font-size:10px; color:var(--muted)"></div>\n<div id="bench" class="panel" data-title="access comparison" hidden style="top:112px; left:12px; width:440px; max-height:calc(100% - 200px); overflow:auto">\n  <h2 style="font-size:13px;margin:0 0 4px">How the data got here — access-method comparison</h2>\n  <div class="small" id="benchMeta"></div>\n  <table id="benchTable" style="width:100%;border-collapse:collapse;font-size:11.5px;margin-top:6px"></table>\n  <div class="small" style="margin-top:6px">Measured on the same area, granules, and photons across every method. The real wins are how many files get opened and parsed — not just bytes moved.</div>\n  <button id="benchClose" style="margin-top:6px">hide</button>\n</div>\n<div id="stats" class="panel" data-title="Δh panels" hidden>\n  <h2>Height difference Δh — ICESat-2 minus ICESat-1</h2>\n  <canvas class="hist" id="histDh"></canvas>\n  <div class="hist-cap">← lower · Δh (metres) · higher → · bar height = number of co-located pairs · dashed line = 0</div>\n  <div class="readout" id="readout1"></div>\n  <h2 style="margin-top:10px">Effect of the plate-motion correction on Δh</h2>\n  <canvas class="hist" id="histArt"></canvas>\n  <div class="hist-cap">how much re-aligning the footprints changes each pair (metres)</div>\n  <div class="readout" id="readout2"></div>\n  <div id="unresolved"></div>\n</div>';
/* Demo B widget: two point clouds, OFF/ON co-registration toggle, Δh histograms, honesty labels,
   plus visual cues: DEM surface, paired-shot highlighting.
   Corrections (plate motion, …) are applied to the Δh computation via checkboxes; the true positional shift is
   sub-pixel, so the 3-D clouds do not visibly move (no exaggeration, no animated snap, no shift arrow). */
const {Deck, OrbitView, PointCloudLayer, PathLayer, TextLayer, SimpleMeshLayer, LightingEffect, AmbientLight, DirectionalLight} = deck;
let params = new URLSearchParams(); let sceneId = null;
let Z_EXAG = 1;
let SHOW_IMAGERY = false;

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

function cloudLayers() {
  const out = [];
  for (const [m, s] of Object.entries(scene.series)) {
    if (visible[m] === false) continue;   // per-mission show/hide (legend toggles)
    const src = s.positions;   // measured photons/shots as delivered; corrections are sub-pixel here (see Δh panel)
    const paired = (coreg && coreg.pair_display_indices && coreg.pair_display_indices[m]) ? new Set(coreg.pair_display_indices[m]) : null;
    if (paired && SHOW_PAIRS) {  // co-located shots: a thin pale ring UNDER the point (subtle marker, not a blob)
      out.push(new PointCloudLayer({
        id: 'paired-' + m, data: [...paired],
        getPosition: i => [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG],
        getColor: PAIR_RING, pointSize: 4.5, sizeUnits: 'pixels', updateTriggers: {getPosition: Z_EXAG},
      }));
    }
    out.push(new PointCloudLayer({
      id: 'pc-' + m, data: indices(src.length / 3),
      getPosition: i => [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG],
      getColor: s.color, pointSize: m === 'GLAS' ? 2.5 : 2, sizeUnits: 'pixels', updateTriggers: {getPosition: Z_EXAG},
    }));
  }
  return out;
}

// ---------------------------------------------------------------- surface (depth cue)
function surfaceLayers() {
  const g = scene.surface; if (!g) return [];
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
      const img = SHOW_IMAGERY && scene.imagery;
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
        getPosition: () => [0, 0, 0], getColor: img ? [255, 255, 255, 235] : [150, 162, 188, 190],
        material: {ambient: 0.5, diffuse: 0.85, shininess: 12, specularColor: [30, 30, 30]},
        updateTriggers: {getPosition: Z_EXAG},
      };
      if (img) meshProps.texture = api.imageryUrl(sceneId);      // omit the key entirely when not draping
      else meshProps.parameters = {depthWriteEnabled: false};    // translucent surface: don't occlude points behind it
      layers.push(new SimpleMeshLayer(meshProps));
    }
  }
  // faint wireframe (rows + columns), always drawn; also the fallback if the mesh layer fails
  const paths = [];
  const run = (len, other, at) => { let cur = []; for (let k = 0; k < len; k++) { const [i, j] = at(k); if (z[j * nx + i] == null) { if (cur.length > 1) paths.push(cur); cur = []; } else cur.push(P(i, j)); } if (cur.length > 1) paths.push(cur); };
  for (let j = 0; j < ny; j += 2) run(nx, j, i => [i, j]);
  for (let i = 0; i < nx; i += 2) run(ny, i, j => [i, j]);
  layers.push(new PathLayer({id: 'surface-wire', data: paths, getPath: d => d, getColor: [200, 205, 220, 35], getWidth: 1, widthUnits: 'pixels'}));
  return layers;
}

// ---------------------------------------------------------------- orientation cues
function niceStep(len) { const t = len / 4, p = Math.pow(10, Math.floor(Math.log10(t))); return [1, 2, 5, 10].map(m => m * p).reduce((a, b) => Math.abs(b - t) < Math.abs(a - t) ? b : a); }
function axesLayers() {
  if (!bounds) return [];
  const {minx, maxx, miny, maxy, minz, maxz} = bounds;
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
  deckgl.setProps({layers: [...surfaceLayers(), ...cloudLayers(), ...axesLayers()]});
}

function fitView() {
  const all = Object.values(scene.series).flatMap(s => s.positions);
  if (!all.length) return;
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9, minz = 1e9, maxz = -1e9;
  for (let i = 0; i < all.length; i += 3) { minx = Math.min(minx, all[i]); maxx = Math.max(maxx, all[i]); miny = Math.min(miny, all[i + 1]); maxy = Math.max(maxy, all[i + 1]); minz = Math.min(minz, all[i + 2]); maxz = Math.max(maxz, all[i + 2]); }
  bounds = {minx, maxx, miny, maxy, minz, maxz};
  const span = Math.max(maxx - minx, maxy - miny) || 1;
  const zoom = Math.log2(Math.min(root.clientWidth, root.clientHeight) / (span * 1.25));
  deckgl.setProps({initialViewState: {target: [(minx + maxx) / 2, (miny + maxy) / 2, 0], rotationX: 35, rotationOrbit: -25, zoom, minZoom: zoom - 6, maxZoom: zoom + 8}});
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
  $('title').textContent = 'Cross-mission altimetry — ' + (Object.keys(scene.series).join(' + ') || 'empty scene');
  $('question').textContent = scene.question || '';
  // Missions as show/hide rows: checkbox + colour swatch + friendly name + count · epoch. The legend IS the control.
  const rows = MISSION_ORDER.filter(m => scene.series[m]).map(m => {
    const s = scene.series[m], info = MISSIONS[m] || {name: m, epoch: '', gloss: ''}, on = visible[m] !== false;
    return `<label class="misrow${on ? '' : ' off'}" title="${info.gloss}"><input type="checkbox" data-m="${m}" ${on ? 'checked' : ''}>` +
      `<span class="dot" style="background:rgb(${s.color.join(',')})"></span><span class="misname">${info.name}</span>` +
      `<span class="mismeta">${s.n.toLocaleString()} pts · ${info.epoch}</span></label>`;
  }).join('');
  const info = [];
  if (coreg && coreg.pair_display_indices) info.push(`<span><span class="dot" style="background:rgb(220,200,150)"></span>co-located pairs: ${coreg.pair_display_indices.GLAS.length}</span>`);
  if (scene.surface) info.push(`<span><span class="dot" style="background:rgb(150,160,185)"></span>surface: ${scene.surface.source || 'DEM'}</span>`);
  if (scene.surface && scene.surface.attribution) $('attrib').dataset.dem = scene.surface.attribution;
  $('legend').innerHTML = `<div class="misrows">${rows}</div>` + (info.length ? `<div class="legend-info">${info.join('')}</div>` : '');
  $('legend').querySelectorAll('input[data-m]').forEach(i => i.onchange = e => { visible[e.target.dataset.m] = e.target.checked; render(); updateLabels(); });
  const acc = scene.series.ICESAT2 && scene.series.ICESAT2.meta.access;
  $('framenote').innerHTML = `<details><summary>details</summary>` +
    (scene.surface ? `${scene.surface.note}<br>` : '') +
    (acc ? `data path: ${scene.series.ICESAT2.meta.access_path} — ${acc.chunks_fetched} photon chunks / ${acc.requests} range requests / ${(acc.bytes / 1e6).toFixed(0)} MB fetched, ${acc.chunks_skipped_already_materialized} chunks already in the lake, ${acc.hdf5_opens_at_query_time} HDF5 opens at query time; ${acc.cells} H3 cells (res ${acc.h3_res})<br>` : '') +
    `local frame ${scene.frame.crs}, bbox ${scene.bbox.map(v => v.toFixed(2)).join(', ')}; z relative to ICESat-2 median (${scene.z0.toFixed(0)} m); vertical ×${Z_EXAG} (axes show true metres)</details>`;
  $('attrib').textContent = (scene.imagery ? `Imagery: ${scene.imagery.attribution}` : '') + (scene.surface && scene.surface.attribution ? ` · DEM: ${scene.surface.attribution}` : '');
  const canCoreg = !!(scene.series.ICESAT2 && scene.series.GLAS);   // co-registration needs ICESat-2 (ATL03) + ICESat-1 (GLAS)
  $('btnCoreg').hidden = !!coreg;
  $('btnCoreg').disabled = !canCoreg;
  $('btnCoreg').title = canCoreg ? 'Align ICESat-2 and ICESat-1 to a common reference frame and epoch, then compare heights'
    : 'Needs both ICESat-2 (ATL03 photons) and ICESat-1 (GLAS) in this scene';
  $('coregOn').hidden = !coreg;   // persistent "Co-registered ✓" + the correction toggles (no disappearing control)
  $('lblGia').hidden = !(coreg && coreg.gia);
}

async function loadScene() {
  let doc;
  try { doc = await api.sceneDoc(sceneId); } catch (e) { $('status').textContent = 'could not load this scene'; AICESAT.showError(`scene ${sceneId}: ${e.message}`); return; }
  scene = doc;
  coreg = scene.coreg;
  Object.keys(scene.series).forEach(m => { visible[m] = true; });   // all missions on by default for a new scene
  $('adjPlate').checked = adj.plate_motion;
  $('adjGia').checked = adj.gia;
  $('zexag').value = Z_EXAG; $('zexagVal').textContent = Z_EXAG; syncExag();
  fitView(); render(); updateLabels(); updateStats();
  if (coreg && !('gia' in coreg)) {   // saved before the GIA correction existed -> recompute in the background, then refresh
    $('status').textContent = 'updating co-registration…';
    api.coregister(sceneId).then(c => { coreg = scene.coreg = c; $('status').textContent = ''; updateStats(); updateLabels(); })
      .catch(e => { $('status').textContent = ''; console.warn('[aicesat] coreg refresh failed', e); });
  }
  console.log('[aicesat] scene loaded', Object.entries(scene.series).map(([m, s]) => m + ':' + s.n).join(' '), 'surface', scene.surface ? scene.surface.n_cells_observed : 'none', 'meshOk', meshOk);
}

async function ensureCoreg() {
  if (coreg) return true;
  $('status').textContent = 'co-registering \u2014 aligning the missions to a common reference frame and epoch\u2026'; $('btnCoreg').disabled = true;
  try { coreg = await api.coregister(sceneId); } catch (e) { $('status').textContent = 'co-registration failed: ' + e.message; $('btnCoreg').disabled = false; return false; }
  $('status').textContent = coreg.cached ? 'co-registration cached' : `co-registration computed in ${coreg.compute_seconds}s`;
  updateLabels(); return true;
}
$('btnCoreg').onclick = async () => { if (await ensureCoreg()) updateStats(); };
$('adjPlate').onchange = e => { adj.plate_motion = e.target.checked; updateStats(); };
$('adjGia').onchange = e => { adj.gia = e.target.checked; updateStats(); };
const syncExag = () => { const w = $('exagWarn'); if (w) { w.hidden = Z_EXAG <= 1; w.textContent = 'Heights exaggerated \u00d7' + Z_EXAG + ' \u2014 vertical only'; } };
$('zexag').oninput = e => { Z_EXAG = parseFloat(e.target.value); $('zexagVal').textContent = Z_EXAG; syncExag(); render(); updateLabels(); };
$('imagery').onchange = e => { SHOW_IMAGERY = e.target.checked; render(); };
$('pairs').onchange = e => { SHOW_PAIRS = e.target.checked; render(); };


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
// opt-in "?" help on the jargon-heaviest labels (Corrections group, Δh panel)
{ const U = AICESAT.util, G = U.GLOSSARY, heads = root.querySelectorAll('#controls .ctl-head');
  if (heads[0]) heads[0].appendChild(U.help(G.coreg));
  const dhH = root.querySelector('#stats h2'); if (dhH) dhH.appendChild(U.help(G.dh));
  const lp = $('lblPlate'); if (lp) lp.appendChild(U.help(G.plate));
  const lg = $('lblGia'); if (lg) lg.appendChild(U.help(G.gia)); }
AICESAT.util.drawer(root, null);
$('stats').addEventListener('reopen', () => updateStats());
// 3-D navigation hint over the canvas, auto-dismissed on first interaction (or after a few seconds)
{ const nh = $('navhint'); if (nh) { $('deck').addEventListener('pointerdown', () => nh.classList.add('hide'), {once: true}); setTimeout(() => nh.classList.add('hide'), 6000); } }
void back;   // Back now lives in the top bar; the legacy in-legend button was removed

// ---------------------------------------------------------------- view API
this.open = async (id, query) => {
  root.classList.add('on');
  params = new URLSearchParams(query || '');
  if (params.get('zexag')) { Z_EXAG = parseFloat(params.get('zexag')); }
  if (id !== sceneId) { sceneId = id; scene = null; coreg = null; bounds = null; deckgl.setProps({layers: []}); const _ld = $('sceneLoading'); if (_ld) _ld.hidden = false; try { await loadScene(); } finally { if (_ld) _ld.hidden = true; } loadBench(); }
  else deckgl.redraw && deckgl.redraw(true);
};
this.hide = () => root.classList.remove('on');

  }
};
