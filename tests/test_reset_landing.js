/* After "Reset this hex" the globe must land where that hex can be clicked again.
 *
 * Regression this pins: the reset first landed on the hex's own extent. MapView.flyTo turns a 0.5 deg box into zoom
 * ~8, where resForZoom draws res-6 hexes; coverage exists only at res 3-5, so nothing there is clickable and the
 * user cannot start the rebuild they just reset for. Uses the real flyTo and resForZoom, at three latitudes.
 *
 * Run: node tests/test_reset_landing.js   (tests/test_reset_region.py runs it under pytest)
 */
const fs = require('fs'), path = require('path'), assert = require('assert');
global.window = global; global.AICESAT = {};
global.h3 = require(path.join(__dirname, '..', 'src', 'aicesat', 'widget', 'vendor', 'h3-js-4.5.0.umd.js'));
const UI = path.join(__dirname, '..', 'src', 'aicesat', 'ui');
eval(fs.readFileSync(path.join(UI, 'timeline.js'), 'utf8'));
eval(fs.readFileSync(path.join(UI, 'map.js'), 'utf8'));
const M = AICESAT.MapView.prototype;

for (const [lat, lon] of [[69.32, -49.99], [0.5, 10.0], [80.0, -30.0]]) {
  const hx = h3.latLngToCell(lat, lon, 5);
  const box = AICESAT.timeline.globeBox(hx);
  const fake = {state: {viewState: {}}, deck: {setProps() {}}, render() {}};
  M.flyTo.call(fake, box);
  const res = M.resForZoom(fake.state.viewState.zoom);
  assert.strictEqual(res, 5, `${hx} at ${lat}N: landed at zoom ${fake.state.viewState.zoom.toFixed(2)}, which draws res ${res}`);
  const [cla, clo] = h3.cellToLatLng(hx);
  assert.ok(Math.abs(fake.state.viewState.latitude - cla) < 1e-9 && Math.abs(fake.state.viewState.longitude - clo) < 1e-9,
    'centred on the hex');
}
console.log('ok test_reset_landing');
