// Offline tests for the adaptive quote-card model (#179): store normalization,
// session selection, movers ranking and pin persistence. No browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'site', 'index.html'), 'utf8');
const instrumentsSource = fs.readFileSync(path.join(root, 'site', 'shared', 'instruments.js'), 'utf8');
const panelSource = fs.readFileSync(path.join(root, 'site', 'shared', 'quote-panel.js'), 'utf8');

function load() {
  const module = { exports: {} };
  const context = vm.createContext({ console, module, Intl, Date, JSON, Math, Number, Object, Array, Set, Map, String });
  vm.runInContext(instrumentsSource, context, { filename: 'site/shared/instruments.js' });
  const instruments = module.exports;
  module.exports = {};
  vm.runInContext(panelSource, context, { filename: 'site/shared/quote-panel.js' });
  const model = module.exports;
  return { instruments, model };
}

const { instruments, model } = load();
const NOW = Date.parse('2026-11-09T15:00:00Z'); // Monday 10:00 ET

function row(label, overrides = {}) {
  const instrument = instruments.QUOTE_INSTRUMENTS[label];
  return {
    instrument_id: instrument?.id,
    price: 100,
    prev_close: 99,
    chg_pct: 1.01,
    source: 'dxlink',
    source_symbol: instrument?.label,
    price_basis: 'last_trade',
    observed_at: new Date(NOW - 10_000).toISOString(),
    collector_received_at: new Date(NOW - 5_000).toISOString(),
    quote_ts: new Date(NOW - 5_000).toISOString(),
    observation_granularity: 'event',
    observation_timezone: 'UTC',
    session: 'regular',
    market_session: 'regular',
    adjustment_basis: 'unknown',
    reference: { instrument_id: instrument?.id, value: 99, source: 'dxlink', price_basis: 'previous_close' },
    ...overrides,
  };
}

function payload(rows, { lane = null, timestamp = new Date(NOW).toISOString() } = {}) {
  return { schema_version: 2, universe_version: 'market-overview-100-v1', timestamp, feed_stale: false, prices: rows, price_lane: lane };
}

function snapshotWith(rows) {
  const store = new model.QuoteStore({ nowFn: () => NOW });
  store.mergePayload(payload(rows));
  return store.snapshot();
}

function storage(initial = null) {
  const data = new Map(initial === null ? [] : [[model.QUOTE_PIN_STORAGE_KEY, initial]]);
  return {
    getItem: key => data.has(key) ? data.get(key) : null,
    setItem: (key, value) => data.set(key, value),
    value: () => data.get(model.QUOTE_PIN_STORAGE_KEY),
  };
}

const labels = list => Array.from(list, item => item.label);

// ── store and session ────────────────────────────────────────────────────────
{
  const store = new model.QuoteStore({ nowFn: () => NOW });
  const ok = store.mergePayload(payload({
    QQQ: row('QQQ', { price: 656.2 }),
    'BTC/USD': row('BTC/USD', { price: 90000, rolling_change_pct: 5.88, market_session: 'continuous', session: 'continuous',
      rolling_reference: { value: 85000, observed_at: new Date(NOW - 86400000).toISOString(), source: 'rolling_history', price_basis: 'rolling_24h' },
      change_basis: 'rolling_24h' }),
  }));
  assert.equal(ok, true);
  const snapshot = store.snapshot();
  assert.equal(snapshot.rows.length, 2);
  assert.equal(store.row('QQQ').price, 656.2);
  assert.equal(store.row('QQQ').state, 'live');
  const btc = store.row('BTC/USD');
  assert.equal(btc.changePct, 5.88, 'crypto uses the rolling 24h change');
  assert.equal(btc.changeBasis, 'rolling_24h');
  assert.equal(btc.session, 'continuous');
}

{
  const snapshot = snapshotWith({
    QQQ: row('QQQ', { market_session: 'premarket', session: 'extended' }),
    'BTC/USD': row('BTC/USD', { market_session: 'continuous', session: 'continuous' }),
  });
  const mode = model.quoteSessionMode({ ...snapshot, meta: { ...snapshot.meta, lane: null } });
  assert.equal(mode.mode, 'equities', 'row sessions infer the active group');
  const produced = model.quoteSessionMode({ ...snapshot, meta: { ...snapshot.meta, lane: { mode: 'futures' } } });
  assert.equal(produced.mode, 'futures', 'the producer lane mode wins');
  assert.equal(produced.source, 'producer');
}

