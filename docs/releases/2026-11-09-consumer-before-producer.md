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

## 1. Produce a consumer-only accepted source

From a clean checkout of current master:

```sh
git fetch origin
git switch -c release/mcp-consumer-20261109 origin/master
git checkout 8816046445fb11be06746d99b2ee3694747b6851 -- \
  market_mcp/overview.js market_mcp/overview-v2.test.js
git diff --stat origin/master
```

The diff must contain only those two `market_mcp/` files. In particular it must
not contain `market_data/live_market/price_lane.py`, `price_sessions.py`,
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

Open a pull request for this consumer-only commit, review it, and merge it. Record
the resulting master SHA as `CONSUMER_SHA`. This merge may rebuild the collector
if Railway autodeploys master, but it contains no collector-path change, so the
old producer remains the running code.

## 2. Publish the additive image from that exact SHA

Use a clean detached checkout of `CONSUMER_SHA`; confirm the tree is clean and
the source is an ancestor of `origin/master` before building.

```sh
git fetch origin
git switch --detach "$CONSUMER_SHA"
test -z "$(git status --porcelain)"
git merge-base --is-ancestor "$CONSUMER_SHA" origin/master
node --test market_mcp/*.test.js

IMAGE=ghcr.io/owentanzer/market-mcp-read-only
TAG="${CONSUMER_SHA}-consumer-$(date -u +%Y%m%dT%H%M%SZ)"
docker build -f market_mcp/Dockerfile \
  --label "org.opencontainers.image.revision=$CONSUMER_SHA" \
  --label "org.opencontainers.image.source=https://github.com/OwenTanzer/live-options-view" \
  -t "$IMAGE:$TAG" .
test "$(docker image inspect "$IMAGE:$TAG" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$CONSUMER_SHA"
bash market_mcp/verify-image.sh "$IMAGE:$TAG"
docker login ghcr.io                     # release credential, not a repo secret
docker push "$IMAGE:$TAG"
docker buildx imagetools inspect "$IMAGE:$TAG"
```

Deploy by digest (`$IMAGE@sha256:...`), never by the mutable run tag. Record the
digest, the source SHA, the test outputs and the image verification output.

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
