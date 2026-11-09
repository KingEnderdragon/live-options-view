# Adaptive quote-card panel (#179)

Issue: [#179](https://github.com/OwenTanzer/live-options-view/issues/179). The
existing QQQ/SPY/VIX/macro strip becomes a three-part panel: fixed core markets,
personal pins and session-aware movers. It is still one compact panel, not a
second dashboard, and it reuses the published price artifact rather than adding
a provider connection per card.

## Sections

**Core** is always visible: `QQQ`, `SPY`, `VIX`, `10Y`, `EUR/USD`, `/CL`,
`/GC`, `BTC/USD`. They cover equity benchmarks, volatility, the Treasury yield,
a currency, oil, gold and Bitcoin while preserving the old QQQ/SPY context.
"Always visible" is not "always live": a closed market shows `closed`, a daily
source shows `daily`, and a missing value shows `—`.

**Pinned** starts with `SMH`, `IGV`, `USO`, `AAPL` and accepts up to twelve
instruments from the tracked 100. Pin, unpin and left/right reorder use real
buttons, so keyboard, touch and screen readers share one path. The order is
persisted in `localStorage` under `optionsView.quotePins.v1` on this device.
This is explicitly device-local: the existing account system does not own a
pin store, so the panel does not claim cross-device sync. If browser storage is
unavailable the panel says pins are visit-only. Unknown or removed instruments
are dropped from storage and reported; the remaining pins survive. A session
change never reorders or erases pins.

**Movers** show three gainers and three losers by default within the active
group only:

| Active group | Eligible rows |
|---|---|
| US premarket/regular/after-hours | `equity`, `etf` |
| Futures overnight | `commodity` (resolved same contract) |
| Crypto weekend/continuous | `crypto` (rolling 24-hour reference) |
| FX | `fx` |

Yields, foreign daily closes, futures and stocks never share one percentage
table. Ties order by label ascending. The list is shorter when fewer valid rows
exist; missing, stale, retained (`last-known`), future-timestamped, failed-refresh
and invalid-reference rows are excluded, never zero-filled. The caption says
"our tracked universe", not market-wide movers.

## Shared store and refresh

`site/shared/quote-panel.js` holds one `QuoteStore`; `site/js/quote-panel.js`
renders it and runs one `QuotePanelController`. The controller:

- fetches `intraday/prices.json` every 15 s while visible and every 60 s while
  hidden, with one in-flight request at a time;
- refreshes immediately on foreground return and window focus;
- backs off 15 s, 30 s, 60 s, 120 s after failures and resets on success;
- keeps the last values and their original observation ages when a refresh
  fails, with a visible "showing last values" status;
- owns the only prices timer. Cards never fetch, so there are no per-card
  polling loops.

The existing live-quote transport may overlay a newer provider tick only when
the tick carries its own provider event time. Its `quote_ts` is a receipt time
and may pair an old last trade with a newly received bid/ask, so receipt-only
ticks never freshen the store. The panel currently reads `prices.json` alone;
the overlay path is retained only with that explicit observation-time guard.

Observation time drives the displayed age. Snapshots re-decorate retained rows
against the current clock, so a failed or partial refresh ages cards and
removes expired rows from movers instead of freezing the last ingestion age.
Publication or retrieval time never freshens a quote. Each card exposes source,
observation age, session state and change basis through its accessible label
and title.

## Session source

The producer's `price_lane.mode` is authoritative when present, and its
`sessions` status supplies the specific banner (`Premarket · US equities`,
`Regular · US equities`, or `After hours · US equities`). Otherwise only rows
whose state is `live` may name the active group; a row's `session` is the last
trade/observation session, not proof that the market is open now. If no fresh
row supports the ET clock fallback group, the panel reports `Session unknown`
rather than claiming extended-hours coverage from a stale Friday print on a
Sunday night.

A streaming row without a provider event timestamp is `unknown`, even when its
collector receipt is current; receipt time is never used as observation age or
mover eligibility. `observation_time_reason` on the producer row states why the
timestamp is absent.

## Release and rollback

Merging a `site/**` change deploys the Cloudflare Worker automatically. Core
and pins work against the current artifact; mover quality during extended hours
improves when #178 publishes `market_session`, `change_basis` and
`rolling_reference`. Rolling back is a normal revert of the site commit; no
producer setting or credential changes.

## Verification

`node tests/quote_panel.test.js` covers store normalization, session selection,
ranking, ties, short lists, invalid references, pin persistence/reorder/limit
and the generated instrument projection. `node tests/quote_panel_dom.test.js`
drives the shipped DOM in a deterministic stub: core/pins/movers rendering,
pin and reorder interactions, empty state, session switch, one-in-flight
refresh, backoff, visibility changes and cleanup. `node tests/quote_instruments.test.js`
fails if the browser projection drifts from the accepted 100 registry.

Rendered desktop and mobile-sized inspection is recorded with the PR, including
a browser-measured focus-retention check: after a refresh, the focused pin
button, its card node and its control node are the same objects. Real
premarket, after-hours, overnight-futures and weekend-crypto observation is
acceptance work for G4 after #178 is deployed, not a fixture claim.
