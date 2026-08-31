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
      <div id="mapLegend" class="map-legend"><b>Map key</b><span><i class="swf" style="--c:#378ADD"></i>data &amp; selection</span><span><i class="swo" style="--c:#4caf7d"></i>scene ready</span><span><i class="swo" style="--c:#E0A030"></i>building</span><span><i class="swo" style="--c:#d9534f"></i>error</span></div>
      <div class="panel" id="exTools" data-title="build a scene" style="top:12px;left:12px;width:344px">
        <div class="step">
          <div class="step-head"><span class="step-n">1</span> Pick an area</div>
          <div class="seg-row"><div class="seg" id="exMode"><button data-mode="pan" class="on">Navigate</button><button data-mode="box">Box</button><button data-mode="poly">Polygon</button></div><button id="exClose" hidden>Close polygon</button><button id="exClear">Clear</button></div>
          <div class="small step-hint">Navigate = drag to spin, scroll to zoom. Box = drag a rectangle. Polygon = click points, then Close.</div>
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
          <div class="small step-hint">Uses all granules over your area. Satellite imagery is built in — toggle it and choose the source in the scene view.</div>
          <div class="row"><button id="exBuild" disabled>Build scene</button></div>
        </div>
      </div>
      <div class="panel" id="exScenes" data-title="scenes" style="top:12px;right:12px;width:308px"><h2>Scenes</h2><div class="list" id="exSceneList"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    const syncBboxFields = a => { if (a && a.bbox) { const [w, s, e, n] = a.bbox; $('bbW').value = w; $('bbS').value = s; $('bbE').value = e; $('bbN').value = n; } };

    this.map = new AICESAT.MapView($('exMap'), {grid: true, selectCells: false, draw: true, footprints: true});
    this.map.onSelect = a => {
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
    // numeric bbox entry (precise; also handles polar caps)
    $('bbSet').onclick = () => {
      const w = +$('bbW').value, s = +$('bbS').value, e = +$('bbE').value, n = +$('bbN').value;
      if ([w, s, e, n].some(v => Number.isNaN(v))) { AICESAT.showError('enter all four coordinates: W, S, E, N'); return; }
      const bbox = [Math.min(w, e), Math.min(s, n), Math.max(w, e), Math.max(s, n)];
      this.map.setArea({bbox}); this.map.flyTo(bbox);
    };

    $('exBuild').onclick = async () => { const a = this.map.area(); if (!a) return;
      const flags = {}; $('exColList').querySelectorAll('input[data-flag]').forEach(i => flags[i.dataset.flag] = i.checked);
      const body = {...a, ...flags,
        question: `area selected on the map (${a.bbox ? 'box' : 'polygon'})`};
      AICESAT.clearError(); $('exBuild').disabled = true;
      try {
        const d = await api.extract(body);
        this.jobs[d.job_id] = {plan: body, j: {status: 'running', log: []}}; this.pollJob(d.job_id, body); this.refresh();
        if (d.scene_id) this.openScene(d.scene_id);   // jump straight into the scene view; it paints in as the build streams
      }
      catch (e) { AICESAT.showError(e); $('exBuild').disabled = false; } };

    AICESAT.util.drawer(root, null);
    { const G = U.GLOSSARY;   // opt-in "?" help on the jargon
      const ch = $('exCovHint'); if (ch) ch.appendChild(U.help(G.coverage)); }
    this.loadCollections();
    this.refresh(); this.startPolling();
  }
  // Poll only while on screen — see the note in lake.js: a never-cleared interval kept refreshing in the background
  // while a scene was building, competing with the build for server CPU.
  startPolling() { this.stopPolling(); this.timer = setInterval(() => this.refresh(true), 5000); }
  stopPolling() { if (this.timer) { clearInterval(this.timer); this.timer = null; } }

  // ---- coverage: auto-fetched when an area is defined, shown inline on each collection row
  covCaveat() {
    return ' <span class="cov-caveat" title="From the sub-granule index: granules with points in your area\'s cells. The scene keeps only points inside the exact box, so this is a tight count, not a footprint upper bound.">ⓘ</span>';
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
        if (c.n_granules == null) { el.innerHTML = c.indexed === false ? '<span class="no" title="No sub-granule index built over this area yet — build the index to see coverage here.">not indexed</span>' : '<span class="no">n/a</span>'; return; }
        el.innerHTML = c.n_granules ? `<b>${c.n_granules}</b> gran.${this.covCaveat()}` : '<span class="no">none</span>';
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
      // Every collection is selectable, ATL03 included. `default` decides what starts checked: ATL03 is the heavy
      // one (whole photon clouds, not segments) so it starts OFF, but excluding it from the list entirely meant the
      // one collection a user might deliberately opt into was the one they could not reach.
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
        (sc.status === 'ready' ? `<button data-open="${sc.scene_id}">Open</button>` : '') +
        (sc.status !== 'loading' ? `<button class="sc-del" data-del="${sc.scene_id}" title="Delete this scene — removes it from Explore and the map. Fetched data stays in the lake, so rebuilding the same area is fast.">✕</button>` : '') + `</div>`;
      const entry = sc.job_id && this.jobs[sc.job_id];
      const prog = (sc.status === 'loading' && entry) ? `<div class="scene-prog">${this.progressHTML(entry.j, entry.plan)}</div>` : '';
      return `<div class="scene-card ${sc.status}">${head}${prog}</div>`;
    }).join('') || '<div class="small">no scenes yet — pick an area and build one</div>';
    list.querySelectorAll('button[data-open]').forEach(b => b.onclick = () => this.openScene(b.dataset.open));
    list.querySelectorAll('button[data-del]').forEach(b => b.onclick = e => { e.stopPropagation(); this.deleteScene(b.dataset.del); });
    // adopt a loading scene whose job we aren't polling yet (e.g. after a page reload)
    scenes.forEach(sc => { if (sc.status === 'loading' && sc.job_id && !this.jobs[sc.job_id]) { this.jobs[sc.job_id] = {plan: {}, j: {status: 'running', log: []}}; this.pollJob(sc.job_id, {}); } });
  }
  async deleteScene(id) {
    // Irreversible: confirm first. Removes only this scene (registry row + doc); the fetched data stays in the lake.
    if (!window.confirm('Delete this scene? It is removed from Explore and the map. Fetched data stays in the lake (rebuilding the same area is fast). This cannot be undone.')) return;
    AICESAT.clearError();
    try {
      await this.api.deleteScene(id);
      this.map.state.scenes = (this.map.state.scenes || []).filter(sc => sc.scene_id !== id);
      this.renderScenes();
      this.map.render();          // drop its footprint from the map's `scenes` layer immediately
      this.refresh();             // reconcile footprints + list with the server
    } catch (e) { AICESAT.showError(e); }
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    await this.map.refreshData(this.api);
    this.renderScenes();
  }
  show() { this.root.classList.add('on'); this.startPolling(); this.refresh(); }
  hide() { this.root.classList.remove('on'); this.stopPolling(); }
};
