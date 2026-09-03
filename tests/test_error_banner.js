/* The error banner must name the right subsystem.
 *
 * Regression: the auth pattern included /\btoken\b/, and the no-index message ends "(check your selection and the
 * token)". A coverage failure was therefore reported as "Earthdata authentication failed", which sent debugging at
 * the token — valid for another 11 days — instead of at the missing index.
 *
 * Run: node tests/test_error_banner.js
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

global.window = global;
global.AICESAT = {};
const el = {hidden: true, innerHTML: '', querySelector: () => ({set onclick(_f) {}})};
global.document = {getElementById: () => el};
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'aicesat', 'ui', 'app.js'), 'utf8');
eval(src.slice(0, src.indexOf('AICESAT.clearError')));

const AUTH = 'Earthdata authentication failed';
const shows = msg => { el.innerHTML = ''; AICESAT.showError(msg); return el.innerHTML.includes(AUTH); };

let n = 0;
const ok = (c, m) => { assert.ok(c, m); n++; };

// the real auth failure (auth.py _AUTH_HELP) -> auth banner
ok(shows('No working Earthdata Login. Fetching NASA data needs a token — browsing the existing lake does not. ' +
         'Provide one of: (1) EARTHDATA_TOKEN in the MCP server config env'), 'the real auth help must flag auth');
ok(shows('RuntimeError: a token file at /opt/aicesat/.edl/token.prod (override with AICESAT_EDL_FILE)'),
   'the token-file help must flag auth');
ok(shows('Generate a token at https://urs.earthdata.nasa.gov'), 'the URS pointer must flag auth');

// the coverage failure -> NOT auth, even though it contains the word "token"
ok(!shows('RuntimeError: no collection returned data over this area (check your selection and the token)'),
   'a coverage failure must NOT be reported as an authentication failure');
ok(!shows('RuntimeError: ATL06 not indexed over (86.8, 27.8, 87.0, 28.0) — build the sub-granule index first'),
   'a missing index must NOT be reported as an authentication failure');
ok(!shows('scene abc123: not available'), 'an unrelated error must not flag auth');

console.log(`ok — ${n} checks`);
