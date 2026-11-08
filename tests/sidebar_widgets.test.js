// B2 (readability quest): the sidebar-widgets section moved out of index.html
// into docs/js/sidebar-widgets.js, loaded in the same position. The test pins
// the ?v= hash in the page to the module's content (like shared.js), asserts
// the section no longer lives inline, and drives the widget data functions
// with stubbed fetch + page globals: the intraday cache, the daily window,
// the 46-element feature vector, cosine similarity, the top-similar query
// and the next-day move. The renderers that consume these (renderHistWidgets
// in the canvas-tooltip section) run in the assembly check, which loads the
// whole page; the historical_archive suite covers the date-select flow that
// calls them. Values returned from the vm context are compared field by
// field, not deepEqual — deepEqual fails across the vm realm boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../docs/js/sidebar-widgets.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../docs/shared.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/sidebar-widgets.js?v=' + moduleHash), 'sidebar-widgets script URL must match its content hash on every hosting path');
assert.ok(!html.includes('async function loadIntradayPrices'), 'loadIntradayPrices must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('function aggregateDailyOHLC'), 'aggregateDailyOHLC must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('async function loadDailyWindow'), 'loadDailyWindow must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('function buildFeatureVector'), 'buildFeatureVector must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('async function ensureVector'), 'ensureVector must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('function cosineSim'), 'cosineSim must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('async function queryTopSimilar'), 'queryTopSimilar must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('async function nextDayMove'), 'nextDayMove must live in js/sidebar-widgets.js, not inline in index.html');
assert.ok(!html.includes('const intradayCache = {}'), 'intradayCache must live in js/sidebar-widgets.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── sidebar widgets:'), 'sidebar widgets banner must stay in index.html as the section marker');

const DATES = ['2026-09-21', '2026-09-22', '2026-09-23'];
const INTRADAY = {
  '2026-09-21': 'datetime,open,high,low,close\n2026-09-21T13:30:00Z,590,595,588,593\n2026-09-21T13:35:00Z,593,594,592,594',
  '2026-09-22': 'datetime,open,high,low,close\n2026-09-22T13:30:00Z,593,598,592,597',
  '2026-09-23': 'datetime,open,high,low,close\n2026-09-23T13:30:00Z,597,602,596,600\n2026-09-23T13:35:00Z,600,601,599,601',
};
// Every offset -11..11 calibrates the same way, so the expected bucket for a
// given OI is the same at every strike: 50 -> 1, 250 -> 3, 500 -> 5, 0 -> 0.
const RANGES = { '0DTE_Regular': {} };
for (let o = -11; o <= 11; o++) RANGES['0DTE_Regular'][o] = { call: [100, 200, 300, 400], put: [100, 200, 300, 400] };

