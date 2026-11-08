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
import collector as _entry  # entry indirection for test patch points

@lru_cache(maxsize=32)
def _weekly_session_bounds(day: date) -> Optional[tuple[datetime, datetime]]:
    """Exchange hours, including holidays and early closes; fail closed on errors."""
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (row["market_open"].to_pydatetime().astimezone(ET),
            row["market_close"].to_pydatetime().astimezone(ET))


def _weekly_snapshot_slot(ts_et: datetime) -> Optional[datetime]:
    """Return the hourly RTH slot containing ``ts_et``, or None outside RTH."""
    ts_et = ts_et.astimezone(ET)
    bounds = _entry._weekly_session_bounds(ts_et.date())
    if bounds is None:
        return None
    session_open, session_close = bounds
    if not session_open <= ts_et < session_close:
        return None
    elapsed = (ts_et - session_open).total_seconds()
    return session_open + timedelta(
        seconds=int(elapsed // WEEKLY_SNAPSHOT_SECS) * WEEKLY_SNAPSHOT_SECS
    )


def _weekly_day_prefix(exp_date: str, trade_date: date) -> str:
    expiry = date.fromisoformat(exp_date).strftime("%Y%m%d")
    return f"{WEEKLY_R2_PREFIX}/{expiry}/qqq_chain_{trade_date.strftime('%Y%m%d')}_"


def _weekly_existing_snapshot(s3, exp_date: str, trade_date: date,
                              slot: datetime) -> Optional[str]:
    """Find an already-written observation in this slot after a restart."""
    prefix = _weekly_day_prefix(exp_date, trade_date)
    response = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix)
    for item in response.get("Contents", []):
        key = item.get("Key", "")
        match = re.search(r"_(\d{6,12})\.csv$", key)
        if not match:
            continue
        observed_time = datetime.strptime(match.group(1)[:4], "%H%M").time()
        observed = datetime.combine(trade_date, observed_time)
        observed_et = ET.localize(observed)
        if _weekly_snapshot_slot(observed_et) == slot:
            return key
    return None


def _load_pipeline_manifest(s3) -> dict:
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key="manifest.json")["Body"].read()
        return json.loads(body.decode())
    except Exception as exc:
        response = getattr(exc, "response", {})
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        if error.get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return {"dates": []}
        raise


def _update_weekly_manifest(s3, exp_date: str, snapshot_key: str) -> None:
    """Extend the existing pipeline manifest without changing its date index."""
    manifest = _load_pipeline_manifest(s3)
    weekly = manifest.setdefault("weekly_expirations", {})
    if not isinstance(weekly, dict):
        raise RuntimeError("manifest weekly_expirations must be an object")
    keys = weekly.setdefault(exp_date, [])
    if not isinstance(keys, list):
        raise RuntimeError(f"manifest weekly_expirations[{exp_date}] must be a list")
    if snapshot_key not in keys:
        keys.append(snapshot_key)
        keys.sort()
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    s3.put_object(
        Bucket=R2_BUCKET,
        Key="manifest.json",
        Body=json.dumps(manifest, indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )


def _weekly_fresh_value(value, observed_at, now: datetime) -> bool:
    """Only finite nonnegative prices with recent feed observations qualify."""
    try:
        age = (now - datetime.fromisoformat(observed_at)).total_seconds()
        return (not isinstance(value, bool) and math.isfinite(float(value))
                and float(value) >= 0 and 0 <= age <= STALE_FEED_SECS)
    except (TypeError, ValueError, OverflowError):
        return False


def take_weekly_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                         exp_date: str, today: date,
                         ts_et: Optional[datetime] = None) -> Optional[str]:
    """Archive observed contracts, retaining the pipeline's 18-column schema.

    Success requires a fresh underlying and at least one fresh, non-crossed
    two-sided option quote. Unquoted/stale contracts are omitted, so partial
    coverage is possible; unknown OI/volume remain blank, observed zero stays
    zero. A slot without qualifying data remains retryable. Summary/Greeks
    fields retain the feed's existing semantics, not new freshness guarantees.
    """
    ts_et = ts_et or datetime.now(ET)
    slot = _weekly_snapshot_slot(ts_et)
    if slot is None:
        return None

    existing_key = _weekly_existing_snapshot(s3, exp_date, today, slot)
    if existing_key:
        _update_weekly_manifest(s3, exp_date, existing_key)
        log.info(f"weekly slot already collected -> {existing_key}")
        return existing_key

    state = feed.get_state()
    qqq = state.get(TICKER, {})
    if (_weekly_fresh_value(qqq.get("bid"), qqq.get("bid_ts"), ts_et)
            and _weekly_fresh_value(qqq.get("ask"), qqq.get("ask_ts"), ts_et)
            and qqq["ask"] >= qqq["bid"]):
        underlying, spot_ts = _resolve_underlying_spot(qqq, None, None)
    else:
        underlying, spot_ts = qqq.get("last"), qqq.get("last_ts")
    if not underlying or not _weekly_fresh_value(underlying, spot_ts, ts_et):
        log.warning("weekly snapshot awaiting fresh underlying data -- slot remains retryable")
        return None
    atm = round(underlying) if underlying else None

    rows = []
    dte = (date.fromisoformat(exp_date) - today).days
    for strike_row in strikes:
        strike = strike_row["strike"]
        if atm is not None and abs(strike - atm) > STRIKE_WINDOW:
            continue
        for option_type, sym_key, occ_key in (
            ("call", "call_sym", "call_occ"),
            ("put",  "put_sym",  "put_occ"),
        ):
            sym = strike_row[sym_key]
            data = state.get(sym, {})
            bid = data.get("bid")
            ask = data.get("ask")
            if not (_weekly_fresh_value(bid, data.get("bid_ts"), ts_et)
                    and _weekly_fresh_value(ask, data.get("ask_ts"), ts_et)
                    and ask >= bid):
                continue
            mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    strike_row[occ_key] or sym,
                "DTE":             dte,
                "OpenInterest":    data.get("oi"),
                "Volume":          data.get("volume"),
                "Bid":             bid,
                "Mid":             mid,
                "Ask":             ask,
                "Last":            data.get("last"),
                "IV":              data.get("volatility"),
                "Delta":           data.get("delta"),
                "Gamma":           data.get("gamma"),
                "Theta":           data.get("theta"),
                "Vega":            data.get("vega"),
                "UnderlyingPrice": underlying,
            })

    if not rows:
        log.warning("weekly snapshot awaiting fresh option quotes -- slot remains retryable")
        return None

    key = (
        _weekly_day_prefix(exp_date, today)
        + ts_et.strftime("%H%M%S%f")
        + ".csv"
    )
    csv_buf = io.StringIO()
    pd.DataFrame(rows, columns=PIPELINE_CHAIN_COLUMNS).to_csv(csv_buf, index=False)
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=csv_buf.getvalue().encode(),
        ContentType="text/csv",
    )
    _update_weekly_manifest(s3, exp_date, key)
    bid_count = sum(1 for row in rows if row["Bid"] is not None)
    log.info(
        f"-> {key}  ({len(rows)} weekly rows, expiration={exp_date}, "
        f"underlying={underlying}, bids={bid_count})"
    )
    return key


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

