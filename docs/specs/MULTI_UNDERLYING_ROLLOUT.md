# Multi-underlying chain / paper-trading rollout (#182)

Release order is the PR stack, bottom to top. Merge in order; each PR is
independently revertable. Nothing here merges to master until Chej reviews
(the quest's G6) and Owen authorizes deployment.

## The stack

1. **G1 — source contract** (`nova/182-g1-source-contract`, base master).
   The checkpoint (multi-underlying registry, collector generalization,
   settlement marks, exact-quote identity) plus two checkpoint fixes (script
   `?v=` hashes recomputed over LF bytes; `occRoot` in the bots-panel test
   scope) and the source-contract doc (`docs/specs/MULTI_UNDERLYING_SOURCE_CONTRACT.md`).
   No behavior change to the deployed QQQ path: QQQ keeps its artifact
   (`intraday/latest.json`) and its legacy date-only settlement marks.
2. **G2 — monitoring slice** (`nova/182-g2-monitoring`, base G1). Same-selection
   overlap guard in `fetchLatest` (a slow SPY/IBIT reply can no longer
   overwrite fresher data) plus IBIT fixtures (valid nearest-expiry, missing
   and mixed expiration rejected). No producer change.
3. **G3 — settlement identity** (`nova/182-g3-settlement`, base G2). The
   durable-mark consumer re-checks the 16:00–16:15 ET post-close window in
   New York time before accepting a mark. Early-close behavior documented
   (those expirations stay pending by design). No producer change.
4. **G4 — user workflow + verification** (`nova/182-multi-underlying`, base G3).
   A rejection after the user switched tickets no longer writes into the new
   ticket (it surfaces in the hint); a stale `paper_trading` assertion fixed
   (spot marks are keyed per underlying); SPY close, mixed-position render,
   and switch-during-submit tests. CI wiring (paper_trading +
   multi_underlying in web CI; test_multi_underlying in collector CI) and the
   browser acceptance script + evidence (`tests/multi_underlying_browser.test.js`,
   `docs/evidence/multi-underlying/`).

## What ships, and what stays off

- QQQ, SPY and IBIT are all in the bounded chain registry. SPY already
  publishes (`intraday/spy/latest.json`). IBIT publishes only when the
  collector runs with `COLLECT_IBIT=1`; the default is `0`, so **IBIT stays
  off in production until a reviewed release flips it** (and until entitled
  tastytrade IBIT chain/quote availability is verified — still an open gap).
- Settlement marks for SPY/IBIT are immutable per-expiry files
  (`settlement/<underlying>/<expiration>.json`, conditional create, only for
  a timed 16:00–16:15 ET post-close observation). QQQ keeps its legacy
  date-only marks from the chain archive. A missing mark leaves that
  expiration's shorts pending; it never fabricates a price.
- No new provider subscription, no second Tradier stream, no new bucket or
  service, no live brokerage trades. Bounded cost: at most one IBIT snapshot
  per collection cadence, one settlement mark per expiry, at most 100
  exact-contract quote subscriptions per session on the existing connection.

## Rollout (after merge, in order)

1. Merge G1–G4 in stack order. The Worker deploys automatically on merge
   to master (Cloudflare); the producer (Railway) deploys on its own push.
   Neither deploy is a flag flip — the code ships dark for IBIT.
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

- **Code:** revert the PR (or the stack, top-down). The QQQ legacy path is
  untouched, so a revert restores the previous QQQ/SPY behavior exactly.
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
- No market-session acceptance has been claimed (the quest ran on a Sunday);
  the browser evidence uses synthetic snapshots, not a live session.
- Early-close sessions produce no 16:00–16:15 observation, so their
  expirations stay pending until a separately verified mark exists.
- The MOO-169 Tradier collector suite (`test_moo144_tradier_collector.py`)
  fails 6 tests + 6 errors identically on master and on this branch — a
  pre-existing failure in the shared-stream main tests, unrelated to #182.
- Steve's PRs #180 (extended price lane) and #181 (adaptive quote panel)
  are still open. When they land, this stack needs a rebase onto the new
  master plus a `?v=` hash recompute (config.py + runner.py for #180;
  site/index.html for #181). Do not merge this stack on top of a stale base
  without that rebase.

## Release order (summary)

G1 source contract → G2 monitoring → G3 settlement identity → G4 workflow
and verification → Chej review (G6) → Owen authorizes deployment → flip
`COLLECT_IBIT=1` only after the entitled-availability check passes.
