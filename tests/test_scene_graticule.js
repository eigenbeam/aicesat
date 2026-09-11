// The pure helpers behind the draped lat/lon graticule and the hover readout, exercised in isolation.
// Extracted by regex rather than imported, for the same reason test_scene_markers.js does it: scene.js is a
// browser classic script with no module boundary.
const fs = require('fs'), assert = require('assert');

const src = fs.readFileSync('src/aicesat/ui/scene.js', 'utf8');
function at(name) {
  const i = src.indexOf(name);
  assert.ok(i > 0, `${name} not found in scene.js`);
  return i;
}
function grabFn(name) {
  const rest = src.slice(at(name));
  const end = rest.indexOf('\n}\n');
  assert.ok(end > 0, `no closing brace for ${name}`);
  return rest.slice(0, end + 2);
}
function grabLine(name) {
  const rest = src.slice(at(name));
  return rest.slice(0, rest.indexOf('\n') + 1).replace(/^const /, 'var ');
}
const code = [
  'const M_PER_DEG_LAT = 110574;',
  grabFn('function frameCentre'),
  grabFn('function mPerDegLon'),
  grabFn('function localToLonLat'),
  grabFn('function lonLatToLocal'),
  grabLine('const fmtLat ='),
  grabLine('const fmtLon ='),
  grabFn('function graticuleStep'),
  grabFn('function drapeSegments'),
  grabFn('function ticksIn'),
  grabLine('const nfmt ='),
  grabLine('const fmtLat5 ='),
  grabLine('const fmtLon5 ='),
  grabLine('const pointTip ='),
  grabFn('function gridTip'),
  grabFn('function edgeMostEnd'),
  grabFn('function hexCacheKey'),
].join('\n');
eval(code);

let pass = 0;
const t = (name, fn) => { fn(); pass++; console.log('  ok ' + name); };

// ---------------------------------------------------------------- graticuleStep
t('graticuleStep picks a 1/2/5 ladder value', () => {
  for (const span of [0.001, 0.007, 0.03, 0.09, 0.33, 1.7, 12, 55]) {
    const s = graticuleStep(span);
    const m = s / Math.pow(10, Math.floor(Math.log10(s)));
    assert.ok([1, 2, 5].some(k => Math.abs(m - k) < 1e-9), `step ${s} for span ${span} is not 1/2/5 x 10^n`);
  }
});

t('graticuleStep yields a readable number of lines', () => {
  // The whole point of a ladder step is that no scene gets 2 lines or 40. Bounds are loose because snapping to
  // 1/2/5 cannot hit a target exactly -- they are here to catch an off-by-a-decade error, not to pin the constant.
  for (const span of [0.002, 0.01, 0.05, 0.214, 0.327, 2.5, 30]) {
    const n = span / graticuleStep(span);
    assert.ok(n >= 2 && n <= 14, `span ${span} deg -> ${n.toFixed(1)} lines`);
  }
});

t('graticuleStep is scale invariant across a decade', () => {
  assert.strictEqual(graticuleStep(0.33) * 10, graticuleStep(3.3));
  assert.strictEqual(graticuleStep(0.33) * 100, graticuleStep(33));
});

// ---------------------------------------------------------------- ticksIn
t('ticksIn returns the step multiples inside the range', () => {
  assert.deepStrictEqual(ticksIn(28.1766, 28.3904, 0.05), [28.2, 28.25, 28.3, 28.35]);
});

t('ticksIn lands exactly on multiples, not on the range start', () => {
  // A graticule labelled 28.1766, 28.2266, ... is unreadable; lines must fall on round coordinates.
  for (const v of ticksIn(28.1766, 28.3904, 0.05)) {
    assert.ok(Math.abs(v / 0.05 - Math.round(v / 0.05)) < 1e-9, `${v} is not a multiple of 0.05`);
  }
});

t('ticksIn handles a range narrower than one step', () => {
  assert.deepStrictEqual(ticksIn(28.201, 28.209, 0.05), []);
});

t('ticksIn works across the prime meridian and the equator', () => {
  assert.deepStrictEqual(ticksIn(-0.06, 0.06, 0.05), [-0.05, 0, 0.05]);
});

// ---------------------------------------------------------------- drapeSegments
t('drapeSegments keeps one unbroken run when the DEM covers everything', () => {
  const s = drapeSegments([[0, 0, 10], [1, 0, 11], [2, 0, 12]]);
  assert.strictEqual(s.length, 1);
  assert.deepStrictEqual(s[0], [[0, 0, 10], [1, 0, 11], [2, 0, 12]]);
});

t('drapeSegments BREAKS at nodata instead of bridging it', () => {
  // surfaceHeightAt returns null over a DEM hole and its docstring forbids inventing ground there. HMA has real
  // holes on the steep faces of this very scene, so a bridged line would draw terrain that does not exist.
  const s = drapeSegments([[0, 0, 10], [1, 0, 11], [2, 0, null], [3, 0, 13], [4, 0, 14]]);
  assert.strictEqual(s.length, 2);
  assert.deepStrictEqual(s[0], [[0, 0, 10], [1, 0, 11]]);
  assert.deepStrictEqual(s[1], [[3, 0, 13], [4, 0, 14]]);
});

t('drapeSegments drops runs too short to draw', () => {
  const s = drapeSegments([[0, 0, 10], [1, 0, null], [2, 0, 12], [3, 0, null], [4, 0, 14], [5, 0, 15]]);
  assert.strictEqual(s.length, 1, 'a single isolated sample is not a line');
  assert.deepStrictEqual(s[0], [[4, 0, 14], [5, 0, 15]]);
});

t('drapeSegments returns nothing when the DEM covers nothing', () => {
  assert.deepStrictEqual(drapeSegments([[0, 0, null], [1, 0, null]]), []);
});

