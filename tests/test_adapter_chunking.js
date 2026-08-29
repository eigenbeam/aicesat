/* Chunk reassembly in the scene adapter.
 *
 * Regression: fetchValuesFrom calls getChunk(part, chunkIndex), but the call sites passed a ONE-argument arrow
 * (`p => getChunk(id, p)`), so the index was silently dropped and every request returned chunk 0. The client then
 * concatenated n_chunks copies of the first chunk — which turned the DEM surface grid into a rolling-offset repeat
 * of the same block ("1234 / 2345 / 3456", because the chunk length is not a multiple of the grid width) and
 * duplicated the point clouds.
 *
 * Run: node tests/test_adapter_chunking.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

// --- minimal DOM/global shims so adapter.js can be evaluated outside a browser
global.window = global;
global.atob = b64 => Buffer.from(b64, 'base64').toString('binary');
global.fetch = async () => { throw new Error('no network in this test'); };

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
// the IIFE keeps its helpers private; evaluate the module body with the internals exported for testing
eval(src.replace('AICESAT.ready = connectApp()', 'AICESAT.__test = {fetchValuesFrom, loadSceneInto, CHUNK_FLOATS};\n  AICESAT.ready = connectApp()'));

const {fetchValuesFrom, loadSceneInto, CHUNK_FLOATS} = AICESAT.__test;

// A server that chunks a Float32Array exactly as api._chunked does (96000 bytes = 24000 floats per chunk).
function makeServer(values) {
  const buf = Float32Array.from(values);
  const bytes = Buffer.from(buf.buffer);
  const CHUNK_BYTES = 96000;
  const nChunks = Math.max(1, Math.ceil(bytes.length / CHUNK_BYTES));
  const calls = [];
  return {
    calls,
    // `chunk = 0` mirrors the real adapter's scenePart(id, part, chunk = 0): a caller that omits the index gets
    // chunk 0 back, which is precisely how the bug stayed invisible.
    getChunk: async (part, chunk = 0) => {
      calls.push(chunk);
      const slice = bytes.subarray(chunk * CHUNK_BYTES, (chunk + 1) * CHUNK_BYTES);
      return {name: 'z', dtype: 'float32', n_values: buf.length, chunk, n_chunks: nChunks,
              b64: slice.toString('base64')};
    },
  };
}

(async () => {
  // --- multi-chunk array must reassemble EXACTLY (this is what the dropped index corrupted)
  const n = CHUNK_FLOATS * 3 + 137;                 // 3 full chunks + a partial
  const values = Array.from({length: n}, (_, i) => i * 0.5);
  const srv = makeServer(values);
  const got = await fetchValuesFrom(srv.getChunk, 'surface', 0);
  assert.strictEqual(got.length, n, `length ${got.length} != ${n}`);
  for (let i = 0; i < n; i++) {
    assert.ok(Math.abs(got[i] - values[i]) < 1e-3, `value ${i}: ${got[i]} != ${values[i]}`);
  }
  assert.deepStrictEqual(srv.calls, [0, 1, 2, 3], `requested chunks ${srv.calls}`);
  console.log('  ok  multi-chunk reassembly is exact and requests every chunk');

  // --- the actual failure mode: a one-argument getChunk must not silently produce repeated chunk 0
  const bad = makeServer(values);
  let threw = false;
  try {
    await fetchValuesFrom(p => bad.getChunk(p), 'surface', 0);   // drops the index, as the old call sites did
  } catch (e) { threw = true; }
  assert.ok(threw, 'a getChunk that ignores the chunk index must be rejected, not silently repeated');
  console.log('  ok  dropped chunk index is detected instead of corrupting the array');

  // --- resuming from an offset fetches only the tail, and lands on the right values
  const srv2 = makeServer(values);
  const from = CHUNK_FLOATS + 10;
  const tail = await fetchValuesFrom(srv2.getChunk, 'positions:ATL06', from);
  assert.strictEqual(tail.length, n - from);
  assert.ok(Math.abs(tail[0] - values[from]) < 1e-3, `tail starts at ${tail[0]}, expected ${values[from]}`);
  assert.strictEqual(srv2.calls[0], 1, 'must start at the chunk containing the offset, not chunk 0');
  console.log('  ok  incremental fetch resumes at the right value');

  console.log('adapter chunking: all checks passed');
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
