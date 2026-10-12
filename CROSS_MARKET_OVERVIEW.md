# Cross-market overview (#132): coverage matrix and first slice

Issue: [#132](https://github.com/OwenTanzer/live-options-view/issues/132).
Status: **first usable slice**. Per Owen's Oct 12 direction, the callable
`market_overview` MCP tool reads the existing dashboard price strip
(`intraday/prices.json`). Its unsupported assets stay as named gaps, and undated
delayed fills are never presented as live. The broader curated-stock and
cross-asset coverage is follow-on work. The 60-second overview artifact,
expanded DXLink subscriptions and the watchlist proposal still need separate
decisions. A one-off, read-only, market-hours coverage/entitlement probe is
approved if needed.

The universe file and `market_overview.py` rules below are groundwork for that
follow-on. The first-slice tool doesn't use them.

## What this slice adds

| File | Purpose |
| --- | --- |
| `market_overview_universe.json` | Versioned candidate universe: 132 stocks in 11 groups of 12 with a rationale for each group, a share-class alias table, a proposed watchlist cap, and a macro instrument registry with exact instrument types, units and candidate sources. |
| `market_overview.py` | Pure functions for change arithmetic, scale normalization, FX inversion, identity/contract/adjustment checks, US equity session classification, freshness, universe validation, watchlist merging, sample breadth and top movers. |
| `tests/test_market_overview.py` | Fixture tests for the arithmetic, session, breadth and watchlist checklist items in #132. They are wired into the Collector suite. |

Only tests import `market_overview.py` today.

## Verified coverage baseline

The baseline comes from source on `master` at `450b5b7` plus the last
public `intraday/prices.json`, published `2026-10-09T20:14:55Z` (Friday close).
That file is one observed publication. It does not establish entitlement,
symbology or live health.

| Ticker strip label | Configured DXLink symbol | Observed source | Previous close | Quote timestamp |
| --- | --- | --- | --- | --- |
| QQQ, SPY, USO, SMH, IGV, META, GOOGL, AMZN, TSLA, MU, SPCX, AAPL | same as label | dxlink | yes | yes |
| JPY/USD | `/6J:XCME` (yen futures, USD per JPY) | dxlink | yes | yes |
| VIX | `$VIX.X` | yfinance fallback | **no** | **no** |
| OVX | `$OVX.X` | yfinance fallback | **no** | **no** |
| 10Y | `$TNX.X` | yfinance fallback (`^TNX`) | **no** | **no** |
| KOSPI | `$KOSPI.X` | yfinance fallback (`^KS11`) | **no** | **no** |
| BTC/USD | `BTC/USD:CXERX` | yfinance fallback (`BTC-USD`) | **no** | **no** |

What this shows:

- **US equities/ETFs over DXLink** work, with event timestamps and previous
  close. These are the natural source for the 132-stock universe, but only 12
  equity symbols are subscribed today.
- **CME futures over DXLink** work for at least `/6J`. That suggests `/CL`,
  `/BZ`, `/SI`, `/6E`, `/6B`, `/6C`, `/6A`, `/6S` and Treasury futures may
  work too. This is unverified.
- **Cash indices, CBOE yield indices and BTC** return nothing over DXLink
  with the configured symbols. We can't yet tell whether the symbology is
  wrong or the entitlement is missing.
- **The yfinance fallback** in `collector.py` reads only a last price. It
  carries no previous close and no trustworthy observation time, so every
  fallback row today has `chg_pct: null`. That fallback can't produce a
  change on its own. A daily-bar history call would be needed for a reference
  value, and it still has no intraday observation time.
- `MAX_LIVE_QUOTE_SYMBOLS = 100` caps the live-quote HTTP request size. It
  isn't a provider subscription limit, and nothing here raises it.

## Per-category mapping (proposed, unverified)

| Category | Proposed primary | Fallback / gap | Notes |
| --- | --- | --- | --- |
| 132 stocks | DXLink Quote/Trade/Summary on the existing connection | none: unsupported symbols stay listed as unavailable | Needs a subscription-limit check. |
| Benchmarks (SPX, NDX, DJI, RUT) | DXLink cash index if a probe finds working symbols | yfinance daily bars, labeled delayed / no observation time | ETF proxies SPY/QQQ/DIA/IWM keep separate identities. |
| International (KOSPI, N225, STOXX50E, FTSE) | yfinance daily bars | n/a | Each has its own calendar. A closed market reads as `closed`. |
| WTI / Brent | DXLink dated front-month `/CL`, `/BZ` | yfinance `CL=F`/`BZ=F` only with a documented roll | USO stays an ETF row. It never stands in for oil. |
| Silver | DXLink dated `/SI` | yfinance `SI=F` with a documented roll | USD per troy ounce. |
| BTC/USD | needs a working source with a 00:00 UTC reference | yfinance `BTC-USD` daily bar | No rolling 24h move is labeled as a daily change. |
| Treasury 2Y/5Y/10Y/30Y | DXLink CBOE yield indices if they work (scale ÷10) | yfinance `^FVX`/`^TNX`/`^TYX` | No 2Y yield index has been found yet, so 2Y is a **named gap**. Moves are in bps. |
| Major FX | spot source TBD | DXLink currency futures as a separately labeled series | JPY/CHF/CAD futures quote USD per unit, so both values get inverted. |

## Rules pinned by the tests

- Change = current − reference. Percent = 100 × change / reference. Missing
  or nonfinite inputs make the row unavailable. A zero reference keeps the
  absolute change but has no percent.
- Yields: moves in bps = (current − reference) × 100. No percent of a yield.
  4.00% → 4.05% = +5 bps, including after a ÷10 CBOE normalization.
- FX inversion inverts both values before computing the change. The tests
  prove this differs from negating the percent.
- Mismatched instrument identity (USO vs WTI), dated futures contract, or
  split-adjustment basis all make the row unavailable.
- Extended-hours values name their session and compare against the previous
  **regular** close. An extended-hours print is rejected as a reference close.
- Freshness is tracked per instrument: a fresh Sunday BTC print leaves
  Friday's equity close `closed`. A cached success keeps its age plus a
  `newer_attempt_failed` warning.
- Breadth keeps the full eligible denominator, with missing and stale counts,
  and uses equal weighting. It excludes watchlist additions by default and
  flags mixed sessions or reference bases. Top movers are ranked over the
  full sample before paging.

## Open decisions (owner approval needed before the producer slice)

1. Add the ~132 equity symbols (plus macro futures) to the existing collector
   DXLink connection, and publish a separate `intraday/overview.json` at a
   slower cadence (proposed 60s). `prices.json` and its 10s cadence stay
   unchanged.
2. Run a one-off, read-only, market-hours DXLink probe of the candidate macro
   symbols. This separates wrong symbology from missing entitlement, the same
   way the MOO-144 Tradier probe did.
3. For instruments with no DXLink source, choose between yfinance daily bars
   (labeled delayed, no observation time) and leaving them as named gaps.
4. Watchlist cap: 25 additions are proposed. Storage would be a versioned
   config file changed through PRs, with no runtime write path in the first
   version.

## Not in this slice

Collection changes, new artifacts, MCP tools, news, sentiment, causal
explanations, alerts and any new paid service or credential.
