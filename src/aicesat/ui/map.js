/* Shared 3-D globe: EOX imagery draped on the sphere, candidate regions, lake cells, global H3 grid (resolution by
   zoom, hover stats), scene footprints (ready/loading/error), box/polygon drawing, cell selection. No flat projection.
   Used by Explore and Lake. Navigate = drag to spin / scroll to zoom; Box/Polygon capture the drag for drawing. */
window.AICESAT = window.AICESAT || {};
AICESAT.MapView = class {
  constructor(container, opts = {}) {
    const {Deck, _GlobeView, GlobeView} = deck;
    const Globe = GlobeView || _GlobeView;
    this.opts = Object.assign({grid: false, gridStats: true, selectCells: false, draw: true, footprints: true}, opts);
    this.container = container;
    this.state = {mode: 'pan', drawing: false, box: null, poly: [], polyClosed: false, cursor: null, regions: {}, cells: null, cellStats: {},
                  scenes: [], grid: this.opts.grid, gridRes: 3, selected: new Set(), hover: null, viewState: {longitude: -42, latitude: 66, zoom: 1.3}};
    this.tooltip = AICESAT.util.el('div', {class: 'tooltip'}); this.tooltip.hidden = true; container.appendChild(this.tooltip);
    this.onSelect = () => {}; this.onOpenScene = () => {}; this.onCellsSelected = () => {};
    this.deck = new Deck({
      parent: container, views: new Globe({resolution: 12}),
      initialViewState: {...this.state.viewState, minZoom: 0, maxZoom: 12}, controller: {dragPan: true, dragRotate: true, doubleClickZoom: false}, layers: [],
      onViewStateChange: ({viewState}) => { this.state.viewState = viewState; this._renderSoon(); },
      getCursor: () => 'crosshair',
      onDragStart: (info) => { if (this.opts.draw && this.state.mode === 'box' && info.coordinate) { this.state.drawing = true; this.state.box = {a: info.coordinate, b: info.coordinate}; this.state.poly = []; this.state.polyClosed = false; this.deck.setProps({controller: false}); } },
      onDrag: (info) => { if (this.state.drawing && info.coordinate) { this.state.box.b = info.coordinate; this.render(); } },
      onDragEnd: (info) => { if (this.state.drawing) { this.state.drawing = false; if (info.coordinate) this.state.box.b = info.coordinate; this.deck.setProps({controller: {dragPan: true, dragRotate: true, doubleClickZoom: false}}); this.render(); this.onSelect(this.area()); } },
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
  setMode(m) { this.state.mode = m; }
  closePolygon() { if (this.state.mode === 'poly' && this.state.poly.length >= 3 && !this.state.polyClosed) { this.state.polyClosed = true; this.render(); this.onSelect(this.area()); } }
  flyTo(bbox, zoom) { const span = Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]) || 1; const z = zoom != null ? zoom : Math.max(2, Math.min(10, Math.log2(140 / span))); this.deck.setProps({initialViewState: {longitude: (bbox[0] + bbox[2]) / 2, latitude: (bbox[1] + bbox[3]) / 2, zoom: z, minZoom: 0, maxZoom: 12}}); }
  setGrid(on) { this.state.grid = on; this.render(); }
  click(info) {
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
    if (info.layer && info.layer.id === 'grid' && info.object) html = this.cellTooltip(info.object);
    else if (info.layer && info.layer.id === 'lake' && info.object) html = this.cellTooltip({hexagon: info.object.properties.cell, stats: info.object.properties});
    else if (info.layer && info.layer.id === 'scenes' && info.object) html = `<b>${info.object.question || info.object.scene_id}</b><br>${(info.object.series || []).join(' + ')} · <span class="status ${info.object.status}">${info.object.status}</span><br>click to open`;
    this.tooltip.hidden = !html; if (html) { this.tooltip.innerHTML = html; this.tooltip.style.left = (info.x + 12) + 'px'; this.tooltip.style.top = (info.y + 12) + 'px'; }
  }
  cellTooltip(o) {
    const U = AICESAT.util, st = o.stats;
    const head = `<b>H3 ${o.hexagon}</b> (res ${h3.getResolution(o.hexagon)})`;
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
  // cells around the point the globe is facing; angular half-extent shrinks with zoom. Bounded so it never blows up.
  gridCells() {
    const vs = this.state.viewState, cLat = vs.latitude, cLon = vs.longitude;
    const halfDeg = Math.min(80, 70 / Math.pow(1.7, Math.max(0, vs.zoom)));   // ~visible cap; whole face when zoomed out
    const s = Math.max(-89, cLat - halfDeg), n = Math.min(89, cLat + halfDeg);
    const dLon = Math.min(179, halfDeg / Math.max(0.15, Math.cos(cLat * Math.PI / 180)));
    const w = cLon - dLon, e = cLon + dLon;
    let res = this.resForZoom(vs.zoom);
    for (let i = 0; i < 6; i++) {
      let cells = [];
      try {
        if (e - w >= 359) cells = h3.getRes0Cells().flatMap(c => res === 0 ? [c] : h3.cellToChildren(c, Math.min(res, 2)));  // whole globe
        else cells = h3.polygonToCells([[s, Math.max(-180, w)], [s, Math.min(180, e)], [n, Math.min(180, e)], [n, Math.max(-180, w)]], res);
      } catch (err) { cells = []; }
      if (cells.length > 6000 && res > 0) { res--; continue; }
      if (cells.length === 0) { try { const c = h3.latLngToCell(cLat, cLon, res); cells = [c, ...h3.gridDisk(c, 1)]; } catch (err) {} }
      return {cells: [...new Set(cells)], res};
    }
    return {cells: [], res};
  }
  render() {
    const {TileLayer, BitmapLayer, PolygonLayer, PathLayer, TextLayer, GeoJsonLayer, H3HexagonLayer, ScatterplotLayer, SolidPolygonLayer} = deck;
    const s = this.state, U = AICESAT.util, layers = [];
    // dark ocean sphere + Natural Earth land polygons (vector basemap; raster tiles do not index on a globe)
    layers.push(new SolidPolygonLayer({id: 'globe-bg', data: [[[-180, 90], [0, 90], [180, 90], [180, -90], [0, -90], [-180, -90]]], getPolygon: d => d, stroked: false, filled: true, getFillColor: [11, 20, 34]}));
    if (window.__NE_LAND) layers.push(new GeoJsonLayer({id: 'land', data: window.__NE_LAND, stroked: true, filled: true, getFillColor: [42, 54, 47], getLineColor: [80, 96, 88], lineWidthMinPixels: 0.5}));
    if (s.grid) {
      const {cells, res} = this.gridCells();
      s.gridRes = res;
      const agg = this.gridData();
      const data = cells.map(hx => agg[hx] || {hexagon: hx, stats: null});
      const fill = d => { const st = d.stats; if (!st || !st.bytes) return [255, 255, 255, 6]; const a = st.age_s == null ? 1 : Math.max(0.35, 1 - st.age_s / (7 * 86400)); return [55, 138, 221, Math.round(40 + 120 * a)]; };
      layers.push(new H3HexagonLayer({id: 'grid', data, getHexagon: d => d.hexagon, highPrecision: true, filled: true, stroked: true, extruded: false,
        getFillColor: fill, getLineColor: d => (d.stats && d.stats.bytes) ? [120, 190, 255, 160] : [255, 255, 255, 40], lineWidthMinPixels: 1, pickable: true,
        updateTriggers: {getFillColor: [s.cells], getLineColor: [s.cells]}}));
    } else if (s.cells) {
      layers.push(new GeoJsonLayer({id: 'lake', data: s.cells, stroked: true, filled: true, getFillColor: [55, 138, 221, 40], getLineColor: [55, 138, 221, 140], lineWidthMinPixels: 1, pickable: true}));
    }
    if (s.selected.size) layers.push(new H3HexagonLayer({id: 'selected', data: [...s.selected].map(hexagon => ({hexagon})), getHexagon: d => d.hexagon, highPrecision: true, filled: true, stroked: true,
      getFillColor: [224, 160, 48, 90], getLineColor: [224, 160, 48, 220], lineWidthMinPixels: 2}));
    const regs = Object.entries(s.regions).map(([k, v]) => ({name: k, poly: U.areaRing({bbox: v.bbox}), bbox: v.bbox}));
    layers.push(new PolygonLayer({id: 'regions', data: regs, getPolygon: d => d.poly, filled: false, stroked: true, getLineColor: [224, 160, 48, 160], lineWidthMinPixels: 1}));
    layers.push(new TextLayer({id: 'region-labels', data: regs, getPosition: d => [(d.bbox[0] + d.bbox[2]) / 2, d.bbox[3]], getText: d => d.name, getSize: 11, getColor: [224, 160, 48, 200], getTextAnchor: 'middle', getAlignmentBaseline: 'bottom', characterSet: 'auto', background: true, getBackgroundColor: [20, 20, 26, 160]}));
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
  async refreshData(api) {
    const [regions, cells, scenes] = await Promise.all([api.regions().catch(() => ({})), api.lakeCells(this.opts.gridStats).catch(() => null), api.scenes().catch(() => [])]);
    Object.assign(this.state, {regions, cells, scenes}); this.render();
  }
};
