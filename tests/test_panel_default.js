/* A panel can start collapsed, and the reader's own choice still wins.
 *
 * The change map's Analysis panel starts collapsed. U.panels remembers each panel's collapsed state in localStorage,
 * so the declared default must apply only when nothing is remembered: someone who expanded it keeps it expanded.
 *
 * Run: node tests/test_panel_default.js   (tests/test_panel_default.py runs it under pytest)
 */
const assert = require('assert');
global.window = global; global.AICESAT = {};
require('../src/aicesat/ui/util.js');
const start = AICESAT.util.startCollapsed;

assert.strictEqual(start(null, {collapsed: '1'}), true, 'declared collapsed, nothing remembered: collapsed');
assert.strictEqual(start(null, {}), false, 'undeclared, nothing remembered: open, as every panel was');
assert.strictEqual(start('0', {collapsed: '1'}), false, 'the reader expanded it before: stays expanded');
assert.strictEqual(start('1', {}), true, 'the reader collapsed it before: stays collapsed');
console.log('ok test_panel_default');
