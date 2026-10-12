'use strict';
// market_overview (#132, first usable slice): the collector's existing
// intraday/prices.json -- the same price pipeline that feeds the dashboard
// strip above the options chain -- exposed with explicit instrument identity,
// source, observation time, freshness and change availability.
//
// Producer values are preserved. The consumer adds identity/units, a dollar
// or unit change (price - prev_close) labeled as consumer-derived, scale
// normalization for the CBOE 10Y index, and query-time age. Undated yfinance
// fills and last-known values are never presented as live observations, and
// a percent change is never synthesized where the producer gave none.
const { DataError } = require('./source');

const KEY = 'intraday/prices.json';
const ASSET_CLASSES = ['equity', 'etf', 'index', 'rates', 'fx', 'crypto'];
const SORTS = ['strip', 'percent_desc', 'percent_asc'];
const LABEL = /^[A-Za-z0-9][A-Za-z0-9./ -]{0,15}$/;
const STALE_SECONDS = 120;
const MAX_LABELS = 64;

// Identity for every strip label the collector publishes today (collector.py
// PRICE_TICKERS plus the separate SPY tile). Proxies keep proxy identities.
const stock = name => ({ asset_class: 'equity', instrument_type: 'stock', unit: 'USD', name });
const etf = name => ({ asset_class: 'etf', instrument_type: 'etf', unit: 'USD', name });
const INSTRUMENTS = Object.freeze({
  QQQ: { id: 'etf.qqq', ...etf('Invesco QQQ Trust (Nasdaq-100 ETF proxy, not the index)') },
  SPY: { id: 'etf.spy', ...etf('SPDR S&P 500 ETF (proxy, not the index)') },
  SMH: { id: 'etf.smh', ...etf('VanEck Semiconductor ETF') },
  IGV: { id: 'etf.igv', ...etf('iShares Expanded Tech-Software ETF') },
  USO: { id: 'etf.uso', ...etf('United States Oil Fund (ETF share price, not crude oil)') },
  VIX: { id: 'idx.vix', asset_class: 'index', instrument_type: 'cash_index', unit: 'index_points', name: 'Cboe Volatility Index' },
  OVX: { id: 'idx.ovx', asset_class: 'index', instrument_type: 'cash_index', unit: 'index_points', name: 'Cboe Crude Oil ETF Volatility Index' },
  KOSPI: { id: 'idx.kospi', asset_class: 'index', instrument_type: 'cash_index', unit: 'index_points', name: 'KOSPI Composite (Korea Exchange calendar)' },
  // dxFeed $TNX.X quotes yield x 10; collector.py's yfinance ^TNX fill is the yield itself.
  '10Y': { id: 'yld.us10y.cboe_tnx', asset_class: 'rates', instrument_type: 'yield_index', unit: 'percent', change_unit: 'bps', scale: { dxlink: 10, yfinance: 1 },
    name: 'Cboe 10-Year Treasury Note Yield Index (TNX), normalized to percent' },
  'JPY/USD': { id: 'fut.6j', asset_class: 'fx', instrument_type: 'future', unit: 'USD_per_JPY', name: 'CME Japanese yen futures (/6J, provider-resolved contract; USD per JPY; not spot USD/JPY)' },
  'BTC/USD': { id: 'crypto.btcusd', asset_class: 'crypto', instrument_type: 'crypto_spot', unit: 'USD_per_BTC', name: 'Bitcoin / US dollar' },
  META: { id: 'META', ...stock('Meta Platforms') }, GOOGL: { id: 'GOOGL', ...stock('Alphabet Class A') },
  AMZN: { id: 'AMZN', ...stock('Amazon.com') }, TSLA: { id: 'TSLA', ...stock('Tesla') },
  MU: { id: 'MU', ...stock('Micron Technology') }, AAPL: { id: 'AAPL', ...stock('Apple') },
  SPCX: { id: 'SPCX', asset_class: 'equity', instrument_type: 'equity_unverified', unit: 'USD', name: 'SPCX (listing type not yet verified)' },
});
const USD_UNITS = new Set(['USD', 'USD_per_BTC']);

