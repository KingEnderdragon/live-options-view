"""Run-level settings for the MOO-169 Tradier collector.

Everything an operator can change through the environment plus the calendar
helpers those settings rely on. The values are read once by
``run.main``; nothing in this module does I/O.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ._probe import EXPIRATION_POLICIES


ET = ZoneInfo("America/New_York")
STOP = False
EXPECTED_TIMESALE_FIELDS = (
    "symbol", "exch", "bid", "ask", "last", "size", "date", "seq",
    "flag", "cancel", "correction", "session",
)
QUOTE_AGE_RESERVOIR_SIZE = 5000
PREOPEN_SETUP_SECONDS = 60
HEALTH_PUBLISH_INTERVAL_SECONDS = 60
MAX_UNDERLYINGS = 4
# Optional lanes must be prepared this long before the open, leaving the main
# thread time to persist/preflight them, create the stream session, connect
# and prove readiness before the opening boundary.
OPTIONAL_LANE_RESERVE_SECONDS = 20
# When the primary is only ready after that cutoff (late start or restart),
# the session is already late; optional lanes get this much more, bounded, so a
# restart does not drop them for the rest of the day over a scheduling race.
LATE_START_OPTIONAL_BUDGET_SECONDS = 5
OPTIONAL_LANE_POLL_SECONDS = 0.25
# Safety net only: configured spool caps may use at most this share of the
# spool volume, leaving room for recovery files and filesystem overhead.
SPOOL_VOLUME_MAX_FRACTION = 0.8


def parse_underlyings(raw: str) -> list[tuple[str, str]]:
    """Parse ``MOO144_UNDERLYINGS``: comma-separated ``SYMBOL[:policy]``.

    All listed underlyings share this process's one Tradier market-data
    stream, so they are configured together rather than as separate services.
    The policy defaults to ``same_day`` (strict 0DTE).
    """
    entries: list[tuple[str, str]] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        symbol, _, policy = item.partition(":")
        symbol = symbol.strip().upper()
        policy = policy.strip().lower() or "same_day"
        if not (symbol.isascii() and symbol.isalpha() and 1 <= len(symbol) <= 6):
            raise RuntimeError(f"MOO144_UNDERLYINGS: invalid symbol {symbol!r}")
        if policy not in EXPIRATION_POLICIES:
            raise RuntimeError(
                f"MOO144_UNDERLYINGS: {symbol} policy must be one of {', '.join(EXPIRATION_POLICIES)}"
            )
        if any(symbol == existing for existing, _ in entries):
            raise RuntimeError(f"MOO144_UNDERLYINGS: duplicate symbol {symbol}")
        entries.append((symbol, policy))
    if not 1 <= len(entries) <= MAX_UNDERLYINGS:
        raise RuntimeError(f"MOO144_UNDERLYINGS must list 1-{MAX_UNDERLYINGS} underlyings")
    return entries


def on_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def nyse_session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Exchange hours for ``day``, including holidays/early closes; None if closed.

    Same pattern as ``market_data/live_market/weekly.py``'s ``_weekly_session_bounds`` --
    kept as its own copy here since the collector package isn't a shared
    library module.
    """
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (
        row["market_open"].to_pydatetime().astimezone(ET),
        row["market_close"].to_pydatetime().astimezone(ET),
    )
