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
import collector as _entry  # entry indirection for test patch points

# -- snapshot upload ----------------------------------------------------------

def _compute_underlying_market(s3, qqq: dict, underlying: float | None, spot_ts_str: str | None,
                                ts_et: datetime, ts_utc: datetime, today: date,
                                us: "UnderlyingSession | None" = None) -> dict:
    """VWAP/RVOL for the `underlying_market` block of `intraday/latest.json`.

    Approximation, not tick-accurate VWAP: weights the snapshot's own spot
    price by the volume delta since the last snapshot, not by each
    individual trade's own price (see market_signals.py's module docstring).

    `spot_ts_str` must already be paired with `underlying` by the caller
    (see take_snapshot's spot-resolution block) -- this function does not
    re-derive it from `qqq`, so a value that fell back to a non-DXLink source
    can't end up stamped with an unrelated DXLink timestamp.
    """
    us = us or QQQ_SESSION
    us.vwap_state = ms.reset_if_new_session(us.vwap_state, today)

    raw_day_volume = qqq.get("volume")
    spot_observed_at = datetime.fromisoformat(spot_ts_str) if spot_ts_str else None

    us.vwap_state = ms.accumulate_vwap(
        us.vwap_state, price=underlying, raw_day_volume=raw_day_volume, observed_at=spot_observed_at,
    )
    _persist_vwap_state(s3, today.strftime("%Y%m%d"), us)
    vs = us.vwap_state

    bucket = ms.bucket_label(ts_et, RVOL_BUCKET_MINUTES)
    if raw_day_volume is not None:
        us.rvol_today[bucket] = raw_day_volume

    baseline_samples = us.rvol_baseline.get("buckets", {}).get(bucket, {}).get("samples", [])
    rvol = ms.compute_rvol(raw_day_volume, baseline_samples, min_days_required=RVOL_MIN_DAYS_REQUIRED)

    price_vs_vwap_abs = price_vs_vwap_pct = None
    if underlying is not None and vs.vwap:
        price_vs_vwap_abs = round(underlying - vs.vwap, 4)
        price_vs_vwap_pct = round((underlying - vs.vwap) / vs.vwap * 100, 4)

    freshness = ms.classify_freshness(
        inside_session_window=True,  # take_snapshot only ever runs inside the session window
        spot_ts=spot_observed_at, now=ts_utc, stale_after_s=STALE_FEED_SECS,
    )

    session_start, _ = _session_bounds(ts_et)
    vwap_partial_session = ms.is_partial_session(vs, session_start)

    momentum = _compute_and_log_momentum(s3, underlying, spot_observed_at, ts_utc, today, us)

    return {
        "symbol": us.symbol,
        "spot": underlying,
        "spot_ts": spot_ts_str,
        "vwap": vs.vwap,
        "vwap_ts": vs.vwap_ts,
        "vwap_session_date": vs.session_date.isoformat() if vs.session_date else None,
        "vwap_session_started_at": vs.session_started_at.isoformat() if vs.session_started_at else None,
        "vwap_partial_session": vwap_partial_session,
        "price_vs_vwap_abs": price_vs_vwap_abs,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "session_volume": raw_day_volume,
        "session_volume_ts": spot_ts_str,
        "rvol": {
            "status": rvol.status,
            "multiple": rvol.multiple,
            "bucket_label": bucket,
            "baseline_volume": rvol.baseline_volume,
            "baseline_days_used": rvol.baseline_days_used,
            "baseline_lookback_days": us.rvol_baseline.get("lookback_days", RVOL_LOOKBACK_DAYS),
            "baseline_updated_through": us.rvol_baseline.get("updated_through"),
        },
        "momentum": momentum,
        "source": "dxlink",
        "freshness": freshness,
    }


def _compute_and_log_momentum(
    s3, underlying: float | None, spot_observed_at: datetime | None, ts_utc: datetime, today: date,
    us: "UnderlyingSession | None" = None,
) -> dict:
    """Display/log-only time-series momentum -- see market_signals.py's
    module docstring for why this is a separate computation from
    momentum_qqq's own live trading signal, not a shared one.
    """
    us = us or QQQ_SESSION
    if underlying is not None and spot_observed_at is not None:
        us.momentum_history.append(ms.SpotPoint(observed_at=spot_observed_at, price=underlying))
    us.momentum_history = ms.prune_spot_history(us.momentum_history, ts_utc, MOMENTUM_RETAIN_MINUTES)

    reading = ms.compute_time_series_momentum(
        us.momentum_history, ts_utc,
        lookback_minutes=MOMENTUM_LOOKBACK_MINUTES,
        max_anchor_overshoot_minutes=MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
        neutral_band_pct=MOMENTUM_NEUTRAL_BAND_PCT,
    )

    log.info(
        f"momentum {us.symbol}: status={reading.status} return_pct={reading.return_pct} "
        f"direction={reading.direction} sample_count={reading.sample_count}"
    )
    _log_momentum_reading(s3, today.strftime("%Y%m%d"), {
        "ts": ts_utc.isoformat(),
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    }, us)

    return {
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    }


