"""MOO-169 permanent daily Tradier option Time & Sale collector.

Launched once per weekday by a Railway Cron Schedule, shortly before the
NYSE open. Unlike the one-shot MOO-144 probe (``producers/tradier/probe.py``,
whose ``select_symbols``/``Tradier``/R2 helpers this package reuses), a run:

- is calendar-gated instead of date-pinned: it exits 0 immediately on a day
  with no NYSE session, and otherwise captures through to the calendar's actual
  close (early closes included) instead of a fixed duration.
- bounds its own memory: dedup keys on each symbol's last-seen sequence number
  only, and keeps only a fixed-size reservoir of quote-age samples plus running
  lifetime min/max/count/sum. The dedup horizon is "this process's lifetime,
  per symbol, last event only" -- it resets on restart.
- decouples ingest from upload: closed segments go to a persistent spool
  directory and a background thread uploads them with retry/backoff, so a slow
  or failing upload never blocks the stream reader. Spool growth is bounded:
  exceeding the cap stops ingestion (``SpoolExhausted``) rather than ever
  silently dropping an event.
- claims the day with a renewable, fenced lease: a heartbeat renews it
  conditionally against the owner's own copy, and loses ownership (stopping
  ingestion) rather than clobbering another owner's lease. A renewal failure
  that isn't a confirmed loss (a transient network/storage error) is retried,
  not swallowed -- ownership is only relinquished once the last *confirmed*
  deadline actually elapses. A crash/restart reconciles against R2 by content
  hash before resuming, and re-enqueues any locally-spooled segment R2 doesn't
  already have -- content identity is always verified, never assumed, before
  deleting local evidence.
- spools each session under its own per-date subdirectory, so a crash on one
  date can never be resumed under a different date's archive prefix; any other
  date's leftover spool is recovered under its own prefix before today's
  session starts.
- persists the day's selected contract universe once and reloads it on any
  same-day restart, instead of re-selecting against the current spot price.
- owns the account's single Tradier market-data stream. Tradier permits one
  simultaneous market-data stream, so several underlyings (``MOO144_UNDERLYINGS``,
  e.g. ``QQQ,IBIT:nearest``) share one subscription and one stream lease; a
  ``StreamRouter`` fans events out to per-underlying archive lanes, each with
  its own universe, spool, uploader, reconciliation, stats and summary. The
  first-listed underlying is the primary and keeps the single-underlying
  startup path; the others are optional and admitted only if their whole
  preparation finishes by a pre-open cutoff, so they can never delay or abort
  the primary's connection.
- derives "complete" vs "partial" from actual coverage -- on-time setup (not
  just an on-time open-wait), at least one captured event, no reconnect gaps,
  spool fully drained, reconciliation clean -- rather than merely "reached
  wall-clock close without an exception". A gap stays open (measuring the full
  outage) until data actually resumes, not merely after a backoff sleep.
- exits non-zero on a recoverable operational failure (spool exhaustion, an
  undrained upload backlog, zero captured events) so the deployment's restart
  policy actually fires, while deliberately exiting 0 on lease loss -- another
  owner is legitimately active for that run_date, and restarting would only
  start a competing-restart loop against them.

Where the run happens, in the order ``run.main`` reads:

    config.py    environment settings, calendar gate, the process STOP flag
    archive.py   R2 key layout and local spool directory names
    lease.py     the day's single stream lease (claim, renew, watch)
    universe.py  select the day's contracts once, reload on restart
    spool.py     segment spool, background uploader, record counting
    recovery.py  reconcile spooled segments against R2 after a crash
    artifacts.py the JSON evidence written to R2
    lanes.py     per-underlying lanes sharing one stream
    capture.py   the session read loop
    stats.py     bounded per-lane capture statistics
    run.py       main(): settings -> lease -> capture -> finalize -> exit code

The entry point stays ``producers/tradier/__main__.py`` (the deployed
start command and ``producers/tradier/launcher.py`` name that path); it re-exports
this package. ``STOP`` is deliberately *not* re-exported: the signal flag has
one owner, ``config.STOP``, so setting a copy elsewhere cannot silently fail to
stop capture.
"""

from __future__ import annotations

import requests

