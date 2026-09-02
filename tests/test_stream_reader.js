/* Browser reader for the PROTOTYPE push transport (server side: src/aicesat/stream.py).
 *
 * The two things that actually break a framed binary reader:
 *   1. A ReadableStream splits at arbitrary byte offsets — mid-header and mid-payload — so the splitter must hold
 *      partial frames. Fed one byte at a time, it must still produce exactly the frames that were written.
 *   2. A payload can start at any offset in the accumulated buffer, and `new Float32Array(buf, unalignedOffset)`
 *      THROWS. The reader copies with slice() to get a fresh, aligned ArrayBuffer; this pins that.
 *
 * Run: node tests/test_stream_reader.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
global.atob = b64 => Buffer.from(b64, 'base64').toString('binary');

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
eval(src.replace('AICESAT.ready = connectApp()',
  'AICESAT.__test = {frameSplitter, growable, fetchApi, K_CONTROL, K_POSITIONS, K_SLOPES, K_SURFACE};\n  AICESAT.ready = connectApp()'));
const {frameSplitter, growable, fetchApi, K_CONTROL, K_POSITIONS, K_SLOPES, K_SURFACE} = AICESAT.__test;

// --- encode frames exactly as stream.py does: <BBHI> header then payload
function frame(kind, mission, payload) {
  const head = Buffer.alloc(8);
  head.writeUInt8(kind, 0); head.writeUInt8(mission, 1); head.writeUInt16LE(0, 2); head.writeUInt32LE(payload.length, 4);
  return Buffer.concat([head, Buffer.from(payload)]);
}
const control = obj => frame(K_CONTROL, 0, Buffer.from(JSON.stringify(obj), 'utf8'));
const f32 = vals => frame(K_POSITIONS, 1, Buffer.from(Float32Array.from(vals).buffer));

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };

// ---------------------------------------------------------------- 1. splitting at every byte offset
{
  const wire = Buffer.concat([
    control({t: 'init'}),
    control({t: 'mission', id: 1, name: 'ATL06'}),
    f32([1, 2, 3, 4, 5, 6]),
    control({t: 'done'}),
  ]);
  for (const step of [1, 3, 7, 8, 9, 13, wire.length]) {
    const got = [];
    const feed = frameSplitter((kind, mission, payload) => got.push({kind, mission, payload}));
    for (let i = 0; i < wire.length; i += step) feed(new Uint8Array(wire.subarray(i, i + step)));
    ok(got.length === 4, `chunk size ${step}: got ${got.length} frames, want 4`);
    ok(got[2].kind === K_POSITIONS && got[2].mission === 1, `chunk size ${step}: bulk frame header`);
    const vals = new Float32Array(got[2].payload.buffer);
    ok(vals.length === 6 && vals[5] === 6, `chunk size ${step}: payload values survived`);
    ok(JSON.parse(Buffer.from(got[3].payload).toString()).t === 'done', `chunk size ${step}: terminal frame`);
  }
}

// ---------------------------------------------------------------- 2. an unaligned payload must not throw
{
  // A 1-byte control payload puts the NEXT frame's payload at an offset that is not a multiple of 4.
  const wire = Buffer.concat([frame(K_CONTROL, 0, Buffer.from('x')), f32([9, 8, 7])]);
  const got = [];
  frameSplitter((k, m, p) => got.push(p))(new Uint8Array(wire));
  const vals = new Float32Array(got[1].buffer);        // throws if the reader handed back an unaligned view
  ok(vals.length === 3 && vals[0] === 9, 'unaligned payload decoded');
}

// ---------------------------------------------------------------- 3. growable buffer, doubling not concatenating
{
  const g = growable(4);
  for (let i = 0; i < 100; i++) g.push(Float32Array.from([i, i, i]));
  ok(g.len === 300 && g.view().length === 300, 'growable length');
  ok(g.view()[299] === 99, 'growable kept order across regrows');
  g.reset();
  ok(g.len === 0 && g.view().length === 0, 'reset empties without reallocating');
}

// ---------------------------------------------------------------- 4. end to end through sceneStreamRun
(async () => {
  const wire = Buffer.concat([
    control({t: 'init', scene_id: 's1'}),
    control({t: 'mission', id: 1, name: 'ATL06', color: [1, 2, 3]}),
    f32([1, 1, 1, 2, 2, 2]),
    control({t: 'reset', mission: 'ATL06', kind: 'positions'}),   // finalize replaced the preview
    f32([5, 5, 5]),
    frame(K_SLOPES, 1, Buffer.from(Float32Array.from([0.1, 0.2]).buffer)),
    control({t: 'done', cursors: {'ATL06:positions': 3}, drained: true}),
  ]);
  // deliver in small pieces, so frames straddle chunk boundaries the way a real socket delivers them
  global.fetch = async () => ({
    ok: true,
    body: {
      getReader() {
        let i = 0;
        return {read: async () => (i >= wire.length ? {done: true}
          : {done: false, value: new Uint8Array(wire.subarray(i, (i += 5)))})};
      },
    },
  });

  const updates = [];
  const handle = fetchApi.sceneStreamRun('s1', (state, stats) => updates.push({...state, stats}), {paintMs: 0});
  const stats = await handle.done;

  ok(stats.resets === 1, `resets counted: ${stats.resets}`);
  ok(stats.bytes === wire.length, `byte count ${stats.bytes} vs ${wire.length}`);
  ok(stats.tDone != null, 'done frame recorded a completion time');
  const last = updates[updates.length - 1].series.ATL06;
  ok(last.positions.length === 3, `after reset the client holds only the replacement (${last.positions.length})`);
  ok(last.positions[0] === 5, 'replacement values, not the discarded preview');
  ok(last.slopes && last.slopes.length === 2, 'slopes arrived on their own kind');

  // ------------------------------------------------------------- 5. the DEM surface rides the same stream
  {
    const grid = {t: 'surface', x0: 0, y0: 0, cell: 100, nx: 3, ny: 2, source: 'ArcticDEM', n_values: 6};
    const wire = Buffer.concat([
      control({t: 'init'}),
      control(grid),
      frame(K_SURFACE, 0, Buffer.from(Float32Array.from([1, 2, NaN, 4, 5, 6]).buffer)),
      control({t: 'done'}),
    ]);
    global.fetch = async () => ({ok: true, body: {getReader() { let i = 0;
      return {read: async () => (i >= wire.length ? {done: true} : {done: false, value: new Uint8Array(wire.subarray(i, (i += 7)))})}; }}});
    const seen = [];
    await fetchApi.sceneStreamRun('s1', st => seen.push(st), {paintMs: 0}).done;
    const surf = seen[seen.length - 1].surface;
    ok(surf && surf.nx === 3 && surf.source === 'ArcticDEM', 'surface grid metadata arrived');
    ok(surf.z.length === 6 && surf.z[0] === 1 && surf.z[2] === null, 'nodata becomes null for the mesh layer');
  }

  // ------------------------------------------------------------- 6. the budget is DECLARED in the request
  {
    let seen = null;
    global.fetch = async (url) => { seen = url; return {ok: true, body: {getReader: () => ({read: async () => ({done: true})})}}; };
    await fetchApi.sceneStreamRun('s1', () => {}, {limit: 400000}).done;
    ok(/[?&]limit=400000\b/.test(seen), `limit not sent to the server: ${seen}`);
    await fetchApi.sceneStreamRun('s1', () => {}, {}).done;
    ok(!/limit=/.test(seen), `uncapped stream must not send a limit: ${seen}`);
  }

  console.log(`ok — ${checks} checks`);
})().catch(e => { console.error(e); process.exit(1); });
