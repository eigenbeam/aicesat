// The browser's lon/lat <-> scene-metre conversion, the terrain drape, the cell-size label and cell lookup.
// Run by tests/test_scene_geo.py, which writes the pyproj reference cases to the JSON path given as argv[2].
const fs = require('fs'), assert = require('assert'), path = require('path');

const VENDOR = 'src/aicesat/widget/vendor';
global.window = global;
global.proj4 = require(path.resolve(VENDOR, fs.readdirSync(VENDOR).find(f => f.startsWith('proj4-'))));
global.h3 = require(path.resolve(VENDOR, fs.readdirSync(VENDOR).find(f => f.startsWith('h3-js-'))));
require(path.resolve('src/aicesat/ui/geo.js'));
require(path.resolve('src/aicesat/ui/tspanel.js'));
const G = AICESAT.geo, TS = AICESAT.ts;

// ---- 1. the same projection as the server -------------------------------------------------------------------------
// The linear approximation this replaced was 68 m off the points 11 km from a Jakobshavn scene's centre, 195 m at
// 17 km. Every case is a real frame from scene.local_frame: polar north (3413), polar south (3031) and a per-scene
// azimuthal-equidistant frame (the mid-latitude default).
for (const c of JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))) {
  c.pts.forEach(([lon, lat], i) => {
    const [x, y] = G.lonLatToLocal(c.frame, lon, lat);
    const err = Math.hypot(x - c.x[i], y - c.y[i]);
    assert.ok(err < 0.01, `${c.label}: lonLatToLocal is ${err.toFixed(3)} m from pyproj at ${lon},${lat}`);
    const [lo, la] = G.localToLonLat(c.frame, c.x[i], c.y[i]);
    // 1e-8 deg is ~1 mm. proj4's aeqd inverse is iterative and lands ~1.2e-9 deg (0.1 mm) off; a hover readout
    // prints 5 decimals (~1 m), so this tolerance is four orders tighter than anything the UI shows.
    assert.ok(Math.abs(lo - lon) < 1e-8 && Math.abs(la - lat) < 1e-8, `${c.label}: localToLonLat does not invert pyproj`);
  });
}

// ---- 2. things drawn on the ground sit on the ground ----------------------------------------------------------------
// Candidate outlines were drawn at each cell's reference-plane height -- the GLAS-era surface, 40-70 m above today's
// ice on a thinning glacier -- so perspective shifted and enlarged them against the terrain-draped grid.
const heightAt = (x, y) => (x < 0 ? null : 100 + x * 0.01);
assert.strictEqual(G.drapedZ(heightAt, 200, 0, 55, 8), 110, 'draped to the terrain, plus the lift');
assert.strictEqual(G.drapedZ(heightAt, -5, 0, 55, 8), 63, 'over a DEM hole it falls back, plus the lift');
assert.strictEqual(G.drapedZ(() => NaN, 1, 1, 55, 0), 55, 'a non-finite height is a hole');

// ---- 3. one source for a cell's size --------------------------------------------------------------------------------
// The two panels labelled res 8 as ~531 m (h3's v4 average) and ~461 m (a hardcoded v3-era table).
for (const r of [7, 8, 9, 10, 11]) {
  assert.strictEqual(TS.cellEdgeM(r), Math.round(h3.getHexagonEdgeLengthAvg(r, 'm')), `res ${r} edge`);
}
assert.ok(!('H3_EDGE_M' in TS), 'the stale table is gone, so nothing can read it');

// ---- 4. finding a cell --------------------------------------------------------------------------------------------
const mk = (lat, lon) => { const id = h3.latLngToCell(lat, lon, 8); const [clat, clon] = h3.cellToLatLng(id); return {h3: id, lat: clat, lon: clon}; };
const cands = [mk(69.03, -49.47), mk(69.1752, -49.3276), mk(69.19, -49.32)];
assert.deepStrictEqual(TS.findCandidate(cands, cands[1].h3, 8), {index: 1, exact: true});
assert.deepStrictEqual(TS.findCandidate(cands, '69.1752, -49.3276', 8), {index: 1, exact: true});
assert.deepStrictEqual(TS.findCandidate(cands, ' 69.1752 -49.3276 ', 8), {index: 1, exact: true}, 'space-separated');
const fine = h3.latLngToCell(69.1752, -49.3276, 11);               // an id at another resolution resolves by its centre
assert.deepStrictEqual(TS.findCandidate(cands, fine, 8), {index: 1, exact: true});
const near = TS.findCandidate(cands, '69.20, -49.10', 8);          // not a candidate: the nearest one, and how far
assert.ok(near.index === 2 && near.exact === false && near.km > 5 && near.km < 15, JSON.stringify(near));
assert.ok(TS.findCandidate(cands, 'Jakobshavn', 8).error, 'unparseable input says so');
assert.ok(TS.findCandidate(cands, '95, 10', 8).error, 'a latitude past 90 is refused, not wrapped');
assert.strictEqual(TS.findCandidate([], '69.1, -49.3', 8), null, 'no candidates, nothing to find');

console.log('ok');
