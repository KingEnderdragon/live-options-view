'use strict';
const test = require('node:test'); const assert = require('node:assert/strict');
const { Consumer } = require('./consumer'); const { Source, DataError, permitted } = require('./source');
const { Protocol, tools } = require('./server'); const { fixtures, NOW } = require('./fixtures');
const KEY = 'intraday/overview.json';

function setup({ files = fixtures(), now = () => NOW } = {}) {
  const source = new Source({ read: async key => { if (!Object.hasOwn(files, key)) throw new DataError('missing_artifact', 'Fixture is absent.', key); return files[key]; } });
  return { consumer: new Consumer(source, { now }), files };
}
function edit(files, fn) { const v = JSON.parse(files[KEY]); fn(v); files[KEY] = JSON.stringify(v); }
const ids = out => out.rows.map(r => r.id);

test('overview is allowlisted and registered as a read-only tool', () => {
  assert.doesNotThrow(() => permitted(KEY));
  assert.throws(() => permitted('intraday/overview.json?x=1'));
  const tool = tools.find(t => t.name === 'market_overview');
  assert.ok(tool.annotations.readOnlyHint && tool.inputSchema.additionalProperties === false);
});

test('default query preserves producer values and excludes watchlist additions', async () => {
  const { consumer } = setup(); const out = await consumer.call('market_overview', { limit: 50 });
  assert.ok(!ids(out).includes('PLTR'));
  const aapl = out.rows.find(r => r.id === 'AAPL');
  assert.equal(aapl.change.percent, 1); assert.equal(aapl.change.absolute_unit, 'USD');
  const tnx = out.rows.find(r => r.id === 'yld.us10y');
  assert.equal(tnx.change.bps, 5); assert.equal(tnx.change.percent, null);
  assert.equal(out.rows.find(r => r.id === 'idx.spx').change.absolute_unit, 'index_points');
  assert.equal(out.status, 'partial'); // gaps + an unavailable change are not hidden
  assert.deepEqual(out.gaps.map(g => g.id), ['yld.us02y', 'fut.si.front']);
  assert.equal(out.actual.universe_version, '2026-10-12.candidate-1');
  assert.ok(out.warnings.some(w => /not a simultaneous/.test(w)));
});

test('unavailable change stays null, never zero', async () => {
  const { consumer } = setup(); const out = await consumer.call('market_overview', { symbols: ['NVDA'] });
  assert.equal(out.rows[0].change.status, 'unavailable'); assert.equal(out.rows[0].change.percent, null);
  assert.deepEqual(out.rows[0].change.reasons, ['missing_reference']);
});

test('filters by asset class, group and explicit symbols; unknown symbols are reported', async () => {
  const { consumer } = setup();
  assert.deepEqual(ids(await consumer.call('market_overview', { asset_class: 'rates' })), ['yld.us10y']);
  assert.deepEqual(ids(await consumer.call('market_overview', { group: 'technology' })), ['AAPL', 'MSFT', 'NVDA']);
  const out = await consumer.call('market_overview', { symbols: ['SPY', 'PLTR', 'yld.us02y', 'ZZZZ'] });
  assert.deepEqual(ids(out), ['PLTR', 'etf.spy']);
  assert.deepEqual(out.unmatched_symbols, [{ symbol: 'yld.us02y', status: 'not_collected' }, { symbol: 'ZZZZ', status: 'not_in_universe' }]);
  assert.equal((await consumer.call('market_overview', { group: 'nope' })).errors[0].code, 'invalid_filter');
  for (const bad of [{ asset_class: 'bonds' }, { symbols: [] }, { symbols: ['../x'] }, { sort: 'random' }, { include_watchlist: 'yes' }, { url: 'https://x' }])
    assert.equal((await consumer.call('market_overview', bad)).errors[0].code, 'invalid_filter', JSON.stringify(bad));
});

test('movers rank over all matching rows before paging, missing percents last', async () => {
  const { consumer } = setup();
  const first = await consumer.call('market_overview', { asset_class: 'equity', sort: 'percent_desc', limit: 1 });
  assert.deepEqual(ids(first), ['JPM']); assert.equal(first.pagination.total, 4); assert.equal(first.pagination.next_offset, 1);
  const down = await consumer.call('market_overview', { asset_class: 'equity', sort: 'percent_asc', limit: 50 });
  assert.deepEqual(ids(down), ['MSFT', 'AAPL', 'JPM', 'NVDA']);
  const withWatch = await consumer.call('market_overview', { asset_class: 'equity', sort: 'percent_desc', include_watchlist: true, limit: 1 });
  assert.deepEqual(ids(withWatch), ['PLTR']);
});

