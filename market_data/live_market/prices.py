"""Publish the asynchronous price strip without confusing observation and receipt.

Yahoo's bounded daily fallback intentionally does not claim an intraday time.
Cache/carry-forward retain the entire observation/reference pair and disclose
newer refresh failures. Provider requests and publication cadence are unchanged.
"""

import copy
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import boto3
import yfinance as yf

import market_data.live_market as _entry
from market_data.overview import INSTRUMENTS, UNIVERSE_VERSION
from .config import (
    COLLECT_SPY, ET, PRICES_SECS, PRICE_TICKERS, R2_BUCKET, STALE_FEED_SECS,
    YF_SYMBOL_MAP, log, past_stop,
)
from .feed import DXLinkFeed
from .quotes import LiveQuoteRegistry, _observed_spy_price_with_basis
from .state import Counters, QQQ_SESSION, _last_prices


def make_s3():
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
    )


def _classify_startup(s3, process_start: datetime) -> str:
    try:
        prior = json.loads(s3.get_object(Bucket=R2_BUCKET, Key="intraday/health.json")["Body"].read())
    except Exception:
        return "clean_start"
    if prior.get("collector", {}).get("past_stop", False):
        return "clean_start"
    if prior.get("updated_at"):
        try:
            prior_dt = datetime.fromisoformat(prior["updated_at"].replace("Z", "+00:00"))
            return "recovery_after_crash" if (process_start - prior_dt).total_seconds() < 7200 else "recovery_after_gap"
        except Exception:
            pass
    return "unknown"


def _log_ticker_health(feed: DXLinkFeed):
    state = feed.get_state()
    for label, sym in PRICE_TICKERS.items():
        d = state.get(sym, {})
        price = d.get("last") or ((d["bid"] + d["ask"]) / 2
                                  if d.get("bid") is not None and d.get("ask") is not None else None)
        log.info(f"price ticker {label} ({sym}): {price if price is not None else 'NO DATA'}")


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _positive(value):
    value = _finite(value)
    return value if value is not None and value > 0 else None


def _instrument_id(label):
    # Loaded from the canonical registry (not a second subscription universe).
    return next(row["id"] for row in INSTRUMENTS if row["label"] == label)


YF_FETCH_TIMEOUT_SECS = 20
YF_COOLDOWN_SECS = 300
YF_MIN_INTERVAL_SECS = 60
_yf_cooldown_until: list = [None]
_yf_cache: dict = {"ts": None, "data": None}
_yf_lock = threading.Lock()
_yf_inflight = None


def _failure(error, now=None):
    return {"price": None, "last_attempt_at": now or datetime.now(timezone.utc).isoformat(),
            "refresh_error": error}


def fetch_yf_prices() -> dict:
    """Get dated, unadjusted daily bars; no fast_info or invented close time.

    The provider's exchange-local date and timezone are retained, including a
    possibly still-forming last bar. 'daily_bar_close' is not 'official_close'.
    The preceding valid bar is a dated reference, not an inferred yesterday.
    """
    result = {}
    tickers = yf.Tickers(" ".join(YF_SYMBOL_MAP.values()))
    for label, sym in YF_SYMBOL_MAP.items():
        try:
            bars = tickers.tickers[sym].history(period="5d", interval="1d", auto_adjust=False)
            valid = [(stamp, _positive(row.get("Close"))) for stamp, row in bars.iterrows()]
            valid = [(stamp, price) for stamp, price in valid if price is not None]
            if not valid:
                result[label] = _failure("no_dated_daily_observation")
                continue
            stamp, price = valid[-1]
            zone = str(stamp.tzinfo) if stamp.tzinfo is not None else None
            # The consumer refuses an artifact whose zone it cannot interpret, so a
            # non-IANA zone string must not enter the public payload.
            if not zone:
                result[label] = _failure("daily_observation_timezone_unknown")
                continue
            try:
                ZoneInfo(zone)
            except Exception:
                result[label] = _failure("daily_observation_timezone_unknown")
                continue
            now = datetime.now(timezone.utc).isoformat()
            identity = _instrument_id(label)
            reference = None
            if len(valid) > 1:
                prev_stamp, prev = valid[-2]
                reference = {
                    "instrument_id": identity, "value": prev, "source": "yfinance",
                    "source_symbol": sym, "price_basis": "daily_bar_close",
                    "observed_at": None, "observation_time_reason": "daily_bar_has_no_intraday_time",
                    "observation_date": prev_stamp.date().isoformat(),
                    "observation_timezone": str(prev_stamp.tzinfo), "observation_granularity": "day",
                    "session": "daily", "adjustment_basis": "unadjusted", "resolved_contract": None,
                }
            result[label] = {
                "instrument_id": identity, "price": price, "bid": None, "ask": None,
                "volume": None, "prev_close": reference["value"] if reference else None,
                "source": "yfinance", "source_symbol": sym, "price_basis": "daily_bar_close",
                "observed_at": None, "observation_time_reason": "daily_bar_has_no_intraday_time",
                "observation_date": stamp.date().isoformat(),
                "observation_timezone": zone, "observation_granularity": "day", "session": "daily",
                "adjustment_basis": "unadjusted", "resolved_contract": None,
                "collector_received_at": None, "quote_ts": None,
                "acquired_at": now, "last_success_at": now, "last_attempt_at": now,
                "refresh_error": None, "reference": reference,
            }
        except Exception:
            # No raw provider exception text (URLs/credentials) in public artifacts.
            result[label] = _failure("daily_observation_fetch_failed")
    return result


