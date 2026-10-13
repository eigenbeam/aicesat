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
// placement on the 2000-2030 axis: every span starts after the axis does and ends before it does
assert.ok(T.T0 % 5 === 0 && T.T1 % 5 === 0, 'axis on 5-year boundaries');
assert.ok(T.place('GLAS').left > 0);
assert.ok(T.place('ATL06').left + T.place('ATL06').width < 100);
// the three missions' names share one row; ICESat-2's two products share a span, so the second drops a row
assert.deepStrictEqual(T.nameRows(['GLAS', 'ICESSN', 'ATL06'], 600), [0, 0, 0]);
assert.deepStrictEqual(T.nameRows(['GLAS', 'ICESSN', 'ATL06', 'ICESAT2'], 600), [0, 0, 0, 1]);
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

// Basemap switch at the change and study levels: the hillshaded DEM without imagery, or the image draped on the DEM's
// heights, unlit and without the DEM wireframe. A scene with no imagery gets the DEM, never a blank ground.
assert.deepStrictEqual(T.basemap('dem', true), {imagery: false, surface: true});
assert.deepStrictEqual(T.basemap('imagery', true), {imagery: true, surface: false});
assert.deepStrictEqual(T.basemap('imagery', false), {imagery: false, surface: true});

// Measurements on top at the change and study levels. The ice has thinned since the DEM was made: on scene
// 69606ee845 ATL06 sits a median 4.7 m BELOW the DEM and 93% of it below the hex fills draped 8 m above it, so depth
// testing hid the data behind the terrain and fills - more the closer the camera, as the depth buffer resolved the
// gap. They draw after the fills, ignore depth, and leave clicks to the hexes (a point on top would steal the pick).
const onTop = T.cloudProps('region');
assert.deepStrictEqual(onTop.parameters, {depthTest: false});
assert.strictEqual(onTop.pickable, false);
assert.deepStrictEqual(T.cloudProps('study'), onTop);
const classic = T.cloudProps(null);
assert.strictEqual(classic.parameters, undefined, 'the classic 3-D view keeps real occlusion');
assert.strictEqual(classic.pickable, true);
const st = T.stack('region'), cl = T.stack(null);
assert.ok(st.indexOf('clouds') > st.indexOf('candidates') && st.indexOf('clouds') > st.indexOf('surface'));
assert.ok(cl.indexOf('clouds') < cl.indexOf('candidates'), 'classic view keeps its picking order');
assert.deepStrictEqual([...st].sort(), [...cl].sort(), 'same layers, different order');
// The trend colour shows only while the Analysis panel is open. Shut, every cell looks the same -- thinning,
// thickening, low confidence alike -- and is still drawn: a visible outline and a fill it can be picked by.
const thin = {trend_cm_yr: -300, level: 'high'}, thick = {trend_cm_yr: 300, level: 'medium'}, low = {trend_cm_yr: -300, level: 'low'};
for (const c of [thin, thick, low]) assert.deepStrictEqual(T.cellPaint(c, true, 370).fill, T.trendColor(c.trend_cm_yr, c.level, 370));
const plain = T.cellPaint(thin, false, 370);
for (const c of [thick, low]) assert.deepStrictEqual(T.cellPaint(c, false, 370), plain, 'shut: one look for every cell');
assert.ok(plain.fill[3] > 0 && plain.line[3] > 0 && plain.width > 0, 'shut: the cell persists');
assert.notDeepStrictEqual(plain.fill, T.cellPaint(thin, true, 370).fill, 'open: the trend colour, not the plain fill');
// A cell the analysis did not pick looks plain open or shut; an empty cell is fainter still, but there to hover.
assert.deepStrictEqual(T.cellPaint(null, true, 370), plain, 'not a candidate: plain even with Analysis open');
const empty = T.cellPaint(null, false, 370, true);
assert.ok(empty.line[3] < plain.line[3] && empty.fill[3] <= plain.fill[3] && empty.fill[3] > 0, 'empty: fainter, still pickable');
assert.deepStrictEqual(T.cellPaint(null, true, 370, true), empty);

// Which cells the change map draws: the whole area tiled, empty cells included, down to res 9; finer, only the cells
// holding data. Edge cells with data (centre outside the area) are drawn too.
const AHX = '8506f257fffffff', aring = h3.cellToBoundary(AHX, true);   // [lon, lat], as a scene's polygon is
const kids8 = new Set(h3.cellToChildren(AHX, 8));
const outside = h3.gridRing(h3.cellToCenterChild(AHX, 8), 13).find(c => !kids8.has(c) && h3.cellToParent(c, 5) !== AHX);
const t8 = new Set(T.areaCells(aring, 8, [outside]));
assert.ok(t8.size > 300 && t8.size < 400, 'res 8 tiles the res-5 hex: ' + t8.size);
assert.ok(outside && t8.has(outside), 'an edge cell holding data is drawn');
assert.ok([...t8].every(c => h3.getResolution(c) === 8));
assert.strictEqual(T.TILE_MAX_RES, 9);
assert.ok(T.areaCells(aring, 9, []).length > 2000, 'res 9 still tiles');
const data10 = h3.cellToChildren(h3.cellToCenterChild(AHX, 9), 10).slice(0, 3);
assert.deepStrictEqual(T.areaCells(aring, 10, data10).sort(), [...data10].sort(), 'res 10: only the cells holding data');
assert.deepStrictEqual(T.areaCells(null, 8, ['a']), ['a'], 'no area: the data cells');

