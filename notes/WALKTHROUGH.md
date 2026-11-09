# Walkthrough: from the page to its producer

Start at the [website](https://options.moopertonic.net) and the repository [front door](../README.md). This is a reading route, not permission to start producers, place a paper order or change a deployment. It works even outside market hours: a closed/stale/missing state is evidence to explain, not a reason to substitute a more attractive value.

## Follow Live VWAP

Choose QQQ in the Live chain header and find the VWAP/RVOL line. It may say session closed. Open [data-fetch-cycle.js](../site/js/data-fetch-cycle.js): `fetchChain` chooses a per-underlying latest snapshot, validates its identity and only renders the currently selected chain. Follow `renderHeatmap` in [render-heatmap.js](../site/js/render-heatmap.js) to `formatVwapRvol` in [format.js](../site/shared/format.js). The header reads the snapshot's `underlying_market`, not a calculation from the visible option cells.

Now follow that object upstream to `_compute_underlying_market` and `take_snapshot` in [producers/live_market/market.py](../producers/live_market/market.py), then `accumulate_vwap` and `compute_rvol` in [producers/live_market/market_signals.py](../producers/live_market/market_signals.py). VWAP uses sampled underlying prices weighted by cumulative-volume changes; RVOL compares session volume with historical samples in the same five-minute bucket. For SPY, [the specification](specs/SPY_COLLECTION.md) explains its separate `intraday/spy/` namespace and why QQQ cannot fill a missing SPY reading. Ask: what observation time, partial-session flag or baseline count would make this value less informative?

A safe fixture check from the repository root is `node tests/vwap_rvol_display.test.js`. This verifies display semantics, not current provider entitlement or publication. The producer tests are named in [Collector CI](../.github/workflows/collector-ci.yml); do not launch a credentialed collector to answer a documentation question.

## Distinguish the live quote from the snapshot

The same cell can contain lagged OI and a much fresher bid/ask. Follow the live quote service and poller from [the page shell](../site/index.html) through `/api/live-quotes` in [worker.js](../worker/index.js) to `handleLiveQuotes` in [worker/browser-data.js](../worker/browser-data.js): the Worker proxies to the collector's in-memory quote service, not an R2 write. For archived data, inspect the shell's `R2` and `BROWSER_DATA` constants: only the configured workers.dev origin uses `/browser-data/`; the custom domain reads fixed public R2. Ask: which timestamp belongs to the quote side, which to snapshot construction, and which to retrieval?

## Follow a Historical OI color

Select Historical and note both the chosen date and any archive-stale warning. Open [historical-panel.js](../site/js/historical-panel.js): `loadHistoryManifest` reads `manifest.json.dates`; `fetchChainCsv` loads `raw/qqq_chain_YYYYMMDD.csv` with the legacy `raw/opex/` fallback only on 404; `renderHistoryHeatmap` delegates the row/bucket calculation to [heatmap.js](../site/shared/heatmap.js). [oi-ranges-loader.js](../site/js/oi-ranges-loader.js) supplies calibration from `derived/OIranges.csv`.

The date list, chain CSV and calibration are external-pipeline products. This collector updates weekly metadata in the same manifest without advancing `dates`. [Archive provenance](../site/historical-archive.md) names the evidence and unconfirmed schedule. The warning counts weekdays, not exchange-calendar holidays, and does not block reading old dates. Ask: could a fresh top-level manifest timestamp coexist with an old Historical date list? Yes; the two have different writers. `node tests/historical_panel.test.js` exercises the warning; [the browser regression](../tests/historical_browser.test.js) exercises rendered date switching and retry under a fixed clock.

## Find the other consumers

Open [squeeze-panel.js](../site/js/squeeze-panel.js): its pointers select an externally produced ranking and report latest success separately from newer attempts; it does not run the squeeze scan. Open [market_mcp/server.js](../market_mcp/server.js) to see the seven read-only tools, then [the MCP guide](../market_mcp/README.md) to follow a `detail_reference` to pinned evidence. OA-203's ranking producer is [the scanner](../producers/options_scanner/__main__.py), whereas Tradier time-and-sales ends at an archive with no reader in this tree. Similar provider names do not make these the same data path.

For Bots, open [bots-panel.js](../site/js/bots-panel.js) and follow `/api/bots` into [worker.js](../worker/index.js). The Cloudflare cron invokes `settleAllBots` even with no page open. Follow it into [worker/settlement.js](../worker/settlement.js), which reads membership through [worker/bots.js](../worker/bots.js), account state and archive spot marks; the dashboard settlement path instead has page-observed spot marks, which are not guaranteed recovery of dates the page never saw. This walkthrough does not change settlement or closed-account policy.

## A usable stopping point

Pick one displayed feature and explain its implementation file, input key or API, producer, consumer, clock/freshness rule and missing-data behavior using the links above. Then identify what keeps running after the browser closes. If any transition cannot be found from these guides, report that exact navigation gap for repair. A successful guided reading is distinct from Owen having completed the walkthrough, and neither is a fresh live-health certification.
