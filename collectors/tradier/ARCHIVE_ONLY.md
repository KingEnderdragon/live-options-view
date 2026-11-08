# The Tradier lane is archive-only

This collector writes to R2 and nothing in this repository reads that back.
That is a recorded observation, not a design document: wiring a consumer and
retiring the lane are both product decisions, and neither is made here.

## What runs

The `moo169-tradier-collector` Railway service starts
`python -u tradier_cron_launcher.py`. The 2026-09-18 deployment receipt records
its schedule as `15 12 * * 1-5` UTC and notes that acceptance was still open
then; the schedule lives on the service, not in this repository
(`TRADIER_DEPLOYMENT.md`).

The launcher re-runs the collector after a non-zero exit, up to ten retries and
never sooner than the lease TTL, because Railway's own restart policy reads
`never` for this service. The collector is calendar-gated: on a day with no
NYSE session it exits 0 without touching storage.

The deployed path `scripts/moo144_tradier_collector.py` is a shim; the code
lives in this package and `run.main` is the composition root.

## What it leaves behind

Under `<archive root>/<run date>/`, in the bucket named by `R2_BUCKET_NAME`
(the Worker's binding in `wrangler.toml` names `qqq-options-chain-data`), where
the archive root is `moo144/tradier` for QQQ — kept so existing archives,
leases and the deployed service are unaffected — and
`moo144/tradier-<symbol>` for every other underlying:

| artifact | written when | contents |
| --- | --- | --- |
| `lease.json` | before any stream opens | the run's single stream lease: owner, expiry |
| `run-started-<owner>.json` | after the universe is selected | run id, session bounds, universe, reconciliation result, lease |
| `owner-<owner>-part-<n>.ndjson.gz` | continuously | normalized Time & Sale / quote records, closed segments |
| `health.json` | every 60 s while capturing | counters, spool backlog, delivery lag |
| `universe.json` | first successful selection of the day | the day's symbols, reloaded on a same-day restart |
| `summary-<owner>.json` | after capture | coverage verdict: `complete` or `partial` with reasons |
| `manifest-<owner>.json` | after capture | the run's artifact list, with size and SHA-256 per object |

## Who reads it

No service in this repository. Verified 2026-11-08 against the current tree:

- The Worker's public-data route serves ten fixed keys plus the QQQ
  chain/intraday archive filename families (`worker.js` `BROWSER_DATA_KEYS`
  and `isPublicArchiveKey`). Nothing under `moo144/` is reachable.
- Neither `docs/` (the published site) nor `collector.py` mentions Tradier at
  all, so no tab, route or scheduled job consumes the archives.

The only readers found anywhere are ad-hoc analysis scripts kept in quest
workspaces outside this repository, run by hand against single dates.

## What that means for changes here

Because nothing consumes the archives, a change to this package cannot break a
reader — the risk is to capture itself: losing events, corrupting a segment, or
falsely claiming a complete session. The test suite therefore checks coverage
and evidence semantics (`tests/test_moo144_tradier_collector.py`), not any
downstream contract.

If the lane is ever given a consumer, the artifacts above are what it would
read. If it is retired, the things that go are the service, its schedule and
`tradier_cron_launcher.py`; that decision belongs to Owen.
