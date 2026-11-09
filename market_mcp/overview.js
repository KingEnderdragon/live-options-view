'use strict';
// #132: the existing price artifact, exact-100 shared identity registry and
// v2 provider observation/reference provenance. Legacy arithmetic stays partial
// and receipt-only evidence never claims live. V2 arithmetic is consumer-derived
// from the retained normalized pair; unknown dates, calendars and contracts
// remain explicit rather than acquiring invented timestamps or identities.
const { DataError } = require('./source');
const calendar = require('../site/squeeze-calendar.json');

const KEY = 'intraday/prices.json';
const ASSET_CLASSES = ['equity', 'etf', 'index', 'rates', 'fx', 'crypto', 'commodity'];
const SORTS = ['strip', 'percent_desc', 'percent_asc'];
const LABEL = /^[A-Za-z0-9/][A-Za-z0-9./ -]{0,15}$/;
const SELECTOR = /^[A-Za-z0-9/][A-Za-z0-9._/ -]{0,63}$/;
const STALE_SECONDS = 120;
const MAX_LABELS = 100;
const universe = require('../market_data/overview/universe.json');
if (universe.schema_version !== 1 || universe.universe_version !== 'market-overview-100-v1' || universe.instruments.length !== 100 ||
    new Set(universe.instruments.map(i => i.label)).size !== 100 || new Set(universe.instruments.map(i => i.id)).size !== 100) throw new Error('Invalid exact-100 overview registry.');

// One reviewed registry drives identity, ordering, source mappings and discovery.
const INSTRUMENTS = Object.freeze(Object.fromEntries(universe.instruments.map(i => [i.label, Object.freeze(i)])));
const USD_UNITS = new Set(['USD', 'USD_per_BTC']);
// A comparison is only a previous-session move when the reference basis is
// meaningful for that instrument family and its claimed session can carry that
// basis. Settlement does not belong to a stock; an extended print is not a
// previous regular close.
const REFERENCE_BASES = Object.freeze({
  E: ['previous_close','prev_day_close','daily_bar_close','daily_close','official_close'],
  I: ['previous_close','prev_day_close','daily_bar_close','daily_close','official_close'],
  V: ['previous_close','daily_bar_close','daily_close','official_close'],
  Y: ['previous_close','prev_day_close','daily_bar_close','daily_close','official_close'],
  Y2: ['previous_observation','official_close','daily_close'],
  F: ['previous_observation','daily_bar_close','daily_close','previous_close'],
  FU: ['settlement','previous_close'],
  C: ['previous_close','prev_day_close','daily_bar_close','daily_close','utc_day_open'],
});
const BASIS_SESSIONS = Object.freeze({
  previous_close: ['regular','daily'],
  prev_day_close: ['regular','daily'],
  official_close: ['regular','daily'],
  daily_bar_close: ['daily','regular'],
  daily_close: ['daily','regular'],
  settlement: ['settlement','daily'],
  previous_observation: ['daily','regular','settlement'],
  utc_day_open: ['daily'],
});
const JPY_SPOT = Object.freeze({ id: 'fx.jpyusd', asset_class: 'fx', instrument_type: 'spot_fx', unit: 'USD_per_JPY', name: 'JPY/USD spot (Yahoo JPYUSD=X; not CME yen futures)' });
const JPY_UNKNOWN = Object.freeze({ id: 'unknown.jpyusd', asset_class: 'fx', instrument_type: 'unknown', unit: 'USD_per_JPY', name: 'JPY/USD identity unavailable: source of carried value was not retained' });
const DXLINK_SYMBOLS = Object.freeze(Object.fromEntries(universe.instruments.map(i => [i.label, i.dxlink_symbol])));
const YF_SYMBOLS = Object.freeze(Object.fromEntries(universe.instruments.map(i => [i.label, i.yahoo_symbol])));

// Requested #132 coverage the strip does not carry. Named, never zero-filled.
const GAPS = Object.freeze([
  ['idx.spx', 'S&P 500 cash index', 'only the SPY ETF proxy is published'],
  ['idx.ndx', 'Nasdaq-100 cash index', 'only the QQQ ETF proxy is published'],
  ['idx.dji', 'Dow Jones Industrial Average', 'not in the price strip'],
  ['idx.rut', 'Russell 2000', 'not in the price strip'],

].map(([id, name, reason]) => Object.freeze({ id, name, reason,
  asset_class:'index', scope:'outside_selected_universe' })));

