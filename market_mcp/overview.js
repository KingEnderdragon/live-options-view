'use strict';
// market_overview (#132, first usable slice): the collector's existing
// intraday/prices.json -- the same price pipeline that feeds the dashboard
// strip above the options chain -- exposed with explicit instrument identity,
// source, collector receipt time, freshness and change availability.
//
// Producer values are preserved. The consumer adds identity/units, a dollar
// or unit change (price - prev_close) labeled as consumer-derived, scale
// normalization for the CBOE 10Y index, and query-time age. Undated yfinance
// fills and last-known values are never presented as live observations, and
// a percent change is never synthesized where the producer gave none.
const { DataError } = require('./source');
const calendar = require('../docs/squeeze-calendar.json');

const KEY = 'intraday/prices.json';
const ASSET_CLASSES = ['equity', 'etf', 'index', 'rates', 'fx', 'crypto', 'commodity'];
const SORTS = ['strip', 'percent_desc', 'percent_asc'];
const LABEL = /^[A-Za-z0-9][A-Za-z0-9./ -]{0,15}$/;
const SELECTOR = /^[A-Za-z0-9][A-Za-z0-9._/ -]{0,39}$/;
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
  'JPY/USD': { id: 'fut.6j', asset_class: 'fx', instrument_type: 'future_contract_unresolved', unit: 'USD_per_JPY', name: 'CME Japanese yen futures (/6J; contract and expiry not published; not spot USD/JPY)' },
  'BTC/USD': { id: 'crypto.btcusd', asset_class: 'crypto', instrument_type: 'crypto_spot', unit: 'USD_per_BTC', name: 'Bitcoin / US dollar' },
  META: { id: 'META', ...stock('Meta Platforms') }, GOOGL: { id: 'GOOGL', ...stock('Alphabet Class A') },
  AMZN: { id: 'AMZN', ...stock('Amazon.com') }, TSLA: { id: 'TSLA', ...stock('Tesla') },
  MU: { id: 'MU', ...stock('Micron Technology') }, AAPL: { id: 'AAPL', ...stock('Apple') },
  SPCX: { id: 'SPCX', asset_class: 'equity', instrument_type: 'equity_unverified', unit: 'USD', name: 'SPCX (listing type not yet verified)' },
});
const USD_UNITS = new Set(['USD', 'USD_per_BTC']);
const JPY_SPOT = Object.freeze({ id: 'fx.jpyusd', asset_class: 'fx', instrument_type: 'spot_fx', unit: 'USD_per_JPY', name: 'JPY/USD spot (Yahoo JPYUSD=X; not CME yen futures)' });
const JPY_UNKNOWN = Object.freeze({ id: 'unknown.jpyusd', asset_class: 'fx', instrument_type: 'unknown', unit: 'USD_per_JPY', name: 'JPY/USD identity unavailable: source of carried value was not retained' });
const DXLINK_SYMBOLS = Object.freeze({QQQ:'QQQ',SPY:'SPY',USO:'USO',SMH:'SMH',IGV:'IGV',VIX:'$VIX.X',OVX:'$OVX.X',KOSPI:'$KOSPI.X','10Y':'$TNX.X','JPY/USD':'/6J:XCME','BTC/USD':'BTC/USD:CXERX',META:'META',GOOGL:'GOOGL',AMZN:'AMZN',TSLA:'TSLA',MU:'MU',SPCX:'SPCX',AAPL:'AAPL'});
const YF_SYMBOLS = Object.freeze({QQQ:'QQQ',USO:'USO',SMH:'SMH',IGV:'IGV',VIX:'^VIX',OVX:'^OVX',KOSPI:'^KS11','10Y':'^TNX','JPY/USD':'JPYUSD=X','BTC/USD':'BTC-USD',META:'META',GOOGL:'GOOGL',AMZN:'AMZN',TSLA:'TSLA',MU:'MU',SPCX:'SPCX',AAPL:'AAPL'});

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
].map(([id, name, reason]) => Object.freeze({ id, name, reason,
  asset_class: id.startsWith('idx.') ? 'index' : id.startsWith('yld.') ? 'rates' : id.startsWith('fx.') ? 'fx' : id.startsWith('fut.') || id === 'silver' ? 'commodity' : id.startsWith('universe.') ? 'equity' : null })));

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
      [null, undefined, 'dxlink', 'yfinance', 'last-known'].includes(r.source) && [undefined, true, false].includes(r.stale) &&
      [undefined, null, 'dxlink', 'yfinance'].includes(r.origin_source) &&
      [undefined, null, 'last_trade', 'bid_ask_midpoint', 'undated_last_price'].includes(r.price_basis) &&
      (r.source_symbol === undefined || r.source_symbol === null || typeof r.source_symbol === 'string'), `Invalid price row ${label}.`);
    const origin = r.source === 'last-known' ? r.origin_source : r.source;
    const expected = origin === 'dxlink' ? DXLINK_SYMBOLS[label] : origin === 'yfinance' ? YF_SYMBOLS[label] : null;
    check(r.source_symbol == null || !expected || r.source_symbol === expected, `Source symbol mismatch for ${label}.`);
  }
}