{
  // Retained rows age from the current clock, including partial payloads and
  // outages that never produce another row for the instrument.
  const clock = [NOW];
  const store = new model.QuoteStore({ nowFn: () => clock[0] });
  store.mergePayload(payload({
    AAPL: row('AAPL', { observed_at: new Date(NOW - 10_000).toISOString() }),
    MSFT: row('MSFT', { observed_at: new Date(NOW - 10_000).toISOString() }),
  }));
  let snapshot = store.snapshot();
  assert.equal(snapshot.byLabel.get('AAPL').ageMs, 10_000);
  assert.equal(snapshot.byLabel.get('AAPL').state, 'live');
  assert.equal(model.computeMovers(snapshot, { mode: 'equities' }).eligible, 2);

  clock[0] += 60 * 60_000;
  snapshot = store.snapshot();
  const aged = snapshot.byLabel.get('AAPL');
  assert.equal(aged.ageMs, 10_000 + 60 * 60_000, 'age advances without another payload');
  assert.equal(aged.state, 'stale', 'a retained row cannot stay live for an hour');
  assert.equal(model.computeMovers(snapshot, { mode: 'equities' }).eligible, 0,
    'expired rows leave the movers list');

  // A partial publication for another instrument must not reset AAPL's age.
  store.mergePayload(payload({ MSFT: row('MSFT', { observed_at: new Date(clock[0] - 10_000).toISOString() }) }));
  snapshot = store.snapshot();
  assert.equal(snapshot.byLabel.get('AAPL').state, 'stale');
  assert.equal(snapshot.byLabel.get('MSFT').state, 'live');
}

{
  // A receipt time is not a provider observation. The compact registry's
  // quote_ts may pair an old last trade with a newer bid/ask; that must never
  // freshen the row, and a fresh provider event must not erase failure flags.
  const store = new model.QuoteStore({ nowFn: () => NOW });
  store.mergePayload(payload({
    AAPL: row('AAPL', {
      observed_at: new Date(NOW - 60 * 60_000).toISOString(),
      refresh_error: 'timeout',
    }),
  }));
  assert.equal(store.snapshot().byLabel.get('AAPL').state, 'stale');
  const receiptOnly = { symbol: 'AAPL', price: 999, source: 'dxlink', quote_ts: new Date(NOW).toISOString() };
  assert.equal(store.mergeTickers([receiptOnly]), false, 'a receipt-only ticker is ignored');
  assert.equal(store.row('AAPL').price, 100);
  assert.equal(store.row('AAPL').observedAt, new Date(NOW - 60 * 60_000).toISOString());

  store.mergeTickers([{ symbol: 'AAPL', price: 1, source: 'r2-snapshot', observedAt: new Date(NOW).toISOString() }]);
  assert.equal(store.row('AAPL').price, 100, 'fallback replay is ignored');

  const providerEvent = { symbol: 'AAPL', price: 101, source: 'dxlink',
    observed_at: new Date(NOW - 1000).toISOString() };
  assert.equal(store.mergeTickers([providerEvent]), true);
  const updated = store.row('AAPL');
  assert.equal(updated.price, 101);
  assert.equal(updated.observedAt, providerEvent.observed_at, 'provider time, not receipt time, is retained');
  assert.equal(updated.refreshError, 'timeout', 'a new observation does not erase a producer failure flag');
  assert.equal(updated.state, 'stale');
}

{
  // Session transitions: producer mode drives the active group; a maintenance
  // break has no eligible futures movers; DST does not change the wall clock.
  const maintenance = snapshotWith({
    '/GC': row('/GC', { market_session: 'maintenance', session: 'maintenance', chg_pct: 2 }),
  });
  const mode = model.quoteSessionMode({ ...maintenance, meta: { ...maintenance.meta, lane: null } });
  assert.equal(mode.mode, 'futures');
  assert.deepEqual(labels(model.computeMovers(maintenance, { mode: 'futures' }).up), []);
  assert.equal(model.clockFallbackMode(Date.parse('2026-11-14T17:00:00Z')), 'crypto', 'Saturday is crypto');
  assert.equal(model.clockFallbackMode(Date.parse('2026-11-09T09:30:00Z')), 'equities', 'Monday premarket is equities');
  assert.equal(model.clockFallbackMode(Date.parse('2026-11-09T07:00:00Z')), 'futures', 'overnight is futures');
  assert.equal(model.clockFallbackMode(Date.parse('2026-11-14T01:30:00Z')), 'crypto', 'Friday after 20:00 ET is crypto');
}

