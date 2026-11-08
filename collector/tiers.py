"""Exchange calendar and tier classification.

WHY this module: the tier label (0DTE vs weekly vs opex) is a labelling
concern, not a market-data concern -- classify_tier says so itself
("never let it take down market-data streaming"). It reads only the
shared log plus a lazily-imported NYSE calendar, so it moves out as a
leaf. Its test patch point (_load_calendar, stubbed by stage2 and
verify_weekly_collection) resolves through the package entry.
"""

from datetime import date, timedelta

import collector as _entry  # entry indirection for classify_tier's _load_calendar patch point

from .config import log

# -- tier classification (mirrors oi_viewer.py) -------------------------------

# The window must reach back far enough to cover the current month's monthly-opex
# Friday, which classify_tier looks up and which is already in the past for most of
# the second half of any month.
_CALENDAR_LOOKBACK_DAYS = 45
_CALENDAR_LOOKAHEAD_DAYS = 90


def _load_calendar():
    try:
        import pandas_market_calendars as mcal
        nyse  = mcal.get_calendar("NYSE")
        start = date.today() - timedelta(days=_CALENDAR_LOOKBACK_DAYS)
        end   = date.today() + timedelta(days=_CALENDAR_LOOKAHEAD_DAYS)
        return {d.date() for d in nyse.valid_days(start_date=start.isoformat(),
                                                    end_date=end.isoformat())}
    except Exception as exc:
        log.warning(f"trading calendar unavailable ({exc}) -- tier falls back to 0DTE_Regular")
        return set()


DEFAULT_TIER = "0DTE_Regular"

# Bound on how far the trading-day walks may step before giving up. The calendar
# window is finite, so an unbounded walk off either end runs to date.min/date.max
# and raises OverflowError -- which used to kill the whole collector session.
_TD_WALK_LIMIT = 30


class _CalendarLookupError(Exception):
    """A trading-day walk ran past the edge of the loaded calendar window."""


def classify_tier(today: date) -> str:
    import calendar as _cal

    valid = _entry._load_calendar()
    if not valid:
        return DEFAULT_TIER

    def prior_td(d):
        for _ in range(_TD_WALK_LIMIT):
            if d in valid:
                return d
            d -= timedelta(days=1)
        raise _CalendarLookupError(f"no trading day at or before {d} within calendar window")

    def next_td(d):
        for _ in range(_TD_WALK_LIMIT):
            d += timedelta(days=1)
            if d in valid:
                return d
        raise _CalendarLookupError(f"no trading day after {d} within calendar window")

    def nominal_fri(d):
        return d + timedelta(days=(4 - d.weekday()) % 7)

    try:
        eow    = prior_td(nominal_fri(today))
        plus1d = next_td(today)
        if plus1d != eow:
            return DEFAULT_TIER

        count, opex = 0, None
        for day in range(1, _cal.monthrange(plus1d.year, plus1d.month)[1] + 1):
            if date(plus1d.year, plus1d.month, day).weekday() == 4:
                count += 1
                if count == 3:
                    opex = prior_td(date(plus1d.year, plus1d.month, day))
                    break
        return "0DTE_Monthly" if plus1d == opex else "0DTE_Weekly"
    except _CalendarLookupError as exc:
        # Tier is a labelling concern; never let it take down market-data streaming.
        log.warning(f"tier classification fell back to {DEFAULT_TIER}: {exc}")
        return DEFAULT_TIER



__all__ = [
    "_CALENDAR_LOOKBACK_DAYS",
    "_CALENDAR_LOOKAHEAD_DAYS",
    "_TD_WALK_LIMIT",
    "DEFAULT_TIER",
    "_load_calendar",
    "_CalendarLookupError",
    "classify_tier",
]