// Requested #132 coverage the strip does not carry. Named, never zero-filled.
const GAPS = Object.freeze([
  ['idx.spx', 'S&P 500 cash index', 'only the SPY ETF proxy is published'],
  ['idx.ndx', 'Nasdaq-100 cash index', 'only the QQQ ETF proxy is published'],
  ['idx.dji', 'Dow Jones Industrial Average', 'not in the price strip'],
  ['idx.rut', 'Russell 2000', 'not in the price strip'],
  ['fut.cl', 'WTI crude oil', 'only the USO ETF share price is published'],
  ['fut.bz', 'Brent crude oil', 'not in the price strip'],
  ['silver', 'Silver (USD per troy ounce)', 'not in the price strip'],
  ['yld.us02y', 'US 2-year Treasury yield', 'no source identified'],
  ['yld.us05y', 'US 5-year Treasury yield', 'not in the price strip'],
  ['yld.us30y', 'US 30-year Treasury yield', 'not in the price strip'],
  ['fx.eurusd', 'EUR/USD', 'not in the price strip'],
  ['fx.gbpusd', 'GBP/USD', 'not in the price strip'],
  ['fx.usdjpy', 'USD/JPY spot', 'only CME yen futures (fut.6j) are published'],
  ['fx.usdchf', 'USD/CHF', 'not in the price strip'],
  ['fx.usdcad', 'USD/CAD', 'not in the price strip'],
  ['fx.audusd', 'AUD/USD', 'not in the price strip'],
  ['universe.curated_stocks', 'Curated 100-200 stock universe', 'follow-on; the strip carries 7 single stocks'],
].map(([id, name, reason]) => Object.freeze({ id, name, reason })));

const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const num = v => v === null || v === undefined || (typeof v === 'number' && Number.isFinite(v));
const time = v => v === null || v === undefined || (typeof v === 'string' && Number.isFinite(Date.parse(v)));
function check(ok, message, code = 'incompatible_schema') { if (!ok) throw new DataError(code, message, KEY); }

function validate(p) {
  check(object(p) && typeof p.timestamp === 'string' && Number.isFinite(Date.parse(p.timestamp)) && typeof p.feed_stale === 'boolean' && object(p.prices), 'Unsupported prices contract.');
  const labels = Object.keys(p.prices);
  check(labels.length <= MAX_LABELS, 'Price strip exceeds the label limit.', 'excessive_or_malformed_artifact');
  for (const label of labels) {
    const r = p.prices[label];
    check(LABEL.test(label) && !['__proto__', 'constructor', 'prototype'].includes(label), 'Invalid price label.');
    check(object(r) && ['price', 'bid', 'ask', 'prev_close', 'chg_pct', 'volume'].every(k => num(r[k])) && time(r.quote_ts) &&
      [null, undefined, 'dxlink', 'yfinance', 'last-known'].includes(r.source) && [undefined, true, false].includes(r.stale), `Invalid price row ${label}.`);
  }
}

const round = (v, places) => v === null ? null : Number(v.toFixed(places));

