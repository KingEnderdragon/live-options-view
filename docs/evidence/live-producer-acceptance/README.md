# Live producer acceptance — 2026-11-09

**Deployed page.** `https://live-options-view.otmooper12.workers.dev/`, served build
`52c16cb-20261109104144` from merge `52c16cb...`. The Snapdragon Edge window used
the existing profile; pins and account state were untouched, the window was closed,
and the DeepSeek Harness foreground was restored. No order, trade, login or
settings change was made.

## Before the producer published

At roughly 02:46 PT the public artifact was still the old one
(`timestamp 2026-11-08T21:10:57-08:00`, no `price_lane`, no
`price_lane_health.json`). The deployed panel correctly showed **Session unknown**
and **No active group**, with QQQ 62h stale and the other cards stale/daily. This
confirms the #188 stale-session repair on the deployed build. See
`deployed-before-producer-session-unknown.png` and
`deployed-before-producer-narrow.png`.

## Producer published

At `2026-11-09T02:48:31-08:00` (`snapshot_time 05:48 ET`) `intraday/prices.json`
gained `price_lane`:

- writer `f65e67179e1d:1:price-lane`, `mode: equities`, `eligible: true`;
- equities `premarket`, futures `overnight`, crypto `continuous`, FX `overnight`;
- 99 published rows; `feed_stale: false`;
- `intraday/price_lane_health.json` `streaming`, same writer, 10s cadence,
  `option_symbols_subscribed: 0`, contracts resolved (e.g. `/GCZ26:XCEC`);
- crypto rolling references warming up (`BTC/USD`, `ETH/USD`, `SOL/USD`), so no
  valid 24h change yet.

Reloading the deployed panel at roughly 02:50 PT produced two defects:

1. **The banner is generic, not session-specific.** It says
   `US equities · extended hours` even though the producer's own
   `sessions.equities.status` is `premarket` and cards are labelled
   `PREMARKET`. See `deployed-producer-generic-label.png` and
   `deployed-producer-generic-label-narrow.png`.
2. **A receipt-only SPY row is treated as a live observation and mover.**
   `prices.SPY` has `observed_at: null`,
   `observation_time_reason: provider_event_time_unavailable`, a fresh
   `quote_ts`/`collector_received_at`, and a previous close. The panel shows SPY
   as `PREMARKET`, `updated 0s ago`, and `MOVERS … 1 eligible` with SPY +0.56%.
   A collector receipt is not a provider observation. See
   `deployed-producer-spy-mover.png` and
   `deployed-producer-spy-mover-narrow.png`.

QQQ, SMH, IGV, USO and AAPL remained `STALE` with 62h age because their last
provider trades were still Friday; the cards honestly show those ages. Pins
remained `4/12` and unchanged.

## Local fixed-build verification

The repair branch was rendered locally against the same live artifact (not a
deployed change):

- banner `Premarket · US equities`;
- SPY `UNKNOWN` / `time unknown`;
- movers `US equities · 0 eligible`, no SPY entry;
- narrow layout has no horizontal overflow.

See `fixed-local-premarket-desktop.png` and `fixed-local-premarket-narrow.png`.

## Remaining honest gaps

No other premarket equity rows had fresh provider observations at this time, so
the movers list is empty after removing the receipt-only SPY row. This is the
correct empty state, not a claim that premarket movers work broadly. Crypto 24h
references are still warming up, futures are running on prior-session references,
and real premarket/after-hours/overnight/weekend coverage plus reconnect/restart
recovery remain G4 market-time evidence. November 9–10 acceptance is untouched.
