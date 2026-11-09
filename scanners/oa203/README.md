# OA-203 options return scanner

One weekday job: scan the day's most-traded underlyings for short-dated option
returns, archive every sweep, and rank the contracts into a leaderboard. The
deployed command is:

    python -u scripts/oa203_return_scanner.py run

The code lives in this package. Railway service `oa203-banana-scanner-AWsT`
uses that command and the [shared Python image](../../runtime/python/README.md),
with repository-root context, restart policy `NEVER` and cron `45 12 * * 1-5`
UTC. Its empty watch list covers all source paths. Build/start/cron settings
are service-managed; activation and readiness are separate runtime settings.

## Where a job lives

| module | responsibility |
| --- | --- |
| `runtime.py` | the event log, wall clock, stop flag and run settings |
| `occ.py` | OCC daily contract volume → the day's underlyings |
| `tradier.py` | the market-data client: retries, rate limits, per-sweep budget |
| `archive.py` | the day directory: journal, manifest, row iteration |
| `sweep.py` | universe selection and one sweep across it |
| `outputs.py` | leaderboard rows and the CSV/JSON artifacts |
| `active.py` | the intraday active snapshot and its artifact budget |
| `backfill.py` | post-close trade-bar backfill for the top contracts |
| `assessment.py` | was the session good enough to trust? |
| `session.py` | one trading day: build, sweep, finalize, recover prior days |
| `readout.py` | offline inspection of an archived day |
| `cli.py` | `run`, `universe`, `smoke`, `build`, `snapshot`, `inspect`, `readout` |
| `returns.py` | return and leaderboard metrics (pure functions) |

## The path a session takes

`run` (`cli.py`) → `run_day` (`session.py`) → `build_universe_for` (OCC volume,
then `sweep.select_universe`) → repeated `sweep.run_sweep` writing
`sweeps/sweep_<n>.jsonl.gz` and one `sweeps/manifest.jsonl` line per sweep →
`active.build_active_snapshot` on its cadence → after the close, `finalize`
(`session.py`) builds `build_contract_rows` (`outputs.py`), writes the
leaderboard, uploads through `DayArchive` and records the assessment.

## Operator commands

    python scripts/oa203_return_scanner.py universe --size 8     # select only, print
    python scripts/oa203_return_scanner.py build --dir <day>     # rebuild from local archive
    python scripts/oa203_return_scanner.py inspect --dir <day> --symbol QQQ260918C00600000
    python scripts/oa203_return_scanner.py readout --dir <day>

## The stop flag

`runtime.STOP` is the one owner. Modules read it through the module
(`runtime.STOP`) rather than importing a copy, so setting it — from a signal
handler or a test — actually stops the scan.