from ._probe import (
    DEFAULT_UNDERLYING,
    EXPIRATION_POLICIES,
    STREAM,
    Tradier,
    is_retryable,
    normalize,
    parse_epoch_ms,
    put_bytes_verified,
    r2_client,
    select_symbols,
    sha256_file,
    stream_payload,
    upload_file_verified,
    utc_now,
)

from .config import (
    ET,
    EXPECTED_TIMESALE_FIELDS,
    QUOTE_AGE_RESERVOIR_SIZE,
    PREOPEN_SETUP_SECONDS,
    HEALTH_PUBLISH_INTERVAL_SECONDS,
    MAX_UNDERLYINGS,
    OPTIONAL_LANE_RESERVE_SECONDS,
    LATE_START_OPTIONAL_BUDGET_SECONDS,
    OPTIONAL_LANE_POLL_SECONDS,
    SPOOL_VOLUME_MAX_FRACTION,
    parse_underlyings,
    on_stop,
    nyse_session_bounds,
)

from .archive import (
    QQQ_ARCHIVE_ROOT,
    archive_root,
    spool_dir_name,
    lease_key,
    universe_key,
    spool_dir_for,
)

from .stats import (
    BoundedStats,
)

from .lease import (
    LeaseLost,
    LeaseTakenByAnotherOwner,
    LeaseMissing,
    LeaseOwnershipUncertain,
    LeaseUnavailable,
    acquire_lease,
    renew_lease,
    run_lease_heartbeat,
    run_lease_deadline_watchdog,
)

from .universe import (
    load_or_select_universe,
    load_persisted_universe,
    persist_universe,
)

from .spool import (
    SpoolExhausted,
    Uploader,
    SegmentSpool,
    count_ndjson_gz_records,
    count_ndjson_gz_records_local,
)

from .recovery import (
    reconcile_existing_segments,
    recover_stale_sessions,
)

from .artifacts import (
    json_artifact,
    write_health,
)

from .lanes import (
    Lane,
    LaneAdmissionClosed,
    prepare_optional_lane,
    admit_optional_lanes,
    StreamRouter,
)

from .capture import (
    CaptureResult,
    capture_session,
)

from .run import (
    main,
)

from .lease import _is_confirmed_absent, _read_lease, _require_live_lease_owner  # noqa: F401
from .recovery import _is_readable_gzip_ndjson, _remote_content_matches  # noqa: F401

__all__ = [
    "requests",
    "BoundedStats",
    "CaptureResult",
    "DEFAULT_UNDERLYING",
    "ET",
    "EXPECTED_TIMESALE_FIELDS",
    "EXPIRATION_POLICIES",
    "HEALTH_PUBLISH_INTERVAL_SECONDS",
    "LATE_START_OPTIONAL_BUDGET_SECONDS",
    "Lane",
    "LaneAdmissionClosed",
    "LeaseLost",
    "LeaseMissing",
    "LeaseOwnershipUncertain",
    "LeaseTakenByAnotherOwner",
    "LeaseUnavailable",
    "MAX_UNDERLYINGS",
    "OPTIONAL_LANE_POLL_SECONDS",
    "OPTIONAL_LANE_RESERVE_SECONDS",
    "PREOPEN_SETUP_SECONDS",
    "QQQ_ARCHIVE_ROOT",
    "QUOTE_AGE_RESERVOIR_SIZE",
    "SPOOL_VOLUME_MAX_FRACTION",
    "STREAM",
    "SegmentSpool",
    "SpoolExhausted",
    "StreamRouter",
    "Tradier",
    "Uploader",
    "acquire_lease",
    "admit_optional_lanes",
    "archive_root",
    "capture_session",
    "count_ndjson_gz_records",
    "count_ndjson_gz_records_local",
    "is_retryable",
    "json_artifact",
    "lease_key",
    "load_or_select_universe",
    "load_persisted_universe",
    "main",
    "normalize",
    "nyse_session_bounds",
    "on_stop",
    "parse_epoch_ms",
    "parse_underlyings",
    "persist_universe",
    "prepare_optional_lane",
    "put_bytes_verified",
    "r2_client",
    "reconcile_existing_segments",
    "recover_stale_sessions",
    "renew_lease",
    "run_lease_deadline_watchdog",
    "run_lease_heartbeat",
    "select_symbols",
    "sha256_file",
    "spool_dir_for",
    "spool_dir_name",
    "stream_payload",
    "universe_key",
    "upload_file_verified",
    "utc_now",
    "write_health",
]
