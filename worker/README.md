# Website and paper-account Worker

[worker/index.js](index.js) is the stable Cloudflare entry point. It dispatches to
these modules; [wrangler.toml](../wrangler.toml) defines assets, bindings and cron.
The browser's implementation lives separately in [site/](../site).

| Responsibility | Source |
|---|---|
| Allowed archive access and live-quote proxy | [browser-data.js](browser-data.js) |
| Accounts and sessions | [auth.js](auth.js) |
| Bot roster and membership reconciliation | [bots.js](bots.js) |
| Quote-backed execution and request records | [execution.js](execution.js), [paper-trade.js](paper-trade.js) |
| Expiration settlement and scheduled sweep | [settlement.js](settlement.js) |
| Shared helpers | [common.js](common.js) |

## Boundaries worth preserving

The live-quote route proxies the collector's in-memory feed. Archive reads use
stored R2 artifacts; the public site's archive-base selection is explained in
[Architecture](../ARCHITECTURE.md#browser-and-data-boundaries).

Paper orders are execution requests, not arbitrary client-supplied fills.
The execution path validates quote freshness and the requested side, and persists
canonical request records. Read the implementation and fixture tests before
changing retry or identity handling.

The configured bot index mode is `r2`; the migration is not waiting to start.
Membership and account state are different records: a retained closed account
must not silently disappear from the roster. The dated
[cutover record](../site/operations/2026-09-28-bot-index-cutover.md) preserves rollout
evidence. Do not replay its migration steps as routine startup instructions.

The settlement cron runs every fifteen minutes during hours 12–23 UTC on weekdays,
even with no browser open. Browser-triggered and scheduled settlement can obtain
spot marks differently. Source availability and closed-account policy are
separate questions; a successful sweep is not proof that every historical mark
exists. See the [system map](../ARCHITECTURE.md#following-one-value).

[Web CI](../.github/workflows/web-ci.yml) and
[consumer CI](../.github/workflows/market-mcp-ci.yml) list offline checks.
Old design and migration instructions are linked from
[documentation history](../notes/HISTORY.md), not treated as current deployment instructions.
