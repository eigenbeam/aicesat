/* Lake view: grid on, per-cell stats, live summary, storage limit, background loading and eviction of selected cells. */
window.AICESAT = window.AICESAT || {};
AICESAT.LakeView = class {
  constructor(root, api) {
    const U = AICESAT.util; this.api = api; this.root = root;
    root.innerHTML = `
      <div class="map" id="lkMap"></div>
      <div class="panel" id="lkSummary" data-title="lake summary" style="top:12px;left:12px;width:320px">
        <h2>Lake</h2>
        <div class="small" style="margin-bottom:6px"><b>Collections</b> <span id="lkCollBoxes">loading…</span></div>
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
      <div class="panel" id="lkLog" data-title="lake log"><div class="small" style="margin-bottom:4px">Live pipeline activity — CMR search, chunk fetch/decode, materialize, evict, query.</div><div id="lkLogBody" class="lakelog"></div></div>
      <div id="attrib">Basemap: Natural Earth (public domain). Scene imagery: Sentinel-2 cloudless / EOX (CC BY-NC-SA 4.0)</div>`;
    const $ = id => root.querySelector('#' + id); this.$ = $;
    this.missions = new Set(['GLAS', 'ICESSN', 'ATL06']);   // default shown (ATL03/ICESAT2 off); loadCollections() re-syncs from /api/collections
    this.map = new AICESAT.MapView($('lkMap'), {grid: true, selectCells: true, draw: false, footprints: true});
    this.map.onCellsSelected = cells => { $('lkSel').textContent = cells.length ? `${cells.length} cells: ${cells.slice(0, 6).join(', ')}${cells.length > 6 ? '…' : ''}` : 'none selected'; $('lkLoad').disabled = $('lkEvict').disabled = !cells.length; };
    $('lkClear').onclick = () => this.map.clear();
    $('lkLoad').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString()); $('lkLoad').disabled = true;
      try { const d = await api.lakeLoad(cells, {max_granules: +$('lkMaxG').value}); this.watch(d.job_id); } catch (e) { AICESAT.showError(e); $('lkLoad').disabled = false; } };
    $('lkEvict').onclick = async () => { const cells = [...this.map.state.selected].map(c => BigInt('0x' + c).toString());
      try { const d = await api.lakeEvict(cells); $('lkLimitMsg').textContent = `evicted ${d.evicted.length} cells`; this.map.clear(); this.refresh(); } catch (e) { AICESAT.showError(e); } };
    $('lkSetLimit').onclick = async () => { const gb = +$('lkLimit').value; if (!(gb > 0)) return;
      try { const d = await api.lakeSettings(Math.round(gb * 1e9)); $('lkLimitMsg').textContent = d.evicted && d.evicted.length ? `limit set; evicted ${d.evicted.length} cells (${U.fmtBytes(d.evicted.reduce((a, e) => a + e.bytes, 0))})` : 'limit set'; this.refresh(); } catch (e) { $('lkLimitMsg').textContent = e.message; } };
    AICESAT.util.drawer(root, null);
    this.loadCollections();
    this.logSeq = 0;
    this.timer = setInterval(() => this.refresh(true), 10000);
    this.logTimer = setInterval(() => this.pollLog(), 2000);   // running log updates faster than the summary
    this.pollLog();
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
  watch(jid) { const tick = async () => { await this.refreshJobs(); const j = await this.api.job(jid); if (j.status === 'running') setTimeout(tick, 2000); else { if (j.error) AICESAT.showError(j.error); this.refresh(); } }; tick(); }
  async refreshJobs() {
    const jobs = await this.api.jobs().catch(() => []);
    this.$('lkJobs').innerHTML = jobs.length ? jobs.map(j => `<div class="row"><span class="status ${j.status === 'running' ? 'loading' : j.status === 'done' ? 'ready' : 'error'}">${j.status}</span><span class="grow" title="${(j.log || []).join('\n')}">${j.kind} ${j.id}${j.scene_id ? ' → ' + j.scene_id : ''}${j.seconds ? ` (${j.seconds}s)` : ''}<br><span class="small">${(j.log || []).slice(-1)[0] || ''}</span></span></div>`).join('') : 'no jobs';
  }
  async refresh(quiet = false) {
    if (!this.root.classList.contains('on') && quiet) return;
    const U = AICESAT.util, $ = this.$, api = this.api;
    const missions = [...this.missions];
    // per-checked-collection summary + cells, in parallel; then merge for a single lake view
    const results = await Promise.all(missions.map(m => Promise.all([api.lakeSummary(m).catch(() => null), api.lakeCells(true, m).catch(() => null)])));
    await this.map.refreshData(api, missions[0] || 'ICESAT2');   // regions + scene footprints (its cells are replaced next)
    const feats = [];
    results.forEach(([, cl]) => { if (cl && cl.features) feats.push(...cl.features); });
    this.map.state.cells = {type: 'FeatureCollection', features: feats};   // union across collections (new ref -> grid memo rebuilds)
    this.map.render();
    await this.refreshJobs();
    const sums = results.map(r => r[0]).filter(Boolean);
    const agg = sums.reduce((a, s2) => ({cells: a.cells + (s2.cells || 0), files: a.files + (s2.files || 0), rows: a.rows + (s2.rows || 0),
      granules: a.granules + (s2.granules || 0), bytes: a.bytes + (s2.bytes || 0)}), {cells: 0, files: 0, rows: 0, granules: 0, bytes: 0});
    const maxb = sums.length ? sums[0].max_bytes : 0;
    const oldest = sums.map(s2 => s2.oldest_ingested).filter(Boolean).sort()[0];
    const newest = sums.map(s2 => s2.newest_ingested).filter(Boolean).sort().slice(-1)[0];
    const per = missions.map((_m, i) => { const s2 = results[i][0]; return s2 && s2.cells ? `${s2.product}: ${U.fmtN(s2.cells)}` : null; }).filter(Boolean).join(' · ');
    $('lkStats').innerHTML = `<table class="stats"><tr><td>cells</td><td class="num">${U.fmtN(agg.cells)}</td><td>files</td><td class="num">${U.fmtN(agg.files)}</td></tr><tr><td>rows</td><td class="num">${U.fmtN(agg.rows)}</td><td>granules</td><td class="num">${U.fmtN(agg.granules)}</td></tr><tr><td>size</td><td class="num">${U.fmtBytes(agg.bytes)}</td><td>limit</td><td class="num">${U.fmtBytes(maxb)}</td></tr><tr><td>oldest</td><td colspan="3" class="small">${U.fmtDate(oldest)} · newest ${U.fmtDate(newest)}</td></tr></table>` +
      (per ? `<div class="small" style="margin-top:3px">${per}</div>` : (missions.length ? '<div class="small">no cells for the selected collections yet — build scenes to fill the lake</div>' : '<div class="small">select a collection above</div>'));
    const u = maxb ? agg.bytes / maxb : 0; const bar = $('lkBar'); bar.style.width = Math.min(100, u * 100).toFixed(1) + '%'; bar.className = u > 1 ? 'over' : u > 0.85 ? 'warn' : '';
    if (document.activeElement !== $('lkLimit') && maxb) $('lkLimit').value = (maxb / 1e9).toFixed(1);
    const ev = sums.flatMap(s2 => s2.evictions_recent || []);
    $('lkEvictions').innerHTML = ev.length ? ev.map(e => `<div>${U.fmtDate(e.evicted_at)} · cell ${e.cell} · ${U.fmtBytes(e.bytes)} · ${e.reason}</div>`).join('') : 'none';
  }
  async loadCollections() {
    let cols; try { cols = await this.api.collections(); } catch (e) { return; }
    this.$('lkCollBoxes').innerHTML = cols.map(c =>
      `<label title="${c.product} · ${c.epoch}" style="margin-right:8px;white-space:nowrap"><input type="checkbox" data-mission="${c.mission}" ${c.default ? 'checked' : ''}> ${c.label}</label>`).join('');
    this.missions = new Set(cols.filter(c => c.default).map(c => c.mission));   // ATL03 (default false) off
    this.$('lkCollBoxes').querySelectorAll('input[data-mission]').forEach(i => i.onchange = () => {
      if (i.checked) this.missions.add(i.dataset.mission); else this.missions.delete(i.dataset.mission);
      this.map.clear(); this.refresh();
    });
    this.refresh();
  }
  show() { this.root.classList.add('on'); this.refresh(); }
  hide() { this.root.classList.remove('on'); }
};
