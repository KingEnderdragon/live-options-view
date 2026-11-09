# Experiments and unintegrated prototypes

These implementations are preserved for evaluation. Their presence does not
mean they are deployed or power an existing Options View feature.

| Home | Status and boundary |
|---|---|
| [market_overview](market_overview/__init__.py) | Pure arithmetic and candidate universe for broader coverage; currently used by fixtures. The working overview tool is [market_mcp/overview.js](../market_mcp/overview.js). See the [specification](../docs/specs/CROSS_MARKET_OVERVIEW.md). |
| [news_pin_bot](news_pin_bot/README.md) | Standalone proposed news/reaction Discord bot; not integrated with the account runner or order execution. |

The overview's [symbol probe](market_overview/probe_symbols.py) can open a separate
provider connection; moving it here does not authorize running it.
Promote an experiment only when its accepted role and operating ownership are clear.