function row(label, r, generatedAt, now) {
  const inst = INSTRUMENTS[label];
  const source = r.source ?? null, lastKnown = r.stale === true || source === 'last-known';
  const scale = inst.scale ? (inst.scale[source] ?? null) : 1;
  const norm = v => (v === null || v === undefined || scale === null) ? null : v / scale;
  const price = norm(r.price ?? null), prev = norm(r.prev_close ?? null);
  const observed = r.quote_ts ?? null;

  const reasons = [];
  if (price === null) reasons.push(r.price == null ? 'missing_current' : 'unknown_source_scale');
  if (lastKnown) reasons.push('last_known_price');            // price and prev_close may come from different times
  if (!lastKnown && source === 'yfinance') reasons.push('undated_fallback');
  if (prev === null) reasons.push('missing_reference');
  else if (prev === 0) reasons.push('zero_reference');
  const usable = price !== null && prev !== null && prev !== 0 && !lastKnown && source === 'dxlink';
  const absolute = usable ? price - prev : null;
  const change = {
    status: usable ? 'ok' : 'unavailable', reasons: usable ? [] : reasons,
    absolute: usable ? round(absolute, 6) : null,
    absolute_unit: usable ? (USD_UNITS.has(inst.unit) ? 'USD' : inst.unit) : null,
    percent: usable && inst.change_unit !== 'bps' ? (r.chg_pct ?? null) : null,
    bps: usable && inst.change_unit === 'bps' ? round(absolute * 100, 4) : null,
    reference_basis: 'previous close as supplied by the DXLink Summary event (prev_close)',
    derivation: { percent: 'producer chg_pct (rounded to 2 dp), preserved', absolute: 'consumer: price - prev_close', bps: 'consumer: (price - prev_close) x 100 after normalization' },
  };
  if (usable && inst.change_unit !== 'bps' && r.chg_pct == null) { change.status = 'partial'; change.reasons = ['producer_percent_missing']; }

  let freshness;
  if (price === null) freshness = { status: 'missing', age_seconds: null };
  else if (observed === null) freshness = { status: 'unknown_time', age_seconds: null };
  else {
    const age = Math.max(0, (now - Date.parse(observed)) / 1000);
    freshness = { status: lastKnown || age > STALE_SECONDS ? 'stale' : 'fresh', age_seconds: age };
  }
  Object.assign(freshness, { stale_after_seconds: STALE_SECONDS, basis: 'quote_ts (provider event time); retrieval and artifact times are never used as observation time',
    live: freshness.status === 'fresh' && source === 'dxlink' && !lastKnown });

  const warnings = [];
  if (source === 'yfinance' && !lastKnown) warnings.push('Undated yfinance fill: no provider observation time and no previous close. Not a live observation.');
  if (lastKnown) warnings.push('Last-known value carried forward after the provider returned nothing; its time is the original observation time if known.');
  if (inst.scale && scale === null && r.price != null) warnings.push(`Unknown scale for source ${source}; value withheld.`);
  return { label, id: inst.id, name: inst.name, asset_class: inst.asset_class, instrument_type: inst.instrument_type, unit: inst.unit,
    source, price, raw_price: r.price ?? null, bid: norm(r.bid ?? null), ask: norm(r.ask ?? null), volume: r.volume ?? null,
    observed_at: observed, previous_close: prev, change, freshness, artifact_generated_at: generatedAt, warnings };
}

function rows(p, now) {
  const known = [], unmapped = [];
  for (const [label, r] of Object.entries(p.prices)) (Object.hasOwn(INSTRUMENTS, label) ? known.push(row(label, r, p.timestamp, now)) : unmapped.push(label));
  return { known, unmapped };
}

const units = { price: 'row.unit (10Y normalized to percent; fut.6j in USD per JPY)', 'change.absolute': 'row.change.absolute_unit (USD only for USD-denominated rows)',
  'change.percent': 'percentage points, producer-rounded to 2 dp', 'change.bps': 'basis points (yield rows only; no percent-of-yield)', 'freshness.age_seconds': 'seconds since quote_ts at query time' };
const warnings = ['Source is the dashboard price strip (intraday/prices.json); coverage is the strip only. Requested assets it lacks are listed under gaps.',
  'Rows are asynchronous observations, not a simultaneous snapshot. The strip publishes during collector sessions only, so off-hours values are the last session\'s.',
  'ETF proxies (QQQ, SPY, USO) keep their own identities; a proxy is never the index or commodity.',
  'Price moves do not establish a news cause.'];

module.exports = { KEY, STALE_SECONDS, ASSET_CLASSES, SORTS, LABEL, INSTRUMENTS, GAPS, validate, rows, units, warnings };
