/* Demo B widget: two point clouds, OFF/ON co-registration toggle, Δh histograms, honesty labels. */
const {Deck, OrbitView, PointCloudLayer} = deck;
const params = new URLSearchParams(location.search);
const sceneId = params.get('scene');
const Z_EXAG = parseFloat(params.get('zexag') || '10');

let scene = null, coreg = null, state = 'off';
const $ = id => document.getElementById(id);

const deckgl = new Deck({
  parent: $('deck'),
  onError: e => console.error('[aicesat] deck error', e && e.message),
  onLoad: () => console.log('[aicesat] deck loaded'),
  views: new OrbitView({orbitAxis: 'Z', fovy: 45}),
  initialViewState: {target: [0, 0, 0], rotationX: 35, rotationOrbit: -25, zoom: -6, minZoom: -12, maxZoom: 6},
  controller: true,
  layers: [],
});

function unflat(flat) { const out = new Array(flat.length / 3); for (let i = 0; i < out.length; i++) out[i] = i; return out; }

function layerFor(mission, s) {
  // positions: native flat array; coreg: optional flat array of *display* positions (already exaggerated server-side? no: client-side)
  const nat = s.positions;
  const disp = (state === 'on' && coreg && coreg.display_positions[mission]) ? coreg.display_positions[mission] : null;
  const idx = unflat(nat);
  return new PointCloudLayer({
    id: 'pc-' + mission,
    data: idx,
    getPosition: i => {
      const src = disp || nat;
      return [src[3 * i], src[3 * i + 1], src[3 * i + 2] * Z_EXAG];
    },
    getColor: s.color,
    pointSize: mission === 'GLAS' ? 4 : 2,
    sizeUnits: 'pixels',
    updateTriggers: {getPosition: [state, !!disp]},
    transitions: {getPosition: {duration: 900, easing: t => t < .5 ? 2 * t * t : -1 + (4 - 2 * t) * t}},
  });
}

function render() {
  if (!scene) return;
  const layers = Object.entries(scene.series).map(([m, s]) => layerFor(m, s));
  deckgl.setProps({layers});
}

function fitView() {
  const all = Object.values(scene.series).flatMap(s => s.positions);
  if (!all.length) return;
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
  for (let i = 0; i < all.length; i += 3) { minx = Math.min(minx, all[i]); maxx = Math.max(maxx, all[i]); miny = Math.min(miny, all[i + 1]); maxy = Math.max(maxy, all[i + 1]); }
  const span = Math.max(maxx - minx, maxy - miny) || 1;
  const zoom = Math.log2(Math.min(innerWidth, innerHeight) / span) ;
  deckgl.setProps({initialViewState: {target: [(minx + maxx) / 2, (miny + maxy) / 2, 0], rotationX: 35, rotationOrbit: -25, zoom, minZoom: zoom - 6, maxZoom: zoom + 8}});
}

function drawHist(canvas, values, {color, refLine = 0, range} = {}) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * devicePixelRatio, H = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  if (!values || !values.length) { ctx.fillStyle = '#777'; ctx.font = `${12 * devicePixelRatio}px sans-serif`; ctx.fillText('no pairs', 10, 20); return; }
  const [lo, hi] = range;
  const nb = 40, counts = new Array(nb).fill(0);
  for (const v of values) { const b = Math.floor((v - lo) / (hi - lo) * nb); if (b >= 0 && b < nb) counts[b]++; }
  const max = Math.max(...counts) || 1;
  const bw = W / nb;
  ctx.fillStyle = color;
  counts.forEach((c, i) => { const h = c / max * (H - 18 * devicePixelRatio); ctx.fillRect(i * bw + 1, H - h - 14 * devicePixelRatio, bw - 2, h); });
  // reference line at refLine
  const x0 = (refLine - lo) / (hi - lo) * W;
  ctx.strokeStyle = '#ddd'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x0, 0); ctx.lineTo(x0, H - 14 * devicePixelRatio); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = '#999'; ctx.font = `${10 * devicePixelRatio}px sans-serif`;
  ctx.fillText(`${lo.toFixed(2)} m`, 2, H - 2); const t = `${hi.toFixed(2)} m`; ctx.fillText(t, W - ctx.measureText(t).width - 2, H - 2);
}

