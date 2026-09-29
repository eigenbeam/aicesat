/* Explore view: globe + a guided Area → Collections/coverage → Build flow. Coverage auto-fetches when an
   area is defined and annotates the collection rows; a scene's build progress lives in its Scenes-list card. */
window.AICESAT = window.AICESAT || {};

// Can a build over this area succeed, given the coverage check and the checked collections? A build SKIPS a checked
// collection whose index does not contain the whole area and fails only when it skips every one, so that case
// disables Build, and the partial case says what the scene will be missing. A collection with NO index reaching this
// area (the check's claim_overlap === false, or a fit's `unindexed`) is named as such and not offered a fit: shrinking
// cannot reach ground that was never indexed.
// Pure: tests/test_coverage_gate.js.
AICESAT.coverageGate = function (byKey, checked, unindexed = []) {
  const none = {outside: [], fittable: []};
  if (!byKey) return {canBuild: true, message: '', ...none};      // no answer (pending, or the check failed): never block on it
  if (!checked.length) return {canBuild: false, message: 'Check at least one collection.', ...none};
  const rows = checked.map(k => byKey[k]).filter(c => c && c.possible !== false);
  if (!rows.length) return {canBuild: false, message: 'None of the checked collections flew over this area.', ...none};
  const outside = rows.filter(c => !c.covered), keys = cs => cs.map(c => c.key);
  const none_here = c => c.claim_overlap === false || unindexed.includes(c.key);
  const absent = outside.filter(none_here), partial = outside.filter(c => !none_here(c));
  const names = cs => { const l = cs.map(c => c.label || c.key); return l.length < 2 ? l.join('') : l.slice(0, -1).join(', ') + ' and ' + l[l.length - 1]; };
  const res = {outside: keys(outside), fittable: keys(partial)};
  if (outside.length === rows.length) {
    return {canBuild: false, ...res, message: 'None of the checked collections is indexed over all of this area, so a build would refuse it.' +
            (partial.length ? ' Draw inside the dashed outline, or fit the area to it.' : ' Build their index here, or pick another area.')};
  }
  const msg = [];
  if (partial.length) msg.push(`${names(partial)} will be skipped: ${partial.length === 1 ? 'its' : 'their'} index does not cover all of this area.`);
  if (absent.length) msg.push(`${names(absent)} ${absent.length === 1 ? 'has' : 'have'} no index here and will be skipped.`);
  return {canBuild: true, ...res, message: msg.join(' ')};
};