const round = (v, places) => v === null ? null : Number(v.toFixed(places));

const eastern = ms => Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(ms)).filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]));
const tradingDays = new Set(calendar.slots.map(s => { const t = eastern(Date.parse(s)); return `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`; }));
function equitySession(ms) {
  const t = eastern(ms), day = `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
  if (ms < Date.parse(calendar.valid_from) || ms >= Date.parse(calendar.valid_until)) return 'unknown';
  if (!tradingDays.has(day)) return 'closed';
  const minutes = t.hour * 60 + t.minute;
  const thanksgiving = t.month === 11 && t.day >= 23 && t.day <= 29 && new Date(Date.UTC(t.year,t.month-1,t.day)).getUTCDay() === 5;
  const earlyClose = thanksgiving || (t.month === 7 && [2,3].includes(t.day)) || (t.month === 12 && [23,24].includes(t.day));
  if (minutes >= 390 && minutes < 570) return 'premarket';
  if (minutes >= 570 && minutes < (earlyClose ? 780 : 960)) return 'regular';
  if (minutes >= (earlyClose ? 780 : 960) && minutes < 975) return earlyClose ? 'closed' : 'postmarket';
  return 'closed';
}

function row(label, r, generatedAt, now, feedStale) {
  const origin = r.source === 'last-known' ? r.origin_source ?? null : r.source ?? null;
  const inst = label === 'JPY/USD' ? origin === 'yfinance' ? JPY_SPOT : origin === 'dxlink' ? INSTRUMENTS[label] : JPY_UNKNOWN : INSTRUMENTS[label];
  const source = r.source ?? null, lastKnown = r.stale === true || source === 'last-known';
  const scale = inst.scale ? (inst.scale[origin] ?? null) : 1;
  const norm = v => (v === null || v === undefined || scale === null) ? null : v / scale;
  const unknownIdentity = label === 'JPY/USD' && inst === JPY_UNKNOWN;
  const rawCurrent = unknownIdentity ? null : norm(r.price ?? null), rawReference = source === 'dxlink' && !unknownIdentity ? norm(r.prev_close ?? null) : null;
  const invalidCurrent = rawCurrent !== null && inst.asset_class !== 'rates' && rawCurrent <= 0;
  const invalidReference = rawReference !== null && rawReference < 0;
  const price = invalidCurrent ? null : rawCurrent, prev = invalidReference ? null : rawReference;
  const received = r.quote_ts ?? null;
  const futureReceipt = received !== null && Date.parse(received) > now + 30000;
  const marketState = ['equity','etf'].includes(inst.asset_class) ? equitySession(now) : inst.asset_class === 'crypto' ? 'continuous_market' : 'unknown';
  const receiptWindow = received && ['equity','etf'].includes(inst.asset_class) ? equitySession(Date.parse(received)) : 'unknown';
  const extendedSession = ['premarket','postmarket'].includes(receiptWindow);
  const unverifiedPriceSession = ['equity','etf'].includes(inst.asset_class) && !['regular','premarket','postmarket'].includes(receiptWindow);

  const reasons = [];
  if (price === null) reasons.push(invalidCurrent ? 'invalid_current' : r.price == null ? 'missing_current' : unknownIdentity ? 'unknown_instrument_identity' : 'unknown_source_scale');
  if (lastKnown) reasons.push('last_known_price');            // price and prev_close may come from different times
  if (!lastKnown && source === 'yfinance') reasons.push('undated_fallback');
  if (futureReceipt) reasons.push('future_collector_receipt');
  if (extendedSession) reasons.push('extended_receipt_window_reference_session_unverified');
  if (unverifiedPriceSession) reasons.push('price_session_unverified');
  if (inst.id === 'fut.6j') reasons.push('contract_unresolved');
  if (prev === null) reasons.push(invalidReference ? 'invalid_reference' : 'missing_reference');
  else if (prev === 0) reasons.push('zero_reference');
  const usable = price !== null && prev !== null && !lastKnown && source === 'dxlink' && received !== null && !futureReceipt && !unverifiedPriceSession && inst.id !== 'fut.6j';
  const absolute = usable ? price - prev : null;
  const change = {
    status: usable ? 'partial' : 'unavailable', reasons: usable ? ['observation_and_reference_session_unverified', prev === 0 ? 'zero_reference' : null, extendedSession ? 'extended_receipt_window_reference_session_unverified' : null].filter(Boolean) : reasons,
    absolute: usable ? round(absolute, 6) : null,
    absolute_unit: usable ? (USD_UNITS.has(inst.unit) ? 'USD' : inst.unit) : null,
    percent: usable && prev !== 0 && inst.change_unit !== 'bps' ? (r.chg_pct ?? null) : null,
    bps: usable && inst.change_unit === 'bps' ? round(absolute * 100, 4) : null,
    reference_basis: 'DXLink Summary prevDayClosePrice; exact reference session, timestamp and adjustment basis are not published',
    current_session: null, collector_receipt_window: receiptWindow, reference_at: null, reference_session: null, adjustment_basis: null,
    derivation: { percent: 'producer chg_pct (rounded to 2 dp), preserved', absolute: 'consumer: price - prev_close', bps: 'consumer: (price - prev_close) x 100 after normalization' },
  };
  if (usable && prev !== 0 && inst.change_unit !== 'bps' && r.chg_pct == null) { change.status = 'partial'; change.reasons.push('producer_percent_missing'); }
  if (usable && prev !== 0 && inst.change_unit !== 'bps' && r.chg_pct !== null && r.chg_pct !== undefined && Math.abs(r.chg_pct - 100 * absolute / prev) > 0.011) {
    change.status = 'partial'; change.percent = null; change.reasons.push('producer_percent_mismatch');
  }

  let freshness;
  if (price === null) freshness = { status: 'missing', age_seconds: null };
  else if (received === null) freshness = { status: 'unknown_time', age_seconds: null };
  else if (futureReceipt) freshness = { status: 'invalid_time', age_seconds: null };
  else {
    const age = (now - Date.parse(received)) / 1000;
    freshness = { status: lastKnown || age > STALE_SECONDS ? 'stale' : marketState === 'closed' ? 'closed' : marketState === 'unknown' ? 'unknown_market_state' : 'fresh', age_seconds: age };
  }
  Object.assign(freshness, { stale_after_seconds: STALE_SECONDS, basis: 'quote_ts = collector receipt time of the selected price input; provider observation time is unavailable',
    market_state: marketState, collector_receipt_window: receiptWindow,
    live: freshness.status === 'fresh' && source === 'dxlink' && !lastKnown && !feedStale && r.price_basis != null && (['regular','premarket','postmarket'].includes(marketState) || marketState === 'continuous_market'),
    live_basis: 'recent DXLink collector receipt during a possible market window; provider observation freshness is unverified' });

  const warnings = [];
  if (source === 'yfinance' && !lastKnown) warnings.push('Undated yfinance fill: no provider observation time and no previous close. Not a live observation.');
  if (lastKnown) warnings.push('Last-known value carried forward after the provider returned nothing; its time is the original observation time if known.');
  if (unknownIdentity) warnings.push('Carried JPY/USD value has no retained source identity; price withheld because spot and futures are different instruments.');
  if (futureReceipt) warnings.push('Collector receipt time is in the future; age and live status are unavailable.');
  if (source === 'dxlink' && r.price_basis == null) warnings.push('Legacy strip does not identify whether price is a last trade or bid/ask midpoint; live status is withheld.');
  if (invalidCurrent || invalidReference) warnings.push('Nonpositive current price or negative reference is withheld for this instrument.');
  if (inst.id === 'fut.6j') warnings.push('The strip does not publish the resolved futures contract or expiry; a continuous-series roll cannot be checked.');
  if (extendedSession) warnings.push('Receipt occurred in an extended-hours window; the provider observation session and Summary close session are unverified. This is not labeled a regular-session change.');
  if (feedStale) warnings.push('Collector marked the DXLink feed stale at this cycle; this row is not asserted live.');
  if (inst.scale && scale === null && r.price != null) warnings.push(`Unknown scale for source ${source}; value withheld.`);
  return { label, id: inst.id, name: inst.name, asset_class: inst.asset_class, instrument_type: inst.instrument_type, unit: inst.unit,
    source, origin_source: origin, source_symbol: r.source_symbol ?? (origin === 'dxlink' ? DXLINK_SYMBOLS[label] : origin === 'yfinance' ? YF_SYMBOLS[label] ?? null : null),
    price, raw_price: unknownIdentity ? null : r.price ?? null, price_basis: r.price_basis ?? 'unknown_last_or_midpoint', bid: source === 'dxlink' ? norm(r.bid ?? null) : null, ask: source === 'dxlink' ? norm(r.ask ?? null) : null, volume: source === 'dxlink' ? r.volume ?? null : null,
    observed_at: null, collector_received_at: received, previous_close: prev, change, freshness, artifact_cycle_at: generatedAt, warnings };
}

function rows(p, now) {
  const known = [], unmapped = [];
  for (const [label, r] of Object.entries(p.prices)) (Object.hasOwn(INSTRUMENTS, label) ? known.push(row(label, r, p.timestamp, now, p.feed_stale)) : unmapped.push(label));
  return { known, unmapped };
}

const units = { price: 'row.unit (10Y normalized to percent; fut.6j and fx.jpyusd in USD per JPY)', 'change.absolute': 'row.change.absolute_unit (USD only for USD-denominated rows)',
  'change.percent': 'percent, producer-rounded to 2 dp', 'change.bps': 'basis points (yield rows only; no percent-of-yield)', 'freshness.age_seconds': 'seconds since collector receipt at query time' };
const warnings = ['Source is the dashboard price strip (intraday/prices.json); coverage is the strip only. Requested assets it lacks are listed under gaps.',
  'Rows are asynchronous values, not a simultaneous snapshot. quote_ts is collector receipt time, not provider observation time. The strip publishes during collector sessions only; off-hours values are from the last publication.',
  'The strip has no previous-close date, adjustment basis, or resolved futures contract. Changes cannot establish same-session comparability or a futures roll.',
  'ETF proxies (QQQ, SPY, USO) keep their own identities; a proxy is never the index or commodity.',
  'Price moves do not establish a news cause.'];

module.exports = { KEY, STALE_SECONDS, ASSET_CLASSES, SORTS, LABEL, SELECTOR, INSTRUMENTS, GAPS, validate, rows, units, warnings };
