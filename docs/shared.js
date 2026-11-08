// shared.js — the entry point for the shared front-end library. The code used
// to live in this one file (1,017 lines); it is being split by concern (B3,
// Nov 2026) into docs/shared/*.js, each a plain classic script like this file,
// loaded by its own <script> tag in docs/index.html and docs/diag.html. This
// file keeps the Node surface: under Node it re-exports the same 26 names
// require('../docs/shared.js') always returned (pinned by
// tests/csv_ranges.test.js); in the browser the export block at the bottom is
// a no-op and the concern files define the globals directly. The CSV/range
// helpers (parseCsv, parseRangesCsv, getThresh, oiBucket) are deliberately NOT
// re-exported here — as before, they are browser globals, defined by
// docs/shared/csv-ranges.js; require that file directly under Node. The heatmap
// names (DISPLAY, TIER_COLORS, the cell formatters, buildHeatmapRows) are
// likewise browser globals, defined by docs/shared/heatmap.js — except
// computeAtmWindow, which IS re-exported below, because
// require('../docs/shared.js') always returned it (see the Node entry point).
// The live-quote names (LiveQuoteService, LiveQuotePoller, TickerStateStore,
// tickerSessionState, SHARE_QUOTE_MAX_AGE_MS, freshShareQuote, parseRetryAfter,
// isTradeableShareSymbol) are likewise defined by docs/shared/quotes.js and
// ARE re-exported below, because require('../docs/shared.js') always returned
// them (see the Node entry point); TRADEABLE_SHARE_SYMBOLS and sameSet stay
// browser globals — they were never among the 26. Kept
// dependency-free of paper-trading state (positions/click handling are passed
// in via opts) so diag.html can exercise the exact same parse/threshold/render
// code the live app runs, without dragging in unrelated app state.

// -- VWAP/RVOL display formatting --------------------------------------------
// Pure formatting for the `underlying_market` block published on
// intraday/latest.json (see crassus/crassus/market.py's UnderlyingMarket and
// docs/plans/2026-07-vwap-rvol.md) -- the same canonical record the
// momentum_qqq strategy reads, so the UI and the strategy never disagree on
// what "VWAP" or "RVOL" mean here. No DOM access, kept testable the same
// simple way as tickerSessionState/TickerStateStore in docs/shared/quotes.js.
//
// `state` is one of the same four values docs/index.html already uses for
// ticker tiles (live/stale/fallback/closed, see TickerStateStore._decorate
// in docs/shared/quotes.js and the `.ticker-btn[data-state=...]` CSS) so a stale VWAP looks exactly
// as visually "stale" as a stale ticker quote already does -- no new state
// vocabulary. "closed" is inferred here from the observation's own age
// (clearly a leftover reading from a prior session), not from session-hour
// math, because the collector can only ever write "live"/"stale" itself
// (see collector.py's `_compute_underlying_market` -- it has no way to know
// it has stopped running).
const VWAP_RVOL_CLOSED_AFTER_MS = 4 * 60 * 60 * 1000;

function formatVwapRvol(underlyingMarket, nowMs = Date.now()) {
  if (!underlyingMarket) {
    return { text: 'VWAP/RVOL: unavailable', state: 'fallback' };
  }
  const um = underlyingMarket;
  const observedMs = Date.parse(um.spot_ts || um.vwap_ts || '');
  const ageMs = Number.isFinite(observedMs) ? nowMs - observedMs : null;
  if (ageMs != null && ageMs > VWAP_RVOL_CLOSED_AFTER_MS) {
    return { text: 'VWAP/RVOL: session closed', state: 'closed' };
  }

  const vwapPart = um.vwap != null
    ? (um.price_vs_vwap_pct != null
      ? `VWAP ${um.vwap.toFixed(2)} (${um.price_vs_vwap_pct >= 0 ? '+' : ''}${um.price_vs_vwap_pct.toFixed(2)}%)`
      : `VWAP ${um.vwap.toFixed(2)}`)
    : 'VWAP —';
  const rvol = um.rvol || {};
  const rvolPart = rvol.status === 'ok' && rvol.multiple != null
    ? `RVOL ${rvol.multiple.toFixed(2)}×`
    : rvol.status === 'insufficient_history' ? 'RVOL warming up'
    : 'RVOL —';

  return { text: `${vwapPart} | ${rvolPart}`, state: um.freshness === 'live' ? 'live' : 'stale' };
}