def fetch_yf_prices_bounded() -> dict:
    """Coalesced, bounded cache refresh; failed attempts never erase success."""
    global _yf_inflight
    with _yf_lock:
        now = datetime.now(timezone.utc)
        if (_yf_cache["ts"] is not None and
                (now - _yf_cache["ts"]).total_seconds() < YF_MIN_INTERVAL_SECS):
            return copy.deepcopy(_yf_cache["data"])
        if _yf_cooldown_until[0] is not None and now < _yf_cooldown_until[0]:
            return copy.deepcopy(_yf_cache["data"] or {})
        box = [None]
        if _yf_inflight is not None and _yf_inflight.is_alive():
            result = {}
            error = "daily_observation_fetch_still_running"
        else:
            def worker():
                try:
                    box[0] = fetch_yf_prices()
                except Exception:
                    box[0] = {}
            _yf_inflight = threading.Thread(target=worker, name="yf-fetch", daemon=True)
            _yf_inflight.start()
            _yf_inflight.join(timeout=YF_FETCH_TIMEOUT_SECS)
            result = box[0] or {}
            error = "daily_observation_timeout" if _yf_inflight.is_alive() else "daily_observation_unavailable"
        finished = datetime.now(timezone.utc)
        merged = {}
        failed = False
        for label in YF_SYMBOL_MAP:
            attempt = result.get(label)
            if isinstance(attempt, dict) and _positive(attempt.get("price")) is not None:
                merged[label] = copy.deepcopy(attempt)
            else:
                failed = True
                remembered = (_yf_cache["data"] or {}).get(label, {})
                merged[label] = copy.deepcopy(remembered)
                merged[label].update(last_attempt_at=(attempt or {}).get("last_attempt_at", now.isoformat())
                                     if isinstance(attempt, dict) else now.isoformat(),
                                     refresh_error=attempt.get("refresh_error", error) if isinstance(attempt, dict) else error)
                merged[label].setdefault("price", None)
        _yf_cache.update(ts=finished, data=merged)
        _yf_cooldown_until[0] = finished + timedelta(seconds=YF_COOLDOWN_SECS) if failed else None
        return copy.deepcopy(merged)


def _dx_row(label, sym, raw, selected=None):
    bid, ask, last = (_positive(raw.get(k)) for k in ("bid", "ask", "last"))
    mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
    if selected is None:
        price, basis = (last, "last_trade") if last is not None else (mid, "bid_ask_midpoint" if mid is not None else None)
        receipt = raw.get("last_ts") if last is not None else (
            min(raw["bid_ts"], raw["ask_ts"]) if mid is not None and raw.get("bid_ts") and raw.get("ask_ts") else None)
    else:
        price, receipt, basis = selected
    if basis == "last_trade":
        observed = raw.get("last_observed_at")
        session = raw.get("last_session")
    else:
        observed = (min(raw["bid_observed_at"], raw["ask_observed_at"])
                    if raw.get("bid_observed_at") and raw.get("ask_observed_at") else None)
        session = None  # Quote has no trustworthy session discriminator.
    identity = _instrument_id(label)
    prev = _finite(raw.get("prev_close"))
    reference = None
    if prev is not None:
        reference = {
            "instrument_id": identity, "value": prev, "source": "dxlink", "source_symbol": sym,
            "price_basis": "previous_close", "observed_at": None,
            "observation_time_reason": "summary_has_no_observation_timestamp",
            "observation_date": raw.get("prev_close_date"), "observation_timezone": None,
            # Day ID retains its provider date, but Summary supplies no zone.
            "observation_granularity": None,
            "session": None, "adjustment_basis": "unknown", "resolved_contract": None,
        }
    return {
        "instrument_id": identity, "price": price, "bid": bid, "ask": ask,
        "prev_close": prev, "volume": raw.get("volume"),
        "source": "dxlink" if price is not None else None,
        "source_symbol": sym if price is not None else None, "price_basis": basis,
        "quote_ts": receipt, "collector_received_at": receipt,
        "observed_at": observed if price is not None else None,
        "observation_time_reason": None if observed and price is not None else "provider_event_time_unavailable",
        "observation_date": None, "observation_timezone": "UTC" if observed else None,
        "observation_granularity": "event" if observed else None, "session": session,
        "adjustment_basis": "unknown", "resolved_contract": None,
        "acquired_at": receipt, "last_success_at": receipt,
        "last_attempt_at": receipt, "refresh_error": None, "reference": reference,
    }


