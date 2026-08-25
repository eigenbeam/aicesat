/* Demo B widget: two point clouds, OFF/ON co-registration toggle, Δh histograms, honesty labels,
   plus visual cues: interpolated surface, paired-shot highlighting + native ghost, scale bar / north / shift arrows. */
const {Deck, OrbitView, PointCloudLayer, PathLayer, TextLayer, SimpleMeshLayer} = deck;
const params = new URLSearchParams(location.search);
const sceneId = params.get('scene');
const Z_EXAG = parseFloat(params.get('zexag') || '10');

let scene = null, coreg = null, state = 'off', bounds = null, meshOk = true;
const $ = id => document.getElementById(id);
const GHOST = [170, 170, 180, 70], DIM_GLAS = [216, 90, 48, 60], CUE = [230, 230, 235, 200], BLUE = [55, 138, 221];

const deckgl = new Deck({
  parent: $('deck'),
  onError: e => { console.error('[aicesat] deck error', e && e.message); if (/mesh/i.test(String(e && e.message))) { meshOk = false; render(); } },
  onLoad: () => console.log('[aicesat] deck loaded'),
  views: new OrbitView({orbitAxis: 'Z', fovy: 45}),
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
    getColor: color, pointSize: size, sizeUnits: 'pixels',
  }, opts));
}

