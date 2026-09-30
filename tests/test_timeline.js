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
console.log('timeline ok');
