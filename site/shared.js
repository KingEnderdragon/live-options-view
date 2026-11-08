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
// browser globals — they were never among the 26.
// The chain underlying names (CHAIN_UNDERLYINGS, chainLatestPath, occRoot,
// validateChainPayload, ChainSelection, relativeOiRanges,
// summarizeSpyHealth) moved to docs/shared/chain.js (B3 slice 5) and ARE
// re-exported below, because require('../docs/shared.js') always returned
// them (see the Node entry point). relativeOiRanges still defaults its
// display to DISPLAY, which chain.js provides itself under Node via its own
// guarded require of heatmap.js. Kept
// dependency-free of paper-trading state (positions/click handling are passed
// in via opts) so diag.html can exercise the exact same parse/threshold/render
// code the live app runs, without dragging in unrelated app state.


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
// The chain names moved to docs/shared/chain.js (B3 slice 5) and are
// re-exported from there the same way; relativeOiRanges still defaults its
// display to DISPLAY, which chain.js provides itself under Node via its own
// guarded require of heatmap.js — so the globalThis.DISPLAY line this entry
// used to carry is gone.
if (typeof module !== 'undefined' && typeof require === 'function') {
  const heatmap = require('./shared/heatmap.js');
  const quotes = require('./shared/quotes.js');
  const format = require('./shared/format.js');
  const chain = require('./shared/chain.js');
  module.exports = {
    CHAIN_UNDERLYINGS: chain.CHAIN_UNDERLYINGS, chainLatestPath: chain.chainLatestPath, occRoot: chain.occRoot, validateChainPayload: chain.validateChainPayload, ChainSelection: chain.ChainSelection, relativeOiRanges: chain.relativeOiRanges,
    summarizeSpyHealth: chain.summarizeSpyHealth,
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