// --- harness: the page's shared scope, as the module sees it. ---
// `validArchiveDate`, `histManifestDates`, `fetchChainCsv` and
// `classifyTierSimple` stay declared in the historical panel (the page's
// shared scope); `BROWSER_DATA` stays in the stale-page section; `parseCsv`,
// `getThresh`/`oiBucket` and `ranges` come from shared.js / the OIranges
// loader. The sandbox provides them here, with a stub fetch + localStorage.
function harness({ fetchImpl, chainRows, manifestDates = DATES } = {}) {
  const fetchCalls = [];
  const saved = {};
  const chainCalls = [];
  const sandbox = {
    console,
    BROWSER_DATA: '/browser-data',
    histManifestDates: [...manifestDates],
    // Mirrors the historical panel's real validArchiveDate (regex + ISO round-trip).
    validArchiveDate: value => {
      if (typeof value !== 'string' || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return false;
      const date = new Date(`${value}T00:00:00Z`);
      return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
    },
    fetchChainCsv: async dateStr => { chainCalls.push(dateStr); return chainRows ? chainRows(dateStr) : []; },
    classifyTierSimple: () => '0DTE_Regular',
    localStorage: {
      getItem: () => null,
      setItem: (k, v) => { saved[k] = v; },
      removeItem: k => { delete saved[k]; },
    },
    fetch: async url => {
      fetchCalls.push(String(url));
      if (fetchImpl) return fetchImpl(url);
      const m = String(url).match(/qqq_intraday_(\d{8})\.csv/);
      const date = m && `${m[1].slice(0, 4)}-${m[1].slice(4, 6)}-${m[1].slice(6)}`;
      const body = date && INTRADAY[date];
      if (!body) return { ok: false, status: 404, text: async () => '' };
      return { ok: true, status: 200, text: async () => body };
    },
  };
  const context = vm.createContext(sandbox);
  // The real shared.js + shared/csv-ranges.js: `parseCsv`,
  // `getThresh`/`oiBucket` are browser globals in production (not in
  // shared.js's module.exports), so load both files into the context — they
  // define them as top-level functions. B3 slice 1 (Nov 2026): the three
  // moved to docs/shared/csv-ranges.js, loaded here in page order.
  // `ranges` is declared in the page's shared scope (just above the loader
  // section), before this module loads.
  vm.runInContext(sharedSource, context, { filename: 'docs/shared.js' });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../docs/shared/csv-ranges.js'), 'utf8'), context, { filename: 'docs/shared/csv-ranges.js' });
  vm.runInContext('let ranges = null;', context);
  vm.runInContext('ranges = ' + JSON.stringify(RANGES) + ';', context);
  vm.runInContext(moduleSource, context, { filename: 'docs/js/sidebar-widgets.js' });
  return { context, fetchCalls, saved, chainCalls };
}
const call = (h, expr) => vm.runInContext(expr, h.context);

// Chain rows for a date: strikes spot-11..spot+11, call + put each, the OI
// the caller chooses. UnderlyingPrice is the spot, Expiration the date.
function chainRowsFor(spot, oi) {
  return dateStr => {
    const rows = [];
    for (let s = spot - 11; s <= spot + 11; s++) {
      rows.push({ Expiration: dateStr, Type: 'call', Strike: s, UnderlyingPrice: spot, OpenInterest: oi, Volume: 1 });
      rows.push({ Expiration: dateStr, Type: 'put', Strike: s, UnderlyingPrice: spot, OpenInterest: oi, Volume: 1 });
    }
    return rows;
  };
}

(async () => {
  // --- loadIntradayPrices: success, cache, HTTP error, malformed, bad date. ---
  let h = harness();
  let bars = await call(h, "loadIntradayPrices('2026-09-23')");
  assert.equal(bars.length, 2, 'two 5-min bars for 2026-09-23');
  assert.equal(bars[0].t, '2026-09-23T13:30:00Z', 'bar keeps its timestamp');
  assert.equal(bars[0].o, 597, 'bar open parsed as a number');
  assert.equal(bars[0].h, 602, 'bar high parsed as a number');
  assert.equal(bars[0].l, 596, 'bar low parsed as a number');
  assert.equal(bars[0].c, 600, 'bar close parsed as a number');
  assert.ok(h.fetchCalls[0].includes('/prices/qqq_intraday_20260923.csv'), 'intraday fetch hits the R2 key for the date');
  await call(h, "loadIntradayPrices('2026-09-23')");
  assert.equal(h.fetchCalls.length, 1, 'second call for the same date is served from the cache');

  h = harness({ fetchImpl: async () => ({ ok: false, status: 500, text: async () => '' }) });
  await assert.rejects(call(h, "loadIntradayPrices('2026-09-23')"), /HTTP 500/, 'an HTTP error rejects with its status');
  h = harness({ fetchImpl: async () => ({ ok: true, status: 200, text: async () => 'datetime,open,high,low,close\n' }) });
  await assert.rejects(call(h, "loadIntradayPrices('2026-09-23')"), /Malformed or empty/, 'a header-only archive rejects');
  h = harness({ fetchImpl: async () => ({ ok: true, status: 200, text: async () => 'datetime,open,high,low,close\n2026-09-23T13:30:00Z,597,602,596,' }) });
  await assert.rejects(call(h, "loadIntradayPrices('2026-09-23')"), /Malformed or empty/, 'a row with a missing close rejects');
  h = harness();
  await assert.rejects(call(h, "loadIntradayPrices('2026-02-30')"), /Invalid archive date/, 'an impossible date rejects before any fetch');
  assert.equal(h.fetchCalls.length, 0, 'an invalid date never reaches the network');

  // --- aggregateDailyOHLC: first open, max high, min low, last close. ---
  h = harness();
  const agg = call(h, 'aggregateDailyOHLC([{o:590,h:595,l:588,c:593},{o:593,h:594,l:592,c:594}])');
  assert.equal(agg.o, 590, 'daily open is the first bar open');
  assert.equal(agg.h, 595, 'daily high is the max bar high');
  assert.equal(agg.l, 588, 'daily low is the min bar low');
  assert.equal(agg.c, 594, 'daily close is the last bar close');
  assert.equal(call(h, 'aggregateDailyOHLC([])'), null, 'no bars aggregates to null');
  assert.equal(call(h, 'aggregateDailyOHLC(null)'), null, 'null aggregates to null');

  // --- loadDailyWindow: ±2 trading days from the manifest, skips a missing day. ---
  h = harness();
  const win = await call(h, "loadDailyWindow('2026-09-23')");
  assert.equal(win.candles.length, 3, 'the window covers the center date and its neighbours');
  assert.equal(win.centerIndex, 2, 'the center date is the last candle');
  assert.equal(win.candles[0].date, '2026-09-21', 'first candle is the earliest date');
  assert.equal(win.candles[0].label, '09/21', 'candle label is MM/DD');
  assert.equal(win.candles[0].o, 590, 'first candle aggregates its day');
  assert.equal(win.candles[0].c, 594, 'first candle closes on its last bar');
  assert.equal(win.candles[2].c, 601, 'center candle closes on its last bar');
  const missing = await call(h, "loadDailyWindow('2026-09-20')");
  assert.equal(missing.candles.length, 0, 'a date outside the manifest has no window');
  assert.equal(missing.centerIndex, -1, 'a date outside the manifest has no center');
  h = harness({ fetchImpl: async url => {
    if (String(url).includes('qqq_intraday_20260922')) return { ok: false, status: 404, text: async () => '' };
    const m = String(url).match(/qqq_intraday_(\d{8})\.csv/);
    const date = m && `${m[1].slice(0, 4)}-${m[1].slice(4, 6)}-${m[1].slice(6)}`;
    return { ok: true, status: 200, text: async () => INTRADAY[date] };
  } });
  const gappy = await call(h, "loadDailyWindow('2026-09-23')");
  assert.equal(gappy.candles.length, 2, 'a day with no intraday archive is skipped, not fatal');
  assert.equal(gappy.centerIndex, 1, 'the center still resolves after a skip');

  // --- buildFeatureVector: 46 buckets, calls then puts, offsets +11..-11. ---
  h = harness();
  const rows = [];
  for (let s = 589; s <= 611; s++) {
    rows.push({ Expiration: '2026-09-23', Type: 'call', Strike: s, UnderlyingPrice: 600, OpenInterest: s === 600 ? 0 : 50 });
    rows.push({ Expiration: '2026-09-23', Type: 'put', Strike: s, UnderlyingPrice: 600, OpenInterest: s === 600 ? 250 : 500 });
  }
  const vec = call(h, 'buildFeatureVector(' + JSON.stringify(rows) + ", '0DTE_Regular')");
  assert.equal(vec.length, 46, '23 call buckets then 23 put buckets');
  assert.equal(vec[0], 1, 'offset +11 call with OI 50 buckets to 1');
  assert.equal(vec[11], 0, 'the ATM call with OI 0 buckets to 0');
  assert.equal(vec[23], 5, 'offset +11 put with OI 500 buckets to 5');
  assert.equal(vec[34], 3, 'the ATM put with OI 250 buckets to 3');
  assert.equal(call(h, 'buildFeatureVector([], "0DTE_Regular")'), null, 'no rows builds no vector');
  assert.equal(call(h, "buildFeatureVector([{Strike:600,Type:'call',OpenInterest:1}], '0DTE_Regular')"), null, 'rows with no spot build no vector');

  // --- cosineSim: orthogonal 0, identical 1, zero vector 0. ---
  h = harness();
  assert.equal(call(h, 'cosineSim([1,0],[0,1])'), 0, 'orthogonal vectors score 0');
  assert.equal(call(h, 'cosineSim([1,2,3],[1,2,3])'), 1, 'identical vectors score 1');
  assert.equal(call(h, 'cosineSim([0,0],[1,2])'), 0, 'a zero vector scores 0, not NaN');

  // --- ensureVector: builds once, caches in memory + localStorage, null on no rows. ---
  h = harness({ chainRows: chainRowsFor(600, 50) });
  const first = await call(h, "ensureVector('2026-09-23')");
  assert.equal(first.length, 46, 'the first call builds the vector from the chain');
  assert.equal(h.chainCalls.length, 1, 'the first call fetches the chain once');
  assert.ok(h.saved['sim_vectors_v1'], 'the vector is persisted to localStorage');
  assert.ok(JSON.parse(h.saved['sim_vectors_v1'])['2026-09-23'], 'the persisted cache holds the date');
  await call(h, "ensureVector('2026-09-23')");
  assert.equal(h.chainCalls.length, 1, 'the second call is served from the in-memory cache');
  h = harness({ chainRows: () => [] });
  assert.equal(await call(h, "ensureVector('2026-09-23')"), null, 'a date with no 0DTE chain has no vector');

  // --- queryTopSimilar: excludes the target, ranks identical first, zero last. ---
  // The target's own chain: OI 50 everywhere, so the 2026-09-21 twin scores
  // ~1 and the all-zero 2026-09-23 vector scores 0.
  const targetRows = chainRowsFor(600, 50)('2026-09-22');
  h = harness({ chainRows: dateStr => {
    if (dateStr === '2026-09-22') return targetRows;
    if (dateStr === '2026-09-21') return chainRowsFor(600, 50)(dateStr);
    return chainRowsFor(600, 0)(dateStr);
  } });
  const matches = await call(h, "queryTopSimilar('2026-09-22', 5)");
  assert.equal(matches.length, 2, 'two other cached dates match');
  assert.equal(matches[0].date, '2026-09-21', 'the identical twin ranks first');
  assert.ok(matches[0].score > 0.999, 'the identical twin scores ~1');
  assert.equal(matches[1].date, '2026-09-23', 'the zero-vector date ranks last');
  assert.equal(matches[1].score, 0, 'the zero-vector date scores 0');
  assert.ok(!matches.some(m => m.date === '2026-09-22'), 'the target never matches itself');

  // --- nextDayMove: percent move to the next manifest date, null at the edges. ---
  h = harness({ chainRows: dateStr => {
    const spot = { '2026-09-21': 600, '2026-09-22': 606, '2026-09-23': 600 }[dateStr];
    return [{ Expiration: dateStr, Type: 'call', Strike: spot, UnderlyingPrice: spot, OpenInterest: 1 }];
  } });
  assert.equal(await call(h, "nextDayMove('2026-09-21')"), 1, '600 -> 606 is a +1% move');
  assert.equal(await call(h, "nextDayMove('2026-09-23')"), null, 'the last manifest date has no next day');
  assert.equal(await call(h, "nextDayMove('2026-09-20')"), null, 'a date outside the manifest has no next day');
  h = harness({ chainRows: () => [{ Expiration: '2026-09-21', Type: 'call', Strike: 600 }] });
  assert.equal(await call(h, "nextDayMove('2026-09-21')"), null, 'a chain with no spot has no next-day move');

  console.log('sidebar widgets: hash pinned, extraction verified, intraday cache, daily window, feature vector, similarity and next-day move passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
