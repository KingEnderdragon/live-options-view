# Options View

Options View connects a live QQQ/SPY options chain, historical QQQ heatmaps, a scheduled squeeze scanner and paper-trading accounts. The browser is one reader of a larger system: collectors publish market artifacts, a Cloudflare Worker serves the website and account API, and a read-only Model Context Protocol (MCP) server makes selected artifacts queryable by an assistant.

Open [the website](https://options.moopertonic.net), then use the [system map](ARCHITECTURE.md) to locate a component or follow the [walkthrough](notes/WALKTHROUGH.md) from a displayed value to its source. The [documentation index](notes/README.md) separates current navigation, specifications, operations and historical design records.

## The shortest route through the repository

| Question | Start here | What it owns |
|---|---|---|
| Where is the page? | [site/index.html](site/index.html), [site/js](site/js) | Page shell and browser features; the source directory is not a public URL prefix |
| Where do prices and chain snapshots come from? | [collector/](collector) → [collector guide](collector/README.md) | tastytrade/DXLink acquisition, snapshot-derived readings and optional EIA forecasts |
| What serves data and accounts? | [Worker guide](worker/README.md), [wrangler.toml](wrangler.toml) | Static assets, allowlisted archive access, live-quote proxy, paper-trading API and scheduled settlement |
| What decides paper trades? | [crassus/README.md](crassus/README.md) | Strategy runner; consumes market snapshots and calls the Worker account API |
| What produces Big Banana rankings? | [scripts/oa203_return_scanner.py](scripts/oa203_return_scanner.py), [scanner contract](site/plans/2026-09-options-return-scanner.md) | Tradier chain sampling, stored sweeps and retrospective return products |
| What stores Tradier time-and-sales? | [launcher](collectors/tradier/launcher.py), [collectors/tradier/OPERATIONS.md](collectors/tradier/OPERATIONS.md) | Separate daily archive collector; it does not feed the displayed chain |
| How does an assistant read it? | [market_mcp/README.md](market_mcp/README.md) | Bounded read-only tools over producer artifacts, not an alternate ranking engine |
| What is offline research? | [synthetic_days](synthetic_days/README.md), [MOO-171 audit](moo171_snapshot_analysis/README.md), [news_pin_bot](news_pin_bot/README.md) | Separate simulation, audit and news tooling; not implied to be deployed |

Each service owns its implementation and operating guide. The three market-data producers share [Python image packaging](runtime/python/README.md); their launch paths and schedules are documented with each component. [wrangler.toml](wrangler.toml) stays at root so both Cloudflare deployment routes discover the same configuration.

## Where does a displayed value come from?

For Live VWAP, [collector/market.py](collector/market.py) accumulates snapshot-weighted price × volume changes with [collector/market_signals.py](collector/market_signals.py), publishes `underlying_market` in `intraday/latest.json` (QQQ) or `intraday/spy/latest.json`, and the browser's [fetch cycle](site/js/data-fetch-cycle.js) passes it to [formatVwapRvol](site/shared/format.js). It is an approximation, not trade-by-trade VWAP. The snapshot carries partial-session metadata; the browser labels insufficient RVOL history, stale input and session-closed states, but does not itself surface every partial-session flag. The MCP consumer preserves that additional caveat.

Do not confuse that snapshot path with live bid/ask: visible contracts and open positions are refreshed through `/api/live-quotes` from the collector's in-memory feed, without an R2 write. The browser uses a same-origin `/browser-data/` archive proxy only on the configured `live-options-view.otmooper12.workers.dev` origin; on other origins, including the custom domain above, its archive base is the fixed public R2 origin. [The map](ARCHITECTURE.md#browser-and-data-boundaries) records that distinction.

Historical reads a different product: `manifest.json.dates` and daily `raw/qqq_chain_YYYYMMDD.csv` archives, whose producer is outside this repository. A recent manifest `updated_at` does not make its date index current. The Historical tab now warns when the newest date is more than five weekdays old; it does not repair missing archives. See [producer and freshness](site/historical-archive.md).

## What runs without an open browser?

The Railway collector runs its session lifecycle, snapshot/price/health loops and optional EIA loop independently of browser traffic. Crassus has its own five-minute strategy loop. Cloudflare invokes the Worker's settlement sweep every fifteen minutes during `12–23` UTC on weekdays. Two other Railway services start daily Tradier capture and OA-203 scanning on service-managed UTC cron schedules. The browser only polls and renders their outputs; closing the page does not stop those producers.

[Architecture](ARCHITECTURE.md#runtime-map) gives entry commands, trigger ownership and the date of available runtime evidence. Configuration, a successful deployment and a fresh market observation are different claims. In particular, an external archive schedule is still unconfirmed; Tradier's daily time-and-sales has no consumer in this repo; and a policy or test for active rankings does not establish a live in-session result. Settlement-source and closed-account policy changes are outside this documentation work.

## Safe local orientation

Start with offline tests, not a production collector or a trading command. From the repository root, these checks use fixtures:

```sh
node --test tests/build_stamp.test.mjs
node tests/historical_panel.test.js
node --test market_mcp/*.test.js
node market_mcp/smoke.js --out fixture-smoke.json
```

The [Web CI](.github/workflows/web-ci.yml), [Collector suite](.github/workflows/collector-ci.yml) and [MCP consumer workflow](.github/workflows/market-mcp-ci.yml) are the executable test inventories. A local test pass is not a deployment receipt. `npx wrangler dev` starts the Worker development path but does not provision production bindings or secrets. Do not run a credentialed collector, scanner or Crassus runner merely to read this guide.

Changes to site assets or Worker/deploy tooling can deploy Cloudflare after a matching `master` push; root prose alone does not match that workflow's path filter. Railway settings are service-specific and may be broader than the repository's manifests, so documentation-only is not a blanket promise of no production rebuild. Chej owns review and integration; no merge, deployment or live setting change is authorized by this guide.
