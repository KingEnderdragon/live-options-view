# Historical design and deployment records

Use [Architecture](../ARCHITECTURE.md), the [collector guide](../market_data/live_market/README.md),
the [backend guide](../backend/README.md) and [Tradier operations](../market_data/tradier/OPERATIONS.md)
for current navigation. The following immutable records preserve the detailed
reasoning and evidence that used to sit at the repository root:

- [Product design at the pre-cleanup revision](https://github.com/OwenTanzer/live-options-view/blob/646e61e4c3c0df71006519eaded1d8c53c8d55a7/DESIGN.md): protocol notes,
  schemas, display conventions, implementation history and rollout proposals.
  It mixes multiple eras; its old Tradier start/restart instructions and bot-index
  migration sequence are superseded. Its June date does not date every later addition.
- [Tradier deployment history and optional-IBIT proposal](https://github.com/OwenTanzer/live-options-view/blob/646e61e4c3c0df71006519eaded1d8c53c8d55a7/TRADIER_DEPLOYMENT.md):
  September receipts, capture requirements, capacity observations and proposed
  enablement steps. Old appointments and pending labels are historical statements,
  not newly assigned work or proof of current enablement.

These records remain in Git rather than being copied into another maintained
manual. Current specifications live in [specs/](specs/README.md); their former
root-level compatibility pointers have been removed. External links to those old
branch-relative paths must use the maintained location or an immutable revision.

The frozen MOO-171 research report and generator retain their original textual
DESIGN.md citation to preserve the recorded source identity. That citation refers
to the historical design record linked above, not a current root file.

## Archived prototypes

The obsolete `experiments/` directory has been removed. The accepted exact-100 overview now lives in `market_data/overview/` and `market_mcp/`; the reusable read-only symbol diagnostic is `python -m market_data.overview.probe_symbols`.

- [Earlier 132-stock/watchlist proposal](https://github.com/OwenTanzer/live-options-view/tree/cd32c7b7c700436dbc35cc4e27dacec6d1783bc3/experiments/market_overview), retained at `archive/options-view-overview-prototype-2026-11-09`. Its membership and proposed watchlist feature are superseded, not active product settings.
- [News/reaction prototype](https://github.com/OwenTanzer/live-options-view/tree/94cf8c8a955015c974058cf5d289f0eba4bdd8a3/experiments/news_pin_bot), retained at `archive/options-view-news-bot-2026-11-08`.

Use a detached worktree at either archive tag to inspect or resume that work separately. Active synthetic-day replay remains under `research/synthetic_days/`.
