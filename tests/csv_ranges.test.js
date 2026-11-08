// B3 slice 1 (Nov 2026): parseCsv / parseRangesCsv / getThresh / oiBucket
// moved verbatim out of site/shared.js into site/shared/csv-ranges.js.
// Pins the module's ?v= hash in the page, asserts the four functions no
// longer live in shared.js, pins the entry's export list (exactly the same
// 26 names as before the split — the four helpers stay browser globals by
// design and are NOT re-exported), and drives the moved code directly.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'csv-ranges.js'), 'utf8');
const csvRanges = require('../site/shared/csv-ranges.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/csv-ranges.js?v=' + moduleHash),
  'csv-ranges script URL must match its content hash on every hosting path');

for (const name of ['parseCsv', 'parseRangesCsv', 'getThresh', 'oiBucket']) {
  assert.ok(!shared.includes(`function ${name}(`), `${name} must no longer live in shared.js`);
  assert.equal(typeof csvRanges[name], 'function', `csv-ranges.js must export ${name}`);
  assert.equal(entry[name], undefined, `${name} stays a browser global — the entry must not re-export it`);
}

// The entry's export list is the behavior evidence for the split: exactly the
// same 26 names shared.js exported before B3 slice 1.
assert.deepEqual(Object.keys(entry).sort(), [
  'CHAIN_UNDERLYINGS', 'ChainSelection', 'LiveQuotePoller', 'LiveQuoteService',
  'SHARE_QUOTE_MAX_AGE_MS', 'TickerStateStore', 'chainLatestPath',
  'computeAtmWindow', 'describeSqueezeAcquisitionFailure', 'findRevision',
  'fmtSteoDelta', 'formatMomentum', 'formatSqueezeScanStatus',
  'formatSqueezeScheduleStatus', 'formatSqueezeFirstSeen', 'formatVwapRvol',
  'freshShareQuote', 'isTradeableShareSymbol', 'normalizePaperOrder',
  'normalizeShareOrder', 'occRoot', 'parseRetryAfter', 'relativeOiRanges',
  'summarizeSpyHealth', 'tickerSessionState', 'validateChainPayload',
].sort());

const { parseCsv, parseRangesCsv, getThresh, oiBucket } = csvRanges;

// parseCsv: headers trimmed, missing trailing values read as ''.
assert.deepEqual(parseCsv('Tier,StrikeOffset,Call_p25\n0DTE_Regular,0,100\n0DTE_Weekly,-1,'), [
  { Tier: '0DTE_Regular', StrikeOffset: '0', Call_p25: '100' },
  { Tier: '0DTE_Weekly', StrikeOffset: '-1', Call_p25: '' },
]);

// parseRangesCsv: tier/offset structure, adj defaults to 1.0 when blank.
const ranges = parseRangesCsv(
  'Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj\n' +
  '0DTE_Regular,0,100,200,300,400,100,200,300,400,,\n' +
  '0DTE_Weekly,0,100,200,300,400,100,200,300,400,1.5,0.5\n');
assert.deepEqual(ranges['0DTE_Regular'][0].call, [100, 200, 300, 400]);
assert.equal(ranges['0DTE_Regular'][0].call_adj, 1.0);
assert.equal(ranges['0DTE_Regular'][0].put_adj, 1.0);
assert.equal(ranges['0DTE_Weekly'][0].call_adj, 1.5);

// getThresh: the regular tier returns the base; other tiers scale by adj;
// unknown tier/offset or missing ranges read as all zeros.
assert.deepEqual(getThresh(ranges, '0DTE_Regular', 0, 'call'), [100, 200, 300, 400]);
assert.deepEqual(getThresh(ranges, '0DTE_Weekly', 0, 'call'), [150, 300, 450, 600]);
assert.deepEqual(getThresh(ranges, '0DTE_Weekly', 0, 'put'), [50, 100, 150, 200]);
assert.deepEqual(getThresh(ranges, '0DTE_Monthly', 0, 'call'), [100, 200, 300, 400]);
assert.deepEqual(getThresh(ranges, '0DTE_Regular', 99, 'call'), [0, 0, 0, 0]);
assert.deepEqual(getThresh(null, '0DTE_Regular', 0, 'call'), [0, 0, 0, 0]);

// oiBucket: 0 is its own bucket; the p25/p50/p75/p90 cuts give 1–5.
const thresh = [100, 200, 300, 400];
assert.equal(oiBucket(0, thresh), 0);
assert.equal(oiBucket(50, thresh), 1);
assert.equal(oiBucket(100, thresh), 2);
assert.equal(oiBucket(250, thresh), 3);
assert.equal(oiBucket(350, thresh), 4);
assert.equal(oiBucket(400, thresh), 5);
assert.equal(oiBucket(9000, thresh), 5);
