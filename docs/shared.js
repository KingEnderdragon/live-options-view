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
// browser globals — they were never among the 26.
// The panel formatters (formatVwapRvol, formatMomentum, the squeeze
// formatters, fmtSteoDelta, findRevision) and the order validators
// (normalizePaperOrder, normalizeShareOrder) moved to docs/shared/format.js
// (B3 slice 4) and ARE re-exported below, because require('../docs/shared.js')
// always returned them (see the Node entry point); the two staleness
// constants (VWAP_RVOL_CLOSED_AFTER_MS, SQUEEZE_SCAN_STALE_AFTER_MS) stay
// browser globals — they were never among the 26. Kept
// dependency-free of paper-trading state (positions/click handling are passed
// in via opts) so diag.html can exercise the exact same parse/threshold/render
// code the live app runs, without dragging in unrelated app state.

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
// The panel formatters and order validators moved to docs/shared/format.js
// (B3 slice 4) and are re-exported from there the same way.
// relativeOiRanges still defaults its display to
// DISPLAY, which under Node is provided as a global below — in the browser it
// is the lexical global heatmap.js declares. A `var DISPLAY` here would break
// the browser: it would pre-create the global before heatmap.js's
// `const DISPLAY` declares it, so the property is set on globalThis instead.
if (typeof module !== 'undefined' && typeof require === 'function') {
  const heatmap = require('./shared/heatmap.js');
  const quotes = require('./shared/quotes.js');
  const format = require('./shared/format.js');
  if (typeof globalThis.DISPLAY === 'undefined') globalThis.DISPLAY = heatmap.DISPLAY;
  module.exports = {
    CHAIN_UNDERLYINGS, chainLatestPath, occRoot, validateChainPayload, ChainSelection, relativeOiRanges,
    summarizeSpyHealth,
    LiveQuoteService: quotes.LiveQuoteService, LiveQuotePoller: quotes.LiveQuotePoller,
    TickerStateStore: quotes.TickerStateStore, tickerSessionState: quotes.tickerSessionState,
    SHARE_QUOTE_MAX_AGE_MS: quotes.SHARE_QUOTE_MAX_AGE_MS, freshShareQuote: quotes.freshShareQuote,
    formatVwapRvol: format.formatVwapRvol, formatMomentum: format.formatMomentum,
    fmtSteoDelta: format.fmtSteoDelta, findRevision: format.findRevision,
    formatSqueezeScheduleStatus: format.formatSqueezeScheduleStatus, formatSqueezeScanStatus: format.formatSqueezeScanStatus,
    describeSqueezeAcquisitionFailure: format.describeSqueezeAcquisitionFailure, formatSqueezeFirstSeen: format.formatSqueezeFirstSeen,
    parseRetryAfter: quotes.parseRetryAfter, normalizePaperOrder: format.normalizePaperOrder, normalizeShareOrder: format.normalizeShareOrder,
    isTradeableShareSymbol: quotes.isTradeableShareSymbol, computeAtmWindow: heatmap.computeAtmWindow,
  };
}