const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const num = v => v === null || v === undefined || (typeof v === 'number' && Number.isFinite(v));
const iso = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(v) && Number.isFinite(Date.parse(v)) && realDate(v.slice(0,10));
const realDate = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0,10) === v;
const time = v => v == null || iso(v);
const zone = v => { try { return typeof v === 'string' && !!new Intl.DateTimeFormat('en-US',{timeZone:v}); } catch { return false; } };
const futureContract = c => object(c) && typeof c.symbol === 'string' && c.symbol.length > 0 && realDate(c.expiry) && typeof c.root === 'string' && c.root.length > 0 && typeof c.exchange === 'string' && c.exchange.length > 0;
function observationValid(r) {
  return time(r.observed_at) && (r.observation_date == null || realDate(r.observation_date)) &&
    (r.observation_timezone == null || zone(r.observation_timezone)) &&
    [undefined,null,'event','day'].includes(r.observation_granularity) &&
    (r.observation_granularity !== 'day' || r.observed_at == null && realDate(r.observation_date) && zone(r.observation_timezone));
}
function check(ok, message, code = 'incompatible_schema') { if (!ok) throw new DataError(code, message, KEY); }

function validate(p) {
  check(object(p) && iso(p.timestamp) && typeof p.feed_stale === 'boolean' && object(p.prices), 'Unsupported prices contract.');
  check(p.schema_version == null || [1,2].includes(p.schema_version), 'Unsupported prices schema version.');
  check(p.universe_version == null || p.universe_version === universe.universe_version, 'Unknown universe version.');
  if (p.price_lane != null) check(object(p.price_lane) && typeof p.price_lane.writer_id === 'string' && p.price_lane.writer_id.length <= 200 &&
    typeof p.price_lane.mode === 'string' && object(p.price_lane.groups_open) && object(p.price_lane.sessions) &&
    typeof p.price_lane.eligible === 'boolean', 'Invalid price lane metadata.');
  const labels = Object.keys(p.prices);
  check(labels.length <= MAX_LABELS, 'Price strip exceeds the label limit.', 'excessive_or_malformed_artifact');
  for (const label of labels) {
    const r = p.prices[label];
    check(LABEL.test(label) && !['__proto__', 'constructor', 'prototype'].includes(label), 'Invalid price label.');
    check(object(r) && ['price', 'bid', 'ask', 'prev_close', 'chg_pct', 'volume'].every(k => num(r[k])) && time(r.quote_ts) &&
      [null, undefined, 'dxlink', 'yfinance', 'treasury', 'last-known'].includes(r.source) && [undefined, true, false].includes(r.stale) &&
      [undefined, null, 'dxlink', 'yfinance', 'treasury'].includes(r.origin_source) &&
      [undefined, null, 'last_trade', 'bid_ask_midpoint', 'undated_last_price', 'daily_bar_close', 'daily_close', 'official_close', 'settlement'].includes(r.price_basis) &&
      (r.source_symbol === undefined || r.source_symbol === null || typeof r.source_symbol === 'string'), `Invalid price row ${label}.`);
    const origin = r.source === 'last-known' ? r.origin_source : r.source;
    const expected = origin === 'dxlink' ? DXLINK_SYMBOLS[label] : origin === 'yfinance' ? label === 'JPY/USD' ? 'JPYUSD=X' : YF_SYMBOLS[label] : origin === 'treasury' ? INSTRUMENTS[label]?.treasury_symbol : null;
    const resolved = futureContract(r.resolved_contract) && r.source_symbol === r.resolved_contract.symbol && INSTRUMENTS[label]?.id.startsWith('fut.');
    check(r.source_symbol == null || !Object.hasOwn(INSTRUMENTS,label) || r.source_symbol === expected || (p.schema_version !== 2 && ((label === 'BTC/USD' && r.source_symbol === 'BTC/USD:CXERX') || (label === 'JPY/USD' && r.source_symbol === '/6J:XCME'))) || resolved, `Source symbol mismatch for ${label}.`);
    if (p.schema_version === 2) {
      const expectedId = label === 'JPY/USD' && origin === 'yfinance' ? JPY_SPOT.id : INSTRUMENTS[label]?.id;
      const bounded = (v, max = 200) => v == null || (typeof v === 'string' && v.length <= max);
      check(r.instrument_id == null || !expectedId || r.instrument_id === expectedId, `Instrument identity mismatch for ${label}.`);
      check(Object.hasOwn(INSTRUMENTS,label) ? typeof r.instrument_id === 'string' && bounded(r.instrument_id, 80) : bounded(r.instrument_id, 80),
        `Missing or oversized instrument identity for ${label}.`);
      check(bounded(r.source_symbol, 80) && bounded(r.adjustment_basis, 80) && bounded(r.observation_time_reason, 200) &&
        (r.refresh_error == null || (typeof r.refresh_error === 'string' ? r.refresh_error.length <= 500
          : object(r.refresh_error) && JSON.stringify(r.refresh_error).length <= 500)),
        `Oversized observation provenance for ${label}.`);
      check([undefined, null, 'regular', 'extended', 'daily'].includes(r.session) &&
        (r.reference == null || [undefined, null, 'regular', 'extended', 'daily'].includes(r.reference.session)),
        `Unknown session label for ${label}.`);
      check(observationValid(r) && ['instrument_id','session','adjustment_basis','observation_time_reason'].every(k => r[k] == null || typeof r[k] === 'string') &&
        (r.resolved_contract == null || object(r.resolved_contract)) && ['collector_received_at','acquired_at','last_success_at','last_attempt_at'].every(k => time(r[k])) &&
        (r.refresh_error == null || typeof r.refresh_error === 'string' || object(r.refresh_error)), `Invalid observation provenance for ${label}.`);
      check(r.collector_received_at == null || r.quote_ts == null || r.collector_received_at === r.quote_ts, `Conflicting collector receipt for ${label}.`);
      if (r.reference != null) check(object(r.reference) && num(r.reference.value) && observationValid(r.reference) &&
        [undefined,null,'dxlink','yfinance','treasury'].includes(r.reference.source) && (r.reference.resolved_contract == null || object(r.reference.resolved_contract)) &&
        ['source','source_symbol','instrument_id','price_basis','session','adjustment_basis'].every(k => r.reference[k] == null || typeof r.reference[k] === 'string'), `Invalid reference provenance for ${label}.`);
      // #178 additive rolling reference: only crypto carries it, and only a
      // bounded 24-hour sample is acceptable. Unknown extra fields elsewhere
      // stay ignored for v2 compatibility.
      if (r.rolling_reference != null) check(object(r.rolling_reference) && num(r.rolling_reference.value) && r.rolling_reference.value > 0 &&
        iso(r.rolling_reference.observed_at) && r.rolling_reference.price_basis === 'rolling_24h' &&
        r.rolling_reference.source === 'rolling_history' &&
        [undefined,null,'continuous'].includes(r.rolling_reference.session) &&
        ['instrument_id','source_symbol','sample_clock','observation_time_reason'].every(k => r.rolling_reference[k] == null || typeof r.rolling_reference[k] === 'string'),
        `Invalid rolling reference for ${label}.`);
      check(r.market_session == null || ['premarket','regular','afterhours','overnight','maintenance','closed','holiday','continuous','daily','unknown'].includes(r.market_session),
        `Unknown market session for ${label}.`);
    }
    check(r.price_basis == null || (origin === 'dxlink' && ['last_trade', 'bid_ask_midpoint'].includes(r.price_basis)) ||
      (origin === 'treasury' && p.schema_version === 2 && r.price_basis === 'daily_close') ||
      (origin === 'yfinance' && ['undated_last_price', ...(p.schema_version === 2 ? ['daily_bar_close','daily_close','official_close'] : [])].includes(r.price_basis)), `Price basis conflicts with source for ${label}.`);
    check(r.source !== 'yfinance' || r.quote_ts == null, `Undated Yahoo fill has a receipt time for ${label}.`);
  }
}

