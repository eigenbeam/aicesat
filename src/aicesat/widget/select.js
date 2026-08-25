/* Area selector: EOX Sentinel-2 tiles on a MapView, box/polygon drawing, coverage check, scene build job. */
const {Deck, MapView, TileLayer, BitmapLayer, PolygonLayer, PathLayer, TextLayer, GeoJsonLayer} = deck;
const $ = id => document.getElementById(id);
let mode = 'box', drawing = false, box = null, poly = [], polyClosed = false, cursor = null, regionsData = {}, lakeCells = null;

const deckgl = new Deck({
  parent: $('map'),
  views: new MapView({repeat: false}),
  initialViewState: {longitude: -42, latitude: 71.5, zoom: 3.6, minZoom: 2, maxZoom: 13},
  controller: {dragPan: true, doubleClickZoom: false},
  layers: [],
  getCursor: () => (mode === 'box' && !polyClosed) ? 'crosshair' : 'crosshair',
  onDragStart: (info, ev) => { if (mode === 'box' && info.coordinate) { drawing = true; box = {a: info.coordinate, b: info.coordinate}; poly = []; polyClosed = false; deckgl.setProps({controller: {dragPan: false, doubleClickZoom: false}}); } },
  onDrag: (info) => { if (drawing && info.coordinate) { box.b = info.coordinate; render(); } },
  onDragEnd: (info) => { if (drawing) { drawing = false; if (info.coordinate) box.b = info.coordinate; deckgl.setProps({controller: {dragPan: true, doubleClickZoom: false}}); render(); updateCoords(); } },
  onClick: (info) => {
    if (mode !== 'poly' || !info.coordinate) return;
    if (polyClosed) { poly = []; polyClosed = false; }
    poly.push(info.coordinate); box = null; render(); updateCoords();
  },
  onHover: (info) => { if (mode === 'poly' && !polyClosed && poly.length && info.coordinate) { cursor = info.coordinate; render(); } },
});

function bboxOf() {
  if (box) return [Math.min(box.a[0], box.b[0]), Math.min(box.a[1], box.b[1]), Math.max(box.a[0], box.b[0]), Math.max(box.a[1], box.b[1])].map(v => +v.toFixed(4));
  return null;
}
function area() {
  if (box) return {bbox: bboxOf()};
  if (polyClosed && poly.length >= 3) return {polygon: poly.map(p => [+p[0].toFixed(4), +p[1].toFixed(4)])};
  return null;
}

function render() {
  const layers = [
    new TileLayer({
      id: 'eox', data: 'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg',
      minZoom: 0, maxZoom: 13, tileSize: 256,
      renderSubLayers: p => { const {west, south, east, north} = p.tile.bbox; return new BitmapLayer(p, {data: null, image: p.data, bounds: [west, south, east, north]}); },
    }),
  ];
  if (lakeCells) layers.push(new GeoJsonLayer({id: 'lake', data: lakeCells, stroked: true, filled: true, getFillColor: [55, 138, 221, 40], getLineColor: [55, 138, 221, 140], lineWidthMinPixels: 1}));
  const regs = Object.entries(regionsData).map(([k, v]) => ({name: k, poly: [[v.bbox[0], v.bbox[1]], [v.bbox[2], v.bbox[1]], [v.bbox[2], v.bbox[3]], [v.bbox[0], v.bbox[3]]]}));
  layers.push(new PolygonLayer({id: 'regions', data: regs, getPolygon: d => d.poly, filled: false, stroked: true, getLineColor: [224, 160, 48, 200], lineWidthMinPixels: 1.5}));
  layers.push(new TextLayer({id: 'region-labels', data: regs, getPosition: d => [(d.poly[0][0] + d.poly[1][0]) / 2, d.poly[2][1]], getText: d => d.name, getSize: 11, getColor: [224, 160, 48], getTextAnchor: 'middle', getAlignmentBaseline: 'bottom', characterSet: 'auto', background: true, getBackgroundColor: [20, 20, 26, 160]}));
  if (box) {
    const b = bboxOf();
    layers.push(new PolygonLayer({id: 'box', data: [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]], getPolygon: d => d, getFillColor: [55, 138, 221, 50], getLineColor: [120, 190, 255], lineWidthMinPixels: 2}));
  }
  if (poly.length) {
    const pts = polyClosed ? poly : (cursor ? [...poly, cursor] : poly);
    if (polyClosed) layers.push(new PolygonLayer({id: 'poly', data: [poly], getPolygon: d => d, getFillColor: [55, 138, 221, 50], getLineColor: [120, 190, 255], lineWidthMinPixels: 2}));
    else layers.push(new PathLayer({id: 'poly-path', data: [pts], getPath: d => d, getColor: [120, 190, 255], getWidth: 2, widthUnits: 'pixels'}));
    layers.push(new deck.ScatterplotLayer({id: 'poly-verts', data: poly, getPosition: d => d, getRadius: 4, radiusUnits: 'pixels', getFillColor: [255, 255, 255]}));
  }
  deckgl.setProps({layers});
}

