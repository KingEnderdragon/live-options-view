"""OA-203 options return scanner.

Scans the day's most-traded underlyings for short-dated option returns: it
ranks underlyings by OCC contract volume, selects an expiration per
underlying, sweeps their chains repeatedly from the open, archives every sweep
row, and after the close ranks the contracts into a leaderboard. The same row
builder also publishes an intraday "active" snapshot for the market MCP.

Where the work happens, in the order a session touches it:

    runtime.py     event log, wall clock, stop flag, run settings
    occ.py         OCC daily volume -> the day's underlyings
    tradier.py     the market-data client (retries, rate limits, budget)
    archive.py     the day directory: journal, manifest, row iteration
    sweep.py       universe selection and one sweep across it
    outputs.py     leaderboard rows and the CSV/JSON artifacts
    active.py      the intraday active snapshot and its artifact budget
    backfill.py    post-close trade-bar backfill for the top contracts
    assessment.py  was the session good enough to trust?
    session.py     one trading day: build, sweep, finalize, recover
    readout.py     offline inspection of an archived day
    cli.py         run | inspect | readout
    returns.py     return/leaderboard metrics (pure functions)

The deployed entry point stays ``market_data/options_scanner/__main__.py`` (the Railway
start command names that path); it re-exports this package.
"""

from __future__ import annotations

import hashlib
import requests
from datetime import datetime

from .runtime import (
    API,
    OCC_VOLUME_URL,
    ET,
    SELECTION_VERSION,
    log,
    on_stop,
    now_ms,
    Config,
    nyse_session_bounds,
    previous_session,
)

from .occ import (
    fetch_occ_volume,
    parse_occ_volume,
    rank_underlyings,
)

from .tradier import (
    RateLimited,
    SamplingDeadline,
    BudgetExhausted,
    Tradier,
    load_token,
)

from .archive import (
    UPLOAD_JOURNAL,
    UPLOAD_RECEIPT,
    SUMMARY,
    INTERRUPTIONS,
    RECOVERY_ERRORS,
    DayArchive,
    published,
    READ_ERRORS,
    iter_rows,
)

from .sweep import (
    nearest_expiration,
    second_friday_expiration,
    select_universe,
    chain_rows,
    BUDGET_SKIP,
    run_sweep,
)

from .outputs import (
    chain_ok_sweeps,
    chain_ok_prefixes,
    load_backfill,
    build_contract_rows,
    write_outputs,
)

from .active import (
    ACTIVE_MEMBERS,
    ACTIVE_BUILD_SECONDS,
    ACTIVE_INTERVAL_S,
    ACTIVE_LATE_FACTOR,
    ACTIVE_MAX_SNAPSHOTS_PER_DAY,
    ACTIVE_MAX_SWEEP,
    ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP,
    active_snapshot_due,
    cadence_report,
    ACTIVE_MAX_MEMBER_BYTES,
    ACTIVE_MAX_MANIFEST_LINE_BYTES,
    reserve_active_upload_attempt,
    write_active_attempt,
    active_build_lock,
    build_active_snapshot,
)

from .backfill import (
    backfill_symbols,
    run_backfill,
)

from .assessment import (
    START_GRACE_MS,
    MIN_UNIVERSE_COVERAGE,
    MIN_CHAIN_SUCCESS,
    SLOW_SWEEP_FACTOR,
    expected_sweep_s,
    assess_session,
    MEASUREMENT_NOTE,
)

from .session import (
    MEASUREMENT_NOTE,
    build_universe_for,
    finalize,
    unfinalized_days,
    recover_days,
    run_day,
)

from .readout import (
    inspect_contract,
    readout,
)

from .cli import (
    main,
)

from .returns import (
    POLICY_VERSION,
    QUALIFIED_VERSION,
    QUALIFIED,
    QualifiedPolicy,
    ReturnPolicy,
    Observation,
    Leg,
    first_to_max,
    open_to_close,
    trough_to_peak,
    ContractInfo,
    contract_metrics,
    qualified_metrics,
    trade_bar_metrics,
    HEADLINE,
    rank,
)

from .runtime import _env_int  # noqa: F401
from .archive import _content_type, _is_date, _r2_uploader, _read_jsonl_gz, _read_manifest, _sha256_file  # noqa: F401
from .sweep import _num, _spot  # noqa: F401
from .tradier import _as_list  # noqa: F401
from .active import _active_cutoff_ms, _build_active_snapshot  # noqa: F401
from .outputs import _contract_paths, _cell  # noqa: F401
from .readout import _hour_bucket, _moneyness_bucket  # noqa: F401
from .session import _session_ms, _sleep_until  # noqa: F401
from .cli import _bounds_from_summary  # noqa: F401

__all__ = [
    "ACTIVE_BUILD_SECONDS",
    "ACTIVE_INTERVAL_S",
    "ACTIVE_LATE_FACTOR",
    "ACTIVE_MAX_MANIFEST_LINE_BYTES",
    "ACTIVE_MAX_MEMBER_BYTES",
    "ACTIVE_MAX_SNAPSHOTS_PER_DAY",
    "ACTIVE_MAX_SWEEP",
    "ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP",
    "ACTIVE_MEMBERS",
    "API",
    "BUDGET_SKIP",
    "BudgetExhausted",
    "Config",
    "ContractInfo",
    "DayArchive",
    "ET",
    "HEADLINE",
    "INTERRUPTIONS",
    "Leg",
    "MEASUREMENT_NOTE",
    "MIN_CHAIN_SUCCESS",
    "MIN_UNIVERSE_COVERAGE",
    "OCC_VOLUME_URL",
    "Observation",
    "POLICY_VERSION",
    "QUALIFIED",
    "QUALIFIED_VERSION",
    "QualifiedPolicy",
    "READ_ERRORS",
    "RECOVERY_ERRORS",
    "RateLimited",
    "ReturnPolicy",
    "SELECTION_VERSION",
    "SLOW_SWEEP_FACTOR",
    "START_GRACE_MS",
    "SUMMARY",
    "SamplingDeadline",
    "Tradier",
    "UPLOAD_JOURNAL",
    "UPLOAD_RECEIPT",
    "active_build_lock",
    "active_snapshot_due",
    "assess_session",
    "backfill_symbols",
    "build_active_snapshot",
    "build_contract_rows",
    "build_universe_for",
    "cadence_report",
    "chain_ok_prefixes",
    "chain_ok_sweeps",
    "chain_rows",
    "contract_metrics",
    "datetime",
    "expected_sweep_s",
    "fetch_occ_volume",
    "finalize",
    "first_to_max",
    "hashlib",
    "inspect_contract",
    "iter_rows",
    "load_backfill",
    "load_token",
    "log",
    "main",
    "nearest_expiration",
    "now_ms",
    "nyse_session_bounds",
    "on_stop",
    "open_to_close",
    "parse_occ_volume",
    "previous_session",
    "published",
    "qualified_metrics",
    "rank",
    "rank_underlyings",
    "readout",
    "recover_days",
    "requests",
    "reserve_active_upload_attempt",
    "run_backfill",
    "run_day",
    "run_sweep",
    "second_friday_expiration",
    "select_universe",
    "trade_bar_metrics",
    "trough_to_peak",
    "unfinalized_days",
    "write_active_attempt",
    "write_outputs",
]