const round = (v, places) => v === null ? null : Number(v.toFixed(places));

const eastern = ms => Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(ms)).filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]));
const tradingDays = new Set(calendar.slots.map(s => { const t = eastern(Date.parse(s)); return `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`; }));
const tradingDayList = [...tradingDays].sort();
// Trading sessions strictly between two XNYS dates, or null when outside the
// published calendar (unverifiable is not the same as one session).
function sessionsBetween(earlier, later) {
  const a = tradingDayList.indexOf(earlier), b = tradingDayList.indexOf(later);
  return a < 0 || b < 0 || b < a ? null : b - a;
}
function equitySession(ms) {
  const t = eastern(ms), day = `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
  if (ms < Date.parse(calendar.valid_from) || ms >= Date.parse(calendar.valid_until)) return 'unknown';
  if (!tradingDays.has(day)) return 'closed';
  const minutes = t.hour * 60 + t.minute;
  // The published calendar names trading days but carries no session-close
  // metadata. A commonly shortened session therefore cannot be certified as
  // regular-length or closed; report unknown rather than guess either way.
  const thanksgiving = t.month === 11 && t.day >= 23 && t.day <= 29 && new Date(Date.UTC(t.year,t.month-1,t.day)).getUTCDay() === 5;
  if (thanksgiving || (t.month === 7 && [2,3].includes(t.day)) || (t.month === 12 && [23,24].includes(t.day))) return 'unknown';
  if (minutes >= 390 && minutes < 570) return 'premarket';
  if (minutes >= 570 && minutes < 960) return 'regular';
  if (minutes >= 960 && minutes < 975) return 'postmarket';
  return 'closed';
}

function row(label, r, generatedAt, now, feedStale) {
  const origin = r.source === 'last-known' ? r.origin_source ?? null : r.source ?? null;
  const inst = label === 'JPY/USD' ? origin === 'yfinance' ? JPY_SPOT : origin === 'dxlink' || r.price == null ? INSTRUMENTS[label] : JPY_UNKNOWN : INSTRUMENTS[label];
  const source = r.source ?? null, lastKnown = r.stale === true || source === 'last-known';
  const scale = inst.scale ? (inst.scale[origin] ?? null) : 1;
  const norm = v => (v === null || v === undefined || scale === null) ? null : v / scale;
  const unknownIdentity = label === 'JPY/USD' && inst === JPY_UNKNOWN;
  const rawCurrent = unknownIdentity ? null : norm(r.price ?? null), rawReference = source === 'dxlink' && !unknownIdentity ? norm(r.prev_close ?? null) : null;
  const invalidCurrent = rawCurrent !== null && inst.asset_class !== 'rates' && rawCurrent <= 0;
  const invalidReference = rawReference !== null && rawReference < 0;
  const price = invalidCurrent ? null : rawCurrent, prev = invalidReference ? null : rawReference;
  const received = r.quote_ts ?? null;
  const futureReceipt = received !== null && Date.parse(received) > now;
  const futureCycle = Date.parse(generatedAt) > now;
  const marketState = ['equity','etf'].includes(inst.asset_class) ? equitySession(now) : inst.asset_class === 'crypto' ? 'continuous_market' : 'unknown';
  const receiptWindow = received && ['equity','etf'].includes(inst.asset_class) ? equitySession(Date.parse(received)) : 'unknown';
  const extendedSession = ['premarket','postmarket'].includes(receiptWindow);
  const unverifiedPriceSession = ['equity','etf'].includes(inst.asset_class) && !['regular','premarket','postmarket'].includes(receiptWindow);

  const reasons = [];
  if (price === null) reasons.push(invalidCurrent ? 'invalid_current' : r.price == null ? 'missing_current' : unknownIdentity ? 'unknown_instrument_identity' : 'unknown_source_scale');
  if (lastKnown) reasons.push('last_known_price');            // price and prev_close may come from different times
  if (!lastKnown && source === 'yfinance') reasons.push('undated_fallback');
  if (futureReceipt) reasons.push('future_collector_receipt');
  if (futureCycle) reasons.push('future_collector_cycle');
  if (extendedSession) reasons.push('extended_receipt_window_reference_session_unverified');
  if (unverifiedPriceSession) reasons.push('price_session_unverified');
  if (inst.id === 'fut.6j') reasons.push('contract_unresolved');
  if (prev === null) reasons.push(invalidReference ? 'invalid_reference' : 'missing_reference');
  else if (prev === 0) reasons.push('zero_reference');
  const usable = price !== null && prev !== null && !lastKnown && source === 'dxlink' && received !== null && !futureReceipt && !futureCycle && !unverifiedPriceSession && inst.id !== 'fut.6j';
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
  else if (futureReceipt || futureCycle) freshness = { status: 'invalid_time', age_seconds: null };
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
  if (futureCycle) warnings.push('Collector cycle time is in the future; this row is not asserted live.');
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

function dateInZone(ms, tz) {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(ms);
  return ['year','month','day'].map(k => parts.find(p => p.type === k).value).join('-');
}
function rolling24h(out, r, now, inst) {
  const ref = r.rolling_reference ?? null;
  const unavailable = reason => ({ status: 'unavailable', reasons: [reason], percent: null,
    absolute: null, absolute_unit: null, reference_at: null, reference_basis: 'rolling_24h',
    reference_age_seconds: null, sample_clock: null });
  if (inst.asset_class !== 'crypto') return unavailable('not_a_continuous_instrument');
  if (r.source !== 'dxlink' || r.stale === true || out.price == null) return unavailable('current_observation_unusable');
  if (!ref) return unavailable('rolling_reference_missing');
  const sampleMs = Date.parse(ref.observed_at);
  const age = Number.isFinite(sampleMs) ? (now - sampleMs) / 1000 : null;
  if (age == null || age < 23.5 * 3600 || age > 24.6 * 3600) return unavailable('rolling_reference_outside_24h_window');
  if (ref.value == null || !Number.isFinite(ref.value) || ref.value <= 0) return unavailable('invalid_rolling_reference');
  if (ref.instrument_id != null && ref.instrument_id !== out.id) return unavailable('rolling_reference_identity_mismatch');
  const percent = 100 * (out.price - ref.value) / ref.value;
  if (!Number.isFinite(percent)) return unavailable('nonfinite_arithmetic');
  const reasons = [];
  if (ref.sample_clock && ref.sample_clock !== 'provider_event_clock') reasons.push('sample_clock_is_collector_receipt');
  return { status: reasons.length ? 'partial' : 'ok', reasons,
    percent: round(percent, 6), absolute: round(out.price - ref.value, 6),
    absolute_unit: USD_UNITS.has(inst.unit) ? 'USD' : inst.unit,
    reference_at: ref.observed_at, reference_basis: 'rolling_24h',
    reference_age_seconds: round(age, 1), sample_clock: ref.sample_clock ?? null,
    derivation: 'consumer: (normalized price - same-instrument 24h historical sample) / sample x 100' };
}


function enrich(out, r, p, now) {
  const inst = INSTRUMENTS[out.label], v2 = p.schema_version === 2;
  const market = inst.profile === 'E' ? equitySession(now) : inst.profile === 'C' ? 'continuous_market' : 'unknown';
  out.freshness.market_state = market;
  out.freshness.live = false; // Receipt time alone never proves observation freshness.
  out.freshness.live_basis = 'requires a recent provider event observation and a verified market window';
  if (!v2) return out;
  const observed = r.observed_at ?? null, received = r.collector_received_at ?? r.quote_ts ?? null;
  Object.assign(out, {observed_at:observed, observation_time_reason:r.observation_time_reason ?? null, collector_received_at:received,
    observation_date:r.observation_date ?? null, observation_timezone:r.observation_timezone ?? null,
    observation_granularity:r.observation_granularity ?? null, session:r.session ?? null,
    adjustment_basis:r.adjustment_basis ?? null, acquired_at:r.acquired_at ?? null,
    last_success_at:r.last_success_at ?? null, last_attempt_at:r.last_attempt_at ?? null, refresh_error:r.refresh_error ?? null,
    resolved_contract:r.resolved_contract ?? null, reference:r.reference ?? null});
  const ref = r.reference ?? {}, origin = out.origin_source;
  const scale = inst.scale ? inst.scale[origin] : 1;
  const referenceScale = inst.scale ? inst.scale[ref.source] : 1;
  const isFuture = inst.id.startsWith('fut.');
  const sameContract = futureContract(r.resolved_contract) && futureContract(ref.resolved_contract) &&
    r.resolved_contract.symbol === ref.resolved_contract.symbol && r.resolved_contract.expiry === ref.resolved_contract.expiry &&
    r.resolved_contract.root.toLowerCase() === inst.id.slice(4) && ref.resolved_contract.root === r.resolved_contract.root &&
    r.resolved_contract.exchange === ref.resolved_contract.exchange;
  const allowNegative = isFuture && inst.asset_class === 'commodity' && sameContract;
  const norm = (value, divisor) => value == null || !divisor ? null : value / divisor;
  const rawPrice = norm(r.price, scale), rawPrev = norm(ref.value, referenceScale);
  out.price = rawPrice == null || out.id === JPY_UNKNOWN.id || rawPrice <= 0 && inst.asset_class !== 'rates' && !allowNegative ? null : rawPrice;
  out.previous_close = rawPrev == null || rawPrev < 0 && !allowNegative ? null : rawPrev;
  const currentDate = observed ? dateInZone(Date.parse(observed), r.observation_timezone ?? 'UTC') : r.observation_date;
  const referenceDate = ref.observed_at ? dateInZone(Date.parse(ref.observed_at), ref.observation_timezone ?? 'UTC') : ref.observation_date;
  const futureObservation = observed != null && Date.parse(observed) > now || r.observation_date != null && r.observation_date > dateInZone(now,r.observation_timezone ?? 'UTC');
  const futureReceipt = received != null && Date.parse(received) > now;
  const invalidTimes = futureObservation || futureReceipt || Date.parse(p.timestamp) > now ||
    ['acquired_at','last_success_at','last_attempt_at'].some(k => r[k] != null && Date.parse(r[k]) > now);
  const carried = r.source === 'last-known' || r.stale === true;
  let status = out.price == null ? 'missing' : invalidTimes ? 'invalid_time' : observed == null ?
    r.observation_granularity === 'day' ? 'dated_close' : 'unknown_time' :
    carried || (now-Date.parse(observed))/1000 > STALE_SECONDS ? 'stale' : market === 'closed' ? 'closed' : market === 'unknown' ? 'unknown_market_state' : 'fresh';
  const observationAgeDays = r.observation_date ? (Date.parse(dateInZone(now,r.observation_timezone ?? 'UTC'))-Date.parse(r.observation_date))/86400000 : null;
  if (status === 'dated_close' && (carried || observationAgeDays > 1)) status = 'stale'; // Conservative; no unverified exchange calendar is inferred.
  // "Live" means a recent provider event inside a verified market window for
  // this instrument. A quote midpoint has no session discriminator, so a row
  // without a reported session cannot claim it; neither can an event whose own
  // timestamp falls outside any session window.
  const observedWindow = observed ? (inst.profile === 'C' ? 'continuous_market'
    : ['E','V','Y'].includes(inst.profile) ? equitySession(Date.parse(observed)) : 'unknown') : 'unknown';
  const liveWindow = inst.profile === 'C' ? observedWindow === 'continuous_market'
    : ['regular','premarket','postmarket'].includes(observedWindow);
  out.freshness = {...out.freshness, status, age_seconds:observed && !invalidTimes ? (now-Date.parse(observed))/1000 : null,
    basis:observed ? 'provider observed_at' : r.observation_granularity === 'day' ? 'provider observation_date; no synthetic intraday timestamp' : 'provider observation time unavailable',
    observation_date:r.observation_date ?? null, observation_age_days:observationAgeDays,
    observation_window:observedWindow, live:status === 'fresh' && r.source === 'dxlink' && !carried && !p.feed_stale && !r.refresh_error && r.observation_granularity === 'event' && ['last_trade','bid_ask_midpoint'].includes(r.price_basis) && ['regular','extended'].includes(r.session) && liveWindow};
  const reasons = [];
  if (out.price == null) reasons.push(r.price == null ? 'missing_current' : 'invalid_current');
  if (out.previous_close == null) reasons.push(ref.value == null ? 'missing_reference' : 'invalid_reference');
  if (invalidTimes) reasons.push('future_observation_or_acquisition');
  if (carried) reasons.push('last_known_price');
  if (isFuture && !sameContract) reasons.push('contract_unresolved_or_mismatch');
  if (ref.instrument_id != null && ref.instrument_id !== out.id) reasons.push('reference_identity_mismatch');
  if (ref.source != null && ref.source !== origin) reasons.push('mixed_reference_source');
  if (ref.source_symbol != null && out.source_symbol != null && ref.source_symbol !== out.source_symbol) reasons.push('reference_symbol_mismatch');
  if (ref.adjustment_basis != null && r.adjustment_basis != null && ref.adjustment_basis !== r.adjustment_basis) reasons.push('adjustment_basis_mismatch');
  if (ref.observed_at != null && Date.parse(ref.observed_at) > now || currentDate && referenceDate && referenceDate >= currentDate) reasons.push('invalid_reference_date');
  // A prior close more than one exchange session back is not the previous
  // session; comparing across the gap would mislabel a multi-day move. Outside
  // the published calendar the session count is unknowable, so a conservative
  // calendar-day bound still catches a clearly stale reference.
  const sessionGap = currentDate && referenceDate && ['E','V','Y'].includes(inst.profile) ? sessionsBetween(referenceDate, currentDate) : null;
  const dayGap = currentDate && referenceDate ? (Date.parse(currentDate) - Date.parse(referenceDate)) / 86400000 : null;
  // Crypto's documented cut is the previous UTC day, so it must be exact: a
  // missing day turns the figure into a multi-day move wearing a daily label.
  const gapInvalid = dayGap == null ? false
    : inst.profile === 'C' ? dayGap !== 1
    : ['E','V','Y'].includes(inst.profile) ? (sessionGap !== null ? sessionGap > 1 : dayGap > 5)
    : false;
  if (gapInvalid) reasons.push('reference_not_previous_session');
  const allowedBases = REFERENCE_BASES[inst.profile] ?? null;
  if (ref.price_basis != null && allowedBases) {
    if (!allowedBases.includes(ref.price_basis)) reasons.push('reference_basis_not_valid_for_instrument');
    else if (ref.session != null && !(BASIS_SESSIONS[ref.price_basis] ?? []).includes(ref.session)) reasons.push('reference_session_basis_conflict');
  }
  const unavailable = reasons.length > 0;
  const partial = [];
  if (!r.instrument_id) partial.push('instrument_identity_unverified');  if (!observed && !r.observation_date) partial.push('observation_time_unverified');
  if (!referenceDate) partial.push('reference_time_unverified');
  if (!ref.instrument_id || !ref.source || !ref.source_symbol) partial.push('reference_identity_unverified');
  if ([r.adjustment_basis,ref.adjustment_basis].some(v => !v || ['unknown','unverified'].includes(v))) partial.push('adjustment_basis_unverified');
  if (!ref.observation_timezone || !ref.observation_granularity) partial.push('reference_precision_or_timezone_unverified');
  if (!r.observation_timezone || !r.observation_granularity) partial.push('observation_precision_or_timezone_unverified');
  if (!['previous_close','prev_day_close','daily_bar_close','daily_close','official_close','settlement'].includes(ref.price_basis)) partial.push('reference_basis_unverified');
  if (!r.session || !ref.session) partial.push('observation_and_reference_session_unverified');
  if (market === 'unknown') partial.push('market_calendar_unverified');
  // A daily bar may be the still-forming current session; its difference is a
  // valid same-source daily comparison but is not a finalized close.
  if (r.observation_granularity === 'day') partial.push('daily_bar_observation_not_final');
  if (out.previous_close === 0) partial.push('zero_reference');
  const absolute = unavailable ? null : out.price - out.previous_close;
  const pct = absolute == null || out.previous_close === 0 || inst.change_unit === 'bps' || inst.asset_class === 'rates' ? null : 100*absolute/out.previous_close;
  const bps = absolute != null && (inst.change_unit === 'bps' || inst.asset_class === 'rates') ? absolute*100 : null;
  // Finite inputs can still overflow an intermediate (1e308 against 1e-9), and
  // JSON would silently turn a non-finite result into null downstream.
  const nonfinite = [absolute, pct, bps].some(v => v != null && !Number.isFinite(v));
  out.change = {status:nonfinite || unavailable ? 'unavailable' : partial.length ? 'partial' : 'ok',
    reasons:nonfinite ? [...reasons, 'nonfinite_arithmetic'] : unavailable ? reasons : partial,
    absolute:nonfinite ? null : round(absolute,6), absolute_unit:absolute == null ? null : USD_UNITS.has(inst.unit) || inst.unit === 'USD/share' ? 'USD' : inst.unit,
    percent:nonfinite ? null : round(pct,6),
    bps:nonfinite ? null : round(bps,4),
    reference_basis:ref.price_basis ?? null, reference_at:ref.observed_at ?? null, reference_session:ref.session ?? null,
    current_session:r.session ?? null, adjustment_basis:ref.adjustment_basis ?? null, collector_receipt_window:out.change.collector_receipt_window,
    derivation:{absolute:'consumer: normalized price - normalized reference.value',percent:'consumer: 100 x absolute / reference',bps:'consumer: normalized yield difference x 100'}};
  out.warnings = out.warnings.filter(w => !/Undated yfinance|Nonpositive current|Legacy strip/.test(w));
  out.market_session = r.market_session ?? null;
  if (inst.asset_class === 'crypto') out.change_24h = rolling24h(out, r, now, inst);
  if (r.refresh_error) out.warnings.push('Newer refresh failed; the retained observation and its original date/time are unchanged.');
  if (futureObservation) out.warnings.push('Provider observation is in the future; live status and comparisons are withheld.');
  return out;
}
function rows(p, now, members = universe.instruments) {
  const known = members.map(inst => {
    const r = p.prices[inst.label] ?? {};
    const out = enrich(row(inst.label,r,p.timestamp,now,p.feed_stale),r,p,now);
    out.enabled = inst.enabled; out.compact = Boolean(inst.compact); out.support_status = inst.status ?? null; out.source_readiness = inst.source_readiness ?? null;
    out.profile = inst.profile; out.universe_version = universe.universe_version;
    // Activation is a production gate. A row the registry has not enabled is
    // never presented as usable, even if a producer unexpectedly published it;
    // the raw artifact value stays visible as evidence rather than being used.
    const unexpected = !inst.enabled && Object.hasOwn(p.prices, inst.label);
    if (unexpected) {
      out.price = null; out.previous_close = null; out.freshness.live = false;
      out.change = {...out.change, status:'unavailable', reasons:['candidate_not_activated'], absolute:null, percent:null, bps:null};
      out.warnings = [...out.warnings, 'Published data for an instrument the registry has not enabled; not used for price, change or live status.'];
    }
    out.coverage = {status:out.price == null ? 'missing' : 'available', reason:out.price == null ?
      unexpected ? 'published_without_activation' :
      !Object.hasOwn(p.prices,inst.label) ? inst.enabled ? 'not_published' : 'candidate_disabled_pending_provider_review' : 'price_unavailable' : null};
    return out;
  });
  return { known, unmapped:Object.keys(p.prices).filter(label => !Object.hasOwn(INSTRUMENTS,label)) };
}

const units = { price: 'row.unit (10Y normalized to percent; fut.6j and fx.jpyusd in USD per JPY)', 'change.absolute': 'row.change.absolute_unit (USD only for USD-denominated rows)',
  'change.percent': 'percent; v2 consumer-normalized pair arithmetic, legacy producer-rounded to 2 dp', 'change.bps': 'basis points (yield rows only; no percent-of-yield)', 'change_24h.percent': 'percent against a bounded same-instrument rolling 24-hour sample (crypto only; sample tolerance ±30 minutes)', 'freshness.age_seconds': 'v2 seconds since provider event at query time; legacy receipt age only, never live' };
const warnings = ['Source is the dashboard price artifact (intraday/prices.json); all 100 configured instruments are returned, including explicit missing candidates. Selection is not proof of provider support.',
  'Rows are asynchronous observations, not a simultaneous snapshot. quote_ts is collector receipt time, never provider observation time. Legacy receipt-only rows are never live.',
  'Date-only daily bars have no synthetic intraday timestamp. Unknown calendars and incomplete reference provenance remain explicit; partial numeric comparisons are not verified session moves.',
  'ETF proxies (QQQ, SPY, USO) keep their own identities; a proxy is never the index or commodity.',
  'Price moves do not establish a news cause.'];

function gaps(known) {
  const ids = new Set(known.map(r => r.id));
  return [...known.filter(r => r.price == null).map(r => ({label:r.label,id:r.id,name:r.name,asset_class:r.asset_class,reason:r.coverage.reason})),
    ...GAPS.filter(g => !ids.has(g.id) && g.id !== 'universe.curated_stocks')];
}
module.exports = { KEY, STALE_SECONDS, MAX_LABELS, ASSET_CLASSES, SORTS, LABEL, SELECTOR, INSTRUMENTS, GAPS, universe, gaps, validate, rows, units, warnings };
