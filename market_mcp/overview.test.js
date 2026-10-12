'use strict';
const test = require('node:test'); const assert = require('node:assert/strict');
const { Consumer } = require('./consumer'); const { Source, DataError, permitted } = require('./source');
const { Protocol, tools } = require('./server'); const { fixtures, NOW } = require('./fixtures');
const KEY = 'intraday/prices.json';

function setup({ files = fixtures(), now = () => NOW } = {}) {
  const source = new Source({ read: async key => { if (!Object.hasOwn(files, key)) throw new DataError('missing_artifact', 'Fixture is absent.', key); return files[key]; } });
  return { consumer: new Consumer(source, { now }), files };
}
function edit(files, fn) { const v = JSON.parse(files[KEY]); fn(v); files[KEY] = JSON.stringify(v); }
const by = out => Object.fromEntries(out.rows.map(r => [r.label, r]));

test('price strip is allowlisted and market_overview is a read-only tool', () => {
  assert.doesNotThrow(() => permitted(KEY));
  assert.throws(() => permitted('intraday/prices.json.bak'));
  assert.throws(() => permitted('intraday/overview.json'));
  const tool = tools.find(t => t.name === 'market_overview');
  assert.ok(tool.annotations.readOnlyHint && tool.inputSchema.additionalProperties === false);
});

test('DXLink rows keep producer percent, gain identity, unit and consumer-derived dollar change', async () => {
  const { consumer } = setup(); const r = by(await consumer.call('market_overview', { limit: 50 }));
  assert.equal(r.AAPL.id, 'AAPL'); assert.equal(r.AAPL.change.status, 'ok');
  assert.equal(r.AAPL.change.percent, 1); assert.equal(r.AAPL.change.absolute, 2); assert.equal(r.AAPL.change.absolute_unit, 'USD');
  assert.equal(r.AAPL.freshness.status, 'fresh'); assert.equal(r.AAPL.freshness.live, true);
  assert.equal(r.QQQ.id, 'etf.qqq'); assert.match(r.QQQ.name, /not the index/);
  assert.equal(r.USO.asset_class, 'etf'); assert.match(r.USO.name, /not crude oil/);
});

test('10Y is normalized from the CBOE x10 scale and moves in bps, not percent', async () => {
  const { consumer } = setup(); const y = by(await consumer.call('market_overview', { asset_class: 'rates' }))['10Y'];
  assert.equal(y.price, 4.05); assert.equal(y.previous_close, 4); assert.equal(y.raw_price, 40.5);
  assert.equal(y.change.bps, 5); assert.equal(y.change.percent, null); assert.equal(y.unit, 'percent');
});

test('a yfinance 10Y fill is taken at scale 1 but has no change and no live label', async () => {
  const { consumer, files } = setup();
  edit(files, p => { p.prices['10Y'] = { price: 4.1, bid: null, ask: null, prev_close: null, chg_pct: null, volume: null, source: 'yfinance', quote_ts: null }; });
  const y = by(await consumer.call('market_overview', { asset_class: 'rates' }))['10Y'];
  assert.equal(y.price, 4.1); assert.equal(y.change.status, 'unavailable'); assert.equal(y.freshness.status, 'unknown_time'); assert.equal(y.freshness.live, false);
});

test('yen futures keep USD-per-JPY units and are not presented as spot USD/JPY', async () => {
  const { consumer } = setup(); const out = await consumer.call('market_overview', { asset_class: 'fx' });
  const j = by(out)['JPY/USD'];
  assert.equal(j.id, 'fut.6j'); assert.equal(j.unit, 'USD_per_JPY'); assert.equal(j.change.absolute_unit, 'USD_per_JPY');
  const spot = await consumer.call('market_overview', { symbols: ['fx.usdjpy'] });
  assert.equal(spot.rows.length, 0); assert.match(spot.gaps[0].reason, /yen futures/);
});

test('undated yfinance fills are never live and never get a synthesized change', async () => {
  const { consumer } = setup(); const r = by(await consumer.call('market_overview', { limit: 50 }));
  for (const label of ['VIX', 'OVX', 'KOSPI', 'BTC/USD']) {
    assert.equal(r[label].source, 'yfinance'); assert.equal(r[label].observed_at, null);
    assert.equal(r[label].freshness.status, 'unknown_time'); assert.equal(r[label].freshness.live, false);
    assert.equal(r[label].change.status, 'unavailable'); assert.equal(r[label].change.percent, null);
    assert.ok(r[label].change.reasons.includes('undated_fallback'));
    assert.ok(r[label].warnings.some(w => /Not a live observation/.test(w)));
  }
});

test('last-known carry-forward is stale with no change', async () => {
  const { consumer } = setup(); const a = by(await consumer.call('market_overview', { symbols: ['AMZN'] })).AMZN;
  assert.equal(a.freshness.status, 'stale'); assert.equal(a.freshness.live, false);
  assert.equal(a.change.status, 'unavailable'); assert.ok(a.change.reasons.includes('last_known_price'));
  assert.equal(a.observed_at, '2026-10-02T14:00:00Z');
});

