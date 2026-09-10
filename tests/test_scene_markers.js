// The coordinate helpers scene.js uses for axis ticks and markers, exercised in isolation.
// Extracted by regex rather than imported: scene.js is a browser classic script with no module boundary.
const fs = require('fs'), assert = require('assert');

const src = fs.readFileSync('src/aicesat/ui/scene.js', 'utf8');
function at(name) {
  const i = src.indexOf(name);
  assert.ok(i > 0, `${name} not found in scene.js`);
  return i;
}
// A `function f() {...}` body ends at the first `\n}` in column 0. A one-line `const f = ...;` ends at its newline.
// Using the brace rule for both swallowed everything up to the NEXT function's closing brace, which redeclared
// half the file — so the two cases are separate on purpose.
function grabFn(name) {
  const rest = src.slice(at(name));
  const end = rest.indexOf('\n}\n');
  assert.ok(end > 0, `no closing brace for ${name}`);
  return rest.slice(0, end + 2);
}
// `const`/`let` declared inside a sloppy-mode eval stay in the eval's own scope; `var` and function declarations
// leak to the enclosing one. The arrow-function one-liners are referenced from module scope below, so they are
// rewritten to var. (The functions' closures keep seeing the eval scope, which is why the consts they read work.)
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
].join('\n');
eval(code);

// the real frame of the Langtang scene: aeqd on the bbox centre, axis-aligned
const AEQD = {bbox: [85.44, 28.21, 85.62, 28.37], east_xy: [1, 4.1e-5], north_xy: [0, 1]};
// a polar frame is ROTATED: projected +y is not north. These helpers must hold there too.
const POLAR = {bbox: [-51.0, 69.0, -49.0, 69.4], east_xy: [0.7071, 0.7071], north_xy: [-0.7071, 0.7071]};

// The inverse is a 2x2 SOLVE, not a projection, so the round trip is exact to floating point even though
// east_xy/north_xy are only approximately orthonormal (finite differences rounded to 6 decimals). Projecting
// instead drifted 0.4 m at this scene's corner and worse on a larger box -- hence this tolerance, which would
// fail immediately if anyone swapped the solve back for a dot product.
const TOL = 1e-11;
function roundtrip(fr, lon, lat, tolDeg) {
  const [x, y] = lonLatToLocal(fr, lon, lat);
  const [lo2, la2] = localToLonLat(fr, x, y);
  assert.ok(Math.abs(lo2 - lon) < tolDeg, `lon ${lon} -> ${lo2}`);
  assert.ok(Math.abs(la2 - lat) < tolDeg, `lat ${lat} -> ${la2}`);
}

// --- round trip is exact to floating point, in both frame shapes -------------------------------------------------
roundtrip(AEQD, 85.52515462, 28.28531746, TOL);   // the avalanche source
roundtrip(AEQD, 85.44, 28.21, TOL);               // bbox corners
roundtrip(AEQD, 85.62, 28.37, TOL);
roundtrip(POLAR, -50.0, 69.2, TOL);
roundtrip(POLAR, -51.0, 69.4, TOL);

// --- the centre of the bbox is the frame origin ------------------------------------------------------------------
{
  const [x, y] = lonLatToLocal(AEQD, 85.53, 28.29);
  assert.ok(Math.hypot(x, y) < 1e-6, `bbox centre should be the origin, got ${x},${y}`);
}

// --- direction sanity: east is +east, north is +north ------------------------------------------------------------
{
  const [xe] = lonLatToLocal(AEQD, 85.55, 28.29);
  assert.ok(xe > 0, 'moving east must increase the east component');
  const [, yn] = lonLatToLocal(AEQD, 85.53, 28.31);
  assert.ok(yn > 0, 'moving north must increase the north component');
  // in the rotated polar frame, due north must NOT be pure +y
  const [xp, yp] = lonLatToLocal(POLAR, -50.0, 69.3);
  assert.ok(Math.abs(xp) > 1, 'a rotated frame puts due north into BOTH components');
  assert.ok(Math.abs(yp) > 1, 'a rotated frame puts due north into BOTH components');
}

// --- scale is right: 0.01 deg of latitude is ~1.1 km ------------------------------------------------------------
{
  const [, y0] = lonLatToLocal(AEQD, 85.53, 28.29);
  const [, y1] = lonLatToLocal(AEQD, 85.53, 28.30);
  const d = Math.abs(y1 - y0);
  assert.ok(d > 1050 && d < 1160, `0.01 deg lat should be ~1106 m, got ${d}`);
}

// --- hemisphere suffixes ----------------------------------------------------------------------------------------
assert.strictEqual(fmtLat(28.2853), '28.285°N');
assert.strictEqual(fmtLat(-77.8), '77.800°S');
assert.strictEqual(fmtLon(85.5252), '85.525°E');
assert.strictEqual(fmtLon(-49.5), '49.500°W');

// --- a missing basis falls back to axis-aligned rather than throwing ---------------------------------------------
roundtrip({bbox: [0, 0, 1, 1]}, 0.5, 0.5, TOL);

// --- surfaceHeightAt: markers are PLANTED on the terrain, not driven through it ----------------------------------
// Spanning the scene's whole vertical extent sent the stick down through the imagery and out below the ground.
var scene = null;   // the helper reads the module-scoped `scene`; eval'd code sees this one
eval(grabFn('function surfaceHeightAt'));

// a 3x3 grid, 100 m cells, origin (0,0), heights rising 10 m per cell eastward
scene = {surface: {x0: 0, y0: 0, cell: 100, nx: 3, ny: 3,
                   z: [0, 10, 20, 0, 10, 20, 0, 10, 20]}};
assert.strictEqual(surfaceHeightAt(0, 0), 0, 'grid corner');
assert.strictEqual(surfaceHeightAt(200, 200), 20, 'far corner');
assert.ok(Math.abs(surfaceHeightAt(50, 0) - 5) < 1e-9, 'bilinear halfway between 0 and 10');
assert.ok(Math.abs(surfaceHeightAt(150, 150) - 15) < 1e-9, 'bilinear in the far cell');

// outside the grid -> null, so the caller falls back instead of extrapolating off the edge
assert.strictEqual(surfaceHeightAt(-1, 0), null, 'west of the grid');
assert.strictEqual(surfaceHeightAt(0, -1), null, 'south of the grid');
assert.strictEqual(surfaceHeightAt(201, 0), null, 'east of the grid');
assert.strictEqual(surfaceHeightAt(0, 201), null, 'north of the grid');

// a DEM hole must report null, NOT an average of the cells around it
scene = {surface: {x0: 0, y0: 0, cell: 100, nx: 2, ny: 2, z: [0, null, 0, 0]}};
assert.strictEqual(surfaceHeightAt(50, 50), null, 'nodata corner poisons the cell, by design');

// no surface at all (meta arrives before the chunked z) -> null, not a throw
scene = {surface: {x0: 0, y0: 0, cell: 100, nx: 2, ny: 2}};
assert.strictEqual(surfaceHeightAt(50, 50), null, 'surface without z');
scene = {};
assert.strictEqual(surfaceHeightAt(0, 0), null, 'no surface');

console.log('ok');
