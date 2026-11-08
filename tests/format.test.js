// B3 slice 4 (Nov 2026): the panel status formatters (formatVwapRvol /
// formatMomentum, the short-squeeze scanner formatters, fmtSteoDelta /
// findRevision) and the order validators (normalizePaperOrder /
// normalizeShareOrder) moved verbatim out of docs/shared.js into
// docs/shared/format.js. Pins the module's ?v= hash in the page, asserts the
// ten functions and two constants no longer live in shared.js, pins the
// entry's export surface (the ten functions are still re-exported — the same
// function objects — while the two staleness constants stay browser globals
// by design), and drives the moved code directly.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'docs', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'docs', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'docs', 'shared', 'format.js'), 'utf8');
const format = require('../docs/shared/format.js');
const entry = require('../docs/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/format.js?v=' + moduleHash),
  'format script URL must match its content hash on every hosting path');

const FUNCTIONS = ['formatVwapRvol', 'formatMomentum', 'formatSqueezeScanStatus', 'formatSqueezeScheduleStatus',
  'describeSqueezeAcquisitionFailure', 'formatSqueezeFirstSeen', 'fmtSteoDelta', 'findRevision',
  'normalizePaperOrder', 'normalizeShareOrder'];
for (const name of FUNCTIONS) {
  assert.ok(!shared.includes(`function ${name}(`), `${name} must no longer live in shared.js`);
  assert.equal(typeof format[name], 'function', `format.js must export ${name}`);
  assert.equal(entry[name], format[name], `the entry must re-export the moved ${name} itself`);
}
for (const decl of ['const VWAP_RVOL_CLOSED_AFTER_MS = 4 * 60 * 60 * 1000;',
  'const SQUEEZE_SCAN_STALE_AFTER_MS = 6 * 60 * 60 * 1000;']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
  assert.equal(entry[decl.slice(6, decl.indexOf(' ='))], undefined, 'a staleness constant stays a browser global');
}
assert.equal(format.VWAP_RVOL_CLOSED_AFTER_MS, 4 * 60 * 60 * 1000, 'VWAP closes 4h after the last observation');
assert.equal(format.SQUEEZE_SCAN_STALE_AFTER_MS, 6 * 60 * 60 * 1000, 'a scan goes stale after 6h');

