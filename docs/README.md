# Documentation homes

[Options View](../README.md) is the entry route; [Architecture](../ARCHITECTURE.md) is the current component and data-flow map. Use [the walkthrough](WALKTHROUGH.md) to follow one visible feature rather than reading every design record first.

## Internal specifications

[docs/specs](specs/README.md) holds the maintained repository-only product contracts: SPY collection, OA-203 qualified returns, active lookup, multi-session windows and cross-market overview. Paths named in code spans inside those specifications are repository-relative unless stated otherwise. The former root compatibility pointers are retired; use these maintained paths for new references. A specification's status is not evidence of a deployment.

The [collector guide](../market_data/live_market/README.md), [backend guide](../backend/README.md) and [Tradier operations](../market_data/tradier/OPERATIONS.md) explain current implementation and operations. [Historical records](HISTORY.md) link to immutable pre-cleanup design and rollout documents. Keep subsystem documentation with its subsystem: [Crassus](../crassus/README.md), [Market MCP](../market_mcp/README.md), [synthetic days](../research/synthetic_days/README.md), [completed studies](../research/README.md#archived-studies) and [archived news prototype](HISTORY.md#archived-prototypes).

## Served reference material

The [site tree](../site) contains both the website and Markdown already publicly served with it. `site/` is a repository home, not a new URL prefix: `/DEPLOYMENTS.md`, `/historical-archive.md`, `/plans/...` and `/tastytrade-oauth.md` keep their public-relative paths. No removal or unpublishing is authorized by this organization.

Start with [Historical archive provenance](../site/historical-archive.md), [market-reading inventory](../site/plans/2026-09-market-reading-inventory.md), [reading ownership](../site/plans/2026-09-market-reading-ownership.md), [OA-203 scanner](../site/plans/2026-09-options-return-scanner.md) or [scheduled squeeze display](../site/plans/2026-09-scheduled-squeeze-display.md). The [Deployment Contract](../site/DEPLOYMENTS.md) is dated September 3; newer runtime observations belong with their dated receipts and must not be inferred from this older policy alone. The served Historical provenance note also retains pre-move `docs/` paths, old collector line numbers and its pre-README follow-up text. Use the [current system map](../ARCHITECTURE.md) and [walkthrough](WALKTHROUGH.md) for implementation navigation; those preserved passages are not current path authority.

[The public-prose inventory](PUBLIC_PROSE.md) lists every served Markdown member, its migrated repository path and unchanged public path. These are retained public references, not copies to move into `docs/specs`. A later unpublishing decision needs its own scope and compatibility plan.

## Current authority versus history

Use current navigation links to locate code; preserve historical path names in dated incident/review records. In particular, `crassus/docs/` is still Crassus's internal history, and external URLs containing `/docs/` belong to those external projects. Do not mechanically replace every occurrence of `docs` with `site`.

Runtime truth is split between versioned source, service configuration and observations of a running revision. The [system map](../ARCHITECTURE.md) names those boundaries. Old local smoke results, image CI, successful deployment and a current connected-client response are different kinds of evidence; none silently replaces the others.
