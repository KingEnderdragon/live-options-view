# Options View system map

This is the companion to the [front door](README.md). It reuses the service/contract/trace structure introduced in [PR #149](https://github.com/OwenTanzer/live-options-view/pull/149), initially checked against source at `2b08a174b1d296943f92c07a0978c5a1ffaf7682`, then refreshed through merged site, Worker and collector organization at `af1758f2fbcf0c17521cf213eaa9000ef979e299`. Paths describe the migrated repository; public URL paths do not acquire a `/site/` prefix. Dated operational observations below are attributed, not new live probes.

## Runtime map

| Component | Source / stable launch entry | Trigger and responsibility | Evidence boundary |
|---|---|---|---|
| Main collector | [entry](producers/live_market/__main__.py), [operations](producers/live_market/README.md) | `python -m producers.live_market`; session lifecycle, QQQ/SPY chain snapshots, prices, health, weekly archives; EIA thread only with `EIA_API_KEY` | The package has a module entry point. Repository HEAD is not automatically the running collector; no new Railway readback in this documentation pass |
| Website + account API | [site/index.html](site/index.html), [site/js](site/js), [worker/index.js](worker/index.js), [wrangler.toml](wrangler.toml) | Browser requests plus cron `*/15 12-23 * * 1-5`; serves assets, data proxy, quotes, paper accounts and settlement | Chej reported master Web CI and Cloudflare deployment passed at `2b08a17`; this is not a fresh build-ID browser readback |
| Crassus | [runner](crassus/crassus/runner.py), [service config](crassus/railway.toml), [guide](crassus/README.md) | `python -m crassus.runner --interval 300` from `crassus/`; observe → decide → validate → submit → reconcile | Command/configuration, not evidence that a particular bot traded today |
| Tradier time-and-sales | [launcher](producers/tradier/launcher.py), [collector](producers/tradier), [operations](producers/tradier/OPERATIONS.md) | Service cron `15 12 * * 1-5` UTC; waits for exchange open, archives a daily session | The [deployment guide](producers/tradier/OPERATIONS.md) records this launcher/cron. No consumer identified in this repository reads these archives; ad-hoc readers outside it and the artifact layout they depend on are recorded in [the lane's note](producers/tradier/ARCHIVE_ONLY.md). No new service readback here |
| OA-203 / Big Banana | [scanner](producers/options_scanner), [entry shim](producers/options_scanner/__main__.py), [operations](producers/options_scanner/README.md), [contract](site/plans/2026-09-options-return-scanner.md) | Service-managed cron starts a session-aware scanner, approximately five-minute sweeps and final rankings | The service-managed command is the normal `run` launch; readiness is a separate runtime setting. Active publication policy is a separate [specification](notes/specs/OA203_ACTIVE_LOOKUP.md) |
| Read-only Market MCP | [server.js](market_mcp/server.js), [production.js](market_mcp/production.js), [guide](market_mcp/README.md) | Client-driven discovery/query/detail; no trading, scanner invocation or producer credentials | Image tests establish packaging; dated [rollout](market_mcp/ROLLOUT.md) is historical, not a current service inventory. Live connector acceptance is recorded separately from source |
| Other tools | [synthetic days](research/synthetic_days/README.md), [MOO-171](research/snapshot_analysis/README.md), [news tooling](experiments/news_pin_bot/README.md) | Simulation/audit/news entry points | Their presence is not evidence of an unattended deployed service |

The UTC Railway cron values do not shift with US daylight saving time; session-aware code waits for the exchange calendar. No schedule is changed by moving source or documentation. The subsystem homes from #158 and #159 have landed with these launch paths retained: [collectors/tradier](producers/tradier) and [scanners/oa203](producers/options_scanner) own the implementations, and the launcher paths above remain the deployment entries.

## Browser and data boundaries

```text
tastytrade/DXLink ── producers/live_market/ ── R2 published artifacts ── browser / MCP / Crassus
                        │                         ▲
                        └─ in-memory quotes       │
                               │          external historical and squeeze producers
                        Worker /api/live-quotes
                               │
                         browser quote store

Crassus ── Worker /api/* ── KV accounts + R2 execution records
Cloudflare cron ── same Worker settlement logic ── archive spot marks
Tradier time-and-sales ── moo144/tradier/...   (archive-only in this repository)
OA-203 scanner ── oa203/scanner/... ── read-only MCP rankings/detail
```

[site/index.html](site/index.html) chooses the archive base. Only the exact `https://live-options-view.otmooper12.workers.dev` origin uses same-origin `/browser-data/`; other origins use the fixed public R2 base. [worker/index.js](worker/index.js) separately enforces the proxy's host/origin/key allowlist. A 403 on a different host is not proof that a data object is missing. The live-quote/account APIs are separate from this archive-base choice.

The shell wires browser responsibilities through [site/js](site/js): data fetching in [data-fetch-cycle.js](site/js/data-fetch-cycle.js), Historical in [historical-panel.js](site/js/historical-panel.js), and common browser formatting/calculations in [site/shared](site/shared). The individual scripts define browser globals; [site/shared.js](site/shared.js) retains Node compatibility re-exports and is a no-op in the browser. Search a feature's visible label or function there before searching the whole page. The Worker entry now dispatches to [worker/](worker): browser/data/quote access in [browser-data.js](worker/browser-data.js), accounts in [auth.js](worker/auth.js), bot membership in [bots.js](worker/bots.js), order execution in [execution.js](worker/execution.js) and [paper-trade.js](worker/paper-trade.js), and settlement in [settlement.js](worker/settlement.js). These are the merged #152 homes, not a second runtime.

## Artifact contracts

R2 bucket `qqq-options-chain-data` holds several products, not one interchangeable feed. Object names below are storage keys, not repository files.

| Artifact / state | Writer | Readers and meaning |
|---|---|---|
| `intraday/latest.json`, `intraday/spy/latest.json` | Main collector, nominal 60-second snapshots | Live chain, per-underlying VWAP/RVOL/momentum; MCP. QQQ also supplies Crassus. Missing SPY never substitutes QQQ |
| `intraday/YYYYMMDD/snapshot_*.csv`, parallel `intraday/spy/YYYYMMDD/` | Main collector | Session archives, restart state and offline analysis; a CSV alone does not reconstruct the full published JSON |
| `intraday/prices.json`, `intraday/health.json` | Main collector, nominal 10/15 seconds | Browser price/health displays and MCP overview; publication time is not necessarily provider observation time |
| `manifest.json` | External daily pipeline owns `dates`; this collector updates weekly metadata | Historical date selection versus weekly archive discovery. Top-level `updated_at` is not date-index freshness |
| `raw/qqq_chain_YYYYMMDD.csv`, `raw/opex/qqq_chain_*.csv`, `prices/qqq_intraday_*.csv`, `derived/OIranges.csv` | External producer identified in source as `qqq-data-pipeline`; schedule unconfirmed | Historical heatmap/candles/calibration; settlement also reads daily chain spot marks |
| `raw/weekly/<expiry>/...` | Main collector | Separate weekly snapshots; they do not replace the daily Historical archive |
| `macro/eia_steo.json`, `baselines/eia_steo_vintages.json` | Optional EIA loop in collector | Browser STEO forecast/revisions. Vintage baseline is not browser-exposed. A first release has no prior vintage to compare |
| `squeeze-scanner/v1/scheduled/*` | External [short-squeeze-scanner](https://github.com/OwenTanzer/short-squeeze-scanner) | Squeeze panel and MCP; latest successful rows and latest attempt/status remain distinct |
| `oa203/scanner/<date>/` | OA-203 scanner | Final retrospective rankings and sweep evidence for MCP; active `inprogress/` generations are provisional, digest-pinned and policy-gated |
| `moo144/tradier[/-ibit]/<date>/` | Separate Tradier collector | Archived time-and-sales; no website/MCP reader in this tree. Product purpose remains a separate decision |
| `system/bot-membership-v1.json`, `paper-trades/requests/*`, KV `USERS`/`SESSIONS` | Worker | Bot roster, canonical execution records, authenticated account/session state; never conflated with public market readings |

## Following one value

Live VWAP follows `take_snapshot` → `_compute_underlying_market` in [producers/live_market/market.py](producers/live_market/market.py) → [producers/live_market/market_signals.py](producers/live_market/market_signals.py) accumulation → latest snapshot's `underlying_market` → [fetch cycle](site/js/data-fetch-cycle.js) → [formatVwapRvol](site/shared/format.js). A historical OI cell instead follows manifest-selected daily CSV → [Historical panel](site/js/historical-panel.js) → [heatmap helpers](site/shared/heatmap.js). Both show prior-day-settled OI, not live creation of new positions.

The optional STEO path is `eia_steo_loop` in [producers/live_market/steo.py](producers/live_market/steo.py) → [producers/live_market/crude_calibration.py](producers/live_market/crude_calibration.py) → `macro/eia_steo.json` → page panel. A forecast level, a release-over-release revision and OVX's current regime are different measurements. Do not promise a revision when only one vintage exists.

Settlement is `scheduled` → `settleAllBots` → bot membership + KV account → `fetchSpotMarkForDate` → `settleAccount` in [worker/settlement.js](worker/settlement.js), using membership from [worker/bots.js](worker/bots.js). The Nov 7/8 trace recorded missing recent daily archives and a retained position on a closed account. Those are dated findings, not a fresh inventory or a new settlement policy. Source-repair Q12 and closed-account semantics Q13 remain outside this organization work.

## Release ownership and verification

[deploy.yml](.github/workflows/deploy.yml) runs on matching `master` paths, verifies packaging, runs [stamp-build.mjs](scripts/stamp-build.mjs), then deploys Cloudflare. [web-ci.yml](.github/workflows/web-ci.yml) checks packaging, browser/Worker syntax and rendered Historical behavior before integration. The [MCP workflow](.github/workflows/market-mcp-ci.yml) also builds/runs the narrow non-root image; shared browser imports must be copied into it.

Railway deploys through service source/watch settings, not a repository deploy workflow. The served [Deployment Contract](site/DEPLOYMENTS.md) is dated September 3 and is not the latest service inventory; for example the October 4 [Crassus inspection](crassus/docs/2026-10-market-reading-contract.md#watch-scope-impact) already superseded its old QQQ/Crassus CI-gate claim. Service readback remains a separate operational record; this documentation pass does not establish every service's current gate or watch scope. A prose-only change under `crassus/**` or an unrestricted Railway watcher may still rebuild a service. Review merge effects explicitly.

All served prose remains in [site](site) at unchanged public-relative paths. [The public-prose inventory](notes/PUBLIC_PROSE.md) distinguishes served references from internal specifications; it proposes no unpublishing. The [walkthrough](notes/WALKTHROUGH.md) is the bounded acceptance route: choose a feature, find the implementation, explain its inputs/outputs and identify the producer and consumer without treating an old receipt as current health.
