// B3 slice 3 (Nov 2026): the live-quote transport + ticker state
// (TRADEABLE_SHARE_SYMBOLS / isTradeableShareSymbol / LiveQuoteService /
// tickerSessionState / SHARE_QUOTE_MAX_AGE_MS / freshShareQuote /
// TickerStateStore / sameSet / LiveQuotePoller / parseRetryAfter) moved
// verbatim out of site/shared.js into site/shared/quotes.js. Pins the
// module's ?v= hash in the page, asserts the ten names no longer live in
// shared.js, pins the entry's export surface (the eight names that were
// always among the 26 are still re-exported — the same function objects —
// while TRADEABLE_SHARE_SYMBOLS and sameSet stay browser globals by design),
// and drives the moved code directly.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'quotes.js'), 'utf8');
const quotes = require('../site/shared/quotes.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/quotes.js?v=' + moduleHash),
  'quotes script URL must match its content hash on every hosting path');

for (const name of ['LiveQuoteService', 'LiveQuotePoller', 'TickerStateStore', 'tickerSessionState', 'parseRetryAfter', 'freshShareQuote']) {
  assert.ok(!shared.includes(`function ${name}(`) && !shared.includes(`class ${name} `), `${name} must no longer live in shared.js`);
  assert.equal(typeof quotes[name], 'function', `quotes.js must export ${name}`);
}
for (const decl of ['const TRADEABLE_SHARE_SYMBOLS = new Set([', 'const SHARE_QUOTE_MAX_AGE_MS = 15_000;', 'function sameSet(', 'function isTradeableShareSymbol(']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
}

// The entry's export list is the behavior evidence for the split: exactly the
// same 26 names shared.js exported before B3 slice 3, with the eight
// live-quote names re-exported from the new home — the same function objects,
// not copies — and TRADEABLE_SHARE_SYMBOLS / sameSet still browser-only.
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
for (const name of ['LiveQuoteService', 'LiveQuotePoller', 'TickerStateStore', 'tickerSessionState', 'SHARE_QUOTE_MAX_AGE_MS', 'freshShareQuote', 'parseRetryAfter', 'isTradeableShareSymbol']) {
  assert.equal(entry[name], quotes[name], `the entry must re-export the same ${name}`);
}
assert.equal(quotes.TRADEABLE_SHARE_SYMBOLS.has('QQQ'), true, 'QQQ is a tradeable share symbol');
assert.equal(quotes.isTradeableShareSymbol('SPY'), false, 'SPY is display-only, not a tradeable share');

const {
  LiveQuoteService, LiveQuotePoller, TickerStateStore, tickerSessionState, parseRetryAfter,
  SHARE_QUOTE_MAX_AGE_MS, freshShareQuote,
} = quotes;

const service = new LiveQuoteService();
let notifications = 0;
service.subscribe(() => notifications++);
service.setVisibleContracts(['VISIBLE']);
service.setOpenPositions(['OPEN']);
service.publish([
  { symbol: 'VISIBLE', bid: 1, ask: 1.2, strike: 500, type: 'call', exp: '2026-07-16' },
  { symbol: 'OPEN', bid: 2, ask: 2.4, strike: 501, type: 'put', exp: '2026-07-16' },
  { symbol: 'IGNORED', bid: 3, ask: 3.4, strike: 502, type: 'call', exp: '2026-07-16' },
], { source: 'r2-snapshot', observedAt: '2026-07-16T12:00:00Z' });

assert.equal(service.get('VISIBLE').mid, 1.1);
assert.equal(service.get('OPEN').mid, 2.2);
assert.equal(service.get('VISIBLE').source, 'r2-snapshot');
assert.equal(service.get('VISIBLE').observedAt, '2026-07-16T12:00:00Z');
assert.equal(service.get('IGNORED'), null);
assert.equal(notifications, 1);

service.publish([
  { symbol: 'VISIBLE', bid: 0.5, ask: 0.7, strike: 500, type: 'call', exp: '2026-07-16' },
], { source: 'older-snapshot', observedAt: '2026-07-16T11:59:00Z' });
assert.equal(service.get('VISIBLE').bid, 1, 'an older snapshot must not overwrite a newer quote');

service.setVisibleContracts([]);
assert.equal(service.get('VISIBLE'), null);
assert.equal(service.get('OPEN').mid, 2.2);
assert.equal(notifications, 2, 'dropping a contract via setVisibleContracts should notify subscribers');

service.setOpenPositions([]);
assert.equal(service.get('OPEN'), null);
assert.equal(notifications, 3, 'dropping a contract via setOpenPositions should notify subscribers');

// Interest-set changes that prune nothing shouldn't fire a spurious notification.
service.setVisibleContracts([]);
assert.equal(notifications, 3, 'a no-op prune should not notify subscribers');

assert.equal(parseRetryAfter('5', 0), 5000);
assert.equal(parseRetryAfter('Thu, 01 Jan 1970 00:00:10 GMT', 2000), 8000);

