/* Explore view: globe + a guided 1-Area → 2-Collections/Coverage → 3-Build flow, plus the scenes list. */
window.AICESAT = window.AICESAT || {};
AICESAT.ExploreView = class {
  constructor(root, api, openScene) {
    const U = AICESAT.util; this.api = api; this.root = root; this.openScene = openScene;
    root.innerHTML = `
      <div class="map" id="exMap"></div>
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
          <div class="step-head"><span class="step-n">2</span> <b id="exCollLbl">Collections</b> &amp; coverage</div>
          <span id="exColBoxes" class="small">loading…</span>
          <div class="row"><button id="exCov" disabled>Check coverage</button></div>
          <div id="exCovOut" class="small covout"></div>
        </div>
        <div class="step">
          <div class="step-head"><span class="step-n">3</span> Build the scene</div>
          <div class="row"><button id="exBuild" disabled>Build scene</button>
            <label class="small" title="upper bound on how many granules the build fetches per collection">max granules <input id="exMaxG" type="number" value="12" min="1" max="250" style="width:54px"></label></div>
          <div id="exBuildOut" class="small mono buildout"></div>
        </div>
      </div>
      <div class="panel" id="exScenes" data-title="scenes" style="top:12px;right:12px;width:300px"><h2>Scenes</h2><div class="list" id="exSceneList"></div></div>
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
      $('exCov').disabled = $('exBuild').disabled = !a;
      $('exClose').hidden = !(this.map.state.mode === 'poly' && !this.map.state.polyClosed && this.map.state.poly.length >= 3);
    };
    this.map.onOpenScene = sc => { if (sc.status === 'ready') openScene(sc.scene_id); };

    // segmented mode picker
    const seg = $('exMode');
    const setMode = m => { this.map.setMode(m); seg.querySelectorAll('button[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m)); };
    seg.querySelectorAll('button[data-mode]').forEach(b => b.onclick = () => setMode(b.dataset.mode));
    $('exClose').onclick = () => this.map.closePolygon();
    root.addEventListener('keydown', e => { if (e.key === 'Enter' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName || '')) this.map.closePolygon(); });
    $('exClear').onclick = () => { this.map.clear(); $('exCovOut').innerHTML = ''; $('exBuildOut').innerHTML = ''; };
    $('exRegion').onchange = e => { const r = this.map.state.regions[e.target.value]; if (!r) return; this.map.setArea({bbox: r.bbox}); this.map.flyTo(r.bbox); };
    // numeric bbox entry (precise; also handles polar caps)
    $('bbSet').onclick = () => {
      const w = +$('bbW').value, s = +$('bbS').value, e = +$('bbE').value, n = +$('bbN').value;
      if ([w, s, e, n].some(v => Number.isNaN(v))) { $('exCoords').textContent = 'enter all four: W, S, E, N'; return; }
      const bbox = [Math.min(w, e), Math.min(s, n), Math.max(w, e), Math.max(s, n)];
      this.map.setArea({bbox}); this.map.flyTo(bbox);
    };

    $('exCov').onclick = async () => { const a = this.map.area(); if (!a) return; AICESAT.clearError(); $('exCovOut').textContent = 'checking the NASA catalog…';
      const brk = o => Object.entries(o || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || '—';
      try { const d = await api.coverage(a);
        $('exCovOut').innerHTML = d.collections.map(c =>
          `<div class="covrow ${c.n_granules ? 'ok' : 'no'}"><b>${c.label}</b> <span class="small">${c.product} v${c.version} · ${c.epoch}</span> · ` +
          (c.n_granules == null ? `<span class="no">unavailable</span>` : `<b>${c.n_granules}</b> granules`) +
          (c.by_month && Object.keys(c.by_month).length ? `<div class="small">by month — ${brk(c.by_month)}</div>` : (c.error ? `<div class="small">${c.error}</div>` : '')) +
          `</div>`).join('');
      } catch (e) { $('exCovOut').textContent = 'error: ' + e.message; AICESAT.showError(e); } };

    $('exBuild').onclick = async () => { const a = this.map.area(); if (!a) return;
      const flags = {}; $('exColBoxes').querySelectorAll('input[data-flag]').forEach(i => flags[i.dataset.flag] = i.checked);
      const body = {...a, max_granules: +$('exMaxG').value, ...flags,
        with_coreg: !!(flags.with_atl03 && flags.with_glas),
        question: `area selected on the map (${a.bbox ? 'box' : 'polygon'})`};
      AICESAT.clearError(); $('exBuild').disabled = true; $('exBuildOut').textContent = 'starting build…';
      try { const d = await api.extract(body); this.pollJob(d.job_id); await this.refresh(); } catch (e) { $('exBuildOut').textContent = 'error: ' + e.message; AICESAT.showError(e); $('exBuild').disabled = false; } };

    AICESAT.util.drawer(root, null);
    { const G = U.GLOSSARY;   // opt-in "?" help on the jargon
      const cl = $('exCollLbl'); if (cl) cl.appendChild(U.help(G.collections));
      const mg = $('exMaxG') && $('exMaxG').closest('label'); if (mg) mg.appendChild(U.help(G.granules));
      const cov = $('exCov'); if (cov && cov.parentElement) cov.parentElement.insertBefore(U.help(G.coverage), cov.nextSibling); }
    this.loadCollections();
    this.refresh(); this.timer = setInterval(() => this.refresh(true), 5000);
  }
  async loadCollections() {
    try {
      const cols = await this.api.collections();
      this.$('exColBoxes').innerHTML = cols.map(c =>
        `<label title="${c.product} v${c.version} · ${c.epoch}" style="display:inline-block;margin:1px 8px 1px 0;white-space:nowrap"><input type="checkbox" data-flag="${c.flag}" ${c.default ? 'checked' : ''}> ${c.label}</label>`).join('');
    } catch (e) { this.$('exColBoxes').textContent = 'collections unavailable'; }
  }
  async pollJob(jid) {
    const $ = this.$, api = this.api;
    const tick = async () => { const j = await api.job(jid);
      $('exBuildOut').innerHTML = `<b>${j.status}</b>${j.seconds ? ` (${j.seconds}s)` : ''}\n` + j.log.join('\n') + (j.error ? `\n${j.error}` : '') + (j.status === 'done' && j.scene_id ? `\n<a href="#scene/${j.scene_id}">open the scene →</a>` : '');
      if (j.status === 'running') setTimeout(tick, 1500); else { if (j.error) AICESAT.showError(j.error); $('exBuild').disabled = false; this.refresh(); } };
    tick();
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    await this.map.refreshData(this.api);
    const list = this.$('exSceneList');
    list.innerHTML = this.map.state.scenes.map(sc => `<div class="row"><span class="status ${sc.status}">${sc.status}</span><span class="grow" title="${sc.question || ''}">${sc.question || sc.scene_id}<br><span class="small">${(sc.series || []).join(' + ') || '…'}${sc.coreg ? ' · coreg' : ''} · ${(sc.created || '').slice(0, 16).replace('T', ' ')}</span></span>` +
      (sc.status === 'ready' ? `<button data-open="${sc.scene_id}">Open</button>` : '') + `</div>`).join('') || '<div class="small">no scenes yet — pick an area and build one</div>';
    list.querySelectorAll('button[data-open]').forEach(b => b.onclick = () => this.openScene(b.dataset.open));
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
