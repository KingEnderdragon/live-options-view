# The Tradier lane is archive-only

This collector writes to R2 and no consumer identified inside this repository
reads that back. That is a recorded observation, not a design document: wiring
a consumer and retiring the lane are both product decisions, and neither is
made here. Ad-hoc readers outside this repository are acknowledged below; the
artifact layout is the contract they depend on.

## What runs

The `moo169-tradier-collector` Railway service starts
`python -u tradier_cron_launcher.py`. The 2026-09-18 deployment receipt records
its schedule as `15 12 * * 1-5` UTC and notes that acceptance was still open
then; the schedule lives on the service, not in this repository
([operations guide](OPERATIONS.md)). The service's watch list covers `collectors/**` as of
2026-11-08, so a change confined to this package redeploys it.

The launcher re-runs the collector after a non-zero exit, up to ten retries and
never sooner than the lease TTL, because Railway's own restart policy reads
`never` for this service. The collector is calendar-gated: on a day with no
NYSE session it exits 0 without touching storage.

The deployed path `scripts/moo144_tradier_collector.py` is a shim; the code
lives in this package and `run.main` is the composition root.

## What it leaves behind

Under `<archive root>/<run date>/`, in the bucket named by `R2_BUCKET_NAME`
(the Worker's asset/R2 binding in `wrangler.toml` names
`qqq-options-chain-data`), where the archive root is `moo144/tradier` for QQQ —
kept so existing archives, leases and the deployed service are unaffected — and
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

That layout is the lane's external contract. Changing a prefix, a key name or
the record shape is a contract change for any reader outside this repository,
not an incidental cleanup.

## Who reads it

No consumer identified in this repository. Verified 2026-11-08 against the tree
at `af1758f`:

- The Worker's public-data route serves ten fixed keys
  (`worker/browser-data.js`, `BROWSER_DATA_KEYS`) plus the QQQ chain/intraday
  archive filename families (`isPublicArchiveKey` in the same module). Nothing
  under `moo144/` is reachable through it.
- The collector package (`collector/`) contains no Tradier reference, and
  `collector.py` is a 14-line shim over it. The published site (`site/`) is
  static HTML, JavaScript and Markdown with no code path that fetches these
  keys; four historical plan and receipt documents in `site/` mention the lane
  by name without reading from it.

Readers we know of are ad-hoc analysis scripts outside this repository, run by
hand against single dates. They are not enumerable from here and this file does
not claim they do not exist; it claims only that no in-repository service
consumes the archives.

## What that means for changes here

Because no identified in-repository consumer reads the archives, a change to
this package cannot break an in-repository reader. The risks are to capture
itself — losing events, corrupting a segment, or falsely claiming a complete
session — and to any external consumer that reads the documented layout. The
test suite therefore checks coverage and evidence semantics
(`tests/test_moo144_tradier_collector.py`), not a downstream contract.

If the lane is ever given a consumer, the artifacts above are what it would
read. If it is retired, the things that go are the service, its schedule and
`tradier_cron_launcher.py`; that decision belongs to Owen.