def _resolve_underlying_spot(
    qqq: dict, last_spot_price: float | None, last_spot_ts: str | None,
) -> tuple[float | None, str | None]:
    """Resolve the current spot price and its paired observation timestamp
    together, branch for branch, so the two always describe the same
    observation. Previously `underlying` could fall through to the
    yfinance-backed `last_spot` fallback while its timestamp was derived
    independently from `qqq`'s DXLink state -- pairing a feed-derived
    timestamp with a value that didn't actually come from the feed.
    `last_spot_ts` is `None` whenever `last_spot_price` came from yfinance
    (see `push_prices()`), which has no trustworthy provider event
    timestamp -- that correctly makes this fallback classify as "stale",
    never "live".
    """
    bid, ask = qqq.get("bid"), qqq.get("ask")
    if bid and ask:
        return round((bid + ask) / 2, 2), max([v for v in (qqq.get("bid_ts"), qqq.get("ask_ts")) if v], default=None)
    if qqq.get("last"):
        return qqq.get("last"), qqq.get("last_ts")
    if last_spot_price is not None:
        return round(last_spot_price, 2), last_spot_ts
    return None, None


def take_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                  exp_date: str, tier: str, today: date,
                  counters: Counters, tracker: SnapshotTracker,
                  us: "UnderlyingSession | None" = None):
    us     = us or QQQ_SESSION
    state  = feed.get_state()
    ts_et  = datetime.now(ET)
    ts_utc = datetime.now(timezone.utc)

    quote = state.get(us.symbol, {})
    if us.symbol == "SPY":
        underlying, spot_ts_str = _observed_spy_price(quote)
        if underlying is None:
            underlying, spot_ts_str = us.last_spot
    else:
        underlying, spot_ts_str = _resolve_underlying_spot(quote, us.last_spot[0], us.last_spot[1])
    atm = round(underlying) if underlying else None

    underlying_market = _compute_underlying_market(s3, quote, underlying, spot_ts_str, ts_et, ts_utc, today, us)

    rows = []
    for s in strikes:
        strike = s["strike"]
        if atm is not None and abs(strike - atm) > STRIKE_WINDOW:
            continue
        for option_type, sym_key, occ_key in (
            ("call", "call_sym", "call_occ"),
            ("put",  "put_sym",  "put_occ"),
        ):
            sym  = s[sym_key]
            data = state.get(sym, {})
            b    = data.get("bid")
            a    = data.get("ask")
            mid  = round((b + a) / 2, 4) if b is not None and a is not None else None
            vol  = data.get("volume", 0) or 0
            vol_delta = max(0, vol - us.prev_vol.get(sym, vol))
            us.prev_vol[sym] = vol
            price_cols = {
                lbl.replace("/", "_"): remembered.get("price")
                for lbl, remembered in _last_prices.items()
            }
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    s[occ_key],
                "DTE":             0,
                "OpenInterest":    data.get("oi", 0) or 0,
                "Volume":          vol,
                "VolDelta":        vol_delta,
                "Bid":             b,
                "Mid":             mid,
                "Ask":             a,
                "Last":            data.get("last"),
                "IV":              data.get("volatility"),
                "Delta":           data.get("delta"),
                "Gamma":           data.get("gamma"),
                "Theta":           data.get("theta"),
                "Vega":            data.get("vega"),
                "UnderlyingPrice": underlying,
                **price_cols,
            })

    if not rows:
        log.warning(f"{us.symbol} snapshot empty -- state not populated yet")
        return

    bid_count = sum(1 for r in rows if r.get("Bid") is not None)

    df      = pd.DataFrame(rows)
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)

    date_str = today.strftime("%Y%m%d")
    time_str = ts_et.strftime("%H%M%S%f")   # microsecond precision prevents overwrite on rapid restart
    csv_key  = f"{us.day_prefix(date_str)}snapshot_{time_str}.csv"

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=csv_key,
            Body=csv_buf.getvalue().encode(),
            ContentType="text/csv",
        )
        counters.inc_csv()
        log.info(f"-> {csv_key}  ({len(rows)} rows,  underlying={underlying},  bids={bid_count})")
    except Exception as e:
        log.error(f"CSV upload failed: {e}")
        counters.inc_failure()
        raise

    if not us.first_snapshot_written:
        first_key = f"{us.day_prefix(date_str)}first.csv"
        try:
            s3.put_object(
                Bucket=R2_BUCKET, Key=first_key,
                Body=csv_buf.getvalue().encode(),
                ContentType="text/csv",
            )
            us.first_snapshot_written = True
            log.info(f"-> {first_key}  (session-open snapshot, mirrors {csv_key})")
        except Exception as e:
            log.warning(f"first.csv upload failed (non-fatal): {e}")

    if bid_count == 0:
        log.warning(f"{us.latest_key} NOT updated -- no option data (DXLink feed down)")
        return

    payload = {
        "symbol":           us.symbol,
        "timestamp":        ts_utc.isoformat(),
        "snapshot_time":    ts_et.strftime("%H:%M ET"),
        "date":             today.isoformat(),
        "expiration":       exp_date,
        "tier":             tier,
        "underlying_price": underlying,
        "snapshot_key":     csv_key,
        "rows":             rows,
        "underlying_market": underlying_market,
    }
    if us.symbol == "SPY":
        payload["subscription_coverage"] = _spy_subscription_coverage(
            us.subscription_coverage, _fresh_spy_spot(quote, ts_utc)[0])

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=us.latest_key,
            Body=json.dumps(payload, default=str).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        counters.inc_snapshot(ts_utc.isoformat())
        tracker.record()
        log.info(f"-> {us.latest_key} updated")
    except Exception as e:
        log.error(f"latest.json upload failed: {e}")
        counters.inc_failure()
        raise


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