def _change(row):
    reference = row.get("reference")
    # Never combine a fallback's value with another identity/source's Summary.
    if (reference and reference.get("instrument_id") == row.get("instrument_id")
            and reference.get("source_symbol") == row.get("source_symbol")
            and reference.get("source") == row.get("origin_source", row.get("source"))):
        prev = _finite(reference.get("value"))
    else:
        prev = None
    # The compact strip has never shown a change for the daily-fallback tiles.
    # Keep that display contract unchanged: the dated pair travels in
    # `reference`, where the overview consumer reports it with its real basis.
    if row.get("origin_source", row.get("source")) != "dxlink":
        row["prev_close"] = None
        row["chg_pct"] = None
        return
    row["prev_close"] = prev
    row["chg_pct"] = round((row["price"] - prev) / prev * 100, 2) if row.get("price") is not None and prev is not None and prev > 0 else None


def push_prices(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    state, fh = feed.get_state(), feed.get_health()
    ts_utc = datetime.now(timezone.utc)
    last_event = fh["last_feed_event_time"]
    feed_stale = last_event is None or (ts_utc - last_event).total_seconds() > STALE_FEED_SECS
    prices = {label: _dx_row(label, sym, state.get(sym, {})) for label, sym in PRICE_TICKERS.items()}
    missing = [label for label, row in prices.items() if row["price"] is None]
    yf_data = _entry.fetch_yf_prices_bounded() if missing else {}
    for label in missing:
        row = prices[label]
        attempt = yf_data.get(label)
        # Explicit map gate excludes JPY spot and any unapproved fallback.
        if (label in YF_SYMBOL_MAP and isinstance(attempt, dict)
                and _positive(attempt.get("price")) is not None
                and attempt.get("instrument_id") == row["instrument_id"]
                and attempt.get("source_symbol") == YF_SYMBOL_MAP[label]):
            prices[label] = copy.deepcopy(attempt)
            if label == "QQQ":
                QQQ_SESSION.last_spot[:] = [attempt["price"], None]
            continue
        remembered = _last_prices.get(label)
        if remembered is not None:
            # Entire pair, including original acquisition and provider times.
            carried = {**row, **copy.deepcopy(remembered)}
            carried.update(source="last-known", origin_source=remembered.get("origin_source", remembered.get("source")), stale=True)
            # Older restart seeds have only a value: keep unknown provenance
            # unknown, and never inherit a partial current DX Summary reference.
            if "instrument_id" not in remembered:
                carried.update(reference=None, source_symbol=remembered.get("source_symbol"),
                               collector_received_at=remembered.get("quote_ts"),
                               acquired_at=None, last_success_at=None)
            prices[label] = carried
        prices[label].update(
            last_attempt_at=attempt.get("last_attempt_at", ts_utc.isoformat()) if isinstance(attempt, dict) else ts_utc.isoformat(),
            refresh_error=attempt.get("refresh_error") or "observation_unavailable" if isinstance(attempt, dict) else "observation_unavailable",
        )
    # SPY retains its receipt-based price selector, with event provenance added.
    raw_spy = state.get("SPY", {}) if COLLECT_SPY else {}
    prices["SPY"] = _dx_row("SPY", "SPY", raw_spy, _observed_spy_price_with_basis(raw_spy))
    for label, row in prices.items():
        _change(row)
        if row["price"] is not None and label != "SPY":
            # SPY is not a legacy macro CSV column or last-known tile.
            _last_prices[label] = copy.deepcopy(row)
    if quote_registry is not None:
        quote_registry.update_ticker_fallbacks(prices, ts_utc.isoformat())
    payload = json.dumps({
        "schema_version": 2, "universe_version": UNIVERSE_VERSION, "timestamp": ts_utc.isoformat(),
        "snapshot_time": ts_utc.astimezone(ET).strftime("%H:%M ET"),
        "feed_stale": feed_stale, "prices": prices,
    }, default=str, allow_nan=False)
    try:
        s3.put_object(Bucket=R2_BUCKET, Key="intraday/prices.json", Body=payload.encode(),
                      ContentType="application/json", CacheControl="no-cache, max-age=0")
        counters.inc_prices(ts_utc.isoformat())
    except Exception:
        counters.inc_failure()
        raise


def prices_loop(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    while not past_stop():
        try:
            push_prices(s3, feed, counters, quote_registry)
        except Exception as e:
            log.error(f"prices.json error: {e}")
        time.sleep(PRICES_SECS)
    log.info("prices loop stopped")


__all__ = [
    "make_s3", "_classify_startup", "_log_ticker_health", "YF_FETCH_TIMEOUT_SECS",
    "YF_COOLDOWN_SECS", "YF_MIN_INTERVAL_SECS", "_yf_cooldown_until", "_yf_cache",
    "fetch_yf_prices", "fetch_yf_prices_bounded", "push_prices", "prices_loop",
]
