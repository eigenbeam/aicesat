/* Explore view: globe + a guided Area → Collections/coverage → Build flow. Coverage auto-fetches when an
   area is defined and annotates the collection rows; a scene's build progress lives in its Scenes-list card. */
window.AICESAT = window.AICESAT || {};
AICESAT.ExploreView = class {
  constructor(root, api, openScene) {
    const U = AICESAT.util; this.api = api; this.root = root; this.openScene = openScene;
    this.jobs = {};          // job_id -> {plan, j}: build progress for the currently-loading scene(s)
    this._covSeq = 0;        // guards against a stale coverage response overwriting a newer one
    root.innerHTML = `
      <div class="map" id="exMap"></div>
      <div id="mapLegend" class="map-legend"><b>Map key</b><span><i class="swf" style="--c:#378ADD"></i>data &amp; selection</span><span><i class="swo" style="--c:#4caf7d"></i>scene ready</span><span><i class="swo" style="--c:#E0A030"></i>building</span><span><i class="swo" style="--c:#d9534f"></i>error</span><span><i class="swo" style="--c:#a882e6"></i>suggested region</span></div>
      <div class="panel" id="exTools" data-title="build a scene" style="top:12px;left:12px;width:344px">
        <div class="step">
          <div class="step-head"><span class="step-n">1</span> Pick an area</div>
          <div class="seg" id="exMode"><button data-mode="pan" class="on">Navigate</button><button data-mode="box">Box</button><button data-mode="poly">Polygon</button></div>
          <div class="small step-hint">Navigate = drag to spin, scroll to zoom. Box = drag a rectangle. Polygon = click points, then Close.</div>
          <div class="row"><button id="exClose" hidden>Close polygon</button><button id="exClear">Clear</button>
            <select id="exRegion"><option value="">jump to region…</option></select></div>
          <div id="exCoords" class="area-readout small">no area selected</div>
          <details class="small"><summary>enter exact coordinates</summary>
            <div class="bbox-entry">W<input id="bbW" type="number" step="0.5"> S<input id="bbS" type="number" step="0.5"> E<input id="bbE" type="number" step="0.5"> N<input id="bbN" type="number" step="0.5"><button id="bbSet">Set</button></div>
            <div class="small step-hint">Also works for polar caps (set N or S near ±90).</div></details>
        </div>
        <div class="step">
          <div class="step-head"><span class="step-n">2</span> <b>Collections</b> <span class="ctl-note" id="exCovHint">& coverage over your area</span></div>
          <div id="exColList" class="small">loading…</div>
          <div id="exCovNote" class="small covnote"></div>
        </div>
        <div class="step">
          <div class="step-head"><span class="step-n">3</span> Build the scene</div>
          <div class="gran-row">
            <label><input type="radio" name="gran" value="all" checked> all granules</label>
            <label><input type="radio" name="gran" value="limit"> limit <input id="exMaxG" type="number" value="12" min="1" max="500" disabled></label>
          </div>
          <div class="row"><button id="exBuild" disabled>Build scene</button></div>
        </div>
      </div>
      <div class="panel" id="exScenes" data-title="scenes" style="top:12px;right:12px;width:308px"><h2>Scenes</h2><div class="list" id="exSceneList"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    const fmtArea = a => {
      if (a && a.bbox) { const [w, s, e, n] = a.bbox;
        const la = v => `${Math.abs(v).toFixed(2)}°${v >= 0 ? 'N' : 'S'}`, lo = v => `${Math.abs(v).toFixed(2)}°${v >= 0 ? 'E' : 'W'}`;
        const hkm = (n - s) * 111, wkm = (e - w) * 111 * Math.cos((s + n) / 2 * Math.PI / 180);
        return `${la(s)}–${la(n)}, ${lo(w)}–${lo(e)}  ·  ~${Math.abs(wkm).toFixed(0)}×${Math.abs(hkm).toFixed(0)} km`; }
      return a && a.polygon ? `polygon · ${a.polygon.length} vertices` : '';
    };
    const syncBboxFields = a => { if (a && a.bbox) { const [w, s, e, n] = a.bbox; $('bbW').value = w; $('bbS').value = s; $('bbE').value = e; $('bbN').value = n; } };

    this.map = new AICESAT.MapView($('exMap'), {grid: true, selectCells: false, draw: true, footprints: true});
    this.map.onSelect = a => {
      $('exCoords').textContent = a ? fmtArea(a) : (this.map.state.poly.length ? `polygon: ${this.map.state.poly.length} vertices (need 3, then Close)` : 'no area selected');
      syncBboxFields(a);
      $('exBuild').disabled = !a;
      $('exClose').hidden = !(this.map.state.mode === 'poly' && !this.map.state.polyClosed && this.map.state.poly.length >= 3);
      this.scheduleCoverage();   // area changed -> auto-refresh coverage
    };
    this.map.onOpenScene = sc => { if (sc.status === 'ready') openScene(sc.scene_id); };

    // segmented mode picker
    const seg = $('exMode');
    const setMode = m => { this.map.setMode(m); seg.querySelectorAll('button[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m)); };
    seg.querySelectorAll('button[data-mode]').forEach(b => b.onclick = () => setMode(b.dataset.mode));
    $('exClose').onclick = () => this.map.closePolygon();
    root.addEventListener('keydown', e => { if (e.key === 'Enter' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName || '')) this.map.closePolygon(); });
    $('exClear').onclick = () => { this.map.clear(); this.scheduleCoverage(); };
    $('exRegion').onchange = e => { const r = this.map.state.regions[e.target.value]; if (!r) return; this.map.setArea({bbox: r.bbox}); this.map.flyTo(r.bbox); };
    // numeric bbox entry (precise; also handles polar caps)
    $('bbSet').onclick = () => {
      const w = +$('bbW').value, s = +$('bbS').value, e = +$('bbE').value, n = +$('bbN').value;
      if ([w, s, e, n].some(v => Number.isNaN(v))) { $('exCoords').textContent = 'enter all four: W, S, E, N'; return; }
      const bbox = [Math.min(w, e), Math.min(s, n), Math.max(w, e), Math.max(s, n)];
      this.map.setArea({bbox}); this.map.flyTo(bbox);
    };
    // granules: all (unlimited) vs limit N
    root.querySelectorAll('input[name="gran"]').forEach(r => r.onchange = () => { $('exMaxG').disabled = root.querySelector('input[name="gran"]:checked').value !== 'limit'; });

    $('exBuild').onclick = async () => { const a = this.map.area(); if (!a) return;
      const flags = {}; $('exColList').querySelectorAll('input[data-flag]').forEach(i => flags[i.dataset.flag] = i.checked);
      const limited = root.querySelector('input[name="gran"]:checked').value === 'limit';
      const body = {...a, max_granules: limited ? +$('exMaxG').value : 100000, ...flags,
        with_coreg: !!(flags.with_atl03 && flags.with_glas),
        question: `area selected on the map (${a.bbox ? 'box' : 'polygon'})`};
      AICESAT.clearError(); $('exBuild').disabled = true;
      try { const d = await api.extract(body); this.jobs[d.job_id] = {plan: body, j: {status: 'running', log: []}}; this.pollJob(d.job_id, body); await this.refresh(); }
      catch (e) { AICESAT.showError(e); $('exBuild').disabled = false; } };

    AICESAT.util.drawer(root, null);
    { const G = U.GLOSSARY;   // opt-in "?" help on the jargon
      const ch = $('exCovHint'); if (ch) ch.appendChild(U.help(G.coverage));
      const mg = $('exMaxG') && $('exMaxG').closest('label'); if (mg) mg.appendChild(U.help(G.granules)); }
    this.loadCollections();
    this.refresh(); this.timer = setInterval(() => this.refresh(true), 5000);
  }

  // ---- coverage: auto-fetched when an area is defined, shown inline on each collection row
  covCaveat(c) {
    if (c.key === 'GLAS') return ' <span class="cov-caveat" title="ICESat-1/GLAS granules are matched to your area by orbit, not a true footprint, so this is an upper bound. The scene keeps only points that actually fall inside your area — expect fewer.">ⓘ</span>';
    if (c.key === 'ICESSN') return ' <span class="cov-caveat" title="IceBridge ATM catalog footprints can over-claim area. The scene keeps only points inside your area.">ⓘ</span>';
    return '';
  }
  setCovCells(html) { this.$('exColList').querySelectorAll('.col-cov').forEach(el => { el.innerHTML = html; }); }
  scheduleCoverage() {
    clearTimeout(this._covTimer);
    const note = this.$('exCovNote'), a = this.map.area();
    if (!a) { this.setCovCells(''); if (note) note.textContent = 'pick an area to see coverage'; return; }
    this.setCovCells('<span class="spin-sm"></span>');
    if (note) note.textContent = '';
    this._covTimer = setTimeout(() => this.fetchCoverage(a), 500);
  }
  async fetchCoverage(a) {
    const seq = ++this._covSeq, note = this.$('exCovNote');
    try {
      const d = await this.api.coverage(a);
      if (seq !== this._covSeq) return;   // a newer area superseded this request
      const byKey = {}; (d.collections || []).forEach(c => byKey[c.key] = c);
      this.$('exColList').querySelectorAll('.col-cov').forEach(el => {
        const c = byKey[el.dataset.key];
        if (!c) { el.innerHTML = ''; return; }
        if (c.n_granules == null) { el.innerHTML = '<span class="no">n/a</span>'; return; }
        el.innerHTML = c.n_granules ? `<b>${c.n_granules}</b> gran.${this.covCaveat(c)}` : '<span class="no">none</span>';
      });
      if (note) note.textContent = 'granule counts over your area — the scene keeps only points inside it';
    } catch (e) {
      if (seq !== this._covSeq) return;
      this.setCovCells('');
      if (note) note.textContent = 'coverage check unavailable';
    }
  }

  async loadCollections() {
    try {
      const cols = await this.api.collections();
      this.$('exColList').innerHTML = cols.map(c =>
        `<label class="col-row" title="${c.product} v${c.version} · ${c.epoch}"><input type="checkbox" data-flag="${c.flag}" ${c.default ? 'checked' : ''}><span class="col-name">${c.label}</span><span class="col-cov" data-key="${c.key}"></span></label>`).join('');
      this.scheduleCoverage();   // fill counts if an area is already set
    } catch (e) { this.$('exColList').textContent = 'collections unavailable'; }
  }

  // ---- build progress: rendered inside the building scene's card in the Scenes list (not the build panel)
  progressHTML(j, plan = {}) {
    const log = j.log || [];
    const ALL = [['GLAS', 'ICESat-1 · GLAS'], ['ICESSN', 'IceBridge · ATM'], ['ATL06', 'ICESat-2 · land ice'], ['ATL03', 'ICESat-2 · photons'], ['surface', 'DEM surface'], ['imagery', 'Satellite imagery'], ['coreg', 'Co-registration']];
    const flagOf = {GLAS: 'with_glas', ICESSN: 'with_icessn', ATL06: 'with_atl06', ATL03: 'with_atl03'};
    const hasPlan = plan && ['with_glas', 'with_icessn', 'with_atl06', 'with_atl03'].some(f => plan[f] !== undefined);
    const wanted = k => {
      if (k === 'surface' || k === 'imagery') return true;
      if (k === 'coreg') return plan.with_coreg || log.some(l => /co-registration/i.test(l));
      if (hasPlan) return !!plan[flagOf[k]];
      return log.some(l => l.startsWith(k + ':') || l.startsWith(k + ' unavailable'));
    };
    const STEPS = ALL.filter(([k]) => wanted(k)).map(([key, label]) => ({key, label}));
    const detailOf = l => l.includes(': ') ? l.slice(l.indexOf(': ') + 2) : '';
    const classify = key => {
      if (log.some(l => l.startsWith(key + ' unavailable'))) return {state: 'failed', detail: detailOf(log.find(l => l.startsWith(key + ' unavailable')))};
      if (key === 'imagery' && log.some(l => l.startsWith('imagery unavailable'))) return {state: 'warn', detail: 'skipped (optional)'};
      let done;
      if (key === 'ATL03') done = log.find(l => /^ATL03: [\d,]+ photons/.test(l));
      else if (key === 'coreg') done = log.find(l => /co-registration/i.test(l));
      else done = log.find(l => l.startsWith(key + ':'));
      return done ? {state: 'done', detail: detailOf(done)} : {state: 'pending'};
    };
    const ICON = {done: '✓', pending: '○', skipped: '–', failed: '✕', warn: '!'};
    const running = j.status === 'running';
    const rows = STEPS.map(s => ({s, st: classify(s.key)}));
    if (running) { const a = rows.find(r => r.st.state === 'pending'); if (a) a.st.state = 'active'; }
    else rows.forEach(r => { if (r.st.state === 'pending') r.st.state = 'skipped'; });
    return rows.map(r => {
      const ic = r.st.state === 'active' ? '<span class="spin-sm"></span>' : (ICON[r.st.state] || '○');
      return `<div class="pstep ${r.st.state}"><span class="picon">${ic}</span><span class="pname">${r.s.label}</span>${r.st.detail ? `<span class="pdetail">${r.st.detail}</span>` : ''}</div>`;
    }).join('');
  }
  async pollJob(jid, plan = {}) {
    this.jobs[jid] = this.jobs[jid] || {plan, j: {status: 'running', log: []}};
    if (plan && Object.keys(plan).length) this.jobs[jid].plan = plan;
    const tick = async () => {
      let j; try { j = await this.api.job(jid); } catch (e) { j = {status: 'running', log: []}; }
      if (this.jobs[jid]) this.jobs[jid].j = j;
      try { this.map.state.scenes = await this.api.scenes(); } catch (e) {}   // keep the loading card fresh (light: scenes only)
      this.renderScenes();
      if (j.status === 'running') setTimeout(tick, 1200);
      else { if (j.error) AICESAT.showError(j.error); delete this.jobs[jid]; this.$('exBuild').disabled = !this.map.area(); await this.refresh(); }
    };
    tick();
  }
  renderScenes() {
    const list = this.$('exSceneList'); if (!list) return;
    const scenes = this.map.state.scenes || [];
    list.innerHTML = scenes.map(sc => {
      const meta = `${(sc.series || []).join(' + ') || '…'}${sc.coreg ? ' · coreg' : ''} · ${(sc.created || '').slice(0, 16).replace('T', ' ')}`;
      const head = `<div class="row"><span class="status ${sc.status}">${sc.status}</span><span class="grow" title="${sc.question || ''}">${sc.question || sc.scene_id}<br><span class="small">${meta}</span></span>` +
        (sc.status === 'ready' ? `<button data-open="${sc.scene_id}">Open</button>` : '') + `</div>`;
      const entry = sc.job_id && this.jobs[sc.job_id];
      const prog = (sc.status === 'loading' && entry) ? `<div class="scene-prog">${this.progressHTML(entry.j, entry.plan)}</div>` : '';
      return `<div class="scene-card ${sc.status}">${head}${prog}</div>`;
    }).join('') || '<div class="small">no scenes yet — pick an area and build one</div>';
    list.querySelectorAll('button[data-open]').forEach(b => b.onclick = () => this.openScene(b.dataset.open));
    // adopt a loading scene whose job we aren't polling yet (e.g. after a page reload)
    scenes.forEach(sc => { if (sc.status === 'loading' && sc.job_id && !this.jobs[sc.job_id]) { this.jobs[sc.job_id] = {plan: {}, j: {status: 'running', log: []}}; this.pollJob(sc.job_id, {}); } });
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    await this.map.refreshData(this.api);
    // populate "jump to region…" once regions have loaded (humanize the snake_case keys; note -> tooltip)
    const rsel = this.$('exRegion');
    if (rsel && rsel.options.length <= 1) {
      const regs = this.map.state.regions || {};
      const human = k => k.replace(/_/g, ' ').replace(/\b(egig|negis|ne|nw|se|sw|k)\b/gi, m => m.toUpperCase()).replace(/\b\w/g, c => c.toUpperCase());
      Object.entries(regs).forEach(([k, v]) => { const o = document.createElement('option'); o.value = k; o.textContent = human(k); if (v && v.note) o.title = v.note; rsel.appendChild(o); });
    }
    this.renderScenes();
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
