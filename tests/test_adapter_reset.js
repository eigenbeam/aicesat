/* Both transports carry the change map's "Reset this hex" to the server, and a refusal reaches the page as an error.
 *
 * Run: node tests/test_adapter_reset.js   (tests/test_reset_region.py runs it under pytest)
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
const HX = '8506f257fffffff';
const posts = [];
let status = 200;
global.fetch = async (url, opts) => {
  posts.push({url, method: opts && opts.method});
  const body = status === 200 ? {hex: HX, scenes_deleted: ['reg0000001']} : {error: 'a build is running (job j1)'};
  return {ok: status === 200, status, statusText: 'x', json: async () => body};
};

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
eval(src.replace('AICESAT.ready = connectApp()', 'AICESAT.__test = {fetchApi, appApi};\n  AICESAT.ready = connectApp()'));
const {fetchApi, appApi} = AICESAT.__test;

(async () => {
  const r = await fetchApi.resetRegion(HX);
  assert.deepStrictEqual(posts.at(-1), {url: `/api/region/${HX}/reset`, method: 'POST'});
  assert.deepStrictEqual(r.scenes_deleted, ['reg0000001'], 'the report comes back to the page');

  status = 409;
  await assert.rejects(fetchApi.resetRegion(HX), /a build is running/, 'a refused reset surfaces the server\'s reason');

  const tools = [];
  const app = {callServerTool: async req => (tools.push(req), {structuredContent: {hex: HX, scenes_deleted: []}})};
  const out = await appApi(app).resetRegion(HX);
  assert.deepStrictEqual(tools, [{name: 'ui_region_reset', arguments: {hex: HX}}]);
  assert.strictEqual(out.hex, HX);
  console.log('ok test_adapter_reset');
})().catch(e => { console.error(e); process.exit(1); });