function closePolygon() { if (mode === 'poly' && poly.length >= 3 && !polyClosed) { polyClosed = true; render(); updateCoords(); } }
function updateCoords() {
  const a = area();
  $('coords').textContent = a ? JSON.stringify(a) : (poly.length ? `polygon: ${poly.length} vertices (need 3, then Close polygon)` : 'no area selected');
  $('cov').disabled = $('build').disabled = !a;
  $('closePoly').hidden = !(mode === 'poly' && !polyClosed && poly.length >= 3);
}
$('closePoly').onclick = closePolygon;
document.addEventListener('keydown', e => { if (e.key === 'Enter') closePolygon(); });

$('modeBox').onclick = () => { mode = 'box'; $('modeBox').classList.add('on'); $('modePoly').classList.remove('on'); updateCoords(); };
$('modePoly').onclick = () => { mode = 'poly'; $('modePoly').classList.add('on'); $('modeBox').classList.remove('on'); updateCoords(); };
$('clear').onclick = () => { box = null; poly = []; polyClosed = false; render(); updateCoords(); $('out').textContent = ''; };
$('regionSel').onchange = e => { const r = regionsData[e.target.value]; if (!r) return; box = {a: [r.bbox[0], r.bbox[1]], b: [r.bbox[2], r.bbox[3]]}; poly = []; polyClosed = false;
  deckgl.setProps({initialViewState: {longitude: (r.bbox[0] + r.bbox[2]) / 2, latitude: (r.bbox[1] + r.bbox[3]) / 2, zoom: 7, minZoom: 2, maxZoom: 13}}); render(); updateCoords(); };

$('cov').onclick = async () => {
  const a = area(); if (!a) return;
  $('out').textContent = 'checking CMR…';
  const qs = a.bbox ? `bbox=${encodeURIComponent(JSON.stringify(a.bbox))}` : `polygon=${encodeURIComponent(JSON.stringify(a.polygon))}`;
  const r = await fetch(`/api/coverage?${qs}`); const d = await r.json();
  if (!r.ok) { $('out').textContent = 'error: ' + d.error; return; }
  $('out').textContent = `ATL03 v${d.ATL03.version} ${d.ATL03.window.join('..')}: ${d.ATL03.n_granules} granules ${JSON.stringify(d.ATL03.by_month)}\n` +
    `GLAH06 v${d.GLAH06.version}: ${d.GLAH06.n_granules} granules by campaign ${JSON.stringify(d.GLAH06.by_campaign)}\n` +
    (d.both_present ? 'both missions present' : 'NOT both missions present') + (a.polygon ? '\n(coverage is computed on the polygon\'s bounding box)' : '');
};

$('build').onclick = async () => {
  const a = area(); if (!a) return;
  const body = {...a, max_granules: +$('maxg').value, with_glas: $('glas').checked, with_coreg: $('coreg').checked, question: 'area selected on the map'};
  $('build').disabled = true; $('out').textContent = 'starting build…';
  const r = await fetch('/api/extract', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) { $('out').textContent = 'error: ' + d.error; $('build').disabled = false; return; }
  const poll = async () => {
    const j = await (await fetch(`/api/job/${d.job_id}`)).json();
    $('out').innerHTML = `<b>job ${j.id}: ${j.status}</b>${j.seconds ? ` (${j.seconds}s)` : ''}\n` + j.log.join('\n') +
      (j.status === 'done' ? `\n\n<a href="${j.widget_url}" target="_blank">open the 3D scene →</a>` : '') + (j.error ? `\n${j.error}` : '');
    if (j.status === 'running') setTimeout(poll, 1500); else { $('build').disabled = false; fetch('/api/lake_cells').then(r => r.json()).then(g => { lakeCells = g; render(); }); }
  };
  poll();
};

(async () => {
  try { regionsData = await (await fetch('/api/regions')).json(); for (const k of Object.keys(regionsData)) { const o = document.createElement('option'); o.value = k; o.textContent = k; $('regionSel').appendChild(o); } } catch (e) {}
  try { lakeCells = await (await fetch('/api/lake_cells')).json(); } catch (e) {}
  render(); updateCoords();
})();