// -- time-series momentum display formatting ---------------------------------
// Pure formatting for `underlying_market.momentum` (see
// crassus/crassus/market.py's UnderlyingMarket.momentum_* fields and
// docs/plans/2026-07-momentum-indicator.md). Display/log-only reference
// signal -- computed by the collector the same way momentum_qqq computes its
// own live trading signal, but independently, not a shared value (each
// momentum_qqq account runs its own separately-configured lookback). Same
// state vocabulary and "closed"-by-age inference as formatVwapRvol above.
function formatMomentum(underlyingMarket, nowMs = Date.now()) {
  if (!underlyingMarket) {
    return { text: 'Momentum: unavailable', state: 'fallback' };
  }
  const um = underlyingMarket;
  const observedMs = Date.parse(um.spot_ts || '');
  const ageMs = Number.isFinite(observedMs) ? nowMs - observedMs : null;
  if (ageMs != null && ageMs > VWAP_RVOL_CLOSED_AFTER_MS) {
    return { text: 'Momentum: session closed', state: 'closed' };
  }

  const momentum = um.momentum || {};
  let text;
  if (momentum.status === 'ok' && momentum.return_pct != null) {
    const sign = momentum.return_pct >= 0 ? '+' : '';
    const arrow = momentum.direction === 'up' ? '▲' : momentum.direction === 'down' ? '▼' : '→';
    const lookback = momentum.lookback_minutes != null ? Math.round(momentum.lookback_minutes) : '?';
    text = `Momentum ${arrow} ${sign}${momentum.return_pct.toFixed(2)}% (${lookback}m)`;
  } else if (momentum.status === 'warming_up') {
    text = 'Momentum warming up';
  } else if (momentum.status === 'stale_anchor') {
    text = 'Momentum stale (gap in data)';
  } else {
    text = 'Momentum —';
  }

  return { text, state: um.freshness === 'live' ? 'live' : 'stale' };
}

// -- short-squeeze scanner panel formatting (OA-191) -------------------------
// Pure formatting/tracking for docs/index.html's squeeze panel, which
// displays the independently-maintained scanner's published `latest.json`
// pointer (OwenTanzer/short-squeeze-scanner's squeeze_scanner/storage.py
// `upload()`). Same producer/display split as formatVwapRvol/formatMomentum
// above: this page never computes a score, only formats what was published.
// Legacy manual formatter retained for compatibility and acquisition-time labels.
// The live panel uses formatSqueezeScheduleStatus for freshness decisions.
const SQUEEZE_SCAN_STALE_AFTER_MS = 6 * 60 * 60 * 1000;

// PR #105 review, finding 2: freshness must be judged against ACQUISITION
// time (when the scan actually ran), not `published_at` (when the pointer
// file happened to be written to R2) -- those can differ by a day if an
// upload is delayed, and `published_at` alone would render a September 22
// scan uploaded September 23 as fresh ("live"). `finished_at` (falling back
// to `started_at`) is the acquisition-time field; `published_at` is never
// used for the age/staleness judgment here anymore.
function formatSqueezeScanStatus(pointer, nowMs = Date.now()) {
  if (!pointer) return { text: 'No scan published yet', state: 'fallback' };
  const acquiredMs = Date.parse(pointer.finished_at || pointer.started_at || '');
  const dateText = Number.isFinite(acquiredMs)
    ? new Date(acquiredMs).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'America/New_York' })
    : 'unknown date';
  const timeText = Number.isFinite(acquiredMs)
    ? new Date(acquiredMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  // A prior-day scan is explicitly stale regardless of raw age in ms -- a
  // 9pm run is under SQUEEZE_SCAN_STALE_AFTER_MS old at 11pm the same
  // night, but is still yesterday's data once a new session has started.
  const todayKey = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(nowMs));
  const scanDateKey = Number.isFinite(acquiredMs)
    ? new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(acquiredMs))
    : null;
  const priorDay = scanDateKey != null && scanDateKey !== todayKey;
  const label = priorDay ? `${dateText}, ${timeText}` : timeText;

  if (pointer.status === 'empty') {
    return { text: `Scan complete ${label} ET — no eligible candidates`, state: 'stale' };
  }
  const ageMs = Number.isFinite(acquiredMs) ? nowMs - acquiredMs : null;
  const stale = priorDay || ageMs == null || ageMs > SQUEEZE_SCAN_STALE_AFTER_MS;
  return { text: `Scan ${label} ET${stale ? ' (stale)' : ''}`, state: stale ? 'stale' : 'live' };
}