t('drapeSegments tolerates NaN as well as null', () => {
  const s = drapeSegments([[0, 0, 10], [1, 0, NaN], [2, 0, 12], [3, 0, 13]]);
  assert.strictEqual(s.length, 1);
  assert.deepStrictEqual(s[0], [[2, 0, 12], [3, 0, 13]]);
});

// ---------------------------------------------------------------- hover readouts
t('pointTip names the mission and gives a located, ellipsoidal height', () => {
  const s = pointTip('ATL06', 85.5166, 28.2561, 4812.3);
  assert.ok(s.includes('ATL06'), s);
  assert.ok(s.includes('28.256') && s.includes('N'), s);
  assert.ok(s.includes('85.516') && s.includes('E'), s);
  assert.ok(/4,?812/.test(s), s);
  assert.ok(/ellipsoid/i.test(s), 'the height reference must be stated, or the number is ambiguous');
});

t('pointTip locates a point finer than its own footprint', () => {
  // 3 decimals of degree is ~100 m, coarser than ATL06's 20 m posting and GLAS's 65 m footprint -- two points a
  // footprint apart would print the same coordinate. The axis ticks keep 3; a point readout needs more.
  const a = pointTip('ATL06', 85.51660, 28.25610, 4812.3);
  const b = pointTip('ATL06', 85.51663, 28.25614, 4813.1);
  assert.notStrictEqual(a, b, 'two points 4 m apart printed the same coordinate');
});

t('pointTip signs the hemispheres', () => {
  const s = pointTip('GLAS', -49.5, -69.2, 1234.0);
  assert.ok(s.includes('S') && s.includes('W'), s);
});

t('gridTip reports the cell, its resolution, its size and the per-mission counts', () => {
  const s = gridTip('8828d0a1bffffff', 8, 461.4, {ATL06: 1204, GLAS: 18});
  assert.ok(s.includes('8828d0a1bffffff'), s);
  assert.ok(s.includes('8'), s);
  assert.ok(/461/.test(s), s);
  assert.ok(s.includes('ATL06') && /1,?204/.test(s), s);
  assert.ok(s.includes('GLAS') && s.includes('18'), s);
});

t('gridTip says so when a cell holds no points', () => {
  const s = gridTip('8828d0a1bffffff', 8, 461.4, {});
  assert.ok(/no points|empty/i.test(s), s);
});

// ---------------------------------------------------------------- graticule label placement
t('edgeMostEnd picks the run end furthest from the scene centre', () => {
  // A line broken by DEM holes has many run ends, and most of them are hole edges in the MIDDLE of the terrain.
  // Labelling one of those strands a coordinate mid-scene; the label belongs out at the perimeter.
  const segs = [[[0, 0, 1], [10, 0, 1]], [[400, 0, 1], [900, 0, 1]]];
  assert.deepStrictEqual(edgeMostEnd(segs, 0, 0), [900, 0, 1]);
});

t('edgeMostEnd considers BOTH ends of every run', () => {
  const segs = [[[-900, 0, 1], [-400, 0, 1]], [[10, 0, 1], [20, 0, 1]]];
  assert.deepStrictEqual(edgeMostEnd(segs, 0, 0), [-900, 0, 1]);
});

t('edgeMostEnd returns null when there is nothing to label', () => {
  assert.strictEqual(edgeMostEnd([], 0, 0), null);
});

// ---------------------------------------------------------------- H3 grid memo
t('hexCacheKey changes when more points arrive', () => {
  // Point arrays fill progressively over the stream. A key of (scene, res) alone froze the counts binned from
  // whatever had landed when the grid was switched on -- a cell read 27 where the finished scene held 48.
  const a = hexCacheKey('s1', 8, {ATL06: {positions: new Array(300)}});
  const b = hexCacheKey('s1', 8, {ATL06: {positions: new Array(900)}});
  assert.notStrictEqual(a, b);
});

t('hexCacheKey is stable once the arrays stop growing', () => {
  const series = {ATL06: {positions: new Array(900)}, GLAS: {positions: new Array(30)}};
  assert.strictEqual(hexCacheKey('s1', 8, series), hexCacheKey('s1', 8, series));
});

t('hexCacheKey separates resolutions and scenes', () => {
  const series = {ATL06: {positions: new Array(900)}};
  assert.notStrictEqual(hexCacheKey('s1', 8, series), hexCacheKey('s1', 9, series));
  assert.notStrictEqual(hexCacheKey('s1', 8, series), hexCacheKey('s2', 8, series));
});

t('hexCacheKey does not depend on mission insertion order', () => {
  const A = {ATL06: {positions: new Array(9)}, GLAS: {positions: new Array(3)}};
  const B = {GLAS: {positions: new Array(3)}, ATL06: {positions: new Array(9)}};
  assert.strictEqual(hexCacheKey('s1', 8, A), hexCacheKey('s1', 8, B));
});

t('hexCacheKey tolerates a mission whose points have not arrived at all', () => {
  assert.doesNotThrow(() => hexCacheKey('s1', 8, {ATL06: {}, GLAS: {positions: null}}));
});

// ---------------------------------------------------------------- integration with the real frame
t('a graticule line converts to local metres and back', () => {
  const fr = {bbox: [85.3319, 28.1766, 85.6589, 28.3904], east_xy: [1, 4.1e-5], north_xy: [0, 1]};
  for (const lat of ticksIn(28.1766, 28.3904, graticuleStep(0.327))) {
    const [x, y] = lonLatToLocal(fr, 85.45, lat);
    const [, back] = localToLonLat(fr, x, y);
    assert.ok(Math.abs(back - lat) < 1e-9, `${lat} -> ${back}`);
  }
});

console.log(`\n${pass} assertions passed`);