const regular = Date.parse('2026-07-17T14:30:00Z'); // 10:30 ET
const premarket = Date.parse('2026-07-17T12:00:00Z'); // 08:00 ET
assert.equal(tickerSessionState('equity', regular), 'live');
assert.equal(tickerSessionState('equity', premarket), 'extended');
assert.equal(tickerSessionState('index', premarket), 'closed');
assert.equal(tickerSessionState('crypto', premarket), 'live');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-17T21:01:00Z')), 'closed', 'Friday after 17:00 ET');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-19T21:59:00Z')), 'closed', 'Sunday before reopen');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-19T22:01:00Z')), 'live', 'Sunday after reopen');
assert.equal(tickerSessionState('futures', Date.parse('2026-07-20T21:30:00Z')), 'closed', 'weekday maintenance');

const tickers = new TickerStateStore(
  { QQQ: 'equity', VIX: 'index', 'BTC/USD': 'crypto' },
  { nowFn: () => premarket, staleAfterMs: 30_000 },
);
tickers.publish([
  { symbol: 'QQQ', price: 500, source: 'dxlink', quote_ts: '2026-07-17T11:59:59Z' },
  { symbol: 'VIX', price: 18, source: 'dxlink', quote_ts: '2026-07-17T11:58:00Z' },
  { symbol: 'BTC/USD', price: 120000, source: 'dxlink', quote_ts: '2026-07-17T11:59:59Z' },
]);
assert.equal(tickers.get('QQQ').state, 'extended');
assert.equal(tickers.get('VIX').state, 'stale', 'one stale symbol must not change another ticker');
assert.equal(tickers.get('BTC/USD').state, 'live');
tickers.publish([
  { symbol: 'VIX', price: 17.5, source: 'yfinance', quote_ts: null },
]);
assert.equal(tickers.get('VIX').state, 'fallback', 'YFinance retrieval cannot imply a live market event');
tickers.publishFallback({ QQQ: { price: 490 } }, '2026-07-17T12:00:00Z');
assert.equal(tickers.get('QQQ').price, 500, 'fallback must not replace a healthy live quote');
tickers.publish([
  { symbol: 'QQQ', price: 490, source: 'yfinance', quote_ts: null },
]);
assert.equal(tickers.get('QQQ').price, 500, 'undated YFinance must not replace fresh DXLink');
assert.equal(tickers.get('QQQ').source, 'dxlink', 'fresh live source retains priority');

const shareNow = Date.parse('2026-07-30T14:30:10Z');
const shareTickers = new TickerStateStore(
  { AAPL: 'equity' },
  { nowFn: () => shareNow, staleAfterMs: SHARE_QUOTE_MAX_AGE_MS },
);
shareTickers.publish([{
  symbol: 'AAPL', price: 100, bid: 110, ask: 111, source: 'dxlink',
  quote_ts: '2026-07-30T14:30:05Z',
  bid_ts: '2026-07-30T14:30:04Z', ask_ts: '2026-07-30T14:30:05Z',
}]);
const freshShare = freshShareQuote(shareTickers.get('AAPL'), shareNow);
assert.equal(freshShare.mid, 110.5, 'share marks use the fresh two-sided midpoint, not an older last trade');
assert.equal(shareTickers.get('AAPL').bid_ts, '2026-07-30T14:30:04Z', 'ticker state preserves bid timestamps');
assert.equal(shareTickers.get('AAPL').ask_ts, '2026-07-30T14:30:05Z', 'ticker state preserves ask timestamps');
assert.equal(freshShareQuote({ ...shareTickers.get('AAPL'), ask_ts: null }, shareNow), null,
  'a two-sided share quote requires the ask timestamp');
assert.equal(freshShareQuote({ ...shareTickers.get('AAPL'), bid_ts: '2026-07-30T14:29:00Z' }, shareNow), null,
  'a stale side disables the whole share quote');

