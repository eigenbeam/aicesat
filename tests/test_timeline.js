/* Pure helpers behind the timeline strip and the change map. Run: node tests/test_timeline.js */
const fs = require('fs'), path = require('path'), assert = require('assert');
global.window = global; global.AICESAT = {};
global.h3 = require(path.join(__dirname, '..', 'src', 'aicesat', 'widget', 'vendor', 'h3-js-4.5.0.umd.js'));
eval(fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'timeline.js'), 'utf8'));
const T = AICESAT.timeline;

// low confidence is grey whatever the trend: the colour must never carry a number the data does not support
assert.deepStrictEqual(T.trendColor(-500, 'low', 100), T.trendColor(500, 'low', 100));
const red = T.trendColor(-100, 'medium', 100), blue = T.trendColor(100, 'medium', 100);
assert.ok(red[0] > red[2], 'thinning reads red'); assert.ok(blue[2] > blue[0], 'thickening reads blue');
assert.deepStrictEqual(T.trendColor(-1000, 'high', 100), T.trendColor(-100, 'high', 100), 'clipped at the limit');
// the limit ignores gated cells, so a -26361 cm/yr artefact cannot wash out the map
const cands = [{trend_cm_yr: -26361, level: 'low'}].concat(Array.from({length: 50}, (_, i) => ({trend_cm_yr: -10 * i, level: 'medium'})));
assert.ok(T.trendLimit(cands) <= 490, 'limit ' + T.trendLimit(cands));
assert.strictEqual(T.trendLimit([]), 1);
// placement on the 2003-2027 axis
assert.strictEqual(T.place('GLAS').left, 0);
assert.ok(T.place('ATL06').left + T.place('ATL06').width <= 100);
// hull of one hex is that hex; hull of a ring spans every vertex's longitude range
const c = h3.latLngToCell(69.175, -49.328, 8);
assert.strictEqual(T.hullOfCells([c]).length, 6);
const ring = h3.gridDisk(c, 1), hull = T.hullOfCells(ring);
const lons = ring.flatMap(x => h3.cellToBoundary(x).map(p => p[1]));
assert.ok(Math.min(...hull.map(p => p[0])) <= Math.min(...lons) + 1e-6 && Math.max(...hull.map(p => p[0])) >= Math.max(...lons) - 1e-6);
// declutter keeps the higher-ranked of two overlapping names, drops off-screen ones, keeps separated ones
const rows = [['Small bay', 0, 0, 'BAY', 3], ['Big glacier', 0, 0, 'GLCR', 1], ['Far island', 0, 0, 'ISL', 4], ['Gone', 0, 0, 'PK', 4]];
const at = {'Small bay': [100, 100], 'Big glacier': [104, 102], 'Far island': [400, 300], 'Gone': null};
const kept = AICESAT.declutter(rows, r => at[r[0]], 800, 600).map(r => r[0]);
assert.deepStrictEqual(kept, ['Big glacier', 'Far island']);
// keep-out rectangles (the page title, the timeline strip) are treated as already occupied
const kept2 = AICESAT.declutter(rows, r => at[r[0]], 800, 600, [[380, 280, 420, 320]]).map(r => r[0]);
assert.deepStrictEqual(kept2, ['Big glacier']);
console.log('timeline ok');
