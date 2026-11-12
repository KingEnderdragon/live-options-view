# Repository instructions for agents

## Start with the current documentation

Read the guidance for the component you are changing. Use these entry points;
do not treat a dated design, receipt or proposal as current operating authority.

| Work | Documentation |
|---|---|
| Repository layout and data flow | [README](README.md), [system map](ARCHITECTURE.md), [documentation index](docs/README.md) |
| Site releases, preview and rollback | [Current site-release procedure](docs/SITE_RELEASES.md) |
| Changed paths and service boundaries | [Release matrix](docs/RELEASE_BOUNDARIES.md), [executable checks](tests/test_changed_paths.py) |
| Backend accounts, execution and settlement | [Backend guide](backend/README.md) |
| Storage and the completed KV retirement | [No-KV record, especially post-cutover cleanup](backend/NO_KV.md#post-cutover-cleanup-retiring-kv-and-the-migration-route), [storage implementation](backend/storage.js) |
| Live collection | [Collector guide](market_data/live_market/README.md) |
| Tradier capture | [Tradier operations](market_data/tradier/OPERATIONS.md) |
| Options scanner | [Scanner guide](market_data/options_scanner/README.md) |
| Shared producer image | [Python packaging](deployment/python/README.md) |
| Strategy runner | [Crassus guide](crassus/README.md) |
| Read-only assistant consumer | [Market MCP guide](market_mcp/README.md) |

Provider settings determine live triggers, bindings and schedules. Repository
configuration and historical receipts do not prove current provider state.
Read only the facts needed for the action at hand; do not turn routine site work
into a fresh infrastructure audit.

## Keep site releases independent of capture

- Ordinary site releases use the protected staged asset workflow,
  [.github/workflows/deploy-staged.yml](.github/workflows/deploy-staged.yml),
  with [wrangler.assets.toml](wrangler.assets.toml). Follow the exact-revision
  build, staging, approval and verification procedure in `docs/SITE_RELEASES.md`.
- Backend/API releases are separate: [deploy.yml](.github/workflows/deploy.yml)
  explicitly uses [wrangler.api.toml](wrangler.api.toml). A site-only release
  must not publish accounts, execution or settlement code.
- **Never use bare `wrangler deploy` for ordinary work.** Root
  [wrangler.toml](wrangler.toml) is the legacy combined-restoration configuration;
  deploying it would undo the split. The asset config also requires its staged
  `payload/` layout, so do not deploy that file directly from the source checkout.
- Merging alone does not publish Cloudflare. Keep both release lanes explicit,
  exact-revision tested, protected and serialized. Do not restore an automatic
  combined deployment or provider-native Builds as a shortcut.
- The asset gateway must have no backend imports, direct production storage
  bindings or scheduled jobs. The API owns the sole settlement cron. Preserve
  same-origin API contracts and keep disposable preview isolated from production.
- Collector, scanner, Tradier and Crassus changes have their own release effects.
  Inspect the complete diff and affected service scopes, including shared imports
  and packaging. Prose inside a producer watch scope can still trigger a rebuild.
  Do not restart, redeploy or manually launch a producer to test a site change.
- Preserve capture artifacts and identities. Fresh uploads alone do not prove
  fresh provider events; a producer restart creates an observation gap that
  resumed state cannot reconstruct. Site rollback changes assets only; combined
  restoration, recovery and schema changes are separate deliberate operations.

## Avoid Cloudflare Workers KV operations

**Use zero Workers KV operations whenever avoidable.** This applies to production
code and operational commands, including reads, lists, writes, deletes, probes,
binding creation and diagnostic scans. Do not introduce KV as a cache, fallback,
health check or convenience store when existing D1, R2, local fixtures or retained
evidence can meet the requirement.

D1 is the serving source of truth for accounts and sessions; R2 retains its
existing roster/artifact role. Legacy KV namespaces are cold archives, not a
serving fallback. Do not rebind them to the normal asset/API Workers, replay old
migration instructions, restore stale KV into serving, or delete archived data
as routine cleanup.

If a KV operation is genuinely necessary, first record the concrete requirement,
why the existing alternatives cannot satisfy it, the exact bounded operations
and namespace, and the expected quota/data effects. Execute only within the
user's applicable authority; obtain specific authorization when the operation
falls outside it. Keep any approved migration/recovery separate from normal
serving and follow its current procedure. This exception is not permission to
reintroduce runtime KV. Preserve offline legacy migration tests and mocks; they
do not authorize contacting a real namespace.

## Verify and hand over proportionately

Run the affected checks listed in the component's workflow; do not launch
credentialed collectors or production writers for fixture testing. For boundary
changes, include `python tests/test_changed_paths.py` and the relevant asset,
preview, storage or deployment tests from [Web CI](.github/workflows/web-ci.yml).
Update the owning guide when behavior changes. Report implementation, checks,
merge, deployment and live observation as separate facts, with exact revisions
and evidence where needed. This file supplies repository guidance, not new
production, recovery or trading authority.