// --- formatVwapRvol / formatMomentum: the same values the price strip renders. ---
assert.deepEqual(format.formatVwapRvol(null), { text: 'VWAP/RVOL: unavailable', state: 'fallback' });
assert.deepEqual(format.formatMomentum(null), { text: 'Momentum: unavailable', state: 'fallback' });
{
  const um = { spot_ts: '2026-09-18T20:00:00Z', vwap: 600.5, price_vs_vwap_pct: 0.4,
    rvol: { status: 'ok', multiple: 1.2 }, freshness: 'live' };
  assert.deepEqual(format.formatVwapRvol(um, Date.parse('2026-09-18T20:30:00Z')),
    { text: 'VWAP 600.50 (+0.40%) | RVOL 1.20×', state: 'live' });
  assert.deepEqual(format.formatVwapRvol(um, Date.parse('2026-09-19T01:00:00Z')),
    { text: 'VWAP/RVOL: session closed', state: 'closed' });
  assert.equal(format.formatVwapRvol({ ...um, rvol: { status: 'insufficient_history' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'VWAP 600.50 (+0.40%) | RVOL warming up');
  assert.equal(format.formatVwapRvol({ ...um, vwap: null }, Date.parse('2026-09-18T20:30:00Z')).text,
    'VWAP — | RVOL 1.20×');
  const mom = { spot_ts: '2026-09-18T20:00:00Z', freshness: 'live',
    momentum: { status: 'ok', return_pct: 0.4, direction: 'up', lookback_minutes: 30 } };
  assert.deepEqual(format.formatMomentum(mom, Date.parse('2026-09-18T20:30:00Z')),
    { text: 'Momentum ▲ +0.40% (30m)', state: 'live' });
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'warming_up' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum warming up');
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'stale_anchor' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum stale (gap in data)');
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'other' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum —');
}

// --- fmtSteoDelta / findRevision: the STEO panel deltas. ---
assert.deepEqual(format.fmtSteoDelta(null), { text: '', cls: '' });
assert.deepEqual(format.fmtSteoDelta(2), { text: '+2.00', cls: 'up' });
assert.deepEqual(format.fmtSteoDelta(-1.5), { text: '-1.50', cls: 'down' });
assert.deepEqual(format.fmtSteoDelta(0), { text: '+0.00', cls: '' });
assert.equal(format.findRevision([{ period: '2026-10', brent_delta: 2 }], '2026-10').brent_delta, 2);
assert.equal(format.findRevision([{ period: '2026-10' }], '2026-11'), null);

// --- squeeze formatters: freshness is judged on acquisition time. ---
assert.deepEqual(format.formatSqueezeScanStatus(null), { text: 'No scan published yet', state: 'fallback' });
{
  const pointer = { status: 'complete', finished_at: '2026-09-23T13:00:13Z', started_at: '2026-09-23T13:00:03Z' };
  assert.deepEqual(format.formatSqueezeScanStatus(pointer, Date.parse('2026-09-23T14:00:00Z')),
    { text: 'Scan 9:00 AM ET', state: 'live' });
  assert.deepEqual(format.formatSqueezeScanStatus(pointer, Date.parse('2026-09-23T20:30:00Z')),
    { text: 'Scan 9:00 AM ET (stale)', state: 'stale' });
  assert.deepEqual(format.formatSqueezeScanStatus({ ...pointer, status: 'empty' }, Date.parse('2026-09-23T14:00:00Z')),
    { text: 'Scan complete 9:00 AM ET — no eligible candidates', state: 'stale' });
  const bad = format.formatSqueezeScheduleStatus(null, null, null, null, Date.parse('2026-09-23T14:00:00Z'));
  assert.equal(bad.text, 'No scan published yet');
  assert.equal(bad.state, 'stale');
  assert.equal(bad.warning, 'Schedule calendar unavailable or outside its coverage. Freshness is unconfirmed.');
  assert.equal(format.describeSqueezeAcquisitionFailure(null, null), null);
  assert.equal(format.describeSqueezeAcquisitionFailure(pointer, { ...pointer, status: 'complete' }), null);
  const failed = format.describeSqueezeAcquisitionFailure(pointer,
    { status: 'failed', started_at: '2026-09-23T17:00:00Z' });
  assert.match(failed, /did not complete: failed/);
  assert.equal(format.describeSqueezeAcquisitionFailure(pointer,
    { status: 'failed', started_at: '2026-09-23T12:00:00Z' }), null, 'an older failure stays invisible');
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: null, is_new: null }),
    { text: 'unavailable', isNew: false });
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: '2026-09-23T13:00:56Z', is_new: true }),
    { text: '9:00 AM', isNew: true });
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: '2026-09-23T13:00:56Z', is_new: false }),
    { text: '9:00 AM', isNew: false });
}

// --- order validation: the trade ticket's gate. ---
assert.deepEqual(format.normalizePaperOrder({ side: 'buy', quantity: 10 }),
  { value: { side: 'buy', qty: 10, order_type: 'market' } });
assert.deepEqual(format.normalizePaperOrder({ side: 'buy', quantity: 2, orderType: 'limit', limitPrice: 10.25 }),
  { value: { side: 'buy', qty: 2, order_type: 'limit', limit_price: 10.25 } });
assert.equal(format.normalizePaperOrder({ side: 'hold', quantity: 1 }).error, 'Choose buy or sell.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 0 }).error, 'Quantity must be a positive whole number.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: 0 }).error,
  'Limit price must be greater than zero.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: 10.005 }).error,
  'Limit price must use increments of $0.01.');
assert.equal(format.normalizeShareOrder({ side: 'sell', quantity: 5, heldQuantity: 2 }).error,
  'You cannot sell more shares than you hold.');
assert.deepEqual(format.normalizeShareOrder({ side: 'sell', quantity: 2, heldQuantity: 2 }),
  { value: { side: 'sell', qty: 2, order_type: 'market' } });
