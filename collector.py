#!/usr/bin/env python3
"""collector.py -- thin entry shim for the collector service.

The service now lives in the collector/ package; `import collector` resolves
to collector/__init__.py, which re-exports every name this file used to
define. This file stays only so the Railway start command
`python collector.py` keeps working, and so the not-yet-moved subsystems
still have a home during the split (see collector/__init__.py's bridge).
"""

import io
import json
import logging
import os
import math
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

import boto3
import pandas as pd
import pytz
import requests
import websocket
import yfinance as yf

from collector import crude_calibration as cc
from collector import market_signals as ms
from collector.config import *  # noqa: F401,F403 -- constants + log
from collector.feed import *  # noqa: F401,F403 -- DXLinkFeed
from collector.oauth import *  # noqa: F401,F403 -- tastytrade OAuth
from collector.tiers import *  # noqa: F401,F403 -- calendar + tier
from collector.chain import *  # noqa: F401,F403 -- chain loading
from collector.state import *  # noqa: F401,F403 -- session state + restore
from collector.quotes import *  # noqa: F401,F403 -- live quotes + SPY spot
from collector.prices import *  # noqa: F401,F403 -- S3 + prices loop
from collector.steo import *  # noqa: F401,F403 -- EIA STEO feed
from collector.health import *  # noqa: F401,F403 -- health telemetry
from collector.market import *  # noqa: F401,F403 -- market compute + snapshot
from collector.weekly import *  # noqa: F401,F403 -- weekly snapshots
import collector as _entry  # entry indirection for test patch points

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


from collector.runner import *  # noqa: F401,F403 -- _run_session, main


if __name__ == "__main__":
    main()