(async () => {
  const live = new LiveQuoteService();
  live.setVisibleContracts(['QQQ260717C00600000']);
  let resolveFetch;
  let calls = 0;
  const responsePromise = new Promise(resolve => { resolveFetch = resolve; });
  const poller = new LiveQuotePoller(live, {
    fetchFn: () => {
      calls++;
      return responsePromise;
    },
    nowFn: () => Date.parse('2026-07-17T14:30:05Z'),
    randomFn: () => 0.5,
    isHidden: () => false,
  });
  poller.running = true;
  const first = poller.poll();
  poller.poll();
  assert.equal(calls, 1, 'overlapping poll calls should share one in-flight request');
  resolveFetch({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({
      server_ts: '2026-07-17T14:30:05Z',
      quotes: [{
        symbol: 'QQQ260717C00600000', bid: 1, ask: 1.2, strike: 600,
        type: 'call', exp: '2026-07-17', quote_ts: '2026-07-17T14:30:04Z',
      }],
    }),
  });
  await first;
  assert.equal(live.get('QQQ260717C00600000').source, 'dxlink-live');
  assert.equal(live.get('QQQ260717C00600000').observedAt, '2026-07-17T14:30:04Z');
  assert.equal(poller.health.state, 'live');
  assert.equal(poller.health.quoteAgeMs, 1000);
  assert.equal(poller._normalInterval(), 5000);
  poller.stop();

  const hiddenPoller = new LiveQuotePoller(new LiveQuoteService(), { isHidden: () => true });
  assert.equal(hiddenPoller._normalInterval(), 60_000);
  hiddenPoller.dispose();

  // ── botPositions is an independent interest slot ────────────────────────────
  // The Automated tab and the viewer's own book share one quote transport, so
  // registering bot contracts must not evict the user's positions (or vice
  // versa) -- that would blank out marks on whichever tab rendered second.
  {
    const svc = new LiveQuoteService();
    svc.setOpenPositions(['MINE']);
    svc.setBotPositions(['BOT_A', 'BOT_B']);
    svc.publish([
      { symbol: 'MINE',  bid: 1, ask: 1.2, strike: 500, type: 'call', exp: '2026-07-16' },
      { symbol: 'BOT_A', bid: 2, ask: 2.4, strike: 501, type: 'put',  exp: '2026-07-16' },
      { symbol: 'OTHER', bid: 9, ask: 9.9, strike: 502, type: 'call', exp: '2026-07-16' },
    ], { source: 'test' });

    assert.equal(svc.get('MINE').mid, 1.1, 'user positions survive bot registration');
    assert.equal(svc.get('BOT_A').mid, 2.2, 'bot positions are quoted');
    assert.equal(svc.get('OTHER'), null, 'unregistered symbols are still ignored');
    assert.deepEqual(svc.interestSymbols(), ['BOT_A', 'BOT_B', 'MINE']);

    // Leaving the Automated tab drops bot-only quotes but keeps the user's.
    svc.setBotPositions([]);
    assert.equal(svc.get('BOT_A'), null, 'clearing bot interest prunes bot-only quotes');
    assert.equal(svc.get('MINE').mid, 1.1, 'clearing bot interest leaves the user book intact');
  }

  // ── A closed quote feed is an answer, not a failure ─────────────────────────
  // The collector's option lane is session-scoped: after the stop it clears the
  // feed pointer and answers option-only requests with 503 + health.state
  // "offline", while ticker symbols keep working from the price lane. Treating
  // that as an error walked the exponential ladder forever and showed
  // "Options: unavailable / backing off" for a state that is simply closed.
  {
    const svc = new LiveQuoteService();
    svc.setVisibleContracts(['QQQ261110C00677000']);
    const CLOSED_NOW = Date.parse('2026-11-10T23:30:00Z');
    const offline = () => ({
      ok: false,
      status: 503,
      headers: { get: name => (name === 'Retry-After' ? '5' : null) },
      json: async () => ({ error: 'Live quote feed unavailable', health: { state: 'offline', server_ts: '2026-11-10T23:30:00Z' } }),
    });
    let next = offline;
    const poller = new LiveQuotePoller(svc, {
      fetchFn: () => next(), nowFn: () => CLOSED_NOW, randomFn: () => 0.5, isHidden: () => false,
    });
    poller.running = true;
    await poller.poll();
    assert.equal(poller.health.state, 'closed', 'an offline producer closes the lane instead of failing it');
    assert.equal(poller.health.serviceState, 'offline', 'the producer state is carried for the title');
    assert.equal(poller.health.httpStatus, 503);
    assert.equal(poller.health.message, 'The option quote feed is closed for this session');
    assert.equal(poller.failureCount, 0, 'a closed lane is not a failure and must not escalate');
    assert.equal(Date.parse(poller.health.retryAt) - CLOSED_NOW, 60_000,
      'a closed lane is probed once a minute, not on the backoff ladder');
    await poller.poll();
    assert.equal(poller.health.state, 'closed', 'repeated closed answers stay closed');
    assert.equal(poller.failureCount, 0);

    // A bodiless 503 is still a genuine failure and still walks the ladder.
    next = () => ({ ok: false, status: 503, headers: { get: () => null }, json: async () => ({ error: 'Live quote service unavailable' }) });
    await poller.poll();
    assert.equal(poller.health.state, 'unavailable', 'an unexplained 503 is still a failure');
    assert.equal(poller.failureCount, 1);
    await poller.poll();
    assert.equal(poller.health.state, 'backing-off', 'the failure ladder is unchanged');
    assert.equal(poller.failureCount, 2);

    // A real answer clears both the failure and the closed state.
    next = () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({
        server_ts: '2026-11-10T23:30:00Z',
        health: { state: 'live' },
        quotes: [{
          symbol: 'QQQ261110C00677000', bid: 1, ask: 1.2, strike: 677,
          type: 'call', exp: '2026-11-10', quote_ts: '2026-11-10T23:30:00Z',
        }],
      }),
    });
    await poller.poll();
    assert.equal(poller.health.state, 'live');
    assert.equal(poller.health.serviceState, 'live');
    assert.equal(poller.failureCount, 0);
    poller.stop();
  }

  console.log('PASS live quote service polling, interests, health, closed-lane handling, and deduplication');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