// Calendar slots are exported from the producer's XNYS calendar and activation
// configuration. An expired/unavailable calendar never implies a fresh scan.
function formatSqueezeScheduleStatus(pointer, attempt, schedule, calendar, nowMs = Date.now()) {
  const label = formatSqueezeScanStatus(pointer, nowMs).text.replace(' (stale)', '');
  const result = (warning, state = 'stale', suffix = '') => ({ text: label + suffix, state, warning });
  const validFrom = Date.parse(calendar?.valid_from || '');
  const validUntil = Date.parse(calendar?.valid_until || '');
  if (calendar?.schema_version !== 1 || calendar?.calendar !== 'XNYS' ||
      calendar?.timezone !== 'America/New_York' || !Array.isArray(calendar?.slots) ||
      !Number.isFinite(validFrom) || !Number.isFinite(validUntil) ||
      nowMs < validFrom || nowMs >= validUntil) {
    return result('Schedule calendar unavailable or outside its coverage. Freshness is unconfirmed.');
  }
  const slots = calendar.slots.map(value => Date.parse(value));
  if (slots.some((value, i) => !Number.isFinite(value) || value < validFrom ||
      value >= validUntil || (i && value <= slots[i - 1]))) {
    return result('Schedule calendar is invalid. Freshness is unconfirmed.');
  }
  const due = slots.filter(value => value <= nowMs).at(-1);
  if (due == null) return result('Waiting for the first scheduled scan.', 'fallback');
  const slotLabel = new Date(due).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York',
  }) + ' ET';
  const matching = Date.parse(schedule?.scheduled_for || '') === due;
  if (matching && ['missed', 'interrupted'].includes(schedule.status)) {
    return result(`Scheduled scan ${slotLabel}: ${schedule.status}. Showing the last successful result, if available.`);
  }
  if (matching && schedule.status === 'claimed') {
    return result(nowMs - due <= 40 * 60_000
      ? `Scheduled scan ${slotLabel} is acquiring or awaiting publication. Previous results remain below.`
      : `Scheduled scan ${slotLabel} has no confirmed completion. Previous results remain below.`);
  }
  const failure = describeSqueezeAcquisitionFailure(pointer, attempt);
  if (failure) return result(failure);
  if (matching && schedule.status === 'finished' && !['complete', 'empty'].includes(schedule.acquisition_status)) {
    return result(`Scheduled scan ${slotLabel} did not complete: ${schedule.acquisition_status || 'unknown'}. Showing the last successful result, if available.`);
  }
  const started = Date.parse(pointer?.started_at || '');
  const finished = Date.parse(pointer?.finished_at || '');
  const current = pointer?.sampling_mode === 'scheduled' && Date.parse(pointer?.scheduled_for || '') === due &&
    ['complete', 'empty'].includes(pointer?.status) && Number.isFinite(started) && Number.isFinite(finished) &&
    started >= due && started <= due + 10 * 60_000 && finished >= started && finished <= nowMs &&
    matching && schedule.status === 'finished' && schedule.run_id === pointer.run_id &&
    schedule.acquisition_status === pointer.status;
  if (current) {
    const nyDay = ms => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(ms));
    const priorSession = nyDay(due) !== nyDay(nowMs);
    return result(null, priorSession ? 'stale' : 'live', priorSession ? ' — previous session' : ' — latest scheduled scan');
  }
  if (matching && schedule.status === 'finished') {
    return result(`Scheduled scan ${slotLabel} finished, but its published result is not yet confirmed. Showing the last successful result, if available.`);
  }
  return result(nowMs - due <= 10 * 60_000
    ? `Scheduled refresh due ${slotLabel}; waiting for the scan.`
    : `No confirmed schedule status for ${slotLabel}. Results may be stale.`);
}

