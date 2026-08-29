/* Data Lake view (dev/debug surface). Leads with a clear per-collection summary — for each mission: is its sub-granule
   INDEX built (+ coverage) and what DATA is materialized in the lake (cells / rows / bytes) — plus overall lake usage
   vs the disk budget. Click a collection to focus it: the map then shows its Index coverage or its Loaded data, and the
   Cells panel loads/evicts against it. Dev noise (background jobs + the live pipeline log) is demoted to "Advanced". */
window.AICESAT = window.AICESAT || {};
AICESAT.LakeView = class {
  constructor(root, api) {
    const U = AICESAT.util; this.api = api; this.root = root;
    root.innerHTML = `
      <div class="map" id="lkMap"></div>
      <div class="panel" id="lkSummary" data-title="data lake" style="top:12px;left:12px;width:360px">
        <h2>Data lake</h2>
        <div class="lk-usage">
          <div class="lk-usage-row"><span id="lkUse" class="small">loading…</span><span class="grow"></span>
            <span class="small">limit</span> <input id="lkLimit" type="number" min="0.1" step="0.5" style="width:62px"> <span class="small">GB</span> <button id="lkSetLimit">set</button></div>
          <div class="bar" id="lkBarWrap"><div id="lkBar" style="width:0%"></div></div>
          <div class="small" id="lkLimitMsg"></div>
        </div>
        <table class="lk-cols" id="lkCols"></table>
        <div class="lkview">
          <span class="small" style="color:var(--muted)">map:</span>
          <div class="seg" id="lkMode"><button data-mode="index" class="on">Index</button><button data-mode="data">Loaded</button></div>
        </div>
        <div class="small lkhint" id="lkHint"></div>
        <details class="small" style="margin-top:4px"><summary>recent evictions</summary><div id="lkEvictions">none</div></details>
      </div>
      <div class="panel" id="lkSelect" data-title="cells" style="top:12px;right:12px;width:300px">
        <h2>Load / evict cells</h2><div class="small">Click cells (res 6) on the map to select. Colour = data age (bright = fresh). Hover for stats.</div>
        <div id="lkSel" class="small mono" style="margin:6px 0">none selected</div>
        <button id="lkLoad" disabled>Load in background</button><button id="lkEvict" class="danger" disabled>Evict</button><button id="lkClear">Clear</button>
        <label class="small">granules <input id="lkMaxG" type="number" value="40" min="1" max="200" style="width:56px"></label>
      </div>
      <div class="panel" id="lkAdvanced" data-title="advanced" style="bottom:12px;left:12px;width:440px">
        <h2>Advanced</h2>
        <details class="small"><summary>background jobs</summary><div id="lkJobs" class="small" style="margin-top:5px">no jobs</div></details>
        <details class="small" style="margin-top:6px"><summary>live pipeline log</summary>
          <div class="small" style="margin:4px 0">CMR search, chunk fetch/decode, materialize, evict, query.</div>
          <div id="lkLogBody" class="lakelog"></div></details>
      </div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    this.mode = 'index'; this.coll = 'ATL06'; this.cols = []; this._viewSeq = 0; this._idxByKey = {};
    this.map = new AICESAT.MapView($('lkMap'), {grid: true, selectCells: true, draw: false, footprints: true});
    this.map.onCellsSelected = cells => { $('lkSel').textContent = cells.length ? `${cells.length} cells: ${cells.slice(0, 6).join(', ')}${cells.length > 6 ? '…' : ''}` : 'none selected'; $('lkLoad').disabled = $('lkEvict').disabled = !cells.length; };
    $('lkClear').onclick = () => this.map.clear();
    $('lkLoad').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString()); $('lkLoad').disabled = true;
      try { const d = await api.lakeLoad(cells, {max_granules: +$('lkMaxG').value}); this.watch(d.job_id); } catch (e) { AICESAT.showError(e); $('lkLoad').disabled = false; } };
    $('lkEvict').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString());
      try { const d = await api.lakeEvict(cells); $('lkLimitMsg').textContent = `evicted ${d.evicted.length} cells`; this.map.clear(); this.refresh(); } catch (e) { AICESAT.showError(e); } };
    $('lkSetLimit').onclick = async () => { const gb = +$('lkLimit').value; if (!(gb > 0)) return;
      try { const d = await api.lakeSettings(Math.round(gb * 1e9)); $('lkLimitMsg').textContent = d.evicted && d.evicted.length ? `limit set; evicted ${d.evicted.length} cells (${U.fmtBytes(d.evicted.reduce((a, e) => a + e.bytes, 0))})` : 'limit set'; this.refresh(); } catch (e) { $('lkLimitMsg').textContent = e.message; } };
    $('lkMode').querySelectorAll('button[data-mode]').forEach(b => b.onclick = () => this.setMode(b.dataset.mode));
    AICESAT.util.drawer(root, null);
    this.loadCollections();
    this.logSeq = 0;
    this.startPolling();
    this.pollLog();
  }
  // Poll ONLY while this view is on screen. These timers used to be started in the constructor and never cleared, so
  // one visit to the Data Lake left index_status (4 collections, each reading every index parquet) firing every 8 s
  // for the rest of the session — including while a scene was building, where it competed with the build thread for
  // the GIL and made builds crawl. stopPolling() on hide is the fix; show() restarts it.
  startPolling() {
    this.stopPolling();
    this.timer = setInterval(() => this.refresh(true), 8000);      // live: index coverage grows / data updates
    this.logTimer = setInterval(() => this.pollLog(), 2000);
  }
  stopPolling() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    if (this.logTimer) { clearInterval(this.logTimer); this.logTimer = null; }
  }
  missionOf(key) { const c = this.cols.find(x => x.key === key); return c ? c.mission : key; }
  collLabel(key = this.coll) { const c = this.cols.find(x => x.key === key); return c ? c.label : key; }
  setMode(m) { this.mode = m; this.$('lkMode').querySelectorAll('button[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m)); this.map.clear(); this.refresh(); }
  focus(key) { if (this.coll === key) return; this.coll = key; this.map.clear(); this.refresh(); }

  async loadCollections() {
    let cols; try { cols = await this.api.collections(); } catch (e) { return; }
    this.cols = cols;
    if (!cols.find(c => c.key === this.coll)) this.coll = cols[0] ? cols[0].key : 'ATL06';
    this.refresh();
  }

  // one focused-mission summary (also carries the cheap all-mission cells/bytes array + budget) + per-collection index
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    if (!this.cols.length) return;
    const U = AICESAT.util, $ = this.$, seq = ++this._viewSeq;
    this.refreshJobs();
    let s, idxAll;
    try {
      [s, idxAll] = await Promise.all([
        this.api.lakeSummary(this.missionOf(this.coll)).catch(() => null),
        Promise.all(this.cols.map(c => this.api.indexStatus(c.key).catch(() => ({indexed: false, cells: []})))),
      ]);
    } catch (e) { return; }
    if (seq !== this._viewSeq) return;
    this._idxByKey = {}; this.cols.forEach((c, i) => { this._idxByKey[c.key] = idxAll[i]; });

    // overall lake usage vs the disk budget (sum every materialized collection; missions[] is footer-free)
    const missions = (s && s.missions) || [], byMission = {};
    missions.forEach(m => { byMission[m.mission] = m; });
    const totalBytes = missions.reduce((a, m) => a + (m.bytes || 0), 0);
    const maxb = (s && s.max_bytes) || 0, u = maxb ? totalBytes / maxb : 0;
    $('lkUse').innerHTML = `<b>${U.fmtBytes(totalBytes)}</b> of ${U.fmtBytes(maxb)}`;
    const bar = $('lkBar'); bar.style.width = Math.min(100, u * 100).toFixed(1) + '%'; bar.className = u > 1 ? 'over' : u > 0.85 ? 'warn' : '';
    if (document.activeElement !== $('lkLimit') && maxb) $('lkLimit').value = (maxb / 1e9).toFixed(1);
    $('lkEvictions').innerHTML = (s && (s.evictions_recent || []).length) ? s.evictions_recent.map(e => `<div>${U.fmtDate(e.evicted_at)} · cell ${e.cell} · ${U.fmtBytes(e.bytes)} · ${e.reason}</div>`).join('') : 'none';

    this.renderCols(byMission, s);
    await this.renderMap(seq);
  }

  // per-collection summary: index (built? coverage) + loaded (cells/bytes for all; +rows/granules for the focused one)
  renderCols(byMission, s) {
    const U = AICESAT.util;
    const rows = this.cols.map(c => {
      const idx = this._idxByKey[c.key] || {}, loaded = byMission[c.mission] || {};
      const idxHtml = idx.indexed
        ? `<span class="idxswatch"></span>${U.fmtN((idx.cells || []).length)} cells` + (idx.pct != null && idx.pct < 100 ? ` · <b>${idx.pct}%</b>` : (idx.pct === 100 ? ' · full' : ''))
        : '<span class="no">not built</span>';
      const cells = loaded.cells || 0, bytes = loaded.bytes || 0;
      let loadedHtml = cells ? `${U.fmtN(cells)} cells · ${U.fmtBytes(bytes)}` : '<span class="no">none</span>';
      if (this.coll === c.key && s && s.cells) loadedHtml += `<br><span class="small">${U.fmtN(s.rows)} rows · ${U.fmtN(s.granules)} gran.</span>`;
      return `<tr class="lk-col-row ${this.coll === c.key ? 'on' : ''}" data-key="${c.key}"><td class="lk-col-name"><b>${c.label}</b><br><span class="small">${c.epoch}</span></td><td>${idxHtml}</td><td>${loadedHtml}</td></tr>`;
    }).join('');
    this.$('lkCols').innerHTML = `<tr class="lk-col-hd"><td>collection</td><td>index</td><td>loaded</td></tr>${rows}`;
    this.$('lkCols').querySelectorAll('.lk-col-row').forEach(r => r.onclick = () => this.focus(r.dataset.key));
  }

  // colour the shared grid for the FOCUSED collection: Index coverage (temporal depth) or Loaded data (age)
  async renderMap(seq) {
    const $ = this.$;
    if (this.mode === 'index') {
      const idx = this._idxByKey[this.coll] || {indexed: false, cells: []};
      this.map.state.cells = null;
      this.map.setIndexCells(idx.cells || [], idx.pct);
      $('lkHint').textContent = idx.indexed
        ? `Map: ${this.collLabel()} index coverage (res ${idx.res}). Colour = distinct cycles per cell (temporal depth).`
        : `${this.collLabel()} has no sub-granule index yet.`;
    } else {
      this.map.setIndexCells([]);
      await this.map.refreshData(this.api, this.missionOf(this.coll)).catch(() => {});
      if (seq !== this._viewSeq) return;
      this.map.render();
      $('lkHint').textContent = `Map: ${this.collLabel()} data materialized in the lake (blue = present; brighter = fresher).`;
    }
  }

  watch(jid) { const tick = async () => { await this.refreshJobs(); const j = await this.api.job(jid); if (j.status === 'running') setTimeout(tick, 2000); else { if (j.error) AICESAT.showError(j.error); this.refresh(); } }; tick(); }
  async refreshJobs() {
    const jobs = await this.api.jobs().catch(() => []);
    this.$('lkJobs').innerHTML = jobs.length ? jobs.map(j => `<div class="row"><span class="status ${j.status === 'running' ? 'loading' : j.status === 'done' ? 'ready' : 'error'}">${j.status}</span><span class="grow" title="${(j.log || []).join('\n')}">${j.kind} ${j.id}${j.scene_id ? ' → ' + j.scene_id : ''}${j.seconds ? ` (${j.seconds}s)` : ''}<br><span class="small">${(j.log || []).slice(-1)[0] || ''}</span></span></div>`).join('') : 'no jobs';
  }
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
  show() { this.root.classList.add('on'); this.startPolling(); this.refresh(); }
  hide() { this.root.classList.remove('on'); this.stopPolling(); }
};
