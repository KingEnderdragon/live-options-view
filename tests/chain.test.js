// B3 slice 5 (Nov 2026): chain underlying selection (#121) moved verbatim
// out of site/shared.js into site/shared/chain.js. Pins the module's ?v=
// hash in the page, asserts the seven names and CHAIN_LATEST_PATHS no longer
// live in shared.js, pins the entry's export surface (the seven names are
// still re-exported — the same function objects / class — while
// CHAIN_LATEST_PATHS stays a browser global by design, as the csv-ranges and
// heatmap constants did), and drives the moved code directly. The DISPLAY
// subtlety is covered too: relativeOiRanges defaults its display to the
// heatmap concern's DISPLAY (20), which chain.js provides itself under Node
// via its guarded require — the entry no longer sets globalThis.DISPLAY.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'chain.js'), 'utf8');
const chain = require('../site/shared/chain.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/chain.js?v=' + moduleHash),
  'chain script URL must match its content hash on every hosting path');

const NAMES = ['CHAIN_UNDERLYINGS', 'chainLatestPath', 'occRoot', 'validateChainPayload',
  'ChainSelection', 'relativeOiRanges', 'summarizeSpyHealth'];
for (const decl of ['const CHAIN_UNDERLYINGS =', 'function chainLatestPath(', 'function occRoot(',
  'function validateChainPayload(', 'class ChainSelection', 'function relativeOiRanges(',
  'function summarizeSpyHealth(', 'const CHAIN_LATEST_PATHS =']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
}
for (const name of NAMES) {
  assert.ok(chain[name] !== undefined, `chain.js must export ${name}`);
  assert.equal(entry[name], chain[name], `the entry must re-export the moved ${name} itself`);
}
assert.equal(entry.CHAIN_LATEST_PATHS, undefined, 'CHAIN_LATEST_PATHS stays a browser global');
assert.equal(chain.CHAIN_LATEST_PATHS, undefined, 'CHAIN_LATEST_PATHS is not a Node export either');

// --- chainLatestPath / occRoot: the #121 routing. ---
assert.deepEqual(chain.CHAIN_UNDERLYINGS, ['QQQ', 'SPY']);
assert.equal(chain.chainLatestPath('QQQ'), 'intraday/latest.json');
assert.equal(chain.chainLatestPath('SPY'), 'intraday/spy/latest.json');
assert.equal(chain.chainLatestPath('AAPL'), null);
assert.equal(chain.occRoot('QQQ260116C00600000'), 'QQQ');
assert.equal(chain.occRoot(' SPY 260116C00600000'), 'SPY');
assert.equal(chain.occRoot(''), '');
assert.equal(chain.occRoot(null), '');

// --- validateChainPayload: a snapshot can never render under the wrong heading. ---
{
  const qqq = { symbol: 'QQQ', rows: [{ OptionSymbol: 'QQQ260116C00600000' }] };
  assert.deepEqual(chain.validateChainPayload(qqq, 'QQQ'), { ok: true, reason: null });
  assert.deepEqual(chain.validateChainPayload({ rows: [{ OptionSymbol: 'QQQ260116C00600000' }] }, 'QQQ'),
    { ok: true, reason: null }, 'a pre-#121 QQQ payload carries no top-level symbol');
  assert.equal(chain.validateChainPayload(qqq, 'SPY').ok, false, 'a QQQ snapshot is not a SPY snapshot');
  assert.equal(chain.validateChainPayload({ symbol: 'QQQ', rows: [] }, 'AAPL').ok, false);
  assert.equal(chain.validateChainPayload({ symbol: 'QQQ' }, 'QQQ').ok, false, 'no rows is malformed');
  assert.equal(chain.validateChainPayload({ symbol: 'QQQ', rows: [{ OptionSymbol: 'SPY260116C00600000' }] }, 'QQQ').ok,
    false, 'a mixed snapshot is rejected');
  assert.match(chain.validateChainPayload({ symbol: 'QQQ', rows: [], underlying_market: { symbol: 'SPY' } }, 'QQQ').reason,
    /belong to SPY/);
}

