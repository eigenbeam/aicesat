/* Data adapter: the UI talks to `AICESAT.api`; this file provides the fetch (localhost) implementation.
   The MCP App adapter (Phase 4) replaces the same methods with tool calls through the host bridge. */
window.AICESAT = window.AICESAT || {};
(function () {
  const j = async (url, opts) => { const r = await fetch(url, opts); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || r.statusText); return d; };
  const post = (url, body) => j(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  AICESAT.api = {
    kind: 'fetch',
    regions: () => j('/api/regions'),
    scenes: () => j('/api/scenes'),
    sceneDoc: id => j(`/api/scene/${id}`),
    scenePart: (id, part, chunk = 0) => j(`/api/scene/${id}/part?part=${encodeURIComponent(part)}&chunk=${chunk}`),
    imageryUrl: id => `/api/scene/${id}/imagery.jpg`,
    coverage: a => j('/api/coverage?' + (a.bbox ? `bbox=${encodeURIComponent(JSON.stringify(a.bbox))}` : `polygon=${encodeURIComponent(JSON.stringify(a.polygon))}`)),
    extract: body => post('/api/extract', body),
    job: id => j(`/api/job/${id}`),
    jobs: () => j('/api/jobs'),
    coregister: id => post(`/api/coregister/${id}`, {}),
    lakeCells: (stats = true) => j(stats ? '/api/lake/cells' : '/api/lake_cells'),
    lakeSummary: () => j('/api/lake/summary'),
    lakeSettings: max_bytes => max_bytes == null ? j('/api/lake/settings') : post('/api/lake/settings', {max_bytes}),
    lakeLoad: (cells, opts = {}) => post('/api/lake/load', {cells, ...opts}),
    lakeEvict: cells => post('/api/lake/evict', {cells}),
    bench: () => j('/api/bench').catch(() => null),
    openLink: url => window.open(url, '_blank'),
  };
})();
