/* Shared 3-D globe: Natural Earth basemap, candidate regions, lake cells, global H3 grid (resolution by zoom, hover
   stats), per-mission coverage hexes (the survey view), scene footprints (ready/loading/error), box/polygon drawing,
   cell selection. No flat projection. Used by Survey, Explore and Lake. Navigate = drag to spin / scroll to zoom;
   Box/Polygon capture the drag for drawing. */
window.AICESAT = window.AICESAT || {};
AICESAT.MapView = class {
  constructor(container, opts = {}) {
    const {Deck, _GlobeView, GlobeView} = deck;
    const Globe = GlobeView || _GlobeView;
    this.opts = Object.assign({grid: false, gridStats: true, selectCells: false, draw: true, footprints: true}, opts);
    this.container = container;
    this.state = {mode: 'pan', drawing: false, box: null, poly: [], polyClosed: false, cursor: null, cells: null, indexCells: null, claims: [], cellStats: {},
                  scenes: [], grid: this.opts.grid, gridRes: 3, selected: new Set(), hover: null, viewState: {longitude: -42, latitude: 66, zoom: 1.3}};
    this.tooltip = AICESAT.util.el('div', {class: 'tooltip'}); this.tooltip.hidden = true; container.appendChild(this.tooltip);
    this.badge = AICESAT.util.el('div', {class: 'mode-badge'}); this.badge.hidden = true; container.appendChild(this.badge);   // on-map "you are in X mode" indicator
    this.onSelect = () => {}; this.onOpenScene = () => {}; this.onCellsSelected = () => {};
    this.coverage = null; this._covKey = 0; this.onHexClick = () => {};
    this.namesEl = AICESAT.util.el('div', {class: 'name-overlay'}); container.appendChild(this.namesEl);
    this.deck = new Deck({
      parent: container, views: new Globe({resolution: 12}),
      // The device is created asynchronously: layers set before it is ready never drew, so a view sat blank until the
      // first drag or scroll. Draw once it exists.
      onLoad: () => { this._covMemo = null; this._gridCache = null; this.render(); },
      // Names follow the frame actually drawn (placed before layout they piled into the corner of a 300x150 canvas).
      onAfterRender: () => this._namesAfterRender(),
      initialViewState: {...this.state.viewState, minZoom: 0, maxZoom: 12}, controller: {dragPan: true, dragRotate: true, doubleClickZoom: false}, layers: [],
      onViewStateChange: ({viewState, interactionState}) => {
        this.state.viewState = viewState;
        const act = interactionState && (interactionState.isDragging || interactionState.isZooming || interactionState.isPanning || interactionState.inTransition);
        this._interacting = !!act;
        this._renderSoon();
        if (act) { clearTimeout(this._settle); this._settle = setTimeout(() => { this._interacting = false; this.render(); }, 180); }   // rebuild the grid once the view settles
      },
      getCursor: ({isDragging}) => (this.opts.draw && this.state.mode !== 'pan') ? 'crosshair' : (isDragging ? 'grabbing' : 'grab'),
      onDragStart: (info) => { if (this.opts.draw && this.state.mode === 'box' && info.coordinate) { this.state.drawing = true; this.state.box = {a: info.coordinate, b: info.coordinate}; this.state.poly = []; this.state.polyClosed = false; } },
      onDrag: (info) => { if (this.state.drawing && info.coordinate) { this.state.box.b = info.coordinate; this.render(); } },
      onDragEnd: (info) => { if (this.state.drawing) { this.state.drawing = false; if (info.coordinate) this.state.box.b = info.coordinate; this.render(); this.onSelect(this.area()); } },
      onClick: (info) => this.click(info),
      onHover: (info) => this.hover(info),
    });
  }
  resForZoom(z) { return z < 1.5 ? 2 : z < 3 ? 3 : z < 5 ? 4 : z < 7 ? 5 : 6; }
  _renderSoon() { if (this._raf) return; this._raf = requestAnimationFrame(() => { this._raf = 0; this.render(); }); }
  area() { const s = this.state; if (s.box) { const b = [Math.min(s.box.a[0], s.box.b[0]), Math.min(s.box.a[1], s.box.b[1]), Math.max(s.box.a[0], s.box.b[0]), Math.max(s.box.a[1], s.box.b[1])].map(v => +v.toFixed(4)); return {bbox: b}; }
    if (s.polyClosed && s.poly.length >= 3) return {polygon: s.poly.map(p => [+p[0].toFixed(4), +p[1].toFixed(4)])}; return null; }
  setArea(a) { this.state.poly = []; this.state.polyClosed = false; this.state.box = a && a.bbox ? {a: [a.bbox[0], a.bbox[1]], b: [a.bbox[2], a.bbox[3]]} : null; if (a && a.polygon) { this.state.poly = a.polygon; this.state.polyClosed = true; } this.render(); this.onSelect(this.area()); }
  clear() { this.setArea(null); this.state.selected.clear(); this.onCellsSelected([]); this.render(); }
  setMode(m) {
    this.state.mode = m;
    const txt = {box: 'Box mode — drag a rectangle on the globe', poly: 'Polygon mode — click points, then Close (or Enter)'};
    if (this.badge) { this.badge.textContent = txt[m] || ''; this.badge.hidden = !txt[m]; }
    // Box mode: a drag must DRAW, not rotate — disable controller drag up front (deck won't cancel an
    // in-flight gesture if we only flip this in onDragStart, which let the drag rotate the globe too).
    // Keep scroll-zoom so you can still zoom while boxing. Navigate/Polygon keep normal drag.
    this.deck.setProps({controller: m === 'box'
      ? {dragPan: false, dragRotate: false, scrollZoom: true, doubleClickZoom: false}
      : {dragPan: true, dragRotate: true, doubleClickZoom: false}});
  }
  closePolygon() { if (this.state.mode === 'poly' && this.state.poly.length >= 3 && !this.state.polyClosed) { this.state.polyClosed = true; this.render(); this.onSelect(this.area()); } }
  flyTo(bbox, zoom) {
    const span = Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]) || 1; const z = zoom != null ? zoom : Math.max(2, Math.min(10, Math.log2(140 / span)));
    const vs = {longitude: (bbox[0] + bbox[2]) / 2, latitude: (bbox[1] + bbox[3]) / 2, zoom: z};
    // Setting initialViewState moves the camera but fires no onViewStateChange, so state.viewState (which picks the
    // grid and coverage resolution) stayed at the startup zoom until the first drag -- sync it, then redraw.
    this.state.viewState = {...this.state.viewState, ...vs};
    this.deck.setProps({initialViewState: {...vs, minZoom: 0, maxZoom: 12}});
    this.render();
  }
  setGrid(on) { this.state.grid = on; this.render(); }
  // [{outer, holes, label, color}]: where a build is accepted (the collections' index claims), drawn dashed
  setClaims(list) { this.state.claims = list || []; this.render(); }
  setIndexCells(cells, pct, spanMax) { this.state.indexCells = cells; this.state.indexPct = pct; this.state.indexSpanMax = spanMax || 0; this.render(); }
  click(info) {
    if (info.layer && info.layer.id === 'coverage' && info.object) { this.onHexClick(info.object); return; }
    const s = this.state;
    if (info.layer && info.layer.id === 'scenes' && info.object) { this.onOpenScene(info.object); return; }
    if (this.opts.selectCells && info.coordinate) {
      const cell = h3.latLngToCell(info.coordinate[1], info.coordinate[0], 6);
      if (s.selected.has(cell)) s.selected.delete(cell); else s.selected.add(cell);
      this.onCellsSelected([...s.selected]); this.render(); return;
    }
    if (this.opts.draw && s.mode === 'poly' && info.coordinate) { if (s.polyClosed) { s.poly = []; s.polyClosed = false; } s.poly.push(info.coordinate); s.box = null; this.render(); this.onSelect(this.area()); }
  }
  hover(info) {
    const s = this.state;
    if (s.mode === 'poly' && !s.polyClosed && s.poly.length && info.coordinate) { s.cursor = info.coordinate; this.render(); }
    let html = null;
    if (info.layer && (info.layer.id === 'grid' || info.layer.id === 'grid-data') && info.object) html = this.cellTooltip(info.object);
    else if (info.layer && info.layer.id === 'lake' && info.object) html = this.cellTooltip({hexagon: info.object.properties.cell, stats: info.object.properties});
    else if (info.layer && info.layer.id === 'coverage' && info.object) html = this.coverageTip(info.object);
    else if (info.layer && info.layer.id === 'claims' && info.object) html =`<b>${info.object.label}</b><br>indexed here: a box drawn inside this outline builds`;
    else if (info.layer && info.layer.id === 'scenes' && info.object) html = `<b>${info.object.question || info.object.scene_id}</b><br>${(info.object.series || []).join(' + ')} · <span class="status ${info.object.status}">${info.object.status}</span><br>click to open`;
    this.tooltip.hidden = !html; if (html) { this.tooltip.innerHTML = html; this.tooltip.style.left = (info.x + 12) + 'px'; this.tooltip.style.top = (info.y + 12) + 'px'; }
  }
  // ---- demo ladder level 1: per-hex mission coverage (survey.js feeds it from /api/coverage_hexes)
  setCoverage(byRes, visible) {
    this.coverage = {byRes, visible}; this._covKey++; this.render();
    // A coverage layer instance created before deck's device was ready never draws, and the memo would hand that
    // same dead instance back on every render (a strip toggle, which rebuilds it, made the hexes appear). So rebuild
    // it -- not just redraw -- a few times while the page settles. Idempotent.
    [60, 400, 1200].forEach(t => setTimeout(() => { this._covMemo = null; this.render(); }, t));
  }
  coverageRes() { const z = this.state.viewState.zoom; return z < 3 ? 3 : z < 5.8 ? 4 : 5; }
  coverageLayers(H3HexagonLayer) {
    const res = this.coverageRes(), key = res + '|' + this._covKey;
    if (this._covMemo && this._covMemo.key === key) return this._covMemo.layers;
    const {byRes, visible} = this.coverage, on = Object.keys(visible).filter(k => visible[k] !== false);
    const data = (byRes[res] || []).map(h => ({...h, seen: on.filter(k => h.missions[k])})).filter(h => h.seen.length);
    const C = AICESAT.missions.MISSION_COLORS;
    const fill = h => {
      if (on.length === 1) { const c = C[on[0]], p = h.missions[on[0]].passes;
        return [c[0], c[1], c[2], Math.round(60 + 150 * Math.min(1, Math.log10(1 + p) / 2.3))]; }
      const n = h.seen.length;   // where all three overlap is where a 20-year record exists
      return n >= 3 ? [255, 214, 102, 190] : n === 2 ? [120, 200, 220, 130] : [120, 140, 170, 70];
    };
    const layers = [new H3HexagonLayer({id: 'coverage', data, getHexagon: d => d.h3, highPrecision: 'auto', filled: true,
      stroked: true, extruded: false, pickable: true, getFillColor: fill, lineWidthMinPixels: 1,
      getLineColor: d => d.claimed ? [255, 255, 255, 150] : [255, 255, 255, 35]})];
    this._covMemo = {key, layers};
    return layers;
  }
  // Physical place names (geonames_data.js, filtered by feature code -- never political or populated places), in
  // zoom tiers: the ice sheet from afar; glaciers, fjords, bays and straits once a region fills the view; islands and
  // peaks at a few tens of km. Overlaps are resolved by rank (the collision filter keeps the higher-ranked name).
  // Drawn as an HTML overlay, not a deck TextLayer: TextLayer does not render on this GlobeView (verified: 56 labels
  // in props, none on screen, with or without depth test or SDF). Positions come from the viewport's own projection,
  // so they track the globe; names on the far side of the sphere are skipped.
  _namesAfterRender() {
    let vp = null;
    try { vp = this.deck.getViewports()[0]; } catch (e) { return; }
    if (!vp) return;
    const key = [vp.width, vp.height, vp.longitude, vp.latitude, vp.zoom].map(v => typeof v === 'number' ? v.toFixed(4) : v).join();
    if (key !== this._namesKey) { this._namesKey = key; this.placeNames(); }
  }
  placeNames() {
    const G = AICESAT.GEONAMES, el = this.namesEl; if (!G || !el) return;
    let vp = null;
    try { vp = this.deck.getViewports()[0]; } catch (e) { vp = null; }   // asserts until deck has initialised
    if (!vp) return;
    const vs = this.state.viewState, z = vs.zoom, tier = z < 5 ? 0 : z < 7 ? 1 : 2, D = Math.PI / 180;
    const show = r => r[4] === 0 ? (tier >= 1 || /Greenland/.test(r[0])) : r[4] <= 3 ? tier >= 1 : tier >= 2;
    const facing = r => Math.sin(vs.latitude * D) * Math.sin(r[1] * D) +
                        Math.cos(vs.latitude * D) * Math.cos(r[1] * D) * Math.cos((r[2] - vs.longitude) * D) > 0.2;
    const pos = new Map();
    const rows = AICESAT.declutter(G.filter(r => show(r) && facing(r)), r => {
      const p = vp.project([r[2], r[1]]); pos.set(r, p); return p; }, vp.width, vp.height, this.keepOut ? this.keepOut() : []);
    const LAND = new Set(['ISL', 'PK', 'MT', 'MTS', 'NTK']);
    const esc = s => String(s).replace(/[<>&]/g, c => ({'<': '&lt;', '>': '&gt;', '&': '&amp;'}[c]));
    el.innerHTML = rows.map(r => { const p = pos.get(r);
      return `<span class="pn ${LAND.has(r[3]) ? 'land' : 'water'} r${r[4]}" style="left:${p[0].toFixed(1)}px;top:${p[1].toFixed(1)}px">${esc(r[0])}</span>`; }).join('');
  }
  coverageTip(h) {
    const L = {GLAS: 'ICESat', ICESSN: 'IceBridge', ATL06: 'ICESat-2'}, res = h3.getResolution(h.h3);
    const rows = ['GLAS', 'ICESSN', 'ATL06'].filter(k => h.missions[k]).map(k => { const m = h.missions[k];
      return `<b>${L[k]}</b> ${m.passes} pass${m.passes === 1 ? '' : 'es'} · ${m.year_min}–${m.year_max}`; });
    const act = res < 5 ? 'click to zoom in' : h.claimed ? 'click to see how the surface changed here' : 'not fully indexed — pick a bright-edged hex';
    return `${rows.join('<br>')}<br><i>${act}</i>`;
  }
  cellTooltip(o) {
    const U = AICESAT.util, st = o.stats;
    const head = `<b>H3 ${o.hexagon}</b> (res ${h3.getResolution(o.hexagon)})`;
    if (o.idx) { const p = this.state.indexPct, x = o.idx;
      return head + `<br><b>${(x.sp || 0).toFixed(1)}</b> yr span` + (x.y0 ? ` (${x.y0}\u2013${x.y1})` : '')
        + `<br><b>${x.e}</b> epoch${x.e === 1 ? '' : 's'} \u00b7 <b>${x.g}</b> granule${x.g === 1 ? '' : 's'} indexed`
        + (p != null && p < 100 ? `<br>index build ${p}% of granules done` : ''); }
    if (!st || !st.bytes) return head + '<br>not in the lake';
    return head + `<br>${U.fmtBytes(st.bytes)} · ${U.fmtN(st.rows)} rows · ${st.files} files<br>${(st.granules || []).length} granules · ${st.chunks || 0} chunks` +
      (st.last_ingested ? `<br>ingested ${U.fmtAge(st.age_s)}` : '') + (st.n_cells ? `<br>(${st.n_cells} res-6 cells aggregated)` : '');
  }
  // aggregate res-6 stats to the current grid resolution
  gridData() {
    const s = this.state, res = s.gridRes, agg = {};
    for (const f of (s.cells ? s.cells.features : [])) {
      const p = f.properties, c6 = h3.intToStr ? f.properties.cell : f.properties.cell;
      const cellStr = typeof c6 === 'string' && /^[0-9]+$/.test(c6) ? BigInt(c6).toString(16) : c6;
      const parent = res === 6 ? cellStr : h3.cellToParent(cellStr, res);
      const a = agg[parent] || (agg[parent] = {hexagon: parent, stats: {bytes: 0, rows: 0, files: 0, chunks: 0, granules: new Set(), age_s: null, last_ingested: null, n_cells: 0}});
      a.stats.bytes += p.bytes || 0; a.stats.rows += p.rows || 0; a.stats.files += p.files || 0; a.stats.chunks += p.chunks || 0; a.stats.n_cells++;
      for (const g of (p.granules || [])) a.stats.granules.add(g);
      if (p.age_s != null && (a.stats.age_s == null || p.age_s < a.stats.age_s)) { a.stats.age_s = p.age_s; a.stats.last_ingested = p.last_ingested; }
    }
    for (const a of Object.values(agg)) a.stats.granules = [...a.stats.granules];
    return agg;
  }
  // Precomputed global H3 grid (cell indexes + centroids) for a resolution, built once and cached. The whole
  // res-2/3 grid builds in a few ms and one cellToLatLng is ~2us, so this is cheap to keep in memory and reuse.
  _globalGrid(res) {
    this._gg = this._gg || {};
    if (this._gg[res]) return this._gg[res];
    const idx = [];
    try { for (const c of h3.getRes0Cells()) for (const cc of h3.cellToChildren(c, res)) idx.push(cc); }
    catch (e) { return {idx: [], lat: new Float64Array(0), lng: new Float64Array(0)}; }
    const lat = new Float64Array(idx.length), lng = new Float64Array(idx.length);
    for (let i = 0; i < idx.length; i++) { const ll = h3.cellToLatLng(idx[i]); lat[i] = ll[0]; lng[i] = ll[1]; }
    return this._gg[res] = {idx, lat, lng};
  }
  // A patch of cells covering the visible cap around the point the globe faces — uniform at ANY latitude (works over
  // the pole, where a lon/lat box degenerates). At low resolution the cap is wide and h3.gridDisk(k) is pathologically
  // slow in JS (k=40 ~114ms), so we filter the precomputed global centroids by angular distance instead (sub-ms). At
  // high resolution the cap is small, so gridDisk(k) with a small k is cheap and avoids materializing a huge grid.
  gridCells() {
    const vs = this.state.viewState, cLat = vs.latitude, cLon = vs.longitude;
    const EDGE_KM = [1107, 418, 158, 59.8, 22.6, 8.54, 3.23];   // mean H3 edge length by resolution
    const halfDeg = Math.min(80, 70 / Math.pow(1.7, Math.max(0, vs.zoom)));   // visible angular half-extent
    const D2R = Math.PI / 180;
    let res = this.resForZoom(vs.zoom);
    for (let i = 0; i < 7; i++) {
      if (res <= 3) {   // wide cap -> precomputed-centroid distance filter (no gridDisk)
        const g = this._globalGrid(res);
        const capDeg = Math.min(88, halfDeg + 8), cosR = Math.cos(capDeg * D2R);
        const sla = Math.sin(cLat * D2R), cla = Math.cos(cLat * D2R), clo = cLon * D2R, cells = [];
        for (let j = 0; j < g.idx.length; j++) {
          const la = g.lat[j] * D2R, lo = g.lng[j] * D2R;
          if (sla * Math.sin(la) + cla * Math.cos(la) * Math.cos(lo - clo) >= cosR) cells.push(g.idx[j]);
        }
        return {cells, res};
      }
      let cells = [];   // narrow cap (zoomed in) -> gridDisk is cheap
      try {
        const center = h3.latLngToCell(cLat, cLon, res);
        const k = Math.max(1, Math.min(40, Math.round(halfDeg * 111 / EDGE_KM[res])));   // rings to span the visible face
        cells = h3.gridDisk(center, k);
      } catch (err) { cells = []; }
      if (cells.length > 5000 && res > 0) { res--; continue; }   // safety: too many -> coarser (may drop to the filter path)
      return {cells, res};
    }
    return {cells: [], res};
  }
  // Build the grid layers, but MEMOIZE them: the H3 tessellation is expensive, so rebuild only when the facing cell,
  // resolution or lake data changes — and never while the globe is being dragged/zoomed (reuse the cached layers so
  // the frame just re-projects). This keeps rotation smooth instead of re-tessellating thousands of hexagons/frame.
  gridLayers(H3HexagonLayer) {
    const s = this.state, vs = s.viewState;
    const res0 = this.resForZoom(vs.zoom);
    let center = null; try { center = h3.latLngToCell(vs.latitude, vs.longitude, res0); } catch (e) {}
    const key = center + '|' + res0;
    const c = this._gridCache;
    if (c && (this._interacting || (c.key === key && c.cellsRef === s.cells && c.indexRef === s.indexCells))) return c.layers;
    const {cells, res} = this.gridCells();
    s.gridRes = res;
    const agg = this.gridData();
    const idxFor = this.indexAt(res);   // grid-cell -> sub-granule-index info (or null), mapped to THIS grid resolution
    const patch = new Set(cells);
    // In index mode the grid IS the index view: a cell is coloured by its OBSERVATION SPAN, first to last, scaled to
    // the widest span this collection has anywhere. Span, not "distinct cycles": only ICESat-2 has repeat cycles, so
    // the old number silently meant years on GLAS and IceBridge and one colour scale meant two different things.
    // Scaling per collection rather than to a fixed 22 yr keeps ATL06 (7 yr of record) from rendering uniformly pale.
    // One layer, so it always aligns with the grid at every zoom.
    const spanMax = s.indexSpanMax || 1;
    const ramp = d => { const t = Math.min(1, (d.idx.sp || 0) / spanMax); return [70 + t * 185, 220 - t * 30, 200 - t * 150, 175]; };
    const lake = d => { const st = d.stats; if (!st || !st.bytes) return [255, 255, 255, 6]; const a = st.age_s == null ? 1 : Math.max(0.35, 1 - st.age_s / (7 * 86400)); return [55, 138, 221, Math.round(40 + 120 * a)]; };
    const fill = d => d.idx ? ramp(d) : lake(d);
    const line = d => d.idx ? [90, 230, 210, 170] : ((d.stats && d.stats.bytes) ? [120, 190, 255, 160] : [255, 255, 255, 40]);
    const mk = hx => { const a = agg[hx]; return {hexagon: hx, stats: a ? a.stats : null, idx: idxFor(hx)}; };
    const trig = {getFillColor: [s.indexCells, s.cells], getLineColor: [s.indexCells, s.cells]};
    const grid = new H3HexagonLayer({id: 'grid', data: cells.map(mk), getHexagon: d => d.hexagon, highPrecision: 'auto', filled: true, stroked: true, extruded: false,
      getFillColor: fill, getLineColor: line, lineWidthMinPixels: 1, pickable: true, updateTriggers: trig});
    const loaded = Object.values(agg).filter(a => !patch.has(a.hexagon)).map(a => ({hexagon: a.hexagon, stats: a.stats, idx: idxFor(a.hexagon)}));   // loaded cells outside the patch -> always drawn
    const layers = loaded.length
      ? [grid, new H3HexagonLayer({id: 'grid-data', data: loaded, getHexagon: d => d.hexagon, highPrecision: 'auto', filled: true, stroked: true, extruded: false,
          getFillColor: fill, getLineColor: line, lineWidthMinPixels: 1, pickable: true, updateTriggers: trig})]
      : [grid];
    this._gridCache = {key, cellsRef: s.cells, indexRef: s.indexCells, layers};
    return layers;
  }
  // Map the sub-granule index onto the CURRENT grid resolution so the one grid can be coloured by index membership
  // (no separate res-5 layer, which renders badly on the globe). Returns gridHex -> {g,c,y0,y1} or null.
  indexAt(gridRes) {
    const cells = this.state.indexCells || [];
    if (!cells.length) return () => null;
    let idxRes = 5; try { idxRes = h3.getResolution(cells[0].h); } catch (e) {}
    if (gridRes === idxRes) { const m = new Map(cells.map(x => [x.h, x])); return hx => m.get(hx) || null; }
    if (gridRes > idxRes) { const m = new Map(cells.map(x => [x.h, x])); return hx => { try { return m.get(h3.cellToParent(hx, idxRes)) || null; } catch (e) { return null; } }; }
    const coarse = new Map();   // grid coarser than the index -> roll index cells up to their grid-res ancestor
    for (const x of cells) {
      let k; try { k = h3.cellToParent(x.h, gridRes); } catch (e) { continue; }
      const e = coarse.get(k) || {g: 0, e: 0, sp: 0, y0: x.y0, y1: x.y1};
      // span and epochs roll up as a MAX, not a sum: what matters is the best record available anywhere inside this
      // coarser cell, and adding two children's spans would invent a record neither of them has.
      e.g += x.g || 0; e.e = Math.max(e.e, x.e || 0); e.sp = Math.max(e.sp, x.sp || 0);
      if (x.y0) e.y0 = Math.min(e.y0 || 9999, x.y0); if (x.y1) e.y1 = Math.max(e.y1 || 0, x.y1);
      coarse.set(k, e);
    }
    return hx => coarse.get(hx) || null;
  }
  render() {
    const {TileLayer, BitmapLayer, PolygonLayer, PathLayer, TextLayer, GeoJsonLayer, H3HexagonLayer, ScatterplotLayer, SolidPolygonLayer} = deck;
    const s = this.state, U = AICESAT.util, layers = [];
    // dark ocean sphere + Natural Earth land polygons (vector basemap; raster tiles do not index on a globe)
    layers.push(new SolidPolygonLayer({id: 'globe-bg', data: [[[-180, 90], [0, 90], [180, 90], [180, -90], [0, -90], [-180, -90]]], getPolygon: d => d, stroked: false, filled: true, getFillColor: [11, 20, 34]}));
    if (window.__NE_LAND) layers.push(new GeoJsonLayer({id: 'land', data: window.__NE_LAND, stroked: true, filled: true, getFillColor: [42, 54, 47], getLineColor: [80, 96, 88], lineWidthMinPixels: 0.5}));
    if (this.coverage) layers.push(...this.coverageLayers(H3HexagonLayer));
    if (s.grid) {
      for (const L of this.gridLayers(H3HexagonLayer)) layers.push(L);
    } else if (s.cells) {
      layers.push(new GeoJsonLayer({id: 'lake', data: s.cells, stroked: true, filled: true, getFillColor: [55, 138, 221, 40], getLineColor: [55, 138, 221, 140], lineWidthMinPixels: 1, pickable: true}));
    }
    // (sub-granule index coverage is drawn by colouring the grid itself in gridLayers — no separate layer)
    // Index CLAIMS are a different fact from those rows: the ground a build will accept. Dashed, so they read as a
    // boundary to draw inside, not as another scene.
    if (s.claims.length) layers.push(new PolygonLayer({id: 'claims', data: s.claims, pickable: true, filled: true, stroked: true,
      getPolygon: d => d.holes && d.holes.length ? [d.outer, ...d.holes] : d.outer,
      getFillColor: d => [...d.color, 18], getLineColor: d => [...d.color, 230], lineWidthMinPixels: 1.5,
      getDashArray: [6, 4], dashJustified: true, extensions: [new deck.PathStyleExtension({dash: true})]}));
    if (s.selected.size) layers.push(new H3HexagonLayer({id: 'selected', data: [...s.selected].map(hexagon => ({hexagon})), getHexagon: d => d.hexagon, highPrecision: true, filled: true, stroked: true,
      getFillColor: [224, 160, 48, 90], getLineColor: [224, 160, 48, 220], lineWidthMinPixels: 2}));
    if (this.opts.footprints && s.scenes.length) {
      const col = sc => sc.status === 'ready' ? [76, 175, 125, 230] : sc.status === 'loading' ? [224, 160, 48, 230] : [217, 83, 79, 230];
      const pulse = 0.5 + 0.5 * Math.sin(Date.now() / 300);
      layers.push(new PolygonLayer({id: 'scenes', data: s.scenes.filter(sc => sc.bbox || sc.polygon), getPolygon: d => U.areaRing(d), filled: true, stroked: true, pickable: true,
        getFillColor: d => d.status === 'loading' ? [224, 160, 48, Math.round(30 + 60 * pulse)] : d.status === 'ready' ? [76, 175, 125, 25] : [217, 83, 79, 30],
        getLineColor: col, lineWidthMinPixels: 2, getDashArray: d => d.status === 'loading' ? [6, 4] : [0, 0], dashJustified: true, extensions: [new deck.PathStyleExtension({dash: true})],
        updateTriggers: {getFillColor: [pulse.toFixed(1)]}}));
      if (s.viewState.zoom >= 5.5) layers.push(new TextLayer({id: 'scene-labels', data: s.scenes.filter(sc => sc.bbox || sc.polygon), getPosition: d => { const b = d.bbox || U.bboxOfPolygon(d.polygon); return [(b[0] + b[2]) / 2, b[1]]; },
        getText: d => (d.question || d.scene_id).slice(0, 40) + (d.status === 'loading' ? ' …' : ''), getSize: 11, getColor: col, getTextAnchor: 'middle', getAlignmentBaseline: 'top', characterSet: 'auto', background: true, getBackgroundColor: [20, 20, 26, 170]}));
      if (s.scenes.some(sc => sc.status === 'loading')) { clearTimeout(this._pulse); this._pulse = setTimeout(() => this.render(), 350); }
    }
    if (s.box) { const b = this.area().bbox; layers.push(new PolygonLayer({id: 'box', data: [U.areaRing({bbox: b})], getPolygon: d => d, getFillColor: [55, 138, 221, 50], getLineColor: [120, 190, 255], lineWidthMinPixels: 2})); }
    if (s.poly.length) {
      const pts = s.polyClosed ? s.poly : (s.cursor ? [...s.poly, s.cursor] : s.poly);
      if (s.polyClosed) layers.push(new PolygonLayer({id: 'poly', data: [s.poly], getPolygon: d => d, getFillColor: [55, 138, 221, 50], getLineColor: [120, 190, 255], lineWidthMinPixels: 2}));
      else layers.push(new PathLayer({id: 'poly-path', data: [pts], getPath: d => d, getColor: [120, 190, 255], getWidth: 2, widthUnits: 'pixels'}));
      layers.push(new ScatterplotLayer({id: 'poly-verts', data: s.poly, getPosition: d => d, getRadius: 4, radiusUnits: 'pixels', getFillColor: [255, 255, 255]}));
    }
    this.deck.setProps({layers});
  }
  async refreshData(api, mission = 'ICESAT2') {
    const [cells, scenes] = await Promise.all([api.lakeCells(this.opts.gridStats, mission).catch(() => null), api.scenes().catch(() => [])]);
    Object.assign(this.state, {cells, scenes}); this.render();
  }
};