// --- ChainSelection: a slow reply for the old selection cannot overwrite the new one. ---
{
  const sel = new chain.ChainSelection();
  assert.equal(sel.symbol, 'QQQ');
  assert.equal(new chain.ChainSelection('SPY').symbol, 'SPY');
  assert.equal(new chain.ChainSelection('AAPL').symbol, 'QQQ', 'an unsupported symbol falls back to QQQ');
  assert.equal(sel.select('SPY'), true);
  assert.equal(sel.symbol, 'SPY');
  assert.equal(sel.generation, 1);
  assert.equal(sel.select('SPY'), true);
  assert.equal(sel.generation, 1, 'selecting the same underlying does not bump the generation');
  assert.equal(sel.select('AAPL'), false);
  const token = sel.token();
  assert.equal(sel.accepts(token), true);
  assert.equal(sel.accepts({ symbol: 'SPY', generation: 0 }), false, 'a stale generation is rejected');
  assert.equal(sel.accepts({ symbol: 'QQQ', generation: 1 }), false, 'a token for the other underlying is rejected');
  assert.equal(sel.accepts(null), false);
}

// --- relativeOiRanges: SPY has no QQQ calibration, so heat is relative to its own snapshot. ---
{
  assert.equal(globalThis.DISPLAY, 20, 'requiring chain.js sets the DISPLAY global under Node — the bare default read resolves to it');
  assert.equal(chain.relativeOiRanges([]), null);
  assert.equal(chain.relativeOiRanges([{ OpenInterest: 0 }]), null);
  const rows = [{ OpenInterest: 100 }, { OpenInterest: 400 }, { OpenInterest: 900 }, { OpenInterest: 1600 }, { OpenInterest: 2500 }];
  const ranges = chain.relativeOiRanges(rows);
  assert.equal(Object.keys(ranges['0DTE_Regular']).length, 41, 'the default display is the heatmap DISPLAY of 20');
  assert.deepEqual(ranges['0DTE_Regular'][0], { call: [400, 900, 1600, 1600], put: [400, 900, 1600, 1600] },
    'thresholds are the p25/p50/p75/p90 of this snapshot’s own nonzero OI');
  const narrow = chain.relativeOiRanges(rows, 2);
  assert.equal(Object.keys(narrow['0DTE_Regular']).length, 5, 'an explicit display still works');
}

// --- summarizeSpyHealth: the SPY lane’s freshness, judged on its own clock. ---
{
  const now = Date.parse('2026-09-23T13:00:30Z');
  assert.deepEqual(chain.summarizeSpyHealth({}, now),
    { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY health has not been published' });
  const live = { updated_at: '2026-09-23T13:00:00Z', process_start_time: '2026-09-23T12:00:00Z',
    spy: { collection_status: 'collecting', last_snapshot_upload_time: '2026-09-23T12:59:00Z', spot: { status: 'live' } } };
  assert.deepEqual(chain.summarizeSpyHealth(live, now),
    { state: 'live', text: 'SPY: live', detail: 'SPY snapshot and spot are current' });
  assert.equal(chain.summarizeSpyHealth(live, Date.parse('2026-09-23T13:02:00Z')).state, 'stale',
    'an update older than 45s is stale');
  assert.equal(chain.summarizeSpyHealth({ ...live, updated_at: '2026-09-23T13:02:00Z' }, now).state, 'stale',
    'an update from the future is stale too');
  assert.deepEqual(chain.summarizeSpyHealth({ ...live, spy: { ...live.spy, collection_status: 'paused', reason: 'paused by operator' } }, now),
    { state: 'unavailable', text: 'SPY: paused', detail: 'paused by operator' });
  const noSnap = { ...live, spy: { collection_status: 'collecting', last_snapshot_upload_time: null } };
  assert.deepEqual(chain.summarizeSpyHealth({ ...noSnap, process_start_time: '2026-09-23T12:56:00Z' }, now),
    { state: 'unavailable', text: 'SPY: unavailable', detail: 'No SPY snapshot uploaded' });
  assert.deepEqual(chain.summarizeSpyHealth({ ...noSnap, process_start_time: '2026-09-23T12:59:30Z' }, now),
    { state: 'connecting', text: 'SPY: warming up', detail: 'No SPY snapshot uploaded yet' });
  assert.equal(chain.summarizeSpyHealth({ ...live, spy: { ...live.spy, last_snapshot_upload_time: '2026-09-23T12:56:00Z' } }, now).state,
    'stale', 'a snapshot older than 3 minutes is stale');
  assert.equal(chain.summarizeSpyHealth({ ...live, spy: { ...live.spy, spot: { status: 'stale' } } }, now).state, 'stale');
  assert.deepEqual(chain.summarizeSpyHealth({ ...live, spy: { ...live.spy, spot: { status: 'connecting' } } }, now),
    { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY spot has no current provider observation' });
}