// ── movers ───────────────────────────────────────────────────────────────────
{
  const snapshot = snapshotWith({
    AAPL: row('AAPL', { price: 110, prev_close: 100, chg_pct: 10 }),
    MSFT: row('MSFT', { price: 105, prev_close: 100, chg_pct: 5 }),
    NVDA: row('NVDA', { price: 101, prev_close: 100, chg_pct: 1 }),
    TSLA: row('TSLA', { price: 95, prev_close: 100, chg_pct: -5 }),
    META: row('META', { price: 90, prev_close: 100, chg_pct: -10 }),
    AMZN: row('AMZN', { price: 99, prev_close: 100, chg_pct: -1 }),
    '10Y': row('10Y', { price: 4.1, prev_close: 4.0, chg_pct: 2.5, market_session: 'regular' }),
  });
  const movers = model.computeMovers(snapshot, { mode: 'equities', count: 2 });
  assert.deepEqual(labels(movers.up), ['AAPL', 'MSFT']);
  assert.deepEqual(labels(movers.down), ['META', 'TSLA']);
  assert.equal(movers.eligible, 6, 'yield rows never rank with equities');
  assert.equal(movers.label, 'US equities');

  const all = model.computeMovers(snapshot, { mode: 'equities', count: 3 });
  assert.deepEqual(labels(all.up), ['AAPL', 'MSFT', 'NVDA']);
  assert.deepEqual(labels(all.down), ['META', 'TSLA', 'AMZN']);
}

{
  // Deterministic ties: equal change orders by label, and negative-only rows
  // never pad the gainers list.
  const snapshot = snapshotWith({
    MSFT: row('MSFT', { price: 105, prev_close: 100, chg_pct: 5 }),
    AAPL: row('AAPL', { price: 105, prev_close: 100, chg_pct: 5 }),
    NVDA: row('NVDA', { price: 95, prev_close: 100, chg_pct: -5 }),
  });
  const movers = model.computeMovers(snapshot, { mode: 'equities', count: 3 });
  assert.deepEqual(labels(movers.up), ['AAPL', 'MSFT']);
  assert.deepEqual(labels(movers.down), ['NVDA']);
}

{
  // Stale, retained, future, invalid-reference and inactive-session rows are
  // excluded instead of zero-filled.
  const snapshot = snapshotWith({
    AAPL: row('AAPL', { chg_pct: 4 }),
    MSFT: row('MSFT', { chg_pct: 5, stale: true }),
    NVDA: row('NVDA', { chg_pct: 6, source: 'last-known', stale: true }),
    AMZN: row('AMZN', { chg_pct: 7, observed_at: new Date(NOW + 60000).toISOString() }),
    META: row('META', { chg_pct: 8, prev_close: null }),
    TSLA: row('TSLA', { chg_pct: 9, market_session: 'closed', session: 'closed' }),
    GOOGL: row('GOOGL', { chg_pct: 10, refresh_error: 'timeout' }),
  });
  const movers = model.computeMovers(snapshot, { mode: 'equities', count: 3 });
  assert.deepEqual(labels(movers.up), ['AAPL']);
  assert.equal(movers.eligible, 1);
}

{
  // Crypto ranks only with a rolling reference; futures only with a matching
  // contract reference basis.
  const snapshot = snapshotWith({
    'BTC/USD': row('BTC/USD', { price: 100, market_session: 'continuous', session: 'continuous', rolling_change_pct: 2, change_basis: 'rolling_24h',
      rolling_reference: { value: 98, observed_at: new Date(NOW - 86400000).toISOString(), source: 'rolling_history', price_basis: 'rolling_24h' } }),
    'ETH/USD': row('ETH/USD', { price: 50, market_session: 'continuous', session: 'continuous', rolling_change_pct: null, change_basis: 'unavailable', chg_pct: 9 }),
    '/CL': row('/CL', { price: 70, market_session: 'overnight', session: 'overnight', chg_pct: 1, change_basis: 'contract_reference_unverified', prev_close: 69 }),
    '/GC': row('/GC', { price: 2100, market_session: 'overnight', session: 'overnight', chg_pct: 3, change_basis: 'prior_day_close_same_contract', prev_close: 2039 }),
  });
  const crypto = model.computeMovers(snapshot, { mode: 'crypto' });
  assert.deepEqual(labels(crypto.up), ['BTC/USD'], 'crypto does not borrow a daily change');
  const futures = model.computeMovers(snapshot, { mode: 'futures' });
  assert.deepEqual(labels(futures.up), ['/GC'], 'an unresolved futures contract cannot rank');
}

