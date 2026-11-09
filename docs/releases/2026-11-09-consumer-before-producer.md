# Consumer-before-producer release checklist — #180

Status: prepared 2026-11-09 from master `e5efd579b5e1f42a9931676bbe781f1ed369216c`.
This is a release runbook, not a deployment receipt. Chej owns release access and
the merge/deploy decision; this document does not authorize a producer launch.

## Why the producer must not merge first

`market_data/live_market/runner.py` starts `PriceLane` whenever
`PRICE_LANE_ENABLED` is unset (default `1`). Merging #180 therefore activates the
new collector lane on the next collector deployment, before any additive MCP
consumer is live. The current MCP image can ignore the new artifact fields, but
the intended verification order is still consumer first: prove the new consumer
accepts both old and new artifacts before the producer starts emitting them.

The MCP image is built only from `market_mcp/` plus
`market_data/overview/universe.json` and `site/shared/**` (see
`market_mcp/Dockerfile` and `market_mcp/Dockerfile.dockerignore`). It does not
contain the collector. A consumer-only source commit is therefore sufficient to
publish the additive MCP without activating the producer.

Do **not** dispatch `.github/workflows/publish-market-mcp-pr133.yml` unchanged:
it hardcodes `SOURCE_SHA: 13637eb7047304763ce456d78174ca7b31bde800`, checks out
that historical commit, and publishes a `13637eb...` release tag. That image
predates the `market_session` / `change_basis` / `rolling_reference` additions.

## 1. Review the consumer-only accepted source

The prepared extraction is PR #190 at
`4d3daafa907e31222448ed35202be4c964002aa1`. Verify its exactness before merge:

```sh
git fetch origin pull/190/head:review/consumer-only
git switch review/consumer-only
test "$(git rev-parse HEAD)" = 4d3daafa907e31222448ed35202be4c964002aa1
git diff --stat origin/master
```

The diff must contain only `market_mcp/overview.js` and
`market_mcp/overview-v2.test.js`. In particular it must not contain
`market_data/live_market/price_lane.py`, `price_sessions.py`,
`price_history.py`, `prices.py`, `runner.py`, `config.py` or
`overview_sources.py`; their presence means the extraction pulled the producer
in. Run and record:

```sh
node --test market_mcp/*.test.js
node tests/squeeze_scanner_display.test.js
node market_mcp/smoke.js --out market-mcp-fixture-smoke.json
node market_mcp/production-smoke.js --out market-mcp-production-smoke.json
node --max-old-space-size=256 market_mcp/load.js --out market-mcp-production-load.json
```

Review and merge it. The reviewed consumer commit, not the merge SHA, is the
accepted source. This merge may rebuild the collector if Railway autodeploys
master, but it contains no collector-path change, so the old producer remains
the running code.

## 2. Publish the additive image from that exact SHA

Use the reviewed publisher workflow, not `publish-market-mcp-pr133.yml`. The
new workflow is deliberately separate and pins the accepted consumer source
`4d3daafa907e31222448ed35202be4c964002aa1`:

```text
.github/workflows/publish-market-mcp-reviewed.yml
```

Merge the consumer extraction first so its reviewed commit is an ancestor of
master, then merge this publisher change so the workflow exists on the default
branch. Use a merge commit for the consumer PR: a squash or rebase merge creates
a new commit, so the pinned reviewed SHA would no longer be an ancestor. If a
squash/rebase merge is chosen, update `ACCEPTED_SOURCE_SHA`, the input default and
`RELEASE_TAG` in this workflow to the resulting commit in a reviewed publisher
change, and re-run the source guard before dispatch.

After both are merged to master, dispatch with the pinned input:

```sh
gh workflow run publish-market-mcp-reviewed.yml --ref master \
  -f source_sha=4d3daafa907e31222448ed35202be4c964002aa1
```

The workflow refuses to run unless all of these hold:

- the input equals `ACCEPTED_SOURCE_SHA` (the pinned consumer commit);
- `HEAD` is that exact SHA and the checkout is clean;
- the SHA is an ancestor of the fetched `master`;
- the commit's own diff against its first parent is exactly
  `market_mcp/overview.js` and `market_mcp/overview-v2.test.js`;
- the existing fixture, production-smoke and load checks pass;
- the image's revision label equals the source SHA;
- `market_mcp/verify-image.sh` passes offline; and
- the existing GHCR tag guard sees an explicit manifest 404 for this run's
  unique tag before publishing.

It publishes `ghcr.io/owentanzer/market-mcp-read-only:<sha>-run-<run_id>-<attempt>`
and prints the immutable digest. Deploy by digest, never by the tag. Record the
digest, source SHA, test outputs and image verification output. Do not fall back
to a manual build-and-push that skips these guards.

## 3. Deploy and verify the consumer before touching the producer

Attach the exact digest to the existing `market-mcp-read-only` service
(`market_mcp/deployment-plan.json`: project `ea9f4549-e34f-47cd-a09e-573b24d696a8`,
production environment `2dcc91a4-fafc-408f-bef8-e5ce24b2ec6a`, service
`7222ee91-be95-4aea-843b-2ff1258e21da`). Keep the existing service limits and
do not change the collector service, the Discord service, credentials or grants.

Required readback:

- `/healthz` returns 200 and the service runs the deployed digest.
- An authenticated `discover_sources` lists `cross_market_overview`.
- `market_overview` still returns the 100-row universe from the **old** artifact;
  existing rows and detail references do not fail.
- A row with no `market_session`/`rolling_reference` remains valid; the additive
  consumer does not require producer fields.
- No `price_lane` writer exists at this point. Record that the old producer is
  still active; do not claim extended-session coverage.

## 4. Then release the producer

Only after the consumer readback is recorded, merge #180 at
`8816046445fb11be06746d99b2ee3694747b6851` (rebased if required) and let the
collector deploy. Verify:

- `intraday/prices.json` gains `price_lane.writer_id`, `market_session`,
  `change_basis`, and crypto `rolling_reference` where available.
- `intraday/price_lane_health.json` reports `streaming`, the expected writer and
  `option_symbols_subscribed: 0`.
- A second publication cycle does not show a second writer.
- The deployed MCP query accepts the new artifact and exposes `change_24h` only
  when a bounded rolling reference exists.

Rollback before producer release: redeploy the prior consumer digest. After
producer release: producer rollback is `PRICE_LANE_ENABLED=0` plus collector
redeploy; do not roll the consumer back alone unless the old producer is restored
first. Panel rollback is a normal revert of the #181 merge.

## 5. Remaining, not covered by this checklist

The checklist proves consumer compatibility and producer activation mechanics.
It does not prove premarket, after-hours, overnight-futures or weekend-crypto
publication, reconnect/restart recovery or the deployed panel's real-session
movers. Those remain G4 market-time evidence. Existing November 9–10 acceptance
tasks remain untouched.
