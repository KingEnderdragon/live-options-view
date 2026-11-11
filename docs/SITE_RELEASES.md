# Independent site releases and rollback (#225)

Proposed operating procedure, pending Nova's G3b/G4b receipts and provider review. This file is the procedure; dated command receipts belong in docs/receipts or the quest Log, not appended here. Current production still uses the combined Worker until the separately approved one-time transition. Do not merge this lane into an automatically promoted branch as though topology already changed.

## Local work

Use an isolated LF worktree at the reviewed revision (Node 24; Node 22 needs --experimental-sqlite). Run:

```
node --test tests/asset_boundary.test.mjs tests/preview_isolation.test.mjs tests/preview_transport.test.mjs tests/asset_rollback.test.mjs
node scripts/preview-local.mjs
```

The preview serves only http://localhost:8787, uses disposable in-memory SQLite with migrations 0001/0002 and an operational-prefix-only memory store, and has no cron or production credentials. All backend outbound fetches are replaced with deterministic quote fixtures; public data is a separate read-only fixture binding. Register a disposable account; never enter a production password. Stop with Ctrl+C; state disappears. The visible banner, environment endpoint, response headers and connect-src/form-action self policy identify and confine preview. The frontend's off-production direct-R2 selector is rewritten in the served preview HTML only; production source remains unchanged. This is application-level local emulation, not a provider-permission test.

Rendered check (Playwright 1.56.1 and its Chromium installed outside the repo):

```
# PowerShell; adjust tool/evidence directories to the local installation.
$env:NODE_PATH='C:\Users\Owen\Apps\renaissance-225-musef-tools\node_modules'
$env:MUSEF_EVIDENCE_DIR='C:\Users\Owen\Apps\renaissance-225-musef-evidence'
node tests/preview_browser.test.mjs
```

The browser test intercepts all requests at the local gateway; it renders registration/session, Historical chain/sidebar and mobile Historical, and attempts a production-origin POST that CSP must block before transport. Direct handler tests also execute local quotes, paper execution and expired-option settlement with production cookies refused by the empty local DB. No production account mutation is tested.

## Exact-revision artifact and plan

From a clean worktree at the full reviewed SHA, choose a NEW output directory OUT outside the source, and a NEW staging directory STAGE outside source/OUT. Replace the capitalized arguments below with literal paths/SHAs, not unexpanded placeholder words.

```
node scripts/build-assets.mjs --revision FULL_REVIEWED_SHA --out OUT
node scripts/build-assets.mjs --verify OUT
node scripts/prepare-asset-release.mjs --artifact OUT --out STAGE
```

The CLI refuses dirty/wrong-HEAD/pre-floor source, copies tracked assets only (not ignored/unreviewed local files), stamps BUILD_ID, copies the tiny gateway/policy, and writes a hash inventory and same-origin-d1-v1/schema contract. It excludes backend code, producer code, Wrangler configs, credentials and Markdown prose. The library's synthetic-ID test helper is not a source attestation; production packaging uses the CLI. Preserve the original release.json separately with the approved revision and provider receipt; a self-hashed manifest is not a signature.

Staging re-verifies every payload hash and schema, places the verified artifact under payload/, and adds the separate proposed asset config and plan.json. The plan has one asset target, unchanged API target, and empty producer/schema/recovery action lists; it DOES NOT execute its proposed command. Nova must review/apply the shared configuration and ordered workflow patches. Build and staging do not grant deployment authority.

## One-time transition and provider gates (Nova C2)

Read docs/ASSET_BOUNDARY.md and Brian's docs/RELEASE_BOUNDARIES.md at his accepted candidate. Verify provider facts rather than treating repository comments as readback. In particular identify all promotion paths including native Builds, check enforcement/order for exact reviewed SHA, routes/custom domains, API secrets and binding targets, settlement trigger ownership, and disposable preview resource IDs. Hosted preview must bind only separately provisioned preview D1/operational storage/API/quote fixtures; it cannot reuse the API production config, production credentials, production sessions or write bucket. Gateway identity strings are checks, not IAM proof. Nova owns the production public-read adapter and generic proxy restriction; local PUBLIC_DATA is fixture-only.

The proposed resource/configuration changes are in patches/musef-225: a separate API service carrying the existing D1/operational binding and exact settlement cron, an asset gateway retaining the original public name/origin with only ASSETS/API service bindings and run_worker_first, one backend fallback-to-404 patch, a deployment-note reconciliation patch, and removal of recovery's unused R2 write binding. Existing root config/workflows are not changed by this lane. Nova alone applies shared entry/config/workflow changes. Copy applicable API secrets under existing approval; never copy them to frontend/preview. Do not run two settlement crons during transfer. If Workers.dev name ownership cannot be switched reversibly without a service gap, return the concrete route constraint for an Owen-reviewed dispatcher alternative; do not silently redirect browsers to a different API hostname.

