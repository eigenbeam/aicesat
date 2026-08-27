/* Lake view (dev): view EITHER the fetched DATA or the sub-granule INDEX coverage of one chosen collection. */
window.AICESAT = window.AICESAT || {};
AICESAT.LakeView = class {
  constructor(root, api) {
    const U = AICESAT.util; this.api = api; this.root = root;
    root.innerHTML = `
      <div class="map" id="lkMap"></div>
      <div class="panel" id="lkSummary" data-title="data lake" style="top:12px;left:12px;width:332px">
        <h2>Data lake</h2>
        <div class="lkview">
          <div class="seg" id="lkMode"><button data-mode="data">Data</button><button data-mode="index" class="on">Index</button></div>
          <select id="lkColl" class="lkcoll"></select>
        </div>
        <div class="small lkhint" id="lkHint"></div>
        <div id="lkStats" class="small">loading…</div>
        <div class="bar" id="lkBarWrap"><div id="lkBar" style="width:0%"></div></div>
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
      <div class="panel" id="lkLog" data-title="lake log"><div class="small" style="margin-bottom:4px">Live pipeline activity — CMR search, chunk fetch/decode, materialize, evict, query.</div><div id="lkLogBody" class="lakelog"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    this.mode = 'index'; this.coll = 'ATL06'; this.cols = []; this._viewSeq = 0;   // view one collection at a time; default: the ATL06 index build
    this.map = new AICESAT.MapView($('lkMap'), {grid: true, selectCells: true, draw: false, footprints: true});
    this.map.onCellsSelected = cells => { $('lkSel').textContent = cells.length ? `${cells.length} cells: ${cells.slice(0, 6).join(', ')}${cells.length > 6 ? '…' : ''}` : 'none selected'; $('lkLoad').disabled = $('lkEvict').disabled = !cells.length; };
    $('lkClear').onclick = () => this.map.clear();
    $('lkLoad').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString()); $('lkLoad').disabled = true;
      try { const d = await api.lakeLoad(cells, {max_granules: +$('lkMaxG').value}); this.watch(d.job_id); } catch (e) { AICESAT.showError(e); $('lkLoad').disabled = false; } };
    $('lkEvict').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString());
      try { const d = await api.lakeEvict(cells); $('lkLimitMsg').textContent = `evicted ${d.evicted.length} cells`; this.map.clear(); this.refresh(); } catch (e) { AICESAT.showError(e); } };
    $('lkSetLimit').onclick = async () => { const gb = +$('lkLimit').value; if (!(gb > 0)) return;
      try { const d = await api.lakeSettings(Math.round(gb * 1e9)); $('lkLimitMsg').textContent = d.evicted && d.evicted.length ? `limit set; evicted ${d.evicted.length} cells (${U.fmtBytes(d.evicted.reduce((a, e) => a + e.bytes, 0))})` : 'limit set'; this.refresh(); } catch (e) { $('lkLimitMsg').textContent = e.message; } };
    // Index | Data toggle + collection selector
    $('lkMode').querySelectorAll('button[data-mode]').forEach(b => b.onclick = () => this.setMode(b.dataset.mode));
    $('lkColl').onchange = e => { this.coll = e.target.value; this.map.clear(); this.refresh(); };
    AICESAT.util.drawer(root, null);
    this.loadCollections();
    this.logSeq = 0;
    this.timer = setInterval(() => this.refresh(true), 6000);   // live: index coverage grows / data updates
    this.logTimer = setInterval(() => this.pollLog(), 2000);
    this.pollLog();
  }
  setMode(m) { this.mode = m; this.$('lkMode').querySelectorAll('button[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m)); this.map.clear(); this.refresh(); }
  collLabel() { const c = this.cols.find(x => x.mission === this.coll); return c ? c.label : this.coll; }
  async pollLog() {
    if (!this.root.classList.contains('on')) return;
    const U = AICESAT.util, body = this.$('lkLogBody');
    let d; try { d = await this.api.lakeLog(this.logSeq); } catch (e) { return; }
    if (d.seq === this.logSeq && body.childElementCount) return;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
    for (const e of d.entries) {
      const t = new Date(e.t * 1000).toLocaleTimeString();
      const div = U.el('div', {class: 'logline ' + (e.level === 'ERROR' ? 'err' : e.level === 'WARNING' ? 'warn' : '')});
      div.textContent = `${t}  ${e.name}  ${e.msg}`;
      body.appendChild(div);
    }
    while (body.childElementCount > 400) body.removeChild(body.firstChild);
    this.logSeq = d.seq;
    if (!body.childElementCount) body.innerHTML = '<div class="small">no activity yet — load or query cells to see the pipeline work</div>';
    if (atBottom) body.scrollTop = body.scrollHeight;
  }
  watch(jid) { const tick = async () => { await this.refreshJobs(); const j = await this.api.job(jid); if (j.status === 'running') setTimeout(tick, 2000); else { if (j.error) AICESAT.showError(j.error); this.refresh(); } }; tick(); }
  async refreshJobs() {
    const jobs = await this.api.jobs().catch(() => []);
    this.$('lkJobs').innerHTML = jobs.length ? jobs.map(j => `<div class="row"><span class="status ${j.status === 'running' ? 'loading' : j.status === 'done' ? 'ready' : 'error'}">${j.status}</span><span class="grow" title="${(j.log || []).join('\n')}">${j.kind} ${j.id}${j.scene_id ? ' → ' + j.scene_id : ''}${j.seconds ? ` (${j.seconds}s)` : ''}<br><span class="small">${(j.log || []).slice(-1)[0] || ''}</span></span></div>`).join('') : 'no jobs';
  }
  async loadCollections() {
    let cols; try { cols = await this.api.collections(); } catch (e) { return; }
    this.cols = cols;
    this.$('lkColl').innerHTML = cols.map(c => `<option value="${c.mission}">${c.label}</option>`).join('');
    if (!cols.find(c => c.mission === this.coll)) this.coll = cols[0] ? cols[0].mission : 'ATL06';
    this.$('lkColl').value = this.coll;
    this.refresh();
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    const U = AICESAT.util, $ = this.$, api = this.api, coll = this.coll, mode = this.mode, seq = ++this._viewSeq;
    this.refreshJobs();                                       // independent of the view; don't block the switch on it
    if (mode === 'index') {
      let d; try { d = await api.indexStatus(coll); } catch (e) { d = {indexed: false, cells: []}; }   // fast: index-only
      if (seq !== this._viewSeq) return;                      // a newer mode/collection selection superseded this one
      this.map.state.cells = null;                            // hide materialized-data cells
      this.map.setIndexCells(d.cells || [], d.pct);
      $('lkHint').textContent = 'Sub-granule H3 index (index-only; data fetched on demand). Colour = distinct cycles per cell (temporal depth). Hover a cell for its status.';
      const building = d.pct != null && d.pct < 100;
      $('lkBarWrap').style.display = building ? '' : 'none';
      if (building) { const bar = $('lkBar'); bar.style.width = d.pct + '%'; bar.className = 'building'; }
      $('lkStats').innerHTML = d.indexed
        ? `<div class="idxstat"><span class="idxswatch"></span><b>${this.collLabel()}</b> index (res ${d.res})</div><table class="stats"><tr><td>granules</td><td class="num">${U.fmtN(d.granules)}${d.target ? ' / ' + U.fmtN(d.target) : ''}</td><td>cells</td><td class="num">${U.fmtN((d.cells || []).length)}</td></tr></table>` + (building ? `<div class="small idxbuild">building index — <b>${d.pct}%</b> of granules done; per-cell counts still rising (roughly uniformly).</div>` : (d.pct === 100 ? '<div class="small">index complete for this area.</div>' : ''))
        : `<div class="small">${this.collLabel()} is not indexed yet — no sub-granule index built for this collection.</div>`;
    } else {
      await this.map.refreshData(api, coll).catch(() => {});  // regions + footprints + this collection's data cells
      if (seq !== this._viewSeq) return;
      this.map.setIndexCells([]);                             // hide index coverage
      this.map.render();
      $('lkHint').textContent = 'Data materialized in the lake (fetched). Blue = present; brighter = fresher.';
      $('lkBarWrap').style.display = '';
      const s2 = await api.lakeSummary(coll).catch(() => null);
      if (seq !== this._viewSeq) return;
      if (s2) {
        $('lkStats').innerHTML = `<table class="stats"><tr><td>cells</td><td class="num">${U.fmtN(s2.cells || 0)}</td><td>files</td><td class="num">${U.fmtN(s2.files || 0)}</td></tr><tr><td>rows</td><td class="num">${U.fmtN(s2.rows || 0)}</td><td>granules</td><td class="num">${U.fmtN(s2.granules || 0)}</td></tr><tr><td>size</td><td class="num">${U.fmtBytes(s2.bytes || 0)}</td><td>limit</td><td class="num">${U.fmtBytes(s2.max_bytes || 0)}</td></tr></table>` + (s2.cells ? '' : `<div class="small">no ${this.collLabel()} cells in the lake yet — build scenes to fill it.</div>`);
        const maxb = s2.max_bytes || 0, u = maxb ? (s2.bytes || 0) / maxb : 0, bar = $('lkBar');
        bar.style.width = Math.min(100, u * 100).toFixed(1) + '%'; bar.className = u > 1 ? 'over' : u > 0.85 ? 'warn' : '';
        if (document.activeElement !== $('lkLimit') && maxb) $('lkLimit').value = (maxb / 1e9).toFixed(1);
        $('lkEvictions').innerHTML = (s2.evictions_recent || []).length ? s2.evictions_recent.map(e => `<div>${U.fmtDate(e.evicted_at)} · cell ${e.cell} · ${U.fmtBytes(e.bytes)} · ${e.reason}</div>`).join('') : 'none';
      } else {
        $('lkStats').innerHTML = '<div class="small">lake summary unavailable</div>';
      }
    }
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
