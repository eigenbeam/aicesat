/* The scene camera frames the area the scene was built over, not whatever data arrived first.
 *
 * Regression this pins: fitView framed the points present when the first doc landed, once (didFit). On a cold build
 * that is whatever streamed first: one GLAS shot is a span of 0, laundered to a 1 m frame, and minZoom (zoom - 6)
 * then stopped the user zooming out past ~80 m. The area is in the doc's shell (polygon / bbox + frame) before any
 * data, so the frame no longer depends on arrival order.
 *
 * Run: node tests/test_scene_fit.js   (tests/test_scene_fit.py runs it under pytest)
 */
const fs = require('fs'), path = require('path'), assert = require('assert');
global.window = global; global.AICESAT = {};
const h3 = require(path.join(__dirname, '..', 'src', 'aicesat', 'widget', 'vendor', 'h3-js-4.5.0.umd.js'));
require(path.resolve('src/aicesat/ui/geo.js'));
const src = fs.readFileSync('src/aicesat/ui/scene.js', 'utf8');
const i = src.indexOf('function fitView()'); assert.ok(i > 0, 'fitView not found');
eval(src.slice(i, i + src.slice(i).indexOf('\n}\n') + 2).replace('function fitView()', 'global.fitView = function ()'));

global.root = {clientWidth: 800, clientHeight: 800};
global.camAngles = () => ({rotationX: 35, rotationOrbit: -25});
global.Z_EXAG = 1;
let props = null;
global.deckgl = {setProps: p => { props = p.initialViewState; }};

const HX = '8506f257fffffff';                                            // ~17 km across
const polygon = h3.cellToBoundary(HX).map(([la, lo]) => [lo, la]);
const bbox = [-50.246664, 69.229494, -49.734233, 69.404746];             // that hex's lon/lat box
const frame = {bbox, east_xy: [1, 0], north_xy: [0, 1]};                 // no CRS: geo.js's local approximation
const framedSpanM = () => 800 / Math.pow(2, props.zoom) / 1.25;           // inverse of the fit's zoom formula
const oneShot = z => ({GLAS: {positions: new Float32Array([120, -40, z])}});

// one GLAS shot is all that has arrived: the frame is still the hex, and zooming out is not capped near the shot
global.scene = {frame, polygon, bbox, series: oneShot(12)};
assert.ok(fitView(), 'fits');
assert.ok(framedSpanM() > 15000, `framed ${framedSpanM().toFixed(0)} m around one shot; the hex is ~17 km`);
assert.ok(800 / Math.pow(2, props.minZoom) > 100000, 'can zoom out well past the hex');
assert.ok(Math.abs(props.target[2] - 12) < 1e-6, 'the data still sets the height the camera aims at');

// nothing has arrived yet (a cold build opens on its shell): frame the hex anyway, aimed at the datum
global.scene = {frame, polygon, bbox, series: {}};
assert.ok(fitView(), 'fits before any data');
assert.ok(framedSpanM() > 15000 && props.target[2] === 0);

// a scene drawn as a box has no polygon: its bbox is the area
global.scene = {frame, polygon: null, bbox, series: oneShot(0)};
assert.ok(fitView() && framedSpanM() > 15000, 'a bbox scene frames its box');
console.log('ok test_scene_fit');
