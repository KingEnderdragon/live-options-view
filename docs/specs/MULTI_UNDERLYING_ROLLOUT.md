# Multi-underlying chain / paper-trading rollout (#182)

Release is ONE merge: **#195** (retargeted to master, base `e873ce7`),
carrying the complete G1–G5 stack. The lower PRs #191 (G1), #192 (G2),
#194 (G3) keep their earlier heads, which are defective intermediate
states — do not merge them. They are closed as superseded after #195
merges. Nothing here deploys until Chej releases under the existing
release grant (Owen authorized Chej to supervise and release after
independent review); no fresh Owen approval round is required.

## The stack (one PR, G1–G5, on master `e873ce7`)

#195 (`nova/182-multi-underlying`, base master) carries the whole stack:

1. **G1 — source contract.** The checkpoint (multi-underlying registry,
   collector generalization, settlement marks, exact-quote identity) plus
   two checkpoint fixes (script `?v=` hashes recomputed over LF bytes;
   `occRoot` in the bots-panel test scope) and the source-contract doc
   (`docs/specs/MULTI_UNDERLYING_SOURCE_CONTRACT.md`). No behavior change
   to the deployed QQQ path: QQQ keeps its artifact
   (`intraday/latest.json`) and its legacy date-only settlement marks.
2. **G2 — monitoring slice.** Same-selection overlap guard in
   `fetchLatest` (a slow SPY/IBIT reply can no longer overwrite fresher
   data) plus IBIT fixtures (valid nearest-expiry, missing and mixed
   expiration rejected). No producer change.
3. **G3 — settlement identity.** The durable-mark consumer re-checks the
   16:00–16:15 ET post-close window in New York time before accepting a
   mark. Early-close behavior documented (those expirations stay pending
   by design). No producer change. The producer side (take_snapshot) was
   repaired twice (see below).
4. **G4 — user workflow.** A rejection after the user switched tickets no
   longer writes into the new ticket (it surfaces in the hint); a stale
   `paper_trading` assertion fixed (spot marks are keyed per underlying);
   SPY close, mixed-position render, and switch-during-submit tests.
5. **G5 — verification.** CI wiring (paper_trading + multi_underlying in
   web CI; test_multi_underlying in collector CI) and the browser
   acceptance script + evidence (`tests/multi_underlying_browser.test.js`,
   `docs/evidence/multi-underlying/`).

Base `e873ce7` already includes #180 producer (merged `52c16cb`,
deployed), #181 (merged `e5efd57`), #183 lifecycle, #184 snapshot
provenance, #185 daily-only policy, #187 reference readings, #188
session label, #189 ubuntu-24.04, #190 MCP consumer, #193 reviewed
publisher, and #196 receipt-only quote/session repair. #180 and #181 are
merged, not open; no further rebase is needed for them.

## Two repaired provider-time boundaries (G3, in #195)

Both were found in independent review of the settlement producer and
are fixed in the #195 head (`7585864`):

1. **Receipt time is not provider time.** `take_snapshot` passed
   `_observed_spy_price`'s receipt time (older of bid_ts/ask_ts) into
   `publish_settlement_mark`, so a fresh receipt of a price whose
   provider observation time is UNKNOWN became an irreversible
   `first_observed_post_close` mark (SPY 670/670.2, receipt 21:00:30/31,
   provider None, now 21:00:40). Fixed by pairing the price with its
   provider event time and stamping the mark with
   `selection.provider_observed_at` (Brian #184's model, preserved
   including IBIT): a midpoint needs both sides (older wins); a last
   trade needs its own event time; a remembered spot has none and can
   never publish.
2. **A future contributing leg vetoes.** Taking the min (oldest) leg
   proves the oldest leg isn't stale but hides a future newer leg: a
   valid bid at 21:00:30 plus a FUTURE ask at 21:01:01 (now 21:00:40)
   still published, stamped 21:00:30, for both SPY and IBIT. Fixed by
   validating EVERY contributing provider timestamp against the
   permitted window and now before publishing
   (`settlement_marks.contributing_provider_times_valid`; both bid/ask
   legs for a midpoint, `last_observed_at` for a last trade, none for a
   remembered spot). Last trade and remembered fallback unchanged.

Unknown/preclose/stale/future/mixed-side provider evidence stays
pending. No provenance sidecars are overwritten.

## What ships, and what stays off