Auth has an existing split worth preserving, not inventing away: checkOrigin currently accepts options.moopertonic.net and localhost:8787, whereas browser-data enforces the workers.dev production origin. Forward the original public Request URL and Origin unchanged; changing either policy belongs to Nova's reviewed shared patch. No CORS expansion or cookie Domain rewrite is included here. Keep the combined Worker as the transition rollback until the new API binding/topology is verified; reversal of that first transition is a separate C2 action, not ordinary asset rollback.

## Ordinary release after transition acceptance

Run the exact-revision checks, package, verify and stage. Nova's ordered gate must select/test the same SHA it promotes and serialize promotions, including explicit manual bypasses; the current combined deploy.yml is not that gate. Only under the applicable existing production authority, execute the proposed asset-only Wrangler command from STAGE/plan.json. Confirm provider success/version and public BUILD_ID/X-Asset-Revision against the retained artifact, unchanged API service revision/config/cron, public reads and an isolated/nonmutating auth check. Record canary/receipt under A2; no collector restart is part of this procedure. Asset, API and producer promotion are separate actions. Account/execution/settlement code changes require their own API candidate, tests and authorization; routine asset release must not publish them.

## Website-only rollback and D1 floor

Choose an accepted previously retained asset artifact, not an arbitrary old Git checkout. Verify both old and current artifacts, then stage the old with current as compatibility reference:

```
node scripts/build-assets.mjs --verify OLD_ARTIFACT
node scripts/build-assets.mjs --verify CURRENT_ARTIFACT
node scripts/prepare-asset-release.mjs --artifact OLD_ARTIFACT --current CURRENT_ARTIFACT --out NEW_ROLLBACK_STAGE
node --test tests/asset_rollback.test.mjs
```

Execute no deployment automatically: Nova reviews the generated old-revision asset-only plan under existing authority. The local executable rollback swaps both packaged gateway/policy and site payload while keeping the same API binding object, D1 instance/accounts/sessions, schema and producer targets. It refuses modified/missing/extra payload, mismatched schemas or API targets, producer actions, a changed floor, recovery serving or legacy KV serving. Its two release IDs are explicitly synthetic same-source protocol fixtures, not a historical production rollback receipt; the exact candidate artifact is separately built and rendered. A real prior accepted separated artifact becomes the ordinary rollback target after the first transition; do not claim it already exists.

The serving API floor is D1-reading revision 90cdc56c207ad0c733bb2f1b1bb15d995472bf47 or a reviewed compatible descendant, with exact migration 0001_accounts_sessions.sql and 0002_cutover_support.sql hashes in release.json. This lane performs no schema migration. Additive/future schema changes need a reviewed compatibility contract; a mismatch refuses rollback rather than guessing. Never use an old KV-serving revision or restore stale archived KV into ordinary serving as a code rollback.

## Recovery, classification and bypasses

backend/recovery.js remains an offline, deliberate migration utility, with no ordinary asset/API route, assets or schedule. The proposed patch removes unused PAPER_TRADES/BOT_INDEX_READS from its config; it retains D1 plus archived USERS/SESSIONS only for an explicitly approved fenced recovery window. Recovery writes affect live D1 and require separate authority, token/fence verification, no public route, and removal after the window. It is never the site rollback route. Recovery/migrate modules do not use the R2 binding, as verified in source; provider state remains Nova's readback.

Root docs/tests changes test but do not promote a runtime. Markdown prose under site/ is not packaged; deployment-note patching currently still matches the old combined site/** trigger, so keep it off production until Nova applies the new selector contract. Existing producer directory globs may promote prose inside their runtime scope (Brian's dated matrix owns the exact behavior); do not describe a global docs exemption. Frontend/site asset changes select the asset lane; backend changes select API; shared gateway/config/build/workflow or schema-contract changes receive affected/mixed review. Mixed asset+API+producer changes require separate explicit plans and authorization, not an automatic bundle. Scheduled producer code promotion and execution are distinct; preserve all existing explicit producer gates. No manual workflow, provider-native build, recovery config or ad-hoc CLI is an authority bypass.

## Remaining finalization gates

G5 is draft until Brian returns the G1-adjusted selector receipt and Nova returns G3b/G4b/promotion/provider contracts. A1 has local emulation evidence only until those dependencies are accepted; hosted preview/provider routing/IAM and exact real accepted-history rollback remain unverified, not inferred from local tests. Independent R1/R2 producer repairs are deliberately outside site acceptance.