test('breadth uses the full eligible sample before paging and keeps the denominator', async () => {
  const { consumer } = setup();
  const out = await consumer.call('market_overview', { group: 'technology', limit: 1 });
  assert.equal(out.rows.length, 1);
  assert.deepEqual([out.breadth.denominator, out.breadth.advancing, out.breadth.declining, out.breadth.missing], [3, 1, 1, 1]);
  assert.equal(out.breadth.label, 'breadth within the curated sample');
  const all = await consumer.call('market_overview', { limit: 1 });
  assert.equal(all.breadth.denominator, 4); // baseline stocks only; ETF/macro rows are not stocks
  const watch = await consumer.call('market_overview', { include_watchlist: true, limit: 1 });
  assert.equal(watch.breadth.denominator, 5); assert.notEqual(watch.breadth.mean_percent, all.breadth.mean_percent);
});

test('query-time age makes old rows stale; closed and unknown times are preserved', async () => {
  const { consumer } = setup({ now: () => NOW + 600000 });
  const out = await consumer.call('market_overview', { limit: 50 });
  const by = Object.fromEntries(out.rows.map(r => [r.id, r.query_freshness.status]));
  assert.equal(by.AAPL, 'stale'); assert.equal(by['idx.spx'], 'unknown_time');
  assert.equal(out.freshness.status, 'stale');
  const { consumer: c2, files } = setup();
  edit(files, p => { p.instruments[0].freshness.status = 'closed'; p.instruments[0].freshness.market_open = false; });
  assert.equal((await c2.call('market_overview', { symbols: ['AAPL'] })).rows[0].query_freshness.status, 'closed');
});

test('partial cycle and newer failed attempt are surfaced with the last success', async () => {
  const { consumer, files } = setup();
  edit(files, p => { p.cycle.status = 'partial'; p.cycle.newer_failed_attempt_at = '2026-10-02T16:10:30Z'; });
  const out = await consumer.call('market_overview');
  assert.equal(out.status, 'partial'); assert.ok(out.warnings.some(w => /newer collection attempt failed/.test(w)));
  assert.ok(out.rows.length > 0);
});

test('missing publication and malformed contracts fail explicitly', async () => {
  const { consumer, files } = setup(); delete files[KEY];
  let out = await consumer.call('market_overview'); assert.equal(out.status, 'missing'); assert.equal(out.errors[0].code, 'missing_artifact');
  for (const mutate of [p => { p.schema_version = 2; }, p => { p.instruments[1].id = 'AAPL'; }, p => { p.instruments[0].change.percent = 'NaN'; },
    p => { p.instruments[2].change.percent = 0; }, p => { p.gaps.push({ id: 'AAPL', reason: 'x' }); }, p => { p.instruments[0].asset_class = 'stock'; }]) {
    const s = setup(); edit(s.files, mutate); out = await s.consumer.call('market_overview');
    assert.equal(out.status, 'failed'); assert.equal(out.errors[0].code, 'incompatible_schema');
  }
});

test('detail stays bound to the queried row after the overview advances', async () => {
  const { consumer, files } = setup();
  const first = await consumer.call('market_overview', { symbols: ['AAPL'] });
  edit(files, p => { p.generated_at = '2026-10-02T16:11:00Z'; p.instruments[0].current.value = 999; });
  const out = await consumer.call('result_detail', { reference: first.rows[0].detail_reference });
  assert.equal(out.status, 'available'); assert.equal(out.row.current.value, 202);
  assert.equal(out.actual.payload_sha256, first.actual.payload_sha256);
  assert.equal(out.producer_times.observation, '2026-10-02T16:09:30Z');
});

test('discovery appends the overview capability without moving existing positions', async () => {
  const { consumer, files } = setup(); let out = await consumer.call('discover_sources');
  assert.deepEqual(out.capabilities.map(c => c.dataset), ['qqq_snapshot', 'scheduled_squeeze', 'options_returns', 'spy_snapshot', 'cross_market_overview']);
  assert.equal(out.capabilities[4].availability, 'available'); assert.equal(out.capabilities[4].coverage.gaps, 2);
  delete files[KEY]; out = await consumer.call('discover_sources');
  assert.equal(out.capabilities[4].availability, 'missing_publication');
});

test('MCP tools/call returns matching text and structured overview', async () => {
  const { consumer } = setup(); const p = new Protocol(consumer);
  await p.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 't', version: '1' } } });
  await p.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const out = await p.handle({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'market_overview', arguments: { asset_class: 'fx' } } });
  assert.deepEqual(JSON.parse(out.result.content[0].text), out.result.structuredContent);
  assert.equal(out.result.structuredContent.rows[0].unit, 'JPY_per_USD');
});
