/* Lake view: grid on, per-cell stats, live summary, storage limit, background loading and eviction of selected cells. */
window.AICESAT = window.AICESAT || {};
AICESAT.LakeView = class {
  constructor(root, api) {
    const U = AICESAT.util; this.api = api; this.root = root;
    root.innerHTML = `
      <div class="map" id="lkMap"></div>
      <div class="panel" id="lkSummary" data-title="lake summary" style="top:12px;left:12px;width:320px">
        <h2>Lake</h2>
        <div class="small" style="margin-bottom:6px">collection <select id="lkMission"><option value="ICESAT2">ICESat-2 ATL03</option></select></div>
        <div id="lkStats" class="small">loading…</div>
        <div class="bar"><div id="lkBar" style="width:0%"></div></div>
        <div class="small">limit <input id="lkLimit" type="number" min="0.1" step="0.5" style="width:70px"> GB <button id="lkSetLimit">set</button> <span id="lkLimitMsg"></span></div>
        <details class="small" style="margin-top:6px"><summary>recent evictions</summary><div id="lkEvictions"></div></details>
      </div>
      <div class="panel" id="lkSelect" data-title="selected cells" style="top:12px;right:12px;width:300px">
        <h2>Cells</h2><div class="small">Click cells (res 6) to select. Colour = data age (bright = fresh). Hover for stats.</div>
        <div id="lkSel" class="small mono" style="margin:6px 0">none selected</div>
        <button id="lkLoad" disabled>Load in background</button><button id="lkEvict" class="danger" disabled>Evict</button><button id="lkClear">Clear</button>
        <label class="small">granules <input id="lkMaxG" type="number" value="40" min="1" max="200" style="width:56px"></label>
      </div>
      <div class="panel" id="lkActivity" data-title="activity" style="bottom:12px;left:12px;width:420px;max-height:36vh;overflow:auto"><h2>Activity</h2><div id="lkJobs" class="small">no jobs</div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    this.mission = 'ICESAT2';
    $('lkMission').onchange = e => { this.mission = e.target.value; this.map.clear(); this.refresh(); };
    this.map = new AICESAT.MapView($('lkMap'), {grid: true, selectCells: true, draw: false, footprints: false});
    this.map.onCellsSelected = cells => { $('lkSel').textContent = cells.length ? `${cells.length} cells: ${cells.slice(0, 6).join(', ')}${cells.length > 6 ? '…' : ''}` : 'none selected'; $('lkLoad').disabled = $('lkEvict').disabled = !cells.length; };
    $('lkClear').onclick = () => this.map.clear();
    $('lkLoad').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString()); $('lkLoad').disabled = true;
      try { const d = await api.lakeLoad(cells, {max_granules: +$('lkMaxG').value}); this.watch(d.job_id); } catch (e) { alert(e.message); } };
    $('lkEvict').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString());
      try { const d = await api.lakeEvict(cells); $('lkLimitMsg').textContent = `evicted ${d.evicted.length} cells`; this.map.clear(); this.refresh(); } catch (e) { alert(e.message); } };
    $('lkSetLimit').onclick = async () => { const gb = +$('lkLimit').value; if (!(gb > 0)) return;
      try { const d = await api.lakeSettings(Math.round(gb * 1e9)); $('lkLimitMsg').textContent = d.evicted && d.evicted.length ? `limit set; evicted ${d.evicted.length} cells (${U.fmtBytes(d.evicted.reduce((a, e) => a + e.bytes, 0))})` : 'limit set'; this.refresh(); } catch (e) { $('lkLimitMsg').textContent = e.message; } };
    AICESAT.util.drawer(root, null);
    this.timer = setInterval(() => this.refresh(true), 10000);
  }
  watch(jid) { const tick = async () => { await this.refreshJobs(); const j = await this.api.job(jid); if (j.status === 'running') setTimeout(tick, 2000); else this.refresh(); }; tick(); }
  async refreshJobs() {
    const jobs = await this.api.jobs().catch(() => []);
    this.$('lkJobs').innerHTML = jobs.length ? jobs.map(j => `<div class="row"><span class="status ${j.status === 'running' ? 'loading' : j.status === 'done' ? 'ready' : 'error'}">${j.status}</span><span class="grow" title="${(j.log || []).join('\n')}">${j.kind} ${j.id}${j.scene_id ? ' → ' + j.scene_id : ''}${j.seconds ? ` (${j.seconds}s)` : ''}<br><span class="small">${(j.log || []).slice(-1)[0] || ''}</span></span></div>`).join('') : 'no jobs';
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    const U = AICESAT.util, $ = this.$;
    const [s] = await Promise.all([this.api.lakeSummary(this.mission), this.map.refreshData(this.api, this.mission), this.refreshJobs()]);
    this.syncMissions(s.missions);
    $('lkStats').innerHTML = `<table class="stats"><tr><td>cells</td><td class="num">${U.fmtN(s.cells)}</td><td>files</td><td class="num">${U.fmtN(s.files)}</td></tr><tr><td>rows</td><td class="num">${U.fmtN(s.rows)}</td><td>granules</td><td class="num">${U.fmtN(s.granules)}</td></tr><tr><td>size</td><td class="num">${U.fmtBytes(s.bytes)}</td><td>limit</td><td class="num">${U.fmtBytes(s.max_bytes)}</td></tr><tr><td>oldest</td><td colspan="3" class="small">${U.fmtDate(s.oldest_ingested)} · newest ${U.fmtDate(s.newest_ingested)}</td></tr></table>`;
    const u = s.usage || 0; const bar = $('lkBar'); bar.style.width = Math.min(100, u * 100).toFixed(1) + '%'; bar.className = u > 1 ? 'over' : u > 0.85 ? 'warn' : '';
    if (document.activeElement !== $('lkLimit')) $('lkLimit').value = (s.max_bytes / 1e9).toFixed(1);
    $('lkEvictions').innerHTML = (s.evictions_recent || []).length ? s.evictions_recent.map(e => `<div>${U.fmtDate(e.evicted_at)} · cell ${e.cell} · ${U.fmtBytes(e.bytes)} · ${e.reason}</div>`).join('') : 'none';
  }
  syncMissions(missions) {
    const sel = this.$('lkMission'), U = AICESAT.util;
    const list = (missions && missions.length) ? missions : [{mission: 'ICESAT2', product: 'ICESat-2 ATL03'}];
    const key = list.map(m => m.mission).join(',');
    if (sel.dataset.key !== key) {   // rebuild options only when the set of collections changes
      sel.innerHTML = list.map(m => `<option value="${m.mission}">${m.product}${m.cells != null ? ` · ${U.fmtN(m.cells)} cells` : ''}</option>`).join('');
      sel.dataset.key = key;
    }
    if (!list.some(m => m.mission === this.mission)) this.mission = list[0].mission;
    sel.value = this.mission;
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