test('requested assets missing from the strip are named gaps, never zero rows', async () => {
  const { consumer } = setup(); const out = await consumer.call('market_overview', { limit: 50 });
  const ids = out.gaps.map(g => g.id);
  for (const id of ['idx.spx', 'idx.ndx', 'fut.cl', 'silver', 'yld.us02y', 'fx.eurusd', 'universe.curated_stocks']) assert.ok(ids.includes(id), id);
  assert.equal(out.status, 'partial');
  assert.ok(!out.rows.some(r => ids.includes(r.id)));
});

test('filters, sorting over all matching rows and unknown symbols', async () => {
  const { consumer } = setup();
  const eq = await consumer.call('market_overview', { asset_class: 'equity', sort: 'percent_desc', limit: 2 });
  assert.deepEqual(eq.rows.map(r => r.label), ['META', 'TSLA']); assert.equal(eq.pagination.total, 7);
  const asc = await consumer.call('market_overview', { asset_class: 'equity', sort: 'percent_asc', limit: 50 });
  assert.equal(asc.rows.at(-1).label, 'AMZN'); // no percent sorts last
  const out = await consumer.call('market_overview', { symbols: ['SPY', 'etf.qqq', 'idx.spx', 'ZZZZ'] });
  assert.deepEqual(out.rows.map(r => r.label), ['QQQ', 'SPY']);
  assert.deepEqual(out.gaps.map(g => g.id), ['idx.spx']);
  assert.deepEqual(out.unmatched_symbols, [{ symbol: 'ZZZZ', status: 'not_in_price_strip' }]);
  for (const bad of [{ asset_class: 'bonds' }, { symbols: [] }, { symbols: ['../x'] }, { sort: 'random' }, { url: 'https://x' }, { group: 'technology' }])
    assert.equal((await consumer.call('market_overview', bad)).errors[0].code, 'invalid_filter', JSON.stringify(bad));
});

test('query-time age turns old DXLink rows stale; feed_stale and unmapped labels are surfaced', async () => {
  const { consumer } = setup({ now: () => NOW + 600000 });
  const out = await consumer.call('market_overview', { symbols: ['AAPL'] });
  assert.equal(out.rows[0].freshness.status, 'stale'); assert.equal(out.freshness.status, 'stale'); assert.equal(out.status, 'partial');
  const s = setup(); edit(s.files, p => { p.feed_stale = true; p.prices.NEWT = { price: 1, source: 'dxlink', quote_ts: null }; });
  const o2 = await s.consumer.call('market_overview');
  assert.ok(o2.warnings.some(w => /feed stale/.test(w))); assert.deepEqual(o2.unmapped_labels, ['NEWT']);
  assert.ok(!o2.rows.some(r => r.label === 'NEWT'));
});

test('missing publication and malformed strips fail explicitly', async () => {
  const { consumer, files } = setup(); delete files[KEY];
  let out = await consumer.call('market_overview'); assert.equal(out.status, 'missing'); assert.equal(out.errors[0].code, 'missing_artifact');
  for (const mutate of [p => { delete p.prices; }, p => { p.prices.AAPL.price = 'NaN'; }, p => { p.prices.AAPL.quote_ts = 'yesterday'; }, p => { p.prices.AAPL.source = 'scraped'; }, p => { p.feed_stale = 'no'; }]) {
    const s = setup(); edit(s.files, mutate); out = await s.consumer.call('market_overview');
    assert.equal(out.status, 'failed'); assert.equal(out.errors[0].code, 'incompatible_schema');
  }
});

test('detail stays bound to the queried row after the strip advances', async () => {
  const { consumer, files } = setup();
  const first = await consumer.call('market_overview', { symbols: ['AAPL'] });
  edit(files, p => { p.timestamp = '2026-10-02T16:11:00Z'; p.prices.AAPL.price = 999; });
  const out = await consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(out.status, 'available'); assert.equal(out.row.price, 202);
  assert.equal(out.actual.payload_sha256, first.actual.payload_sha256);
  assert.equal(out.producer_times.observation, '2026-10-02T16:09:55Z');
});

test('discovery appends the overview capability without moving existing positions', async () => {
  const { consumer, files } = setup(); let out = await consumer.call('discover_sources');
  assert.deepEqual(out.capabilities.map(c => c.dataset), ['qqq_snapshot', 'scheduled_squeeze', 'options_returns', 'spy_snapshot', 'cross_market_overview']);
  assert.equal(out.capabilities[4].availability, 'available'); assert.equal(out.capabilities[4].source_locator, KEY);
  assert.ok(out.capabilities[4].gaps.length > 0);
  delete files[KEY]; out = await consumer.call('discover_sources');
  assert.equal(out.capabilities[4].availability, 'missing_publication');
});

test('MCP tools/call returns matching text and structured overview', async () => {
  const { consumer } = setup(); const p = new Protocol(consumer);
  await p.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 't', version: '1' } } });
  await p.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const out = await p.handle({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'market_overview', arguments: { asset_class: 'crypto' } } });
  assert.deepEqual(JSON.parse(out.result.content[0].text), out.result.structuredContent);
  assert.equal(out.result.structuredContent.rows[0].unit, 'USD_per_BTC');
});
