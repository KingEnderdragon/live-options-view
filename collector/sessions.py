"""Session lifecycle: exchange-session gating and the weekly loop.

WHY this module: everything here answers one question -- is it an
eligible NYSE session right now, and if not, when is the next one? The
collector must never authenticate with tastytrade outside a session, so
wait_for_premarket() blocks (sleeping, never logging in) until
eligibility holds, and the weekly loop runs on the same hourly rhythm
without ever touching the tactical minute loop. Its test patch points
(_exchange_session_dates, stubbed by test_auth_safety) resolve through
the package entry.
"""

import time
from datetime import date, datetime, timedelta
from typing import Optional

import collector as _entry  # entry indirection for _session_is_eligible/_next_session_start's _exchange_session_dates patch point

from .config import (
    ET,
    PREMARKET_HOUR,
    SNAPSHOT_SECS,
    _inside_session_window,
    _session_bounds,
    log,
    past_stop,
)
from .feed import DXLinkFeed
from .weekly import _weekly_snapshot_slot, take_weekly_snapshot

# -- session lifecycle --------------------------------------------------------

class ExchangeCalendarUnavailable(RuntimeError):
    """Raised when exchange-session eligibility cannot be established safely."""


def _exchange_session_dates(start: date, end: date) -> set[date]:
    try:
        import pandas_market_calendars as mcal
        calendar = mcal.get_calendar("NYSE")
        return {
            timestamp.date()
            for timestamp in calendar.valid_days(
                start_date=start.isoformat(), end_date=end.isoformat()
            )
        }
    except Exception as exc:
        raise ExchangeCalendarUnavailable(type(exc).__name__) from exc

def weekly_snapshot_loop(s3, feed: DXLinkFeed, strikes: list[dict],
                         exp_date: str, today: date) -> None:
    """Collect one weekly snapshot per hourly regular-session slot."""
    last_slot = None
    while not past_stop():
        now_et = datetime.now(ET)
        try:
            slot = _weekly_snapshot_slot(now_et)
            if slot is not None and slot != last_slot:
                if take_weekly_snapshot(s3, feed, strikes, exp_date, today, now_et):
                    last_slot = slot
        except Exception as exc:
            # Calendar/data/storage errors leave the slot retryable and never
            # affect the tactical minute loop.
            log.error(f"weekly snapshot error: {exc}")
        time.sleep(SNAPSHOT_SECS)


def _session_is_eligible(et: datetime, blocked_session_date: date | None = None) -> bool:
    """Return True only when auth is allowed for this exchange-session date."""
    if blocked_session_date == et.date() or not _inside_session_window(et):
        return False
    return et.date() in _entry._exchange_session_dates(et.date(), et.date())


def _next_session_start(et: datetime, blocked_session_date: date | None = None) -> datetime:
    horizon = et.date() + timedelta(days=14)
    for session_date in sorted(_entry._exchange_session_dates(et.date(), horizon)):
        if session_date == blocked_session_date:
            continue
        candidate = ET.localize(datetime(
            session_date.year, session_date.month, session_date.day,
            PREMARKET_HOUR, 0, 0,
        ))
        if session_date == et.date() and et < _session_bounds(et)[1]:
            # Preserve the helper's established contract: while today's
            # unblocked session has not ended, its start is still "next".
            # wait_for_premarket() checks eligibility before calling us.
            return candidate
        if candidate >= et:
            return candidate
    raise ExchangeCalendarUnavailable("no-session-in-14-day-window")


def wait_for_premarket(blocked_session_date: date | None = None):
    """Block until an unblocked NYSE session is inside 06:00-16:15 ET.

    Calendar lookup failures are fail-closed: the collector sleeps and makes no
    tastytrade request until exchange-session eligibility can be established.
    """
    while True:
        et = datetime.now(ET)
        try:
            if _session_is_eligible(et, blocked_session_date):
                return
            base = _next_session_start(et, blocked_session_date)
        except ExchangeCalendarUnavailable as exc:
            log.error(f"exchange calendar unavailable ({exc}) -- auth remains disabled; retrying in 1h")
            time.sleep(3600)
            continue
        delay = (base - et).total_seconds()
        log.info(
            f"outside eligible exchange session -- sleeping "
            f"{int(delay // 3600)}h {int((delay % 3600) // 60)}m "
            f"until {base.strftime('%Y-%m-%d %H:%M ET')}"
        )
        time.sleep(min(delay, 3600))


        # After session end or any failed startup, wait_for_premarket() sleeps
        # until the next eligible exchange session. A downstream startup error
        # therefore cannot indirectly repeat the tastytrade login chain.



__all__ = [
    "ExchangeCalendarUnavailable",
    "_exchange_session_dates",
    "weekly_snapshot_loop",
    "_session_is_eligible",
    "_next_session_start",
    "wait_for_premarket",
]
