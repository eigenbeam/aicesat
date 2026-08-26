/* Explore view: map + tools + scenes list. */
window.AICESAT = window.AICESAT || {};
AICESAT.ExploreView = class {
  constructor(root, api, openScene) {
    const U = AICESAT.util; this.api = api; this.root = root; this.openScene = openScene;
    root.innerHTML = `
      <div class="map" id="exMap"></div>
      <div class="panel" id="exTools" data-title="area tools" style="top:12px;left:12px;width:344px">
        <h2>Select an area</h2>
        <div class="small">Navigate: drag to spin the globe, scroll to zoom. Box: drag a rectangle. Polygon: click vertices then Close (Enter). Blue hexagons = cells in the lake; grid shows all cells with stats on hover.</div>
        <div style="margin:8px 0 4px"><button id="exPan" class="on">Navigate</button><button id="exBox">Box</button><button id="exPoly">Polygon</button><button id="exClose" hidden>Close polygon</button><button id="exClear">Clear</button>
          <select id="exRegion"><option value="">regions…</option></select></div>
        <div id="exCoords" class="mono small">no area selected</div>
        <div style="margin-top:8px"><button id="exCov" disabled>Check coverage</button><button id="exBuild" disabled>Build scene</button>
          <label class="small" title="upper bound on how many ATL03/GLAS granules the build will fetch">max granules <input id="exMaxG" type="number" value="12" min="1" max="250" style="width:54px"></label>
          <label class="small" title="ICESat/GLAS GLAH06 (2003-2009)"><input id="exGlas" type="checkbox" checked> GLAS</label>
          <label class="small" title="ICESat-2 ATL06 land-ice height (2018-)"><input id="exAtl06" type="checkbox"> ATL06</label>
          <label class="small" title="Operation IceBridge ATM ICESSN (2009-2019); flight-line coverage only"><input id="exIcessn" type="checkbox"> ICESSN</label></div>
        <div id="exOut" class="small mono" style="white-space:pre-wrap;max-height:30vh;overflow:auto;margin-top:8px"></div>
      </div>
      <div class="panel" id="exScenes" data-title="scenes" style="top:12px;right:12px;width:300px"><h2>Scenes</h2><div class="list" id="exSceneList"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id);
    this.map = new AICESAT.MapView($('exMap'), {grid: true, selectCells: false, draw: true, footprints: true});
    this.map.onSelect = a => { $('exCoords').textContent = a ? JSON.stringify(a) : (this.map.state.poly.length ? `polygon: ${this.map.state.poly.length} vertices (need 3, then Close)` : 'no area selected'); $('exCov').disabled = $('exBuild').disabled = !a; $('exClose').hidden = !(this.map.state.mode === 'poly' && !this.map.state.polyClosed && this.map.state.poly.length >= 3); };
    this.map.onOpenScene = sc => { if (sc.status === 'ready') openScene(sc.scene_id); };
    const modeBtns = {pan: $('exPan'), box: $('exBox'), poly: $('exPoly')};
    const setMode = m => { this.map.setMode(m); for (const k in modeBtns) modeBtns[k].classList.toggle('on', k === m); };
    $('exPan').onclick = () => setMode('pan');
    $('exBox').onclick = () => setMode('box');
    $('exPoly').onclick = () => setMode('poly');
    $('exClose').onclick = () => this.map.closePolygon(); root.addEventListener('keydown', e => { if (e.key === 'Enter') this.map.closePolygon(); });
    $('exClear').onclick = () => { this.map.clear(); $('exOut').textContent = ''; };
    $('exRegion').onchange = e => { const r = this.map.state.regions[e.target.value]; if (!r) return; setMode('box'); this.map.setArea({bbox: r.bbox}); this.map.flyTo(r.bbox); };
    $('exCov').onclick = async () => { const a = this.map.area(); if (!a) return; $('exOut').textContent = 'checking CMR…';
      const brk = o => Object.entries(o || {}).map(([k, v]) => `${k}\u2009${v}`).join(' · ') || '—';
      try { const d = await api.coverage(a);
        $('exOut').innerHTML =
          `<div class="covrow"><b>ATL03</b> <span class="small">v${d.ATL03.version} · ${d.ATL03.window.join(' – ')}</span> · <b>${d.ATL03.n_granules}</b> granules<div class="small">by month — ${brk(d.ATL03.by_month)}</div></div>` +
          `<div class="covrow"><b>GLAH06</b> <span class="small">v${d.GLAH06.version}</span> · <b>${d.GLAH06.n_granules}</b> granules<div class="small">by campaign — ${brk(d.GLAH06.by_campaign)}</div></div>` +
          `<div class="covrow ${d.both_present ? 'ok' : 'no'}">${d.both_present ? '✓ both missions present' : '✗ not both missions present'}</div>`;
      } catch (e) { $('exOut').textContent = 'error: ' + e.message; } };
    $('exBuild').onclick = async () => { const a = this.map.area(); if (!a) return;
      const body = {...a, max_granules: +$('exMaxG').value, with_glas: $('exGlas').checked, with_coreg: $('exGlas').checked,
        with_atl06: $('exAtl06').checked, with_icessn: $('exIcessn').checked, question: `area selected on the map (${a.bbox ? 'box' : 'polygon'})`};
      $('exBuild').disabled = true; $('exOut').textContent = 'starting build…';
      try { const d = await api.extract(body); this.pollJob(d.job_id); await this.refresh(); } catch (e) { $('exOut').textContent = 'error: ' + e.message; $('exBuild').disabled = false; } };
    this.$ = $;
    AICESAT.util.drawer(root, null);
    this.refresh(); this.timer = setInterval(() => this.refresh(true), 5000);
  }
  async pollJob(jid) {
    const $ = this.$, api = this.api;
    const tick = async () => { const j = await api.job(jid); $('exOut').innerHTML = `<b>job ${j.id}: ${j.status}</b>${j.seconds ? ` (${j.seconds}s)` : ''}\n` + j.log.join('\n') + (j.error ? `\n${j.error}` : '') + (j.status === 'done' && j.scene_id ? `\n<a href="#scene/${j.scene_id}">open the scene →</a>` : '');
      if (j.status === 'running') setTimeout(tick, 1500); else { $('exBuild').disabled = false; this.refresh(); } };
    tick();
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    await this.map.refreshData(this.api);
    const U = AICESAT.util, list = this.$('exSceneList');
    list.innerHTML = this.map.state.scenes.map(sc => `<div class="row"><span class="status ${sc.status}">${sc.status}</span><span class="grow" title="${sc.question || ''}">${sc.question || sc.scene_id}<br><span class="small">${(sc.series || []).join(' + ') || '…'}${sc.coreg ? ' · coreg' : ''} · ${(sc.created || '').slice(0, 16).replace('T', ' ')}</span></span>` +
      (sc.status === 'ready' ? `<button data-open="${sc.scene_id}">Open</button>` : '') + `</div>`).join('') || '<div class="small">no scenes yet — draw an area and build one</div>';
    list.querySelectorAll('button[data-open]').forEach(b => b.onclick = () => this.openScene(b.dataset.open));
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
