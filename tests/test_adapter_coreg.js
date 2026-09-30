/* The browser adapter's metadata poll must bring a scene's co-registration with it.
 *
 * Regression this pins: when the scene transport went incremental (e3ac471) the fetch adapter's poll kept only
 * `coreg: prev ? prev.coreg : null` — carrying forward a value it never fetched. The first coreg used to arrive in the
 * response to the "Co-register" button; once co-registration became part of the build, nothing loaded it, so the web
 * UI's Δh panel said "no Δh here" on every scene that had one. The MCP App adapter fetched it all along.
 *
 * Run: node tests/test_adapter_coreg.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
const calls = [];
let hasCoreg = true;
global.fetch = async url => {
  calls.push(url);
  const body = url.includes('part=meta') ? {series: {}, has_coreg: hasCoreg}
    : url.includes('part=coreg') ? {n_pairs: {native: 7}}
    : url.includes('part=dh') ? {dh_native: [1.5], dh_coreg: [1.4], artifact: [0.01]} : {};
  return {ok: true, json: async () => body};
};

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
eval(src.replace('AICESAT.ready = connectApp()', 'AICESAT.__test = {fetchApi};\n  AICESAT.ready = connectApp()'));
const {fetchApi} = AICESAT.__test;

(async () => {
  const first = await fetchApi.sceneUpdate(null, 'sc1');
  assert.ok(first.doc.coreg, 'a scene carrying co-registration reaches the viewer with it');
  assert.strictEqual(first.doc.coreg.n_pairs.native, 7);
  assert.deepStrictEqual(first.doc.coreg.dh_native, [1.5]);

  const before = calls.length;
  const next = await fetchApi.sceneUpdate(first.doc, 'sc1');
  assert.strictEqual(calls.length - before, 1, 'later polls carry it forward: one meta request, no refetch');
  assert.strictEqual(next.doc.coreg, first.doc.coreg);

  hasCoreg = false;
  const plainBefore = calls.length;
  const plain = await fetchApi.sceneUpdate(null, 'sc2');
  assert.strictEqual(plain.doc.coreg, null);
  assert.strictEqual(calls.length - plainBefore, 1, 'a scene without co-registration costs one request');
  console.log('ok test_adapter_coreg');
})().catch(e => { console.error(e); process.exit(1); });
