# Experiments and unintegrated prototypes

These implementations are preserved for evaluation. Their presence does not
mean they are deployed or power an existing Options View feature.

| Home | Status and boundary |
|---|---|
| [market_overview](market_overview/__init__.py) | Pure arithmetic and candidate universe for broader coverage; currently used by fixtures. The working overview tool is [market_mcp/overview.js](../market_mcp/overview.js). See the [specification](../docs/specs/CROSS_MARKET_OVERVIEW.md). |

The overview's [symbol probe](market_overview/probe_symbols.py) can open a separate
provider connection; moving it here does not authorize running it.
Promote an experiment only when its accepted role and operating ownership are clear.

## Archived news bot

The standalone news/reaction Discord prototype was archived on November 8, 2026.
It was not integrated with the account runner or order execution. Its 25 tracked
files and dedicated CI workflow remain recoverable at tag
`archive/options-view-news-bot-2026-11-08` (commit `94cf8c8a955015c974058cf5d289f0eba4bdd8a3`).

[Browse the archived prototype](https://github.com/OwenTanzer/live-options-view/tree/94cf8c8a955015c974058cf5d289f0eba4bdd8a3/experiments/news_pin_bot).

To inspect or resume it separately:

```sh
git fetch origin tag archive/options-view-news-bot-2026-11-08
git worktree add --detach ../options-view-news-bot archive/options-view-news-bot-2026-11-08
```

The existing ignore rules for local credentials and databases remain protective.
