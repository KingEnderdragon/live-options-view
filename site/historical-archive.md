# Historical archive — producer and freshness

The Historical tab renders the settled daily QQQ 0DTE chain archive from R2.
This note records where that archive comes from and how the tab says when it
is stale. (Q1, readability quest, issue #148.)

## What the archive is

| Key | Contents |
|---|---|
| `manifest.json` (`dates`) | Trading-day index, 2025-12-29 → 2026-09-18 (176 dates as observed Nov 7, 2026) |
| `raw/qqq_chain_YYYYMMDD.csv` | One file per trade date: that date's 0DTE chain rows with D−1 settlement OI |
| `raw/opex/qqq_chain_YYYYMMDD.csv` | Fallback for pre-pipeline seeded dates (404 on the daily path only) |
| `prices/qqq_intraday_YYYYMMDD.csv` | 5-minute bars for the date (intraday widget, daily candles) |
| `derived/OIranges.csv` | Heatmap calibration (bucket thresholds per tier/offset) |

Readers in this repo: the Historical tab (`docs/js/historical-panel.js`) and
the Worker settlement sweep (`worker.js` `fetchSpotMarkForDate`, which prices
expired shorts from the same archive — see Q12).

## Producer

The writer of every key above lives **outside this repo**: the
**`qqq-data-pipeline`** repo (`OwenTanzer/qqq-data-pipeline`), which pulled
the daily QQQ 0DTE chain into this R2 bucket. Evidence in this repo:

- `docs/index.html` sidebar-widgets comment: the widgets use "R2 keys
  qqq-data-pipeline already writes".
- `collector.py:111`: the weekly archive is kept byte-for-byte compatible
  with "qqq-data-pipeline's" format; `collector.py:610`: the EoW expiration
  uses "qqq-data-pipeline's holiday behavior".
- The manifest's own `note`: the June 25, 2026 re-backfill was done "via
  MarketData.app date=D calls".

This repo's collector only extends `manifest.json`'s `weekly_expirations`
and bumps `updated_at`; its docstring is explicit that it does so "without
changing its date index". Nothing in this repo writes the `dates` array,
the daily chain/intraday archives, or `derived/OIranges.csv`.

## Schedule: unconfirmed

No schedule for the producer is recorded anywhere in this repo. Do not
assume a cadence. As observed Nov 8, 2026 the `dates` index stops at
**2026-09-18** — seven weeks behind the live collector, which runs through
Nov 6 — with an unexplained gap Aug 28–Sep 8 inside it. Whether the
pipeline stopped, changed keys, or only the manifest update stopped is an
external-repo question (open verification item #4).

## Freshness tripwire

When the Historical tab loads the manifest and the newest date is more than
**5 trading days** old, the status line says so explicitly, e.g.:

> Trade date 2026-09-18 — archive stale: newest date 2026-09-18 is 35 trading days old

- "Trading days" are counted as weekdays (Mon–Fri) after the newest date up
  to today. No holiday calendar exists in the browser, so a holiday counts
  as a trading day — the tripwire errs toward stale.
- The dates stay usable: the select is still populated and the latest date
  still loads. The tripwire is a warning, not a block.
- The check lives in `docs/js/historical-panel.js`
  (`tradingDaysSince` / `archiveStaleState`); the regression test is in
  `tests/historical_panel.test.js`.

## Follow-up

The repo-root README should link here once PR #149 (repo-root README +
system map) merges — the README on this branch does not exist yet as tracked
content, so this note is the record for now. Confirming the producer's
schedule is open verification item #4.