function cloudLayers() {
  const out = [];
  for (const [m, s] of Object.entries(scene.series)) {
    const nat = s.positions;
    const disp = (state === 'on' && coreg && coreg.display_positions[m]) ? coreg.display_positions[m] : null;
    const src = disp || nat;
    const paired = (coreg && coreg.pair_display_indices && coreg.pair_display_indices[m]) ? new Set(coreg.pair_display_indices[m]) : null;
    if (m === 'ICESAT2' && disp) out.push(cloudLayer('ghost-' + m, nat, GHOST, 1.5));          // where it was (native)
    out.push(new PointCloudLayer({
      id: 'pc-' + m, data: indices(nat.length / 3),
      getPosition: i => [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG],
      getColor: paired ? (i => paired.has(i) ? s.color : DIM_GLAS) : s.color,
      pointSize: m === 'GLAS' ? (paired ? 3 : 4) : 2, sizeUnits: 'pixels',
      updateTriggers: {getPosition: [state, !!disp], getColor: [!!paired]},
      transitions: {getPosition: {duration: 900, easing: ease}},
    }));
    if (paired) {  // paired shots on top, bigger, with a bright rim
      const pi = [...paired];
      out.push(new PointCloudLayer({
        id: 'paired-' + m, data: pi,
        getPosition: i => [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG],
        getColor: [255, 214, 120], pointSize: 7, sizeUnits: 'pixels',
        updateTriggers: {getPosition: [state, !!disp]}, transitions: {getPosition: {duration: 900, easing: ease}},
      }));
    }
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
      layers.push(new SimpleMeshLayer({
        id: 'surface-mesh', data: [{}],
        mesh: {attributes: {positions: {value: positions, size: 3}, normals: {value: normals, size: 3}}, indices: {value: new Uint32Array(idx)}},
        getPosition: () => [0, 0, 0], getColor: [150, 160, 185, 55], material: {ambient: 0.6, diffuse: 0.5, shininess: 8},
        parameters: {depthWriteEnabled: false},
      }));
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
function niceLength(span) { const t = span / 5; const p = Math.pow(10, Math.floor(Math.log10(t))); return [1, 2, 5, 10].map(m => m * p).reduce((a, b) => Math.abs(b - t) < Math.abs(a - t) ? b : a); }
function arrow(from, dir, len, head = 0.18) {
  const [dx, dy] = dir, to = [from[0] + dx * len, from[1] + dy * len, from[2]];
  const px = -dy, py = dx, h = len * head;
  return [[from, to], [[to[0] - (dx * 0.7 + px * 0.5) * h, to[1] - (dy * 0.7 + py * 0.5) * h, to[2]], to, [to[0] - (dx * 0.7 - px * 0.5) * h, to[1] - (dy * 0.7 - py * 0.5) * h, to[2]]]];
}
function cueLayers() {
  if (!bounds) return [];
  const {minx, maxx, miny, maxy, minz} = bounds;
  const span = Math.max(maxx - minx, maxy - miny), zc = minz * Z_EXAG - 0.02 * span;
  const paths = [], texts = [];
  // scale bar
  const L = niceLength(span), bx = minx, by = miny - 0.06 * span, tick = 0.012 * span;
  paths.push({p: [[bx, by, zc], [bx + L, by, zc]], c: CUE, w: 2});
  paths.push({p: [[bx, by - tick, zc], [bx, by + tick, zc]], c: CUE, w: 2}); paths.push({p: [[bx + L, by - tick, zc], [bx + L, by + tick, zc]], c: CUE, w: 2});
  texts.push({position: [bx + L / 2, by - 0.03 * span, zc], text: L >= 1000 ? `${L / 1000} km` : `${L} m`, color: CUE});
  // north arrow
  const [nx_, ny_] = scene.frame.north_xy, nb = [bx + L + 0.12 * span, by, zc], nl = 0.12 * span;
  for (const seg of arrow(nb, [nx_, ny_], nl)) paths.push({p: seg, c: CUE, w: 2});
  texts.push({position: [nb[0] + nx_ * nl * 1.25, nb[1] + ny_ * nl * 1.25, zc], text: 'N', color: CUE});
  // plate-motion shift direction, in the cue cluster below the scale bar. Drawn at a readable length (not the
  // clouds' exaggeration: even x10000 is a ~3 km stub that vanishes under foreshortening) and labelled as such.
  if (coreg) {
    const v = coreg.relative_shift_vector_m, len = Math.hypot(v[0], v[1]), dir = [v[0] / len, v[1] / len];
    const al = 0.10 * span, sb = [bx + 0.02 * span, by - 0.10 * span, zc];
    for (const seg of arrow(sb, dir, al, 0.25)) paths.push({p: seg, c: BLUE, w: 4});
    texts.push({position: [bx, sb[1] - 0.13 * span, zc], anchor: 'start',
      text: `direction of ICESat-2 shift to common epoch (arrow length not to scale)\n${(len * 100).toFixed(1)} cm true; clouds and ghost drawn ×${coreg.exaggeration}`, color: BLUE});
  }
  return [
    new PathLayer({id: 'cues', data: paths, getPath: d => d.p, getColor: d => d.c, getWidth: d => d.w, widthUnits: 'pixels'}),
    new TextLayer({id: 'cue-text', data: texts, getPosition: d => d.position, getText: d => d.text, getColor: d => d.color, getTextAnchor: d => d.anchor || 'middle',
      getSize: 13, sizeUnits: 'pixels', billboard: true, fontFamily: 'ui-sans-serif, system-ui, sans-serif', characterSet: 'auto',
      background: true, getBackgroundColor: [20, 20, 26, 190], backgroundPadding: [4, 2]}),
  ];
}

// ---------------------------------------------------------------- render / view
function render() {
  if (!scene) return;
  deckgl.setProps({layers: [...surfaceLayers(), ...cloudLayers(), ...cueLayers()]});
}

function fitView() {
  const all = Object.values(scene.series).flatMap(s => s.positions);
  if (!all.length) return;
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9, minz = 1e9;
  for (let i = 0; i < all.length; i += 3) { minx = Math.min(minx, all[i]); maxx = Math.max(maxx, all[i]); miny = Math.min(miny, all[i + 1]); maxy = Math.max(maxy, all[i + 1]); minz = Math.min(minz, all[i + 2]); }
  bounds = {minx, maxx, miny, maxy, minz};
  const span = Math.max(maxx - minx, maxy - miny) || 1;
  const zoom = Math.log2(Math.min(innerWidth, innerHeight) / (span * 1.25));
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
  ctx.fillStyle = '#999'; ctx.font = `${10 * devicePixelRatio}px sans-serif`;
  ctx.fillText(`${lo.toFixed(2)} m`, 2, H - 2); const t = `${hi.toFixed(2)} m`; ctx.fillText(t, W - ctx.measureText(t).width - 2, H - 2);
}

function updateStats() {
  if (!coreg) { $('stats').hidden = true; return; }
  $('stats').hidden = false;
  const on = state === 'on';
  const dh = on ? coreg.dh_coreg : coreg.dh_native, st = on ? coreg.stats.coreg : coreg.stats.native;
  drawHist($('histDh'), dh, {color: on ? '#378ADD' : '#D85A30', range: coreg.stats.dh_range});
  $('readout1').innerHTML = `median Δh = <b>${(st.median * 100).toFixed(1)} cm</b> (MAD ${(st.mad * 100).toFixed(1)} cm, n = ${st.n}) — ` +
    (on ? 'plate-motion artifact removed; remaining Δh is real change + unresolved terms' : 'includes the plate-motion registration artifact');
  drawHist($('histArt'), coreg.artifact, {color: '#E0A030', range: coreg.stats.artifact_range});
  const a = coreg.stats.artifact, c = coreg.comparability;
  $('readout2').innerHTML = `plate-motion artifact: median <b>${(a.median * 100).toFixed(2)} cm</b> vertical (MAD ${(a.mad * 100).toFixed(2)} cm), from a true horizontal displacement of ` +
    `<b>${(coreg.displacement_m * 100).toFixed(1)} cm</b> over ${coreg.years_apart.toFixed(1)} yr; regional plane slope ${c.surface_slope_deg.toFixed(2)}°` +
    (coreg.along_track_slope_deg != null ? `, median along-beam slope at the pairs ${coreg.along_track_slope_deg.toFixed(2)}°` : '') +
    ` <span class="small">(${coreg.dh_estimator}; only the along-beam component of the shift is observable; ${coreg.n_pairs.gross_outliers_dropped.native} gross pairs > ${coreg.n_pairs.gross_outliers_dropped.threshold_m} m dropped)</span>`;
  $('unresolved').innerHTML = `<b>Unresolved (not corrected, in both states):</b> ${c.unresolved.join(', ')}` +
    `<br>Applied: plate motion (ITRF2014-PMM, ${coreg.common_frame} @ ${coreg.common_epoch}); ${c.ellipsoid_correction_applied}` +
    `<br>Frame step ${coreg.native_frames.GLAS}→${coreg.common_frame} shifts GLAS heights by ${(coreg.frame_vertical_shift_m.GLAS * 1000).toFixed(1)} mm (in the ON Δh, not in the artifact panel)` +
    (c.dynamic_ice_flag === true ? '<br><b style="color:#D85A30">dynamic_ice_flag = true — ice flow is NOT corrected; trajectory may mislead</b>'
      : c.dynamic_ice_flag === null ? `<br>Dynamic ice: <b>unknown</b> — ${c.dynamic_ice_note}` : '');
}

function updateLabels() {
  $('title').textContent = 'Cross-mission altimetry — ' + (Object.keys(scene.series).join(' + ') || 'empty scene');
  $('question').textContent = scene.question || '';
  const items = Object.entries(scene.series).map(([m, s]) =>
    `<span><span class="dot" style="background:rgb(${s.color.join(',')})"></span>${m} · ${s.n.toLocaleString()} pts · ${s.meta.product} · ${s.meta.native_frame}</span>`);
  if (coreg && coreg.pair_display_indices) items.push(`<span><span class="dot" style="background:rgb(255,214,120)"></span>paired GLAS shots (n = ${coreg.pair_display_indices.GLAS.length}) — the only points behind the histograms</span>`);
  if (coreg) items.push(`<span><span class="dot" style="background:rgb(170,170,180)"></span>ghost = ICESat-2 native position (ON state)</span>`);
  if (scene.surface) items.push(`<span><span class="dot" style="background:rgb(150,160,185)"></span>surface: ${scene.surface.note}</span>`);
  const acc = scene.series.ICESAT2 && scene.series.ICESAT2.meta.access;
  if (acc) items.push(`<span class="small">data path: ${scene.series.ICESAT2.meta.access_path} — ${acc.chunks} chunks / ${acc.requests} range requests / ${(acc.bytes / 1e6).toFixed(0)} MB fetched, ${acc.chunks_skipped_already_materialized} chunks already in the lake, ${acc.hdf5_opens_at_query_time} HDF5 opens at query time; ${acc.cells} H3 cells (res ${acc.h3_res})</span>`);
  $('legend').innerHTML = items.join('');
  $('framenote').textContent = `Local frame ${scene.frame.crs}, bbox ${scene.bbox.map(v => v.toFixed(2)).join(', ')}; z relative to ICESat-2 median; vertical ×${Z_EXAG}; scale bar and north arrow in-scene.`;
  if (coreg) {
    $('exag').hidden = false;
    $('exag').innerHTML = `<b>Horizontal offset exaggerated ×${coreg.exaggeration}</b> for visibility (clouds and blue arrow alike); true plate-motion displacement ≈ ` +
      `<b>${(coreg.displacement_m * 100).toFixed(1)} cm</b> between epochs ${coreg.epochs.ICESAT2.toFixed(1)} and ${coreg.epochs.GLAS.toFixed(1)}. Readout numbers are un-exaggerated.`;
  }
  $('btnOn').disabled = false;
}

async function loadScene() {
  const r = await fetch(`/api/scene/${sceneId}`);
  if (!r.ok) { $('status').textContent = `scene ${sceneId} not found`; return; }
  scene = await r.json();
  coreg = scene.coreg;
  if (params.get('state') === 'on' && coreg) state = 'on';
  $('btnOff').classList.toggle('on', state === 'off'); $('btnOn').classList.toggle('on', state === 'on');
  fitView(); render(); updateLabels(); updateStats();
  console.log('[aicesat] scene loaded', Object.entries(scene.series).map(([m, s]) => m + ':' + s.n).join(' '), 'surface', scene.surface ? scene.surface.n_cells_observed : 'none', 'meshOk', meshOk);
}

async function setState(s) {
  if (s === 'on' && !coreg) {
    $('status').textContent = 'running ITRF+epoch co-registration (pyproj)…'; $('btnOn').disabled = true;
    const r = await fetch(`/api/coregister/${sceneId}`, {method: 'POST'});
    if (!r.ok) { $('status').textContent = 'co-registration failed: ' + (await r.text()); $('btnOn').disabled = false; return; }
    coreg = await r.json();
    $('status').textContent = coreg.cached ? 'co-registration (cached)' : `co-registration computed live in ${coreg.compute_seconds}s`;
    updateLabels();
  }
  state = s;
  $('btnOff').classList.toggle('on', s === 'off'); $('btnOn').classList.toggle('on', s === 'on');
  render(); updateStats();
}
$('btnOff').onclick = () => setState('off');
$('btnOn').onclick = () => setState('on');
loadScene();
