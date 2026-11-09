# Multi-underlying source contract — #182 G1

G1 return for issue #182 (QQQ / SPY / IBIT chain monitoring and paper trading).
Established 2026-11-08 ~23:30 PT from the preserved checkpoint `263ae05`,
verified by re-probe. IBIT has no collected archive and no live browser feed;
the DXLink adapter below is the provisional choice, IBIT disabled by default
(`COLLECT_IBIT=0`) until reviewed release.

## Source contract (observed 2026-11-08 ~23:15 PT)

Row counts are from the Log's G1 probe; this re-probe confirmed the
headers/timestamps are unchanged. QQQ and SPY latest are retained Friday
Nov 6 snapshots (markets closed Sun Nov 8), not fresh executable quotes.

| Underlying | Producer | Endpoint (R2) | Schema | Cadence | Observed | Coverage | Expiry policy | Spot source |
|---|---|---|---|---|---|---|---|---|
| QQQ | live_market collector (tastytrade/DXLink) | `intraday/latest.json` | Snapshot JSON: symbol, timestamp, snapshot_time, date, expiration, tier, underlying_price, snapshot_key, rows[] (OCC symbols, OI, Volume, Bid/Mid/Ask/Last, IV, Greeks, UnderlyingPrice + price strip) | Intraday, market session | 2026-11-06T21:14:24.572771Z (16:14 ET) | 134 rows | 0DTE (2026-11-06) | DXLink underlying price 656.51 (strip QQQ 656.2) |
| SPY | same | `intraday/spy/latest.json` | Same | Same | 2026-11-06T21:14:26.703657Z (16:14 ET) | 114 rows | 0DTE (2026-11-06) | DXLink 736.00 |
| IBIT | none | `intraday/ibit/latest.json` → HTTP 404 | — | — | — | 0 rows | nearest (provisional) | none observed |

Notes: the QQQ key is `intraday/latest.json`; there is no
`intraday/qqq/latest.json`. SPY uses snapshot-relative OI colors. Tradier
IBIT lanes (`IBIT:nearest` / `IBIT:same_day`, `market_data/tradier/OPERATIONS.md`)
are archive-only, share one stream/lease, never a second stream; "Source
support does not establish live enablement."

## Adapter choice (provisional, pending live verification)

Reuse the existing tastytrade/DXLink connection and nearest-expiry chain loader
for IBIT. Do NOT treat Tradier archives as a live chain. IBIT joins the bounded
registry (`CHAIN_UNDERLYINGS`) with `COLLECT_IBIT=0` by default. Optional
startup chains share the fresh-spot/67-strike selection path. On-demand exact
held-contract subscriptions are bounded at 100 per session on the existing
connection. Settlement marks are immutable per-underlying files
`settlement/<underlying>/<expiration>.json` (conditional create, only for
timed post-close observations 16:00–16:15 ET, SPY/IBIT only; QQQ keeps legacy
date-only marks). The 16:00–16:15 ET window is enforced on both sides: the
publisher only writes a mark from a timed post-close observation in that
window, and the consumer (`fetchSpotMarkForDate`) re-checks the window in New
York time before accepting a mark, so a manually written R2 object with a
midday observation cannot become an expiration's mark. Early-close sessions
(e.g. a 13:00 ET close) produce no 16:00–16:15 observation, so their
expirations stay pending by design rather than borrowing an intraday price;
they need a separately verified mark.

Justification: no new connection, no new provider subscription, bounded load,
IBIT disabled until reviewed release. The choice is provisional pending (a)
entitled tastytrade IBIT chain/quote availability and (b) a real browser
publication.

## Bounded provider/publication cost

$0 new provider cost. No second Tradier stream, no new subscription.
Publication adds at most one IBIT chain snapshot per collection cadence (same
cadence as SPY, 67-strike window), at most one settlement mark per expiry
date, and at most 100 exact-contract quote subscriptions per session (no extra
connection). R2 writes are small JSON objects; no new bucket or service.

## Gaps (exact — source-independent work continues)

1. Entitled tastytrade IBIT chain/quote availability — unverified (no access
   denial; the check was not run).
2. Deployed IBIT lane variables (`COLLECT_IBIT` on Railway) — unverified;
   default is 0 (disabled).
3. Current Railway revisions (collector + Worker) — unverified.
4. Real browser publication of IBIT — none (404); requires a collection run
   with `COLLECT_IBIT=1` and a market session.
5. Market-session acceptance — none claimed; Sunday, markets closed. Live
   acceptance stays explicit.
6. (Found in verification, fixed in this branch) Script `?v=` hashes in
   `site/index.html` were CRLF-computed on the MSI; recomputed over LF bytes
   so the hash pins pass on every host. The bots panel test needed `occRoot`
   in its scope (shared.js loads first on the page). Both fixed and verified
   below.

## Verification evidence (checkpoint 263ae05 + repairs)

- Branch recovered on the MSI at exactly `263ae05`, clean tree; master still
  `762efaf`. Steve PR heads refreshed (#180 `8816046`, #181 `dc51be7` — both
  moved since the Astra inspection). Overlap: `config.py` + `runner.py` with
  #180, `site/index.html` with #181; rebase + hash recompute needed after
  they land.
- Web CI non-browser suite: 15/15 pass (build_stamp, browser_data,
  historical_archive, worker_logic, account_closure, bots_panel,
  live_quote_service, order_logic, vwap_rvol_display, momentum_display,
  chain_underlying, steo_calibration_display, steo_panel,
  squeeze_scanner_display, squeeze_panel). Parse check passes.
- New #182 tests: `tests/multi_underlying.test.js` PASS;
  `tests/test_multi_underlying.py` 3/3 PASS;
  `tests/verify_collector_spy.py` 18 checks PASS (venv, requirements.txt).
- Not yet run: historical rendered browser test (needs Playwright/Chromium),
  full collector CI, market/limit lifecycle + retry/close workflows,
  desktop/narrow screenshots. These are G5 items.
- Fixtures: `tests/multi_underlying.test.js` (mixed settlement, missing
  marks, repeat, legacy, exact quote identity),
  `tests/test_multi_underlying.py` (timed marks, nearest IBIT vs exact-day
  SPY expiry, bounded held-contract interests),
  `tests/verify_collector_spy.py` (updated for IBIT in the registry).

## What G1 returns

The adapter choice above, the fixtures (in the branch), and the bounded cost.
IBIT stays disabled until reviewed release. Next: G2 (expose supported
chains).
