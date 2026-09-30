/* Level 1 of the demo ladder: the globe, each hex coloured by which missions measured it (index only -- nothing is
   fetched). Click a res-4/5 hex to open its change map; the build behind it is implicit and shared with the model's
   elevation_change (same "H3 <hex>" question, same collections as api.REGION_FLAGS). */
window.AICESAT = window.AICESAT || {};
AICESAT.SurveyView = class {
  constructor(root, api, openScene) {
    root.innerHTML = `<div class="map" id="svMap"></div>
      <label class="sv-toggle"><input type="checkbox" id="svHex"> Hex grid &amp; coverage</label>
      <div id="svStrip"></div>
      <div id="attrib">Basemap: Natural Earth (public domain) · Place names: GeoNames (CC BY 4.0)</div>`;
    const $ = id => root.querySelector('#' + id);
    this.api = api; this.root = root; this.openScene = openScene;
    this.visible = {GLAS: true, ICESSN: true, ATL06: true}; this.byRes = {};
    // Natural Earth, not imagery: whole-image BitmapLayers on the globe tessellate too coarsely (their chords dip under
    // the sphere and clip into jagged shapes) and z-fight the land. Imagery lives one level down, in the change map.
    this.map = new AICESAT.MapView($('svMap'), {grid: false, draw: false, footprints: false});
    this.map.onHexClick = h => this.openHex(h);
    // Off by default: the globe opens on the Earth itself; the switch shows the grid and the coverage shading.
    this.map.coverageOn = false;
    $('svHex').onchange = e => { this.map.coverageOn = e.target.checked; this.map._covMemo = null; this.map.render(); };
    // Names stay clear of the timeline strip.
    this.map.keepOut = () => {
      const c = $('svMap').getBoundingClientRect();
      return [$('svStrip')].map(e => e.getBoundingClientRect())
        .map(r => [r.left - c.left, r.top - c.top, r.right - c.left, r.bottom - c.top]);
    };
    this.strip = AICESAT.timeline.mount($('svStrip'), m => { this.visible[m] = this.visible[m] === false; this.paint(); });
    this.strip.update(['GLAS', 'ICESSN', 'ATL06'], this.visible);
  }
  async load() {
    try { for (const r of [3, 4, 5]) this.byRes[r] = (await this.api.coverageHexes(r)).hexes; }
    catch (e) { AICESAT.showError(e); return; }
    this.paint();
  }
  paint() { this.map.setCoverage(this.byRes, this.visible); this.strip.update(['GLAS', 'ICESSN', 'ATL06'], this.visible); }
  async openHex(hx) {
    const res = h3.getResolution(hx.h3);
    const ring = h3.cellToBoundary(hx.h3), lats = ring.map(p => p[0]), lons = ring.map(p => p[1]);
    // Coarser hexes zoom in; a res-5 hex (~17 km, what elevation_change builds for the model too) opens its change map.
    if (res < 5) { this.map.flyTo([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)]); return; }
    if (!hx.claimed) { AICESAT.showError('Not every mission is indexed over all of this hex yet — pick one with a bright edge.'); return; }
    const question = 'H3 ' + hx.h3;   // == api.region_question: the model's elevation_change reuses this scene
    try {
      const hit = ((await this.api.scenes()) || []).find(s => s.question === question && (s.status === 'ready' || s.status === 'loading'));
      if (hit) return this.openScene(hit.scene_id, hx.h3);
      const d = await this.api.extract({polygon: ring.map(([lat, lon]) => [+lon.toFixed(6), +lat.toFixed(6)]), question,
        with_glas: true, with_icessn: true, with_atl06: true, with_atl03: false, with_gedi: false, with_gpstruth: false, with_coreg: false});
      this.openScene(d.scene_id, hx.h3);
    } catch (e) { AICESAT.showError(e); }
  }
  show(arg) {
    this.root.classList.add('on');
    if (!this._loaded) { this._loaded = true; this.load(); }
    const b = (arg || '').split(',').map(Number);
    // Default: most of the Earth, centred on Greenland.
    if (b.length === 4 && b.every(Number.isFinite)) this.map.flyTo(b); else this.map.flyTo([-62, 58, -22, 78], 1.25);
  }
  hide() { this.root.classList.remove('on'); }
};