AICESAT.ExploreView = class {
  constructor(root, api, openScene) {
    const U = AICESAT.util; this.api = api; this.root = root; this.openScene = openScene;
    this.jobs = {};          // job_id -> {plan, j}: build progress for the currently-loading scene(s)
    this._covSeq = 0;        // guards against a stale coverage response overwriting a newer one
    this._cov = null;        // the last coverage answer for the current area, by collection key (null: none yet)
    this._claims = null;     // where each collection's index will accept a build, for the dashed outlines
    this._fitNote = null;    // {sig, moved, unindexed}: what "Fit to indexed area" did, kept while that area is current
    root.innerHTML = `
      <div class="map" id="exMap"></div>
      <div id="mapLegend" class="map-legend"><b>Map key</b><span><i class="swf" style="--c:#378ADD"></i>data &amp; selection</span><span><i class="swo" style="--c:#4caf7d"></i>scene ready</span><span><i class="swo" style="--c:#E0A030"></i>building</span><span><i class="swo" style="--c:#d9534f"></i>error</span><span><i class="swo swd" style="--c:#c8d2e6"></i>indexed: draw inside</span></div>
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
          <div id="exGate" class="small gate" hidden><span id="exGateMsg"></span> <button id="exFit" hidden title="Bring the overhanging sides in until every checked collection's index contains the area. Only the sides that reach outside move; a polygon keeps its shape and loses only what lies outside.">Fit to indexed area</button></div>
          <div class="row"><button id="exBuild" disabled>Build scene</button></div>
        </div>
      </div>
      <div class="panel" id="exScenes" data-title="scenes" style="top:12px;right:12px;width:308px"><h2>Scenes</h2><div class="list" id="exSceneList"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    const areaSig = a => a ? JSON.stringify(a.bbox || a.polygon) : '';
    this._areaSig = areaSig;
    const syncBboxFields = a => { if (a && a.bbox) { const [w, s, e, n] = a.bbox; $('bbW').value = w; $('bbS').value = s; $('bbE').value = e; $('bbN').value = n; } };

    this.map = new AICESAT.MapView($('exMap'), {grid: true, selectCells: false, draw: true, footprints: true});
    this.map.onSelect = a => {
      syncBboxFields(a);
      this._cov = null;
      if (this._fitNote && areaSig(a) !== this._fitNote.sig) this._fitNote = null;
      this.applyGate();
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
    $('exFit').onclick = () => this.fitArea();
    $('exColList').addEventListener('change', () => { this.showClaims(); this.applyGate(); });
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
    this.loadCollections().then(() => this.loadClaims());
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
        // An instrument that never flew here is a different fact from one we have not indexed yet, and only one of
        // them is worth acting on. IceBridge (ICESSN) exists at 60..90 N and -90..-53 S only, so over Nepal it is
        // not "not indexed" — it is impossible, and telling the user to build an index would waste their time on a
        // search that finds nothing. Uncheck and disable, rather than let the build raise a coverage error.
        const box = el.closest('.col-row') && el.closest('.col-row').querySelector('input[type=checkbox]');
        if (c.possible === false) {
          if (box) { box.checked = false; box.disabled = true; }
          el.innerHTML = '<span class="no" title="This instrument never surveyed here — IceBridge flew the Arctic and Antarctic only. Nothing to index.">not flown here</span>';
          return;
        }
        if (box) box.disabled = false;
        // The last build here searched and found no granules, so it claimed nothing. "not indexed" would send the
        // user to build again and get the same empty answer; say what actually happened instead.
        if (c.searched_empty && !c.covered) {
          const w = c.searched_empty.window ? ` for ${c.searched_empty.window.join('..')}` : '';
          el.innerHTML = `<span class="no" title="The last index build over this area (${(c.searched_empty.at || '').slice(0, 10)}) found no granules in NASA's catalog${w}. Expected if the instrument never passed here; otherwise the search itself may have come back empty.">none found</span>`;
          return;
        }
        if (c.n_granules == null) { el.innerHTML = c.indexed === false ? '<span class="no" title="No sub-granule index built over this area yet — build the index to see coverage here.">not indexed</span>' : '<span class="no">n/a</span>'; return; }
        // `indexed` and `covered` are different facts, and the gap is real. An index is built by searching CMR over
        // a RECTANGLE and indexing the granules that intersect it. Cells outside that rectangle still end up with
        // rows — ATL03/ATL06 index a granule's whole pole-to-pole track, and GLAS/ICESSN clip shots to the box so
        // boundary hexes keep only their inside-the-box shots. Either way such a cell holds SOME rows but not all
        // of them: every granule that crosses the cell without crossing the built rectangle was never searched.
        // The Lake view calls that cell indexed because rows exist. A build requires CONTAINMENT precisely because
        // containment is the only thing that guarantees the granule set is complete for the area.
        if (c.covered === false && c.claim_overlap === false) {
          el.innerHTML = '<span class="no" title="No index for this collection reaches any of this area (it may be indexed elsewhere). Build its index here to use it.">not indexed here</span>';
          return;
        }
        if (c.covered === false) {
          const n = c.n_granules ? `<b>${c.n_granules}</b> gran. ` : '';
          el.innerHTML = `${n}<span class="partial" title="Your area reaches outside the region this index was built over. The granules shown are real but incomplete: out there the index only holds granules that also crossed the built region, so a build refuses the area rather than silently return part of it. Draw inside the dashed outline on the map, use Fit to indexed area below, or extend the index.">reaches outside index</span>`;
          return;
        }
        el.innerHTML = c.n_granules ? `<b>${c.n_granules}</b> gran.${this.covCaveat()}` : '<span class="no">none</span>';
      });
      if (note) note.textContent = 'granule counts over your area — the scene keeps only points inside it';
      this._cov = byKey; this.showClaims(); this.applyGate();   // rows unchecked above fire no change event
    } catch (e) {
      if (seq !== this._covSeq) return;
      this.setCovCells('');
      if (note) note.textContent = 'coverage check unavailable';
      this._cov = null; this.applyGate();
    }
  }

  async loadCollections() {
    try {
      const cols = await this.api.collections();
      this._cols = cols;
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
    const ALL = [['GLAS', 'ICESat-1 · GLAS'], ['ICESSN', 'IceBridge · ATM'], ['ATL06', 'ICESat-2 · land ice'], ['ATL03', 'ICESat-2 · photons'], ['GEDI', 'GEDI · L2A'], ['GPSTRUTH', 'Summit GPS traverse'], ['surface', 'DEM surface'], ['imagery', 'Satellite imagery'], ['coreg', 'Co-registration']];
    const flagOf = {GLAS: 'with_glas', ICESSN: 'with_icessn', ATL06: 'with_atl06', ATL03: 'with_atl03', GEDI: 'with_gedi', GPSTRUTH: 'with_gpstruth'};
    const hasPlan = plan && Object.values(flagOf).some(f => plan[f] !== undefined);
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
      else { if (j.error) AICESAT.showError(j.error); delete this.jobs[jid]; this.applyGate(); await this.refresh(); }
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
  // ---- where a build is accepted: dashed outlines of each checked collection's index, a Build gate, and a fit
  checkedKeys() {
    return [...this.$('exColList').querySelectorAll('.col-row')].filter(r => { const i = r.querySelector('input'); return i && i.checked && !i.disabled; })
      .map(r => r.querySelector('.col-cov').dataset.key);
  }
  async loadClaims() {
    try { this._claims = await this.api.claims(); } catch (e) { this._claims = null; }
    this.showClaims();
  }
  showClaims() {
    if (!this._claims) { this.map.setClaims([]); return; }
    const C = AICESAT.missions.MISSION_COLORS, label = k => ((this._cols || []).find(c => c.key === k) || {}).label || k;
    // Collections indexed over the same box share one outline: drawn once, named together, in a neutral colour.
    // Drawn per collection they stack exactly and only the last colour and name survive.
    const groups = new Map();
    for (const k of this.checkedKeys()) for (const p of this._claims[k] || []) {
      const sig = p.bbox.join() + '|' + p.outer.length;
      const g = groups.get(sig) || {outer: p.outer, holes: p.holes, keys: []};
      g.keys.push(k); groups.set(sig, g);
    }
    this.map.setClaims([...groups.values()].map(g => ({outer: g.outer, holes: g.holes, label: g.keys.map(label).join(', '),
      color: g.keys.length === 1 ? (C[g.keys[0] === 'ATL03' ? 'ICESAT2' : g.keys[0]] || [200, 210, 230]) : [200, 210, 230]})));
  }
  applyGate() {
    const a = this.map.area(), btn = this.$('exBuild'), box = this.$('exGate'), fit = this.$('exFit');
    if (!a) { btn.disabled = true; box.hidden = true; return; }
    const fn = this._fitNote, km = v => v < 1 ? Math.round(v * 1000) + ' m' : v.toFixed(1) + ' km';
    const g = AICESAT.coverageGate(this._cov, this.checkedKeys(), fn ? fn.unindexed : []);
    btn.disabled = !g.canBuild;
    fit.hidden = !g.fittable.length;
    const moved = fn && Object.keys(fn.moved).length ? 'Fitted: ' + Object.entries(fn.moved).map(([s, v]) => s + ' edge in ' + km(v)).join(', ') + '.' : '';
    const msg = [moved, g.message].filter(Boolean).join(' ');
    this.$('exGateMsg').textContent = msg;
    box.hidden = !msg && fit.hidden;
  }
  async fitArea() {
    const a = this.map.area(); if (!a) return;
    const keys = this.checkedKeys().filter(k => !this._cov || !this._cov[k] || this._cov[k].possible !== false);
    const fit = this.$('exFit'), msg = this.$('exGateMsg');
    fit.disabled = true; msg.textContent = 'fitting…';
    try {
      const r = await this.api.fit(a, keys);
      if (!r.bbox) { msg.textContent = r.reason || 'could not fit this box'; return; }
      // Kept for the gate, not as text: which collections have no index here must track the checkboxes.
      // A box comes back a box; a polygon comes back as the part of it inside the index (never more than was drawn).
      const next = r.polygon ? {polygon: r.polygon} : {bbox: r.bbox}, sig = this._areaSig(next);
      const prev = this._fitNote && this._fitNote.sig === sig ? this._fitNote.moved : {};
      this._fitNote = {sig, moved: Object.keys(r.moved || {}).length ? r.moved : prev, unindexed: r.unindexed || []};
      this.map.setArea(next);                            // -> onSelect -> coverage refresh -> applyGate shows the note
    } catch (e) { msg.textContent = 'fit unavailable: ' + (e.message || e); }
    finally { fit.disabled = false; }
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    await this.map.refreshData(this.api);
    this.renderScenes();
  }
  show() { this.root.classList.add('on'); this.startPolling(); this.refresh(); this.loadClaims(); }
  hide() { this.root.classList.remove('on'); this.stopPolling(); }
};
