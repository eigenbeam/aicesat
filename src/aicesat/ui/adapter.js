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
    sceneMeta: id => j(`/api/scene/${id}/part?part=meta`),
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
  // ---- PROTOTYPE push transport (server: src/aicesat/stream.py) -------------------------------------------------
  // A/B against the poll path above, which stays exactly as it was. One long-lived response carries every mission's
  // points as raw f32 frames as they land: no polling, no seriesVersion, no chunk arithmetic, no base64, and no
  // prefix — so DISPLAY_BUDGET and PARTIAL_PREVIEW_CAP have nothing to decide here. Frame layout is documented in
  // stream.py; this is the same state machine, over a ReadableStream that splits frames at arbitrary byte offsets.
  const FRAME_HEADER = 8;
  const K_CONTROL = 0, K_POSITIONS = 1, K_SLOPES = 2;

  // Growable f32 buffer. Doubling, because concatenating on every frame is quadratic in the frame count — the exact
  // cost that pushed the pull transport into its incremental design in the first place.
  function growable(initial = 1 << 16) {
    return {buf: new Float32Array(initial), len: 0,
      push(vals) {
        if (this.len + vals.length > this.buf.length) {
          let cap = this.buf.length || 1;
          while (cap < this.len + vals.length) cap *= 2;
          const next = new Float32Array(cap); next.set(this.buf.subarray(0, this.len)); this.buf = next;
        }
        this.buf.set(vals, this.len); this.len += vals.length;
      },
      reset() { this.len = 0; },
      view() { return this.buf.subarray(0, this.len); }};
  }

  // Split a byte stream into frames. Payload is copied via slice() so the Float32Array view is 4-byte aligned —
  // a frame can start at any offset in the accumulated bytes, and an unaligned view throws.
  function frameSplitter(onFrame) {
    let pend = new Uint8Array(0);
    return bytes => {
      const merged = new Uint8Array(pend.length + bytes.length);
      merged.set(pend); merged.set(bytes, pend.length);
      let off = 0;
      const dv = new DataView(merged.buffer, merged.byteOffset, merged.byteLength);
      while (off + FRAME_HEADER <= merged.length) {
        const kind = dv.getUint8(off), mission = dv.getUint8(off + 1), n = dv.getUint32(off + 4, true);
        if (off + FRAME_HEADER + n > merged.length) break;          // header seen, payload still in flight
        onFrame(kind, mission, merged.slice(off + FRAME_HEADER, off + FRAME_HEADER + n));
        off += FRAME_HEADER + n;
      }
      pend = merged.subarray(off);
    };
  }

  // Open the stream for `id` and call onUpdate(series) as points land. `series` is {mission: {positions, slopes, n}},
  // the same shape the renderer already consumes. Returns {done, stop, stats} — stats is what the A/B needs: bytes on
  // the wire, frame count, and RESETS (each one a sidecar the writer replaced under us; see stream.py).
  fetchApi.sceneStreamRun = function (id, onUpdate, opts = {}) {
    const ctl = new AbortController();
    const missions = new Map();                                     // id -> {name, pos, slopes}
    const stats = {bytes: 0, frames: 0, resets: 0, t0: performance.now(), tFirst: null, tDone: null};
    let dirty = false, timer = null;
    const emit = () => {
      timer = null;
      if (!dirty) return;
      dirty = false;
      const series = {};
      for (const m of missions.values()) {
        series[m.name] = {positions: m.pos.view(), slopes: m.slopes.len ? m.slopes.view() : null,
                          n_shown: m.pos.len / 3, n: m.pos.len / 3, color: m.color};
      }
      onUpdate(series, stats);
    };
    const touch = () => { dirty = true; if (timer == null) timer = setTimeout(emit, opts.paintMs || 100); };

    const onFrame = (kind, mid, payload) => {
      stats.frames++;
      if (stats.tFirst == null && kind !== K_CONTROL) stats.tFirst = performance.now() - stats.t0;
      if (kind === K_CONTROL) {
        const msg = JSON.parse(new TextDecoder().decode(payload));
        if (msg.t === 'mission') missions.set(msg.id, {name: msg.name, color: msg.color, pos: growable(), slopes: growable(1 << 10)});
        else if (msg.t === 'reset') {
          stats.resets++;
          for (const m of missions.values()) if (m.name === msg.mission) (msg.kind === 'slopes' ? m.slopes : m.pos).reset();
          touch();
        } else if (msg.t === 'done') { stats.tDone = performance.now() - stats.t0; stats.cursors = msg.cursors; }
        return;
      }
      const m = missions.get(mid);
      if (!m) return;                                               // frame for a mission we never saw announced
      (kind === K_SLOPES ? m.slopes : m.pos).push(new Float32Array(payload.buffer));
      touch();
    };

    const done = (async () => {
      const qs = [];
      if (opts.from) qs.push('from=' + encodeURIComponent(opts.from));
      if (opts.limit) qs.push('limit=' + (opts.limit | 0));      // DECLARED: stopping the read does not stop the server
      const q = qs.length ? '?' + qs.join('&') : '';
      const res = await fetch(`/api/scene/${id}/stream${q}`, {signal: ctl.signal});
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
      const reader = res.body.getReader(), feed = frameSplitter(onFrame);
      for (;;) {
        const {done: fin, value} = await reader.read();
        if (fin) break;
        stats.bytes += value.length;
        feed(value);
      }
      if (timer != null) clearTimeout(timer);
      dirty = true; emit();
      return stats;
    })();

    return {done, stats, stop: () => ctl.abort()};
  };

  // incremental poll: small `meta` + only the new position/slope chunks (see loadSceneInto)
  fetchApi.sceneUpdate = (prev, id, budget) => loadSceneInto(prev, id, fetchApi.sceneMeta,
                                                     (sid, part, chunk) => fetchApi.scenePart(sid, part, chunk), budget);

  // ---- base64 helpers
  const b64ToF32 = b64 => { const bin = atob(b64); const buf = new ArrayBuffer(bin.length); const u8 = new Uint8Array(buf); for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i); return new Float32Array(buf); };
  const concatF32 = parts => { const n = parts.reduce((a, p) => a + p.length, 0); const out = new Float32Array(n); let o = 0; for (const p of parts) { out.set(p, o); o += p.length; } return out; };

  // ---- incremental scene loading -------------------------------------------------------------------------------
  // A build streams: the scene grows granule by granule. Re-fetching the WHOLE doc on every poll re-ships millions of
  // floats each tick (the doc is tens of MB) — quadratic in wall-clock and the dominant cost of a large build. Instead
  // poll the small `meta` part (no positions/slopes/surface-z) and fetch only the bulk arrays that actually changed,
  // appending onto client-side buffers. Shared by both transports; each supplies its own chunk fetcher.
  //
  // Identity/versioning: a mission's preview (meta.partial, cache_key null) is REPLACED wholesale at finalize by the
  // authoritative strided series. `seriesVersion` captures that transition (plus any stride change), so we append while
  // the version holds and refetch once when it flips. Never trust n alone: finalize can shrink n (stride kicks in).
  const seriesVersion = s => `${s.cache_key || 'partial'}|${s.stride || 1}|${!!(s.meta && s.meta.partial)}`;

  // Fetch float32 values [fromValue, ...) of a chunked part. chunk_bytes is fixed server-side (96000 = 24000 floats),
  // so we can start at the chunk containing fromValue and trim the remainder — only NEW data crosses the wire.
  // Floats per chunk, LEARNED from the server: the HTTP route serves ~1 MB chunks while an MCP host needs small
  // ones, so hard-coding it here made the two disagree the moment either changed. Only used to resume a partial
  // fetch; `n_chunks` from the reply still drives the loop.
  let CHUNK_FLOATS = 24000;
  // Points per mission the viewer holds. The server now stores EVERY extracted point, in a shuffled order, so any
  // prefix is a fair spatial sample of the whole — fetching the first N is the level-of-detail control, and the cap
  // lives here (where the memory and the frame budget actually are) rather than in the stored scene.
  const DISPLAY_BUDGET = 400000;
  // `getChunk` MUST accept (part, chunkIndex). Passing a one-argument function here silently drops the index, so every
  // request returns chunk 0 and the caller concatenates n_chunks copies of it — which corrupted the surface grid into
  // a rolling-offset repeat and duplicated the point clouds.
  // `toValue` (opt-in) stops the fetch once that many values are in hand. The stored order is shuffled, so stopping
  // early yields a fair sample rather than one corner of the scene — this is the display cap, and it belongs here.
  async function fetchValuesFrom(getChunk, part, fromValue, toValue) {
    const startChunk = Math.floor(fromValue / CHUNK_FLOATS);
    const parts = []; let n = startChunk + 1;
    for (let c = startChunk; c < n; c++) {
      const d = await getChunk(part, c);
      if (d.chunk != null && d.chunk !== c) throw new Error(`scene_part ${part}: asked for chunk ${c}, got ${d.chunk}`);
      if (d.chunk_values && d.chunk_values !== CHUNK_FLOATS) {   // server chunk size differs from our assumption
        CHUNK_FLOATS = d.chunk_values;
        if (fromValue > 0 && c === startChunk) return fetchValuesFrom(getChunk, part, fromValue);   // redo the maths
      }
      n = d.n_chunks;
      if (c >= n) break;                       // server has fewer chunks than expected (array shrank) -> stop
      parts.push(b64ToF32(d.b64));
      if (toValue != null && (startChunk * CHUNK_FLOATS + parts.reduce((a, v) => a + v.length, 0)) >= toValue) break;
    }
    let got = concatF32(parts);
    const skip = fromValue - startChunk * CHUNK_FLOATS;   // trim the head of the first chunk
    if (skip > 0) got = got.subarray(skip);
    const room = toValue == null ? null : toValue - fromValue;
    return room != null && got.length > room ? got.subarray(0, room) : got;
  }

  // Build/refresh a scene doc incrementally against `prev` (the last doc this view rendered, or null).
  // Returns {doc, changed:Set<mission>} — `changed` lets the caller rebuild only the layers whose data moved.
  async function loadSceneInto(prev, id, getMeta, getChunk, budget = DISPLAY_BUDGET) {
    const meta = await getMeta(id);
    const doc = {...meta, series: {}, coreg: prev ? prev.coreg : null, surface: prev ? prev.surface : null};
    const changed = new Set();
    for (const [m, s] of Object.entries(meta.series || {})) {
      const old = prev && prev.series && prev.series[m];
      const sameVersion = old && old._ver === seriesVersion(s);
      const haveVals = sameVersion ? (old._pos ? old._pos.length : 0) : 0;
      const shown = Math.min(s.n || 0, budget);
      const wantVals = shown * 3;
      let pos = sameVersion ? old._pos : null;
      if (budget <= 0) {
        // The push transport owns the point arrays; skip them entirely rather than issuing a zero-length fetch,
        // which would still cost one round-trip per array per poll tick.
        pos = new Float32Array(0);
      } else if (wantVals > haveVals) {                            // grew (or first sight): fetch ONLY the new tail
        const add = await fetchValuesFrom((p, c) => getChunk(id, p, c), 'positions:' + m, haveVals, wantVals);
        pos = haveVals ? concatF32([pos, add]) : add;
        changed.add(m);
      } else if (!sameVersion) {
        pos = await fetchValuesFrom((p, c) => getChunk(id, p, c), 'positions:' + m, 0, wantVals);
        changed.add(m);
      }
      let slopes = sameVersion ? old._slopes : null;
      if (s.has_slopes) {                                          // budget 0 -> wantS is 0, so this fetches nothing
        const haveS = sameVersion && slopes ? slopes.length : 0, wantS = shown * 2;   // prefix-aligned with positions
        if (wantS > haveS) {
          const add = await fetchValuesFrom((p, c) => getChunk(id, p, c), 'slopes:' + m, haveS, wantS);
          slopes = haveS ? concatF32([slopes, add]) : add;
          changed.add(m);
        }
      } else slopes = null;
      const shownPos = pos || new Float32Array(0);
      doc.series[m] = {...s, positions: shownPos, slopes: slopes || null,
                       n_shown: shownPos.length / 3,          // what is actually on screen, which may be a sample of s.n
                       _pos: shownPos, _slopes: slopes, _ver: seriesVersion(s)};
    }
    // surface z: static once it lands — fetch exactly once
    if (meta.surface && !(prev && prev.surface && prev.surface.z)) {
      const z = await fetchValuesFrom((p, c) => getChunk(id, p, c), 'surface', 0);
      doc.surface = {...meta.surface, z: Array.from(z, v => Number.isFinite(v) ? v : null)};
      changed.add('_surface');
    } else if (meta.surface && prev && prev.surface) {
      doc.surface = {...meta.surface, z: prev.surface.z};
    } else if (!meta.surface) doc.surface = null;
    return {doc, changed, meta};
  }

  function appApi(app) {
    const imagery = new Map();
    const call = async (name, args = {}) => {
      const r = await app.callServerTool({name, arguments: args});
      if (r.isError) throw new Error((r.content || []).map(c => c.text).join(' ') || name + ' failed');
      if (r.structuredContent) return r.structuredContent;
      const t = (r.content || []).find(c => c.type === 'text'); return t ? JSON.parse(t.text) : {};
    };
    // Fetch chunks from the front until `maxValues` is reached. The stored order is shuffled, so a prefix is a fair
    // sample — this replaces asking the server to stride, which made it read the whole array to throw most away.
    const chunked = async (id, part, maxValues) => {
      const parts = []; let n = 1, got = 0;
      for (let c = 0; c < n && (maxValues == null || got < maxValues); c++) {
        const d = await call('ui_scene_part', {scene_id: id, part, chunk: c});
        n = d.n_chunks;
        const v = b64ToF32(d.b64); parts.push(v); got += v.length;
      }
      const all = concatF32(parts);
      return maxValues != null && all.length > maxValues ? all.subarray(0, maxValues) : all;
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
          const pos = await chunked(id, 'positions:' + m, Math.min(s.n || 0, budget) * 3);
          doc.series[m] = {...s, n_shown: pos.length / 3, positions: pos};
        }
        if (meta.surface) { const z = await chunked(id, 'surface', null); doc.surface = {...meta.surface, z: Array.from(z, v => Number.isFinite(v) ? v : null)}; }
        if (meta.has_coreg) {
          const c = await call('ui_scene_part', {scene_id: id, part: 'coreg'});
          const dh = await call('ui_scene_part', {scene_id: id, part: 'dh'});
          doc.coreg = {...c, ...dh};
        }
        if (meta.imagery) { const b64 = await chunkedBytes(id, 'imagery'); if (b64) imagery.set(id, 'data:image/jpeg;base64,' + b64); }
        return doc;
      },
      scenePart: (id, part, chunk = 0) => call('ui_scene_part', {scene_id: id, part, chunk}),
      sceneMeta: id => call('ui_scene_part', {scene_id: id, part: 'meta'}),
      // incremental poll (same contract as the fetch adapter): small meta + only the new chunks, plus the coreg/imagery
      // legs this transport needs (imagery arrives as base64 bytes rather than a URL).
      sceneUpdate: async function (prev, id) {
        const r = await loadSceneInto(prev, id, this.sceneMeta, (sid, part, chunk) => this.scenePart(sid, part, chunk));
        const meta = r.meta;
        if (meta.has_coreg && !(prev && prev.coreg)) {
          const c = await call('ui_scene_part', {scene_id: id, part: 'coreg'});
          const dh = await call('ui_scene_part', {scene_id: id, part: 'dh'});
          r.doc.coreg = {...c, ...dh};
        }
        if (meta.imagery && !imagery.has(id)) {
          const b64 = await chunkedBytes(id, 'imagery');
          if (b64) imagery.set(id, 'data:image/jpeg;base64,' + b64);
        }
        return r;
      },
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