- QQQ, SPY and IBIT are all in the bounded chain registry. SPY already
  publishes (`intraday/spy/latest.json`). IBIT publishes only when the
  collector runs with `COLLECT_IBIT=1`; the default is `0`, so **IBIT stays
  off in production until a reviewed release flips it** (and until entitled
  tastytrade IBIT chain/quote availability is verified — still an open gap).
  Current actual IBIT flag remains `0`; entitlement and live observations
  are not claimed.
- Settlement marks for SPY/IBIT are immutable per-expiry files
  (`settlement/<underlying>/<expiration>.json`, conditional create, only for
  a timed 16:00–16:15 ET post-close provider observation). QQQ keeps its
  legacy date-only marks from the chain archive. A missing mark leaves that
  expiration's shorts pending; it never fabricates a price.
- No new provider subscription, no second Tradier stream, no new bucket or
  service, no live brokerage trades. Bounded cost: at most one IBIT snapshot
  per collection cadence, one settlement mark per expiry, at most 100
  exact-contract quote subscriptions per session on the existing connection.

## Release (after the #195 merge, in order)

The consumer (Worker/Cloudflare) and the producer (Railway collector)
are separate, compatible releases — do NOT call the cross-provider
deploys atomic. Each deploys on its own push and is verified by its own
readback; the code ships dark for IBIT either way.

1. Merge #195 to master. Close #191, #192, #194 as superseded.
   (Pre-release: Steve is fixing a duplicate Cloudflare publisher
   before final release; confirm that is resolved. Shared Worker edits
   coordinate with Brian before his Bot Operations G0 implementation.)
2. Verify the deployed site still serves QQQ first (title, chain, paper
   positions), then SPY (title `SPY 0DTE Live OI`, chain renders). IBIT
   must still read unavailable (`intraday/ibit/latest.json` 404) until
   the flag flips.
3. In a market session, run the collector once with `COLLECT_IBIT=1` in a
   non-production check (or a single Railway revision) and confirm an IBIT
   snapshot publishes with the nearest expiry and a settlement mark appears
   only after 16:00 ET. Then flip `COLLECT_IBIT=1` on the production
   collector revision.
4. Confirm the first post-close IBIT mark lands (`settlement/ibit/<exp>.json`,
   `price_basis` `first_observed_post_close`, `source` `dxlink`) and that a
   mixed-expiry settlement (QQQ + SPY + IBIT shorts) prices each against its
   own mark.

## Rollback

- **Code:** revert #195 (single revert; the stack is one PR). The QQQ
  legacy path is untouched, so a revert restores the previous QQQ/SPY
  behavior exactly.
- **IBIT off:** set `COLLECT_IBIT=0` on the collector and redeploy. The
  chain registry still lists IBIT, but no IBIT snapshot is published and the
  UI reads unavailable. No data cleanup is needed: settlement marks are
  immutable files, and a mark for an expiry that never traded is inert
  (settlement only prices positions that exist).
- **A bad mark:** marks are immutable (`IfNoneMatch: "*"`), so a wrong mark
  cannot be overwritten in place. If a mark is ever proven wrong, the
  expiry's shorts stay pending until a corrected mark is published under a
  separately verified path — do not delete the file to "fix" it, and do not
  hand-edit R2.

## Known gaps (not blockers for merge, tracked in #182)

- Entitled tastytrade IBIT chain/quote availability is unverified (no access
  denial; the check was not run). IBIT must not be enabled in production
  until it is.
- Deployed IBIT lane variables (COLLECT_IBIT on Railway) and current Railway
  revisions are unverified; default is off.
- No market-session acceptance has been claimed (the quest ran overnight
  Nov 8–9); the browser evidence uses synthetic snapshots, not a live
  session.
- Early-close sessions produce no 16:00–16:15 observation, so their
  expirations stay pending until a separately verified mark exists.
- The MOO-169 Tradier collector suite (`test_moo144_tradier_collector.py`)
  fails 6 tests + 6 errors identically on master and on this branch — a
  pre-existing failure in the shared-stream main tests, unrelated to #182.
- Required Web/Collector CI is green on the #195 head; independent review
  passed 10 multi-underlying + 10 provenance + 20 readings plus mixed
  browser/cron settlement, paper/order/worker/quotes/build packaging
  checks.

## Release order (summary)

#195 (complete G1–G5 stack on `e873ce7`, one merge) → close #191/#192/#194
as superseded → Chej releases under the existing grant → consumer and
producer deploy separately (compatible, verified by readbacks) → flip
`COLLECT_IBIT=1` only after the entitled-availability check passes.