function updateStats() {
  if (!coreg) { $('stats').hidden = true; return; }
  $('stats').hidden = false;
  const on = state === 'on';
  const dh = on ? coreg.dh_coreg : coreg.dh_native;
  const st = on ? coreg.stats.coreg : coreg.stats.native;
  drawHist($('histDh'), dh, {color: on ? '#378ADD' : '#D85A30', range: coreg.stats.dh_range});
  $('readout1').innerHTML = `median Δh = <b>${(st.median * 100).toFixed(1)} cm</b> (MAD ${(st.mad * 100).toFixed(1)} cm, n = ${st.n}) — ` +
    (on ? 'plate-motion artifact removed; remaining Δh is real change + unresolved terms'
        : 'includes the plate-motion registration artifact');
  drawHist($('histArt'), coreg.artifact, {color: '#E0A030', range: coreg.stats.artifact_range});
  const a = coreg.stats.artifact;
  const vs = coreg.relative_shift_vector_m;
  $('readout2').innerHTML = `plate-motion artifact: median <b>${(a.median * 100).toFixed(2)} cm</b> vertical (MAD ${(a.mad * 100).toFixed(2)} cm), from a true horizontal displacement of ` +
    `<b>${(coreg.displacement_m * 100).toFixed(1)} cm</b> over ${coreg.years_apart.toFixed(1)} yr; regional plane slope ${coreg.comparability.surface_slope_deg.toFixed(2)}°` +
    (coreg.along_track_slope_deg != null ? `, median along-beam slope at the pairs ${coreg.along_track_slope_deg.toFixed(2)}°` : '') +
    ` <span class="small">(${coreg.dh_estimator}; only the along-beam component of the shift is observable)</span>` +
    ` <span class="small">(${coreg.comparability.horizontal_to_vertical_sensitivity}; ${coreg.n_pairs.gross_outliers_dropped.native} gross pairs > ${coreg.n_pairs.gross_outliers_dropped.threshold_m} m dropped)</span>`;
  const c = coreg.comparability;
  $('unresolved').innerHTML = `<b>Unresolved (not corrected, in both states):</b> ${c.unresolved.join(', ')}` +
    `<br>Applied: plate motion (ITRF2014-PMM, ${coreg.common_frame} @ ${coreg.common_epoch}); ${c.ellipsoid_correction_applied}` +
    `<br>Frame step ${coreg.native_frames.GLAS}→${coreg.common_frame} shifts GLAS heights by ${(coreg.frame_vertical_shift_m.GLAS * 1000).toFixed(1)} mm (in the ON Δh, not in the artifact panel)` +
    (c.dynamic_ice_flag === true ? '<br><b style="color:#D85A30">dynamic_ice_flag = true — ice flow is NOT corrected; trajectory may mislead</b>'
      : c.dynamic_ice_flag === null ? `<br>Dynamic ice: <b>unknown</b> — ${c.dynamic_ice_note}` : '');
}

function updateLabels() {
  $('title').textContent = 'Cross-mission altimetry — ' + (Object.keys(scene.series).join(' + ') || 'empty scene');
  $('question').textContent = scene.question || '';
  $('legend').innerHTML = Object.entries(scene.series).map(([m, s]) =>
    `<span><span class="dot" style="background:rgb(${s.color.join(',')})"></span>${m} · ${s.n.toLocaleString()} pts · ${s.meta.product} · ${s.meta.native_frame}</span>`).join('');
  $('framenote').textContent = `Local frame ${scene.frame.crs}, bbox ${scene.bbox.map(v => v.toFixed(2)).join(', ')}; z relative to ICESat-2 median; vertical ×${Z_EXAG}.`;
  if (coreg) {
    $('exag').hidden = false;
    $('exag').innerHTML = `<b>Horizontal offset exaggerated ×${coreg.exaggeration}</b> for visibility; true plate-motion displacement ≈ ` +
      `<b>${(coreg.displacement_m * 100).toFixed(1)} cm</b> between epochs ${coreg.epochs.ICESAT2.toFixed(1)} and ${coreg.epochs.GLAS.toFixed(1)}. ` +
      `Readout numbers are un-exaggerated.`;
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
  console.log('[aicesat] scene loaded', Object.entries(scene.series).map(([m,s])=>m+':'+s.n).join(' '), 'viewState', JSON.stringify(deckgl.props.initialViewState));
}

async function setState(s) {
  if (s === 'on' && !coreg) {
    $('status').textContent = 'running ITRF+epoch co-registration (pyproj)…';
    $('btnOn').disabled = true;
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