// ── pins ─────────────────────────────────────────────────────────────────────
{
  const backing = storage();
  const pins = new model.QuotePins({ storage: backing });
  assert.deepEqual(Array.from(pins.list()), [...instruments.QUOTE_DEFAULT_PINS]);
  assert.equal(pins.add('QQQ').reason, 'core_instrument');
  assert.equal(pins.add('SMH').reason, 'already_pinned');
  assert.equal(pins.add('NOTREAL').reason, 'unknown_instrument');
  assert.equal(pins.add('NFLX').ok, true);
  assert.equal(pins.move('NFLX', -1).ok, true);
  assert.equal(backing.value(), JSON.stringify(pins.list()), 'every change persists');
  const reloaded = new model.QuotePins({ storage: backing });
  assert.deepEqual(Array.from(reloaded.list()), Array.from(pins.list()), 'order survives reload');
  assert.equal(reloaded.persistent, true);
  assert.equal(reloaded.remove('NFLX').ok, true);
  assert.equal(reloaded.remove('NFLX').reason, 'not_pinned');
  assert.equal(reloaded.move('SMH', -1).reason, 'edge');
}

{
  // Unknown and duplicate stored values are dropped without losing the rest;
  // a corrupt value falls back to defaults rather than throwing.
  const pins = new model.QuotePins({ storage: storage(JSON.stringify(['AAPL', 'AAPL', 'QQQ', 'REMOVED', 'MSFT'])) });
  assert.deepEqual(Array.from(pins.list()), ['AAPL', 'MSFT']);
  assert.deepEqual(Array.from(pins.dropped), ['AAPL', 'QQQ', 'REMOVED']);
  const corrupt = new model.QuotePins({ storage: storage('{not json') });
  assert.deepEqual(Array.from(corrupt.list()), [...instruments.QUOTE_DEFAULT_PINS]);
}

{
  const pins = new model.QuotePins({ storage: storage('[]'), limit: 2 });
  assert.equal(pins.add('NFLX').ok, true);
  assert.equal(pins.add('NVDA').ok, true);
  assert.equal(pins.add('AMD').reason, 'pin_limit');
}

{
  // No storage is NVDAclosed as visit-only, not silently claimed as saved.
  const pins = new model.QuotePins({ storage: null });
  assert.equal(pins.persistent, false);
  assert.equal(pins.add('NFLX').ok, true);
}

// ── formatting and candidate search ──────────────────────────────────────────
{
  assert.equal(model.formatQuotePercent(1.006), '+1.01%');
  assert.equal(model.formatQuotePercent(-1.006), '-1.01%');
  assert.equal(model.formatQuotePercent(null), '—');
  assert.equal(model.formatQuoteAge(65_000), '1m');
  assert.equal(model.formatQuotePrice({ label: 'JPY/USD', price: 0.0067, asset_class: 'fx', unit: 'USD_per_JPY' }), '¥149.25');
}

{
  const store = new model.QuoteStore({ nowFn: () => NOW });
  store.mergePayload(payload({
    QQQ: row('QQQ'),
    NFLX: row('NFLX', { name: 'Netflix, Inc.' }),
    NVDA: row('NVDA', { name: 'The Walt NVDAney Company' }),
  }));
  const pins = new model.QuotePins({ storage: storage('[]') });
  assert.deepEqual(labels(model.quoteCandidates(store.snapshot(), pins, 'net')), ['NFLX']);
  assert.ok(model.quoteCandidates(store.snapshot(), pins, '').every(row => row.label !== 'QQQ'), 'core instruments are already always visible');
}

// ── page packaging ───────────────────────────────────────────────────────────
{
  for (const id of ['qp-core', 'qp-pins', 'qp-movers-up', 'qp-movers-down', 'qp-pin-dialog', 'qp-pin-search', 'qp-announce']) {
    assert.ok(html.includes(`id="${id}"`), `page must contain #${id}`);
  }
  for (const file of ['shared/instruments.js', 'shared/quote-panel.js', 'js/quote-panel.js']) {
    const source = fs.readFileSync(path.join(root, 'site', file), 'utf8');
    const hash = createHash('sha256').update(source).digest('hex').slice(0, 16);
    assert.ok(html.includes(`${file}?v=${hash}`), `${file} hash must match the page`);
  }
  assert.ok(html.indexOf('shared/instruments.js') < html.indexOf('shared/quote-panel.js'), 'instruments load first');
  assert.ok(html.indexOf('src="js/quote-panel.js') < html.indexOf('src="js/price-strip.js'), 'panel helpers load before the price strip');
}

console.log('quote panel model: ok');
