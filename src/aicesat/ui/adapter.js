/* Data adapter. Two implementations behind the same `AICESAT.api` surface:
   - fetch: the localhost widget server's /api/* routes (dev loop, headless tests, hosts without MCP Apps)
   - app:   inside Claude Desktop as an MCP App — every call is a `tools/call` of an app-visible `ui_*` tool through
            the host bridge; large payloads (positions, surface, imagery) arrive as base64 chunks.
   `AICESAT.ready` resolves once the adapter is chosen (the bridge handshake is tried when running in an iframe). */
window.AICESAT = window.AICESAT || {};
(function () {
  const j = async (url, opts) => { const r = await fetch(url, opts); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || r.statusText); return d; };
  const post = (url, body) => j(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const fetchApi = {
    kind: 'fetch',
    regions: () => j('/api/regions'),
    scenes: () => j('/api/scenes'),
    indexStatus: collection => j('/api/index_status?collection=' + (collection || 'ATL06')),
    sceneDoc: id => j(`/api/scene/${id}`),
    scenePart: (id, part, chunk = 0) => j(`/api/scene/${id}/part?part=${encodeURIComponent(part)}&chunk=${chunk}`),
    imageryUrl: (id, v) => `/api/scene/${id}/imagery.jpg` + (v ? `?v=${v}` : ''),   // v busts the texture cache after a re-fetch
    sceneImagery: (id, source) => post(`/api/scene/${id}/imagery`, {source}),       // re-fetch imagery with a new source
    deleteScene: id => post(`/api/scene/${id}/delete`, {}),                          // remove a scene (registry + doc); never touches the lake/cache
    coverage: a => j('/api/coverage?' + (a.bbox ? `bbox=${encodeURIComponent(JSON.stringify(a.bbox))}` : `polygon=${encodeURIComponent(JSON.stringify(a.polygon))}`)),
    extract: body => post('/api/extract', body),
    job: id => j(`/api/job/${id}`),
    jobs: () => j('/api/jobs'),
    coregister: id => post(`/api/coregister/${id}`, {}),
    candidates: (id, opts) => post(`/api/candidates/${id}`, opts || {}),
    collections: () => j('/api/collections'),
    lakeCells: (stats = true, mission = 'ICESAT2') => j(stats ? '/api/lake/cells?mission=' + encodeURIComponent(mission) : '/api/lake_cells'),
    lakeSummary: (mission = 'ICESAT2') => j('/api/lake/summary?mission=' + encodeURIComponent(mission)),
    lakeLog: (after = 0) => j('/api/lake/log?after=' + (after || 0)),
    lakeSettings: max_bytes => max_bytes == null ? j('/api/lake/settings') : post('/api/lake/settings', {max_bytes}),
    lakeLoad: (cells, opts = {}) => post('/api/lake/load', {cells, ...opts}),
    lakeEvict: cells => post('/api/lake/evict', {cells}),
    bench: () => j('/api/bench').catch(() => null),
    openLink: url => window.open(url, '_blank'),
  };

  // ---- base64 helpers
  const b64ToF32 = b64 => { const bin = atob(b64); const buf = new ArrayBuffer(bin.length); const u8 = new Uint8Array(buf); for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i); return new Float32Array(buf); };
  const concatF32 = parts => { const n = parts.reduce((a, p) => a + p.length, 0); const out = new Float32Array(n); let o = 0; for (const p of parts) { out.set(p, o); o += p.length; } return out; };

  function appApi(app) {
    const imagery = new Map();
    const call = async (name, args = {}) => {
      const r = await app.callServerTool({name, arguments: args});
      if (r.isError) throw new Error((r.content || []).map(c => c.text).join(' ') || name + ' failed');
      if (r.structuredContent) return r.structuredContent;
      const t = (r.content || []).find(c => c.type === 'text'); return t ? JSON.parse(t.text) : {};
    };
    const chunked = async (id, part, stride) => {
      const parts = []; let n = 1;
      for (let c = 0; c < n; c++) { const d = await call('ui_scene_part', {scene_id: id, part, chunk: c, stride}); n = d.n_chunks; parts.push(b64ToF32(d.b64)); }
      return concatF32(parts);
    };
    const chunkedBytes = async (id, part) => {
      let n = 1, b64 = '';
      for (let c = 0; c < n; c++) { const d = await call('ui_scene_part', {scene_id: id, part, chunk: c}); n = d.n_chunks; if (d.data === null) return null; b64 += d.b64; }
      return b64;
    };
    return {
      kind: 'app', app,
      regions: () => call('ui_regions'),
      scenes: async () => (await call('ui_scenes')).scenes,
      indexStatus: collection => call('ui_index_status', {collection: collection || 'ATL06'}),
      sceneDoc: async (id, budget = 150000) => {
        const meta = await call('ui_scene_part', {scene_id: id, part: 'meta'});
        const doc = {...meta, series: {}, coreg: null, surface: null};
        for (const [m, s] of Object.entries(meta.series)) {
          const stride = Math.max(1, Math.ceil(s.n / budget));
          const pos = await chunked(id, 'positions:' + m, stride);
          doc.series[m] = {...s, n: pos.length / 3, stride: s.stride * stride, positions: Array.from(pos)};
        }
        if (meta.surface) { const z = await chunked(id, 'surface', 1); doc.surface = {...meta.surface, z: Array.from(z, v => Number.isFinite(v) ? v : null)}; }
        if (meta.has_coreg) {
          const c = await call('ui_scene_part', {scene_id: id, part: 'coreg'});
          const dh = await call('ui_scene_part', {scene_id: id, part: 'dh'});
          doc.coreg = {...c, ...dh};
        }
        if (meta.imagery) { const b64 = await chunkedBytes(id, 'imagery'); if (b64) imagery.set(id, 'data:image/jpeg;base64,' + b64); }
        return doc;
      },
      scenePart: (id, part, chunk = 0) => call('ui_scene_part', {scene_id: id, part, chunk}),
      imageryUrl: id => imagery.get(id) || null,   // returns the (re-fetched) data URL; the string changes when bytes change
      sceneImagery: async (id, source) => {        // re-fetch imagery server-side, then refresh the cached data URL
        const meta = await call('ui_scene_imagery', {scene_id: id, source});
        const b64 = await chunkedBytes(id, 'imagery');
        if (b64) imagery.set(id, 'data:image/jpeg;base64,' + b64);
        return meta;
      },
      deleteScene: async id => { const r = await call('ui_scene_delete', {scene_id: id}); imagery.delete(id); return r; },
      coverage: a => call('ui_coverage', a),
      extract: body => call('ui_extract', body),
      job: id => call('ui_job', {job_id: id}),
      jobs: async () => (await call('ui_jobs')).jobs,
      coregister: id => call('ui_coregister', {scene_id: id}),
      candidates: (id, opts) => call('ui_candidates', {scene_id: id, ...(opts || {})}),
      collections: () => call('ui_collections'),
      lakeCells: (stats = true, mission = 'ICESAT2') => call('ui_lake_cells', {stats, mission}),
      lakeSummary: (mission = 'ICESAT2') => call('ui_lake_summary', {mission}),
      lakeLog: (after = 0) => call('ui_lake_log', {after}),
      lakeSettings: max_bytes => call('ui_lake_settings', max_bytes == null ? {} : {max_bytes}),
      lakeLoad: (cells, opts = {}) => call('ui_lake_load', {cells, ...opts}),
      lakeEvict: cells => call('ui_lake_evict', {cells}),
      bench: () => call('ui_bench').catch(() => null),
      openLink: url => app.openLink ? app.openLink({url}) : window.open(url, '_blank'),
      fullscreen: (mode = 'fullscreen') => app.requestDisplayMode && app.requestDisplayMode({mode}),
    };
  }

  async function connectApp() {
    const X = window.__extApps;
    if (!X || !X.App || window.parent === window) return null;
    const app = new X.App({name: 'aicesat', version: '1.0'}, {}, {autoResize: true});
    const pending = [];
    app.ontoolresult = r => { AICESAT.lastToolResult = r; (AICESAT.onToolResult || (x => pending.push(x)))(r); };
    app.onhostcontextchanged = ctx => AICESAT.applyHostContext && AICESAT.applyHostContext(ctx);
    const ok = await Promise.race([app.connect().then(() => true).catch(() => false), new Promise(res => setTimeout(() => res(false), 3000))]);
    if (!ok) return null;
    AICESAT.pendingToolResults = pending;
    try { const ctx = app.getHostContext && app.getHostContext(); if (ctx && AICESAT.applyHostContext) AICESAT.applyHostContext(ctx); } catch (e) {}
    return appApi(app);
  }

  AICESAT.ready = connectApp().then(a => { AICESAT.api = a || fetchApi; return AICESAT.api; });
})();
