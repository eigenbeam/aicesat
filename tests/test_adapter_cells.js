/* Both transports fetch a scene's per-cell stats; the app's arrives in host-sized chunks and is put back together.
 *
 * Run: node tests/test_adapter_cells.js   (tests/test_cell_stats.py runs it under pytest)
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
const posts = [];
const ANSWER = {params: {h3_res: 9}, cells: {'8906f20b6a3ffff': {ATL06: {n: 3, first: '2019-06-30', last: '2021-01-15', days: 3}}}};
global.fetch = async (url, opts) => {
  posts.push({url, method: opts && opts.method, body: JSON.parse(opts.body)});
  return {ok: true, status: 200, statusText: 'ok', json: async () => ANSWER};
};

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'adapter.js'), 'utf8');
eval(src.replace('AICESAT.ready = connectApp()', 'AICESAT.__test = {fetchApi, appApi};\n  AICESAT.ready = connectApp()'));
const {fetchApi, appApi} = AICESAT.__test;

(async () => {
  assert.deepStrictEqual(await fetchApi.cells('s1', {h3_res: 9}), ANSWER);
  assert.deepStrictEqual(posts.at(-1), {url: '/api/cells/s1', method: 'POST', body: {h3_res: 9}});

  const text = JSON.stringify(ANSWER), half = Math.ceil(text.length / 2), tools = [];
  const app = {callServerTool: async req => {
    tools.push(req);
    const c = req.arguments.chunk;
    return {structuredContent: {n_chunks: 2, chunk: c, text: text.slice(c * half, (c + 1) * half)}};
  }};
  assert.deepStrictEqual(await appApi(app).cells('s1', {h3_res: 9}), ANSWER, 'the chunks reassemble');
  assert.deepStrictEqual(tools.map(t => t.name), ['ui_cells', 'ui_cells']);
  assert.deepStrictEqual(tools.map(t => t.arguments), [{scene_id: 's1', h3_res: 9, chunk: 0}, {scene_id: 's1', h3_res: 9, chunk: 1}]);
  console.log('ok test_adapter_cells');
})().catch(e => { console.error(e); process.exit(1); });