// PR #105 review, finding 1: `latest.json` only ever advances on a
// successful (complete/empty) run -- storage.py's `upload()` always
// advances `latest-attempt.json` regardless of outcome. Without checking
// the attempt pointer too, a failed or partial noon scan is invisible: the
// morning's successful result keeps showing as if nothing had gone wrong
// since. `successPointer`/`attemptPointer` are the parsed latest.json /
// latest-attempt.json bodies (either may be null if nothing was ever
// published to that path yet).
function describeSqueezeAcquisitionFailure(successPointer, attemptPointer) {
  if (!attemptPointer) return null;
  if (attemptPointer.status === 'complete' || attemptPointer.status === 'empty') return null; // the attempt succeeded
  const attemptIsNewer = !successPointer ||
    Date.parse(attemptPointer.started_at || '') > Date.parse(successPointer.started_at || '');
  if (!attemptIsNewer) return null; // the failure is older than the successful result already shown
  const startedMs = Date.parse(attemptPointer.started_at || '');
  const time = Number.isFinite(startedMs)
    ? new Date(startedMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  return successPointer
    ? `Most recent scan (started ${time} ET) did not complete: ${attemptPointer.status}. Showing the last successful result below.`
    : `No successful scan yet. Most recent attempt (started ${time} ET): ${attemptPointer.status}.`;
}

// PR #105 review round 2: renders the "First seen today" column against
// the real producer contract (short-squeeze-scanner#2's
// docs/archive-contract.md, "Display contract v1: canonical first
// appearance") -- `first_seen_at` (UTC write-attempt timestamp, null if
// unknown) and `is_new` (true only for the run that first introduces the
// ticker; false for a known existing candidate, including a reappearance;
// null if unknown/legacy). The round-1 placeholder
// (`row.first_seen_at ? 'New' : 'unavailable'`) treated ANY non-null
// first_seen_at as New and never rendered the time itself -- both an
// incumbent (is_new=false) and a genuine newcomer (is_new=true) rendered
// identically as bare "New" text. `is_new` alone decides the New label;
// first_seen_at (New York time) is shown either way when known.
function formatSqueezeFirstSeen(row) {
  if (row.first_seen_at == null || row.is_new == null) return { text: 'unavailable', isNew: false };
  const seenMs = Date.parse(row.first_seen_at);
  const time = Number.isFinite(seenMs)
    ? new Date(seenMs).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
    : 'unknown time';
  return { text: time, isNew: row.is_new === true };
}

// EIA STEO crude calibration panel formatting (docs/index.html's
// fetchSteoCalibration). Pure, DOM-free -- same rationale as
// formatVwapRvol/formatMomentum above: testable directly, no fixture DOM
// needed.
function fmtSteoDelta(delta, unit = '') {
  if (delta == null) return { text: '', cls: '' };
  const sign = delta >= 0 ? '+' : '';
  return { text: `${sign}${delta.toFixed(2)}${unit}`, cls: delta > 0 ? 'up' : delta < 0 ? 'down' : '' };
}

function findRevision(revisions, period) {
  return (revisions || []).find(r => r.period === period) || null;
}

function normalizePaperOrder({ side, quantity, orderType = 'market', limitPrice = null } = {}) {
  const qty = Number(quantity);
  if (side !== 'buy' && side !== 'sell') {
    return { error: 'Choose buy or sell.' };
  }
  if (!Number.isInteger(qty) || qty < 1 || qty > 100_000) {
    return { error: 'Quantity must be a positive whole number.' };
  }
  if (orderType !== 'market' && orderType !== 'limit') {
    return { error: 'Choose market or limit.' };
  }

  if (orderType === 'market') {
    return { value: { side, qty, order_type: 'market' } };
  }

  const price = Number(limitPrice);
  if (!Number.isFinite(price) || price <= 0 || price > 1_000_000) {
    return { error: 'Limit price must be greater than zero.' };
  }
  const cents = Math.round(price * 100);
  if (Math.abs(price * 100 - cents) > 1e-7) {
    return { error: 'Limit price must use increments of $0.01.' };
  }
  return { value: { side, qty, order_type: 'limit', limit_price: cents / 100 } };
}

function normalizeShareOrder({
  side, quantity, heldQuantity = 0, orderType = 'market', limitPrice = null,
} = {}) {
  const normalized = normalizePaperOrder({ side, quantity, orderType, limitPrice });
  if (normalized.error) return normalized;
  if (side === 'sell' && normalized.value.qty > Math.max(0, Number(heldQuantity) || 0)) {
    return { error: 'You cannot sell more shares than you hold.' };
  }
  return normalized;
}

// ── chain underlying selection (#121) ───────────────────────────────────────
// QQQ keeps its original artifact; SPY has its own tree (see
// SPY_COLLECTION.md). Anything not listed is unsupported -- never mapped
// onto QQQ's file.
const CHAIN_UNDERLYINGS = ['QQQ', 'SPY'];
const CHAIN_LATEST_PATHS = { QQQ: 'intraday/latest.json', SPY: 'intraday/spy/latest.json' };

function chainLatestPath(symbol) {
  return Object.hasOwn(CHAIN_LATEST_PATHS, symbol) ? CHAIN_LATEST_PATHS[symbol] : null;
}

function occRoot(occSymbol) {
  const match = /^([A-Z]+)/.exec(String(occSymbol || '').replace(/\s+/g, ''));
  return match ? match[1] : '';
}

// Rejects a payload that doesn't prove it belongs to `symbol`, so a QQQ
// snapshot (or a mixed one) can never be rendered under a SPY heading or
// vice versa. Pre-#121 QQQ payloads carry no top-level symbol; they're only
// accepted when QQQ itself was requested.
function validateChainPayload(data, symbol) {
  if (!CHAIN_UNDERLYINGS.includes(symbol)) return { ok: false, reason: `${symbol} is not a supported underlying` };
  if (!data || typeof data !== 'object' || !Array.isArray(data.rows)) return { ok: false, reason: 'snapshot is malformed' };
  const declared = data.symbol ?? (symbol === 'QQQ' ? 'QQQ' : null);
  if (declared !== symbol) return { ok: false, reason: `snapshot is labeled ${declared ?? 'without a symbol'}, not ${symbol}` };
  const marketSymbol = data.underlying_market?.symbol;
  if (marketSymbol != null && marketSymbol !== symbol) return { ok: false, reason: `underlying readings belong to ${marketSymbol}` };
  if (data.rows.some(row => occRoot(row?.OptionSymbol) !== symbol)) return { ok: false, reason: `snapshot contains non-${symbol} contracts` };
  return { ok: true, reason: null };
}

// Tracks which underlying is on screen. Every request takes a token; a
// response is applied only if its token is still current, so a slow reply
// for the previous selection can't overwrite (or be labeled as) the new one.
class ChainSelection {
  constructor(symbol = 'QQQ') {
    this.symbol = CHAIN_UNDERLYINGS.includes(symbol) ? symbol : 'QQQ';
    this.generation = 0;
  }
  select(symbol) {
    if (!CHAIN_UNDERLYINGS.includes(symbol)) return false;
    if (symbol !== this.symbol) { this.symbol = symbol; this.generation++; }
    return true;
  }
  token() { return { symbol: this.symbol, generation: this.generation }; }
  accepts(token) { return !!token && token.symbol === this.symbol && token.generation === this.generation; }
}

// OIranges.csv is calibrated on QQQ's history only. For another underlying
// there is no calibration yet, so heat levels are relative to this snapshot's
// own nonzero OI distribution (p25/p50/p75/p90) -- labeled as such in the UI.
// Returns the same shape parseRangesCsv() produces, for getThresh().
function relativeOiRanges(rows, display = DISPLAY) {
  const values = (rows || []).map(r => parseInt(r?.OpenInterest ?? 0) || 0).filter(v => v > 0).sort((a, b) => a - b);
  if (!values.length) return null;
  const q = p => values[Math.min(values.length - 1, Math.floor(p * (values.length - 1)))];
  const thresholds = [q(0.25), q(0.5), q(0.75), q(0.9)];
  const regular = {};
  for (let o = -display; o <= display; o++) regular[o] = { call: thresholds, put: thresholds };
  return { '0DTE_Regular': regular };
}

function summarizeSpyHealth(payload, nowMs = Date.now()) {
  const spy = payload?.spy;
  if (!spy) return { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY health has not been published' };
  const updated = Date.parse(payload.updated_at);
  if (!Number.isFinite(updated) || nowMs - updated > 45_000 || nowMs - updated < -30_000) {
    return { state: 'stale', text: 'SPY: stale', detail: 'SPY health update is old or untimed' };
  }
  if (spy.collection_status !== 'collecting') {
    const status = spy.collection_status || 'unavailable';
    return { state: 'unavailable', text: `SPY: ${status.replaceAll('_', ' ')}`, detail: spy.reason || status };
  }
  const snapshotAt = Date.parse(spy.last_snapshot_upload_time);
  if (!Number.isFinite(snapshotAt)) {
    const started = Date.parse(payload.process_start_time);
    if (Number.isFinite(started) && nowMs - started > 180_000) {
      return { state: 'unavailable', text: 'SPY: unavailable', detail: 'No SPY snapshot uploaded' };
    }
    return { state: 'connecting', text: 'SPY: warming up', detail: 'No SPY snapshot uploaded yet' };
  }
  if (nowMs - snapshotAt > 180_000 || nowMs - snapshotAt < -30_000 || spy.spot?.status === 'stale') {
    return { state: 'stale', text: 'SPY: stale', detail: 'SPY snapshot or spot is stale' };
  }
  if (spy.spot?.status !== 'live') {
    return { state: 'unavailable', text: 'SPY: unavailable', detail: 'SPY spot has no current provider observation' };
  }
  return { state: 'live', text: 'SPY: live', detail: 'SPY snapshot and spot are current' };
}

// Node entry point: the concern modules are plain classic scripts in the
// browser (loaded by their own <script> tags — see docs/index.html and
// docs/diag.html), so this block only runs under Node, where it re-exports
// exactly the same 26 names this file always exported. The CSV/range helpers
// are not re-exported (see the header): they stay browser globals.
// computeAtmWindow moved to docs/shared/heatmap.js (B3 slice 2) and is
// re-exported from there; the live-quote names moved to docs/shared/quotes.js
// (B3 slice 3) and are re-exported from there the same way.
// relativeOiRanges still defaults its display to
// DISPLAY, which under Node is provided as a global below — in the browser it
// is the lexical global heatmap.js declares. A `var DISPLAY` here would break
// the browser: it would pre-create the global before heatmap.js's
// `const DISPLAY` declares it, so the property is set on globalThis instead.
if (typeof module !== 'undefined' && typeof require === 'function') {
  const heatmap = require('./shared/heatmap.js');
  const quotes = require('./shared/quotes.js');
  if (typeof globalThis.DISPLAY === 'undefined') globalThis.DISPLAY = heatmap.DISPLAY;
  module.exports = {
    CHAIN_UNDERLYINGS, chainLatestPath, occRoot, validateChainPayload, ChainSelection, relativeOiRanges,
    summarizeSpyHealth,
    LiveQuoteService: quotes.LiveQuoteService, LiveQuotePoller: quotes.LiveQuotePoller,
    TickerStateStore: quotes.TickerStateStore, tickerSessionState: quotes.tickerSessionState,
    SHARE_QUOTE_MAX_AGE_MS: quotes.SHARE_QUOTE_MAX_AGE_MS, freshShareQuote: quotes.freshShareQuote,
    formatVwapRvol, formatMomentum,
    fmtSteoDelta, findRevision, formatSqueezeScheduleStatus, formatSqueezeScanStatus, describeSqueezeAcquisitionFailure, formatSqueezeFirstSeen,
    parseRetryAfter: quotes.parseRetryAfter, normalizePaperOrder, normalizeShareOrder,
    isTradeableShareSymbol: quotes.isTradeableShareSymbol, computeAtmWindow: heatmap.computeAtmWindow,
  };
}
