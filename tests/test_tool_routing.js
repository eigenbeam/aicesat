/* The app must route on a tool result, and the payload is not where it looks like it is.
 *
 * Regression: onResult read only `r.structuredContent`. The server's tools are annotated `-> dict`, which is too
 * loose for the SDK to derive an output schema from, and with no output schema it emits NO structuredContent —
 * verified against mcp 2.1.1: every tool on this server, including open_ui and show_photons, returns
 * structured_content=None and puts the JSON in a text content block. So the handler bailed on every result and the
 * inline app always opened on Explore, whatever tool the user had called.
 *
 * Run: node tests/test_tool_routing.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
global.AICESAT = {};
const el = {hidden: true, innerHTML: '', querySelector: () => ({set onclick(_f) {}})};
global.document = {getElementById: () => el};
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'app.js'), 'utf8');
eval(src.slice(0, src.indexOf('AICESAT.ready.then')));

let n = 0;
const ok = (c, m) => { assert.ok(c, m); n++; };
const eq = (a, b, m) => { assert.deepStrictEqual(a, b, m); n++; };

const asText = obj => ({content: [{type: 'text', text: JSON.stringify(obj)}]});

// --- the shape the server ACTUALLY sends (no output schema -> no structuredContent)
const ts = {view: 'ts', scene_id: 'a21b78e07e', n_candidates_total: 306};
eq(AICESAT.toolPayload(asText(ts)), ts, 'a text content block is the payload');

// --- the shape a schema-carrying tool would send: still honoured, and preferred
eq(AICESAT.toolPayload({structuredContent: ts, content: [{type: 'text', text: '{"view":"stale"}'}]}), ts,
   'structuredContent wins when present');

// --- degenerate inputs must yield null, never throw: onResult returns early on null
for (const bad of [null, undefined, {}, {content: []}, {content: [{type: 'image', data: 'x'}]},
                   {content: [{type: 'text', text: 'not json'}]}, {content: [{type: 'text', text: '"a string"'}]},
                   {content: [{type: 'text', text: '42'}]}, {structuredContent: 'nope', content: []}]) {
  eq(AICESAT.toolPayload(bad), null, 'non-payload must be null: ' + JSON.stringify(bad));
}

// --- the routing decision each payload drives (mirrors onResult's branch order)
const route = r => {
  const sc = AICESAT.toolPayload(r);
  if (!sc) return null;
  if (sc.view === 'ts' && sc.scene_id) return '#ts/' + sc.scene_id + (sc.select ? '?sel=' + sc.select : '');
  if (sc.scene_id) return '#scene/' + sc.scene_id;
  if (sc.view) return '#' + sc.view;
  return null;
};
eq(route(asText(ts)), '#ts/a21b78e07e', 'find_timeseries_candidates opens the time-series view');
eq(route(asText({view: 'ts', scene_id: 'abc', select: '8906f2734b3ffff'})), '#ts/abc?sel=8906f2734b3ffff',
   'show_timeseries opens focused on its cell');
eq(route(asText({scene_id: 'abc', n_photons: 5})), '#scene/abc', 'show_photons still opens the 3-D scene');
eq(route(asText({view: 'lake'})), '#lake', 'open_ui still picks its view');
// A view with no scene falls through to the plain view branch; the router then bounces a deep hash with no
// id back to the list view rather than calling open('').
eq(route(asText({view: 'ts'})), '#ts', 'view without scene_id falls through to the plain view branch');

console.log('ok ' + n + ' tool-routing checks');
