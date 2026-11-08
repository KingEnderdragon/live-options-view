// B2 (readability quest): the OIranges loader section moved out of index.html
// into site/js/oi-ranges-loader.js, loaded in the same position. The test
// loads the shipped module instead of slicing the page by banner text, pins
// the ?v= hash in the page to the module's content (like shared.js), and
// drives loadRanges with a stub fetch so the success + failure paths are
// verified without the network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/oi-ranges-loader.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/oi-ranges-loader.js?v=' + moduleHash), 'oi-ranges-loader script URL must match its content hash on every hosting path');
assert.ok(!html.includes('async function loadRanges'), 'loadRanges must live in js/oi-ranges-loader.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── OIranges loader'), 'OIranges banner must stay in index.html as the section marker');

// --- harness: the page's shared scope, as the module sees it. ---
const parsed = { '0DTE_Regular': { 0: { call: [1, 2, 3, 4], put: [1, 2, 3, 4] } } };
let fetchCalls = [];
let fetchMode = 'ok'; // 'ok' | 'http500' | 'throws'
let warnings = [];
const calError = { style: { display: 'block' } };
const sandbox = {
  console: { ...console, warn: (...args) => { warnings.push(args); } },
  BROWSER_DATA: '/browser-data',
  parseRangesCsv: text => { assert.equal(text, 'Tier,StrikeOffset,Call_p25\n'); return parsed; },
  document: { getElementById: id => { assert.equal(id, 'cal-error'); return calError; } },
  fetch: async (url, options) => {
    fetchCalls.push({ url, options });
    if (fetchMode === 'throws') throw new Error('network down');
    if (fetchMode === 'http500') return { ok: false, status: 500, text: async () => '' };
    return { ok: true, status: 200, text: async () => 'Tier,StrikeOffset,Call_p25\n' };
  },
};
const context = vm.createContext(sandbox);
// `ranges` is declared in the page's shared scope (just above the loader
// section), before this module loads.
vm.runInContext('let ranges = null;', context);
vm.runInContext(moduleSource, context, { filename: 'site/js/oi-ranges-loader.js' });
const get = name => vm.runInContext(name, context);

assert.equal(typeof get('loadRanges'), 'function', 'module must define loadRanges');
assert.equal(get('ranges'), null, 'module load alone must not fetch the calibration file');
assert.equal(fetchCalls.length, 0, 'no fetch before loadRanges is called');

(async () => {
  // Success: fetches the CSV with cache disabled, parses it, hides the banner.
  await vm.runInContext('loadRanges()', context);
  assert.equal(fetchCalls.length, 1, 'loadRanges fetches once');
  assert.equal(fetchCalls[0].url, '/browser-data/derived/OIranges.csv', 'loadRanges fetches the OIranges CSV');
  assert.equal(fetchCalls[0].options.cache, 'no-store', 'the calibration fetch bypasses the cache');
  assert.equal(get('ranges'), parsed, 'loadRanges stores the parsed ranges');
  assert.equal(calError.style.display, 'none', 'a successful load hides the calibration banner');
  assert.equal(warnings.length, 0, 'no warning on success');

  // HTTP failure: keeps the old ranges, shows the banner, warns.
  fetchMode = 'http500';
  calError.style.display = 'none';
  await vm.runInContext('loadRanges()', context);
  assert.equal(get('ranges'), parsed, 'a failed reload keeps the previous ranges');
  assert.equal(calError.style.display, 'block', 'a failed load shows the calibration banner');
  assert.equal(warnings.length, 1, 'a failed load warns once');
  assert.match(String(warnings[0][0]), /OIranges load failed/, 'the warning names the loader');

  // Network failure: same recovery shape, no throw out of loadRanges.
  fetchMode = 'throws';
  await vm.runInContext('loadRanges()', context);
  assert.equal(get('ranges'), parsed, 'a thrown fetch keeps the previous ranges');
  assert.equal(calError.style.display, 'block', 'a thrown fetch shows the calibration banner');
  assert.equal(warnings.length, 2, 'a thrown fetch warns once');
  console.log('OIranges loader: hash pinned, extraction verified, success + HTTP + network failure paths passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
