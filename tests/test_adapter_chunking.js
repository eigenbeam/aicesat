/* Chunk reassembly for the MCP App transport's DEM surface.
 *
 * Regression this pins: the reader calls getChunk(part, chunkIndex), but a call site once passed a ONE-argument arrow
 * (`p => getChunk(id, p)`), so the index was silently dropped and every request returned chunk 0. The client then
 * concatenated n_chunks copies of the first chunk — which turned the DEM surface grid into a rolling-offset repeat of
 * the same block ("1234 / 2345 / 3456", because the chunk length is not a multiple of the grid width).
 *
 * Scope note: this used to cover the point-cloud pull as well. That transport is deleted — points and the surface both
 * ride the push stream for the browser (tests/test_stream_reader.js). What survives here is the ONE chunked path that
 * remains, the MCP App's surface fetch, because tools/call cannot stream and an MCP host caps a tool result.
 *
 * Run: node tests/test_adapter_chunking.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
global.atob = b64 => Buffer.from(b64, 'base64').toString('binary');
global.fetch = async () => { throw new Error('no network in this test'); };

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
eval(src.replace('AICESAT.ready = connectApp()',
  'AICESAT.__test = {fetchAllValues, b64ToF32, concatF32};\n  AICESAT.ready = connectApp()'));
const {fetchAllValues} = AICESAT.__test;

// A server that chunks a Float32Array exactly as api._chunked does.
function makeServer(values, CHUNK_BYTES = 96000) {
  const buf = Float32Array.from(values);
  const bytes = Buffer.from(buf.buffer);
  const nChunks = Math.max(1, Math.ceil(bytes.length / CHUNK_BYTES));
  const calls = [];
  return {
    calls,
    // `chunk = 0` mirrors the real scenePart(id, part, chunk = 0): a caller that omits the index gets chunk 0 back,
    // which is precisely how the bug stayed invisible.
    getChunk: async (part, chunk = 0) => {
      calls.push(chunk);
      const slice = bytes.subarray(chunk * CHUNK_BYTES, (chunk + 1) * CHUNK_BYTES);
      return {name: 'z', dtype: 'float32', n_values: buf.length, chunk, n_chunks: nChunks,
              b64: slice.toString('base64')};
    },
  };
}

let checks = 0;
const ok = (c, m) => { assert.ok(c, m); checks++; };

(async () => {
  // --- a multi-chunk grid must come back byte-identical, in order
  {
    const n = 60000;                                   // 240 KB -> 3 chunks at 96000 bytes
    const values = Array.from({length: n}, (_, i) => i * 0.5);
    const srv = makeServer(values);
    const got = await fetchAllValues(srv.getChunk, 'surface');
    ok(got.length === n, `got ${got.length} values, want ${n}`);
    ok(srv.calls.join(',') === '0,1,2', `chunk indices requested: ${srv.calls.join(',')}`);
    let bad = -1;
    for (let i = 0; i < n; i++) if (Math.abs(got[i] - values[i]) > 1e-6) { bad = i; break; }
    ok(bad === -1, `value ${bad} differs — the classic symptom of a dropped chunk index`);
  }

  // --- the failure mode itself: a one-argument getChunk must NOT silently yield a repeated first chunk
  {
    const n = 60000;
    const values = Array.from({length: n}, (_, i) => i * 0.5);
    const srv = makeServer(values);
    const oneArg = async part => srv.getChunk(part);   // the bug: index dropped
    const got = await fetchAllValues(oneArg, 'surface');
    // Three requests all answered with chunk 0: 3 x 24000 values, and value 24000 is a repeat of value 0 rather
    // than the real one. Both symptoms, spelled out, so the test above is demonstrably not vacuous.
    ok(got.length === 72000, `dropped index should yield 3 copies of chunk 0, got ${got.length} values`);
    ok(got[24000] === values[0] && got[24000] !== values[24000], 'the rolling-offset repeat that sheared the DEM');
  }

  // --- a single-chunk array still works
  {
    const srv = makeServer([1, 2, 3, 4]);
    const got = await fetchAllValues(srv.getChunk, 'surface');
    ok(got.length === 4 && got[3] === 4, 'single chunk');
    ok(srv.calls.length === 1, `one chunk should mean one request, got ${srv.calls.length}`);
  }

  console.log(`ok — ${checks} checks`);
})().catch(e => { console.error(e); process.exit(1); });
