// Explore's Build gate: from the coverage check and the checked collections, can a build succeed, and what to say.
// Run by tests/test_coverage_visibility.py.
const assert = require('assert'), path = require('path');
global.window = global;
require(path.resolve('src/aicesat/ui/explore.js'));
const gate = AICESAT.coverageGate;

const row = (key, o) => Object.assign({key, label: key, possible: true, covered: true}, o);
const cov = {GLAS: row('GLAS', {label: 'ICESat-1 (GLAS)'}), ICESSN: row('ICESSN', {label: 'IceBridge (ATM)', covered: false}),
             ATL06: row('ATL06', {label: 'ICESat-2 land ice (ATL06)', covered: false}), GEDI: row('GEDI', {possible: false, covered: false})};

// The reported case: every checked collection reaches outside its index. A build is certain to fail.
let g = gate(cov, ['ICESSN', 'ATL06']);
assert.strictEqual(g.canBuild, false);
assert.ok(/None of the checked collections/.test(g.message), g.message);
assert.deepStrictEqual(g.outside, ['ICESSN', 'ATL06']);

// Some covered: the build runs and skips the rest, and says which.
g = gate(cov, ['GLAS', 'ICESSN', 'ATL06']);
assert.strictEqual(g.canBuild, true);
assert.ok(/IceBridge \(ATM\) and ICESat-2 land ice \(ATL06\) will be skipped/.test(g.message), g.message);
assert.deepStrictEqual(g.fittable, ['ICESSN', 'ATL06']);

// Once a fit has said a collection has NO index here, it gets one accurate sentence (not "reaches outside its
// index"), and Fit is no longer offered for it: shrinking cannot reach ground that was never indexed.
g = gate(cov, ['GLAS', 'ICESSN', 'ATL06'], ['ATL06']);
assert.ok(/^IceBridge \(ATM\) will be skipped: its index does not cover all of this area\. ICESat-2 land ice \(ATL06\) has no index here and will be skipped\.$/.test(g.message), g.message);
assert.deepStrictEqual(g.fittable, ['ICESSN']);
g = gate(cov, ['GLAS', 'ATL06'], ['ATL06']);
assert.deepStrictEqual(g.fittable, [], 'nothing left that fitting can help');
// ...and a collection unchecked since the fit is not mentioned at all.
assert.ok(!/ATL06/.test(gate(cov, ['GLAS'], ['ATL06']).message));

// All covered: nothing to say.
g = gate(cov, ['GLAS']);
assert.deepStrictEqual([g.canBuild, g.message, g.outside, g.fittable], [true, '', [], []]);

// The coverage check can already tell "no index here at all" (claim_overlap: false) without a fit.
g = gate(Object.assign({}, cov, {ATL06: row('ATL06', {label: 'ICESat-2 land ice (ATL06)', covered: false, claim_overlap: false})}), ['GLAS', 'ICESSN', 'ATL06']);
assert.ok(/ICESat-2 land ice \(ATL06\) has no index here and will be skipped/.test(g.message), g.message);
assert.deepStrictEqual(g.fittable, ['ICESSN']);

// A collection that never flew here is not a coverage problem (the row is disabled and unchecked separately).
g = gate(cov, ['GLAS', 'GEDI']);
assert.deepStrictEqual([g.canBuild, g.outside], [true, []]);

// Nothing checked.
g = gate(cov, []);
assert.strictEqual(g.canBuild, false);
assert.ok(/at least one/.test(g.message));

// No coverage answer (the check failed or is pending): never block on it.
assert.strictEqual(gate(null, ['GLAS']).canBuild, true);

console.log('ok');