// What a cell holds, as the Cell panel says it: a table with one column per mission of the SCENE (flight order),
// whether or not this cell has any, so the layout holds still as the panel follows the mouse; "—" where it has none.
const ST = {ATL06: {n: 1234, first: '2019-03-02', last: '2026-01-15', days: 31, h_med: 242.04, h_min: 238.1, h_max: 245.9},
            GLAS: {n: 1, first: '2003-10-08', last: '2003-10-08', days: 1, h_med: 250.0, h_min: 250.0, h_max: 250.0}};
const SCENE_M = ['ATL06', 'ICESSN', 'GLAS'];
assert.deepStrictEqual(T.cellRows(ST).map(r => r.m), ['GLAS', 'ATL06'], 'flight order');
const card = T.cellCard('8806f257e1fffff', 8, 531, ST, SCENE_M, 'pinned');
const heads = [...card.matchAll(/<th class="m"[^>]*>(.*?)<\/th>/g)].map(m => m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
assert.deepStrictEqual(heads, ['ICESat', 'IceBridge', 'ICESat-2 land ice'], 'every scene mission, in flight order: ' + heads);
const trs = [...card.matchAll(/<tr[^>]*><td class="k">(.*?)<\/td>(.*?)<\/tr>/g)].map(m => [m[1], [...m[2].matchAll(/<td>(.*?)<\/td>/g)].map(c => c[1])]);
assert.deepStrictEqual(trs.map(r => r[0]), ['Count', 'First', 'Last', 'Days', 'Median h', 'Min h', 'Max h']);
const col = k => Object.fromEntries(trs.map(r => [r[0], r[1][k]]));
assert.deepStrictEqual(col(0), {Count: '1', First: '2003-10-08', Last: '2003-10-08', Days: '1', 'Median h': '250.0',
                                'Min h': '250.0', 'Max h': '250.0'});
assert.ok(trs.every(r => r[1][1] === '—'), 'IceBridge has nothing here: a dash in every row');
assert.deepStrictEqual(col(2), {Count: '1,234', First: '2019-03-02', Last: '2026-01-15', Days: '31', 'Median h': '242.0',
                                'Min h': '238.1', 'Max h': '245.9'});
assert.ok(card.includes('H3 8806f257e1fffff · res 8 · ~531 m'), 'the cell, its size');
assert.ok(card.includes('Pinned') && !card.includes('Under the cursor'));
assert.ok(T.cellCard('x', 8, 531, ST, SCENE_M, 'hover').includes('Under the cursor'));
assert.ok(card.includes('WGS84 ellipsoid'));
const none = T.cellCard('x', 8, 531, undefined, SCENE_M, 'hover');
assert.strictEqual((none.match(/<td>—<\/td>/g) || []).length, 21, 'an empty cell keeps the table: all dashes');
assert.ok(none.includes('No measurements in this cell.'));
assert.ok(!card.includes('cell-tbl dense') && T.cellCard('x', 8, 531, ST, ['GLAS', 'ICESSN', 'ATL06', 'ICESAT2'], 'hover').includes('cell-tbl dense'),
          'four missions: the denser table');
// Mission colours must not borrow the change map's meaning: ATL06 used to be the ramp's blue ("rose") and IceBridge its
// red ("fell"), so a track read as change the data did not show. Each demo mission sits >= 35 deg of hue from both ramp
// ends and from each other.
eval(fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'tspanel.js'), 'utf8'));
const hue = ([r, g, b]) => { const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn || 1;
  const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; return (h * 60 + 360) % 360; };
const apart = (a, b) => { const d = Math.abs(hue(a) - hue(b)) % 360; return Math.min(d, 360 - d); };
const PAL = AICESAT.missions.MISSION_COLORS, DEMO = ['GLAS', 'ICESSN', 'ATL06', 'ICESAT2'];
const fell = T.trendColor(-100, 'high', 100), rose = T.trendColor(100, 'high', 100);
for (const m of DEMO) for (const [end, c] of [['fell', fell], ['rose', rose]])
  assert.ok(apart(PAL[m], c) >= 35, `${m} is ${apart(PAL[m], c).toFixed(0)} deg from the ramp's "${end}" colour`);
for (const a of DEMO) for (const b of DEMO) if (a < b)
  assert.ok(apart(PAL[a], PAL[b]) >= 35, `${a} and ${b} are ${apart(PAL[a], PAL[b]).toFixed(0)} deg apart`);
// the change map's hex: the globe passes ?hex=, the model's elevation_change opens it with only the scene's question
assert.strictEqual(T.regionHex('H3 8506f257fffffff', '8506f2cffffffff'), '8506f2cffffffff', 'the URL wins');
assert.strictEqual(T.regionHex('H3 8506f257fffffff', null), '8506f257fffffff', 'else the question names it');
assert.strictEqual(T.regionHex('study: 2 cells of H3 8506f257fffffff', null), null, 'a study is not a change map');
assert.strictEqual(T.regionHex('my drawn box', 'zzz'), null, 'nothing that is not a cell');
console.log('timeline ok');
