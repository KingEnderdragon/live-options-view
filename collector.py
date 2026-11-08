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
import collector as _entry  # entry indirection for test patch points

# -- R2 client ----------------------------------------------------------------

def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# -- startup classification ---------------------------------------------------

def _classify_startup(s3, process_start: datetime) -> str:
    """Read prior health.json to classify this startup."""
    try:
        resp  = s3.get_object(Bucket=R2_BUCKET, Key="intraday/health.json")
        prior = json.loads(resp["Body"].read())
    except Exception:
        return "clean_start"

    if prior.get("collector", {}).get("past_stop", False):
        return "clean_start"

    prior_updated = prior.get("updated_at")
    if prior_updated:
        try:
            prior_dt  = datetime.fromisoformat(prior_updated.replace("Z", "+00:00"))
            gap_mins  = (process_start - prior_dt).total_seconds() / 60
            return "recovery_after_crash" if gap_mins < 120 else "recovery_after_gap"
        except Exception:
            pass

    return "unknown"


# -- ticker health diagnostics ------------------------------------------------

def _log_ticker_health(feed: DXLinkFeed):
    state = feed.get_state()
    dead  = []
    log.info("-- price ticker health check ------------------------------------")
    for label, dxlink_sym in PRICE_TICKERS.items():
        d     = state.get(dxlink_sym, {})
        price = d.get("last") or (
            round((d["bid"] + d["ask"]) / 2, 4)
            if d.get("bid") is not None and d.get("ask") is not None else None
        )
        if price is not None:
            log.info(f"  OK    {label:<10} ({dxlink_sym})  price={price}")
        else:
            log.warning(f"  WARN  {label:<10} ({dxlink_sym})  NO DATA -- symbol may be wrong")
            dead.append(label)
    if dead:
        log.warning(f"  {len(dead)} ticker(s) with no data: {', '.join(dead)}")
    else:
        log.info("  all price tickers returning data")
    log.info("-----------------------------------------------------------------")


# -- prices.json upload (every PRICES_SECS) -----------------------------------

# yfinance can degrade to ~10s per symbol when Yahoo's API is down, turning one
# fill into a multi-minute stall of the prices loop. Bound each fill with a
# timeout and back off for a cooldown after a failure. Successful results are
# cached so Yahoo is hit at most once per YF_MIN_INTERVAL_SECS even though the
# prices loop runs faster.
YF_FETCH_TIMEOUT_SECS   = 20
YF_COOLDOWN_SECS        = 300
YF_MIN_INTERVAL_SECS    = 60
_yf_cooldown_until: list = [None]   # [datetime|None]
_yf_cache: dict = {"ts": None, "data": None}


def fetch_yf_prices() -> dict[str, Optional[float]]:
    """Fetch current prices from Yahoo Finance. Supports pre/post-market."""
    result: dict[str, Optional[float]] = {k: None for k in YF_SYMBOL_MAP}
    try:
        tickers = yf.Tickers(" ".join(YF_SYMBOL_MAP.values()))
        for label, sym in YF_SYMBOL_MAP.items():
            try:
                fi = tickers.tickers[sym].fast_info
                price = None
                for attr in ("pre_market_price", "last_price", "post_market_price"):
                    val = getattr(fi, attr, None)
                    if val is not None and float(val) > 0:
                        price = float(val)
                        break
                result[label] = price
            except Exception:
                pass
    except Exception as e:
        log.warning(f"yfinance fetch failed: {e}")
    return result


def fetch_yf_prices_bounded() -> dict[str, Optional[float]]:
    """fetch_yf_prices with a result cache, hard deadline, and outage cooldown.

    Returns the cached result if fetched within YF_MIN_INTERVAL_SECS. Returns
    all-None immediately while in cooldown, or if the fetch exceeds
    YF_FETCH_TIMEOUT_SECS / returns no data (both open a new cooldown).
    """
    empty: dict[str, Optional[float]] = {k: None for k in YF_SYMBOL_MAP}
    now = datetime.now(timezone.utc)
    if (_yf_cache["ts"] is not None and
            (now - _yf_cache["ts"]).total_seconds() < YF_MIN_INTERVAL_SECS):
        return _yf_cache["data"]
    until = _yf_cooldown_until[0]
    if until is not None and now < until:
        return empty
    box: list = [None]

    def _worker():
        try:
            box[0] = fetch_yf_prices()
        except Exception as e:
            log.warning(f"yfinance fetch failed: {e}")

    # daemon thread (not ThreadPoolExecutor) so a hung Yahoo call can't block
    # interpreter shutdown; a stranded thread just dies with the process
    t = threading.Thread(target=_worker, name="yf-fetch", daemon=True)
    t.start()
    t.join(timeout=YF_FETCH_TIMEOUT_SECS)
    if t.is_alive():
        _yf_cooldown_until[0] = now + timedelta(seconds=YF_COOLDOWN_SECS)
        log.warning(f"yfinance timed out after {YF_FETCH_TIMEOUT_SECS}s -- "
                    f"cooling down for {YF_COOLDOWN_SECS}s")
        return empty
    result = box[0]
    if result is None or all(v is None for v in result.values()):
        _yf_cooldown_until[0] = now + timedelta(seconds=YF_COOLDOWN_SECS)
        log.warning(f"yfinance returned no data -- cooling down for {YF_COOLDOWN_SECS}s")
    else:
        _yf_cooldown_until[0] = None
        _yf_cache["ts"] = now
        _yf_cache["data"] = result
    return result if result is not None else empty


def push_prices(s3, feed: DXLinkFeed, counters: Counters,
                quote_registry: Optional[LiveQuoteRegistry] = None):
    state      = feed.get_state()
    fh         = feed.get_health()
    ts_et      = datetime.now(ET)
    ts_utc     = datetime.now(timezone.utc)

    last_event = fh["last_feed_event_time"]
    feed_stale = (last_event is None or
                  (ts_utc - last_event).total_seconds() > STALE_FEED_SECS)
    if feed_stale:
        log.warning(f"prices.json -- feed stale (last event: {last_event})")

    prices = {}
    for label, dxlink_sym in PRICE_TICKERS.items():
        d       = state.get(dxlink_sym, {})
        bid     = d.get("bid")
        ask     = d.get("ask")
        last    = d.get("last")
        mid     = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
        price   = last or mid
        # quote_ts is a collector receipt time, and must describe the value
        # selected for price. A newer bid cannot refresh an older last trade;
        # both sides of a midpoint must have receipt times.
        price_basis = "last_trade" if last else "bid_ask_midpoint" if mid is not None else None
        price_ts = (d.get("last_ts") if last else
                    min(d["bid_ts"], d["ask_ts"])
                    if mid is not None and d.get("bid_ts") and d.get("ask_ts") else None)
        prev    = d.get("prev_close")
        chg_pct = None
        if price and prev and prev != 0:
            chg_pct = round((price - prev) / prev * 100, 2)
        prices[label] = {
            "price":      price,
            "bid":        bid,
            "ask":        ask,
            "prev_close": prev,
            "chg_pct":    chg_pct,
            "volume":     d.get("volume"),
            "source":     "dxlink" if price is not None else None,
            "source_symbol": dxlink_sym if price is not None else None,
            "price_basis": price_basis,
            "quote_ts":   price_ts,
        }

    # yfinance fallback for any tickers DXLink didn't populate
    yf_missing = [lbl for lbl, d in prices.items() if d["price"] is None]
    if yf_missing:
        yf_data = _entry.fetch_yf_prices_bounded()
        filled = []
        for lbl in yf_missing:
            yf_price = yf_data.get(lbl)
            if yf_price is not None:
                prices[lbl]["price"] = yf_price
                prices[lbl]["source"] = "yfinance"
                prices[lbl]["source_symbol"] = YF_SYMBOL_MAP[lbl]
                prices[lbl]["price_basis"] = "undated_last_price"
                # Any Summary or one-sided quote still in d belongs to the
                # DXLink instrument, which may differ from the Yahoo fallback.
                for field in ("bid", "ask", "prev_close", "chg_pct", "volume"):
                    prices[lbl][field] = None
                # yfinance's simple last-price response has no trustworthy
                # provider event timestamp. Retrieval time must not masquerade
                # as market observation time.
                prices[lbl]["quote_ts"] = None
                filled.append(f"{lbl}={yf_price}")
        if filled:
            log.info(f"prices -- yfinance filled: {', '.join(filled)}")
        qqq_yf = yf_data.get("QQQ")
        if qqq_yf is not None:
            QQQ_SESSION.last_spot[0] = qqq_yf
            # Same reasoning as quote_ts above: yfinance has no trustworthy
            # provider event timestamp, so last_spot[1] must not carry over
            # whatever (older, DXLink-sourced) timestamp was there before --
            # take_snapshot's freshness check needs spot_ts=None here to
            # correctly report this fallback as not "live".
            QQQ_SESSION.last_spot[1] = None

    for lbl, d in prices.items():
        if d["price"] is not None:
            _last_prices[lbl] = {
                "price": d["price"],
                "quote_ts": d.get("quote_ts"),
                "origin_source": d.get("source"),
                "source_symbol": d.get("source_symbol"),
                "price_basis": d.get("price_basis"),
            }

    # last-known-value fallback for anything still missing
    stale_filled = []
    for lbl, d in prices.items():
        remembered = _last_prices.get(lbl)
        if d["price"] is None and remembered is not None:
            d["price"] = remembered["price"]
            d["stale"] = True
            d["source"] = "last-known"
            d["quote_ts"] = remembered.get("quote_ts")
            d["origin_source"] = remembered.get("origin_source")
            d["source_symbol"] = remembered.get("source_symbol")
            d["price_basis"] = remembered.get("price_basis")
            for field in ("bid", "ask", "prev_close", "chg_pct", "volume"):
                d[field] = None
            stale_filled.append(lbl)
    if stale_filled:
        log.warning(f"prices -- serving last-known values for: {', '.join(stale_filled)}")

    # SPY is an additional read-only tile sourced solely from its own DXLink
    # quote. Keep it outside the legacy macro strip and its yfinance/last-known
    # fallbacks so QQQ's CSV columns and health symbol counts do not change.
    spy = state.get("SPY", {}) if COLLECT_SPY else {}
    spy_bid, spy_ask = spy.get("bid"), spy.get("ask")
    spy_price, spy_ts, spy_basis = _observed_spy_price_with_basis(spy)
    spy_prev = spy.get("prev_close")
    prices["SPY"] = {
        "price": spy_price, "bid": spy_bid, "ask": spy_ask,
        "prev_close": spy_prev,
        "chg_pct": round((spy_price - spy_prev) / spy_prev * 100, 2) if spy_price is not None and spy_prev else None,
        "volume": spy.get("volume"), "source": "dxlink" if spy_price is not None and spy_ts else None,
        "source_symbol": "SPY" if spy_price is not None and spy_ts else None,
        "price_basis": spy_basis,
        "quote_ts": spy_ts if spy_price is not None else None,
    }

    dead = [label for label, d in prices.items() if d["price"] is None]
    if dead:
        log.warning(f"prices.json -- no data for: {', '.join(dead)}")

    if quote_registry is not None:
        quote_registry.update_ticker_fallbacks(prices, ts_utc.isoformat())

    payload = json.dumps({
        "timestamp":     ts_utc.isoformat(),
        "snapshot_time": ts_et.strftime("%H:%M ET"),
        "feed_stale":    feed_stale,
        "prices":        prices,
    }, default=str)

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/prices.json",
            Body=payload.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        counters.inc_prices(ts_utc.isoformat())
    except Exception as e:
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


# -- eia_steo.json upload (every EIA_STEO_POLL_SECS, optional feature) -------


def fetch_eia_steo_rows(api_key: str) -> list[dict]:
    """Raw rows from EIA's v2 STEO API for the series crude_calibration.py
    tracks (see BRENT_SERIES_ID/WTI_SERIES_ID/BALANCE_SERIES_ID there),
    most-recent-period-first. Network I/O only -- parsing/normalization is
    crude_calibration.parse_steo_rows.

    Series IDs and the response shape assumed here (`response.data`, each row
    a flat dict with `period`/`seriesId`/`value`) should be verified against
    a live call before this is trusted -- see crude_calibration.py's module
    docstring.

    An earlier version passed `length=36` intending "36 periods of history
    per series" -- but EIA v2's `length` caps *total rows in the response*,
    and with 3 series requested together that capped the combined response
    at 36 rows total (~12 periods across 3 series), silently truncating
    history without any error. Flagged in review. Fixed two ways: request a
    `length` (`EIA_STEO_PAGE_LENGTH`) with real margin over what 3 series x
    36 periods needs, and verify the server's own `response.total` count
    against what was actually returned -- raising rather than silently
    returning a truncated page if EIA's per-request cap or a future
    additional series ever exceeds it. `total` is only checked when present;
    an unexpected response schema degrades to "can't verify," not a crash.
    """
    params = {
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[seriesId][]": [cc.BRENT_SERIES_ID, cc.WTI_SERIES_ID, cc.BALANCE_SERIES_ID],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": EIA_STEO_PAGE_LENGTH,
    }
    resp = requests.get(EIA_STEO_URL, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()["response"]
    data = payload["data"]
    total = payload.get("total")
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        # A missing/malformed `total` means we cannot verify the response
        # wasn't truncated -- fail closed rather than silently persisting
        # unverifiable partial data as a new vintage. Flagged in review: the
        # original `if total is not None:` guard skipped verification
        # entirely (and swallowed unparseable values into a no-op) whenever
        # EIA's response didn't carry a clean int `total`, which is exactly
        # the case this check exists to catch.
        raise RuntimeError(
            f"EIA STEO response missing or malformed 'total' field ({total!r}) -- cannot "
            f"verify the {len(data)} returned row(s) are complete, refusing to treat them "
            f"as a trustworthy vintage."
        )
    if total_int > len(data):
        raise RuntimeError(
            f"EIA STEO response truncated: server reports {total_int} total row(s) but "
            f"only {len(data)} were returned (length={EIA_STEO_PAGE_LENGTH}). Increase "
            f"EIA_STEO_PAGE_LENGTH or add real offset-based pagination rather than "
            f"silently using a partial history."
        )
    return data


def load_steo_vintage_log(s3) -> list[dict]:
    """Rolling log of past STEO releases this collector has itself observed
    -- EIA's live API serves the current published values, not a queryable
    history of what a *prior* release forecast for the same future month, so
    that vintage-over-vintage comparison has to be built up locally over
    time, one release per stored entry (same reasoning as RVOL's own
    baselines/ history, see market_signals.py).

    Raises on anything other than the log genuinely never having been
    written yet (`NoSuchKey` -- a fresh deployment/bucket). A transient R2
    read failure, an auth error, or corrupt JSON must NOT be silently
    treated as "no history exists": `push_eia_steo` appends to whatever
    this returns and overwrites the R2 object with the result, so treating
    a *read* failure as an empty log would destroy every real prior
    vintage on the next write. Flagged in review; let the caller's own
    exception handling (`eia_steo_loop`'s per-cycle try/except) skip this
    poll and retry later instead.
    """
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return []
    return json.loads(body)


def save_steo_vintage_log(s3, entries: list[dict]) -> None:
    payload = json.dumps(entries[-EIA_STEO_MAX_VINTAGES:]).encode()
    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY,
        Body=payload, ContentType="application/json",
    )


def push_eia_steo(s3) -> None:
    rows = _entry.fetch_eia_steo_rows(EIA_API_KEY)
    # EIA's v2 API doesn't hand back "which monthly release this row came
    # from" directly, so the calendar month we happen to be polling in is
    # used as the vintage *label* -- but whether to mint a new vintage entry
    # at all is decided by whether the fetched data actually changed from
    # the most recently logged entry, not by the calendar alone. Labeling by
    # calendar month unconditionally (the original approach) creates a
    # phantom release right at a boundary: a poll in the first few days of a
    # month, before that month's STEO has actually been published, would log
    # a new entry under the new month's label containing last month's
    # unchanged data -- and anything reading macro/eia_steo.json in that
    # window (or a vintage-over-vintage comparison run against it) would see
    # a fabricated "this month's release" that was never actually published.
    # Flagged in review. Content-equality against the last logged entry is
    # the real signal for "did a new release land"; the calendar month is
    # only the label attached once that's true.
    release_period = _entry.datetime.now(timezone.utc).strftime("%Y-%m")
    current = cc.parse_steo_rows(rows, release_period=release_period)
    current_points_payload = [
        {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
        for p in current.points
    ]

    entries = load_steo_vintage_log(s3)  # raises on a real read failure -- see that function's docstring
    if entries and entries[-1].get("points") == current_points_payload:
        # Byte-identical to the most recent logged vintage: EIA hasn't
        # actually published a new STEO since we last saw one, even if the
        # calendar month has rolled over. Reuse the existing label instead
        # of minting a phantom one, and skip the R2 write entirely.
        release_period = entries[-1]["release_period"]
        # Flagged in review: `current` was built above with the pre-
        # canonicalization calendar guess, so leaving it as-is would let
        # compare_vintages() below stamp each SteoRevision's current_release
        # with that stale guess while the top-level payload's current_release
        # uses the canonicalized label -- the two would disagree. Rebuild
        # `current` with the final label before it's used for anything else.
        current = cc.SteoVintage(release_period=release_period, points=current.points)
    else:
        entries = [e for e in entries if e.get("release_period") != release_period]
        entries.append({"release_period": release_period, "points": current_points_payload})
        save_steo_vintage_log(s3, entries)

    prior_vintage = None
    if len(entries) >= 2:
        prior_entry = entries[-2]
        prior_vintage = cc.SteoVintage(
            release_period=prior_entry["release_period"],
            points=tuple(cc.SteoPricePoint(**p) for p in prior_entry["points"]),
        )

    revisions = []
    if prior_vintage is not None:
        for point in current.points:
            revision = cc.compare_vintages(prior_vintage, current, point.period)
            if revision is not None:
                revisions.append(revision.__dict__)

    ovx_value = _last_prices.get("OVX", {}).get("price")

    payload = json.dumps({
        "timestamp":       _entry.datetime.now(timezone.utc).isoformat(),
        "current_release": release_period,
        "prior_release":   prior_vintage.release_period if prior_vintage else None,
        "points":          [
            {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
            for p in current.points
        ],
        "revisions":       revisions,
        "ovx": {
            "value":  ovx_value,
            "regime": cc.classify_ovx_regime(ovx_value),
        },
    }, default=str).encode()

    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_KEY,
        Body=payload, ContentType="application/json",
        CacheControl="no-cache, max-age=300",
    )


def eia_steo_loop(s3) -> None:
    if not EIA_API_KEY:
        log.info("EIA_API_KEY not set -- skipping macro/eia_steo.json (crude calibration) feed")
        return
    while True:
        try:
            push_eia_steo(s3)
        except Exception as e:
            log.warning(f"eia_steo.json error: {e}")
        time.sleep(EIA_STEO_POLL_SECS)


# -- health.json upload (every 15s) ------------------------------------------

def push_health(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date,
                spy_context: Optional[dict] = None):
    fh   = feed.get_health()
    ctr  = counters.get()
    trk  = tracker.get()
    now  = datetime.now(timezone.utc)

    last_event = fh["last_feed_event_time"]
    feed_stale = (last_event is None or (now - last_event).total_seconds() > STALE_FEED_SECS)

    state     = feed.get_state()
    no_data   = [label for label, sym in PRICE_TICKERS.items()
                 if state.get(sym, {}).get("last") is None and state.get(sym, {}).get("bid") is None]
    with_data = len(PRICE_TICKERS) - len(no_data)

    spy_context = spy_context or {"status": "unavailable", "reason": "no_spy_session"}
    spy_state = state.get("SPY", {}) if COLLECT_SPY else {}
    spy_price, spy_ts = _observed_spy_price(spy_state)
    spy_age = None
    if spy_ts:
        try:
            spy_age = (now - datetime.fromisoformat(spy_ts)).total_seconds()
        except ValueError:
            pass
    spy_spot_status = ("unavailable" if spy_price is None or spy_age is None else
                       "live" if 0 <= spy_age <= STALE_FEED_SECS else "stale")
    spy_counters = spy_context.get("counters")
    spy_tracker = spy_context.get("tracker")
    spy_health = {
        "collection_status": spy_context.get("status"),
        "reason": spy_context.get("reason"),
        "spot": {"price": spy_price, "observed_at": spy_ts, "status": spy_spot_status},
        "subscription_coverage": _spy_subscription_coverage(
            spy_context.get("subscription_coverage"), _fresh_spy_spot(spy_state, now)[0]),
        "subscription_delivery": feed.get_subscription_delivery(),
        "last_snapshot_upload_time": spy_counters.get()["last_snapshot_time"] if spy_counters else None,
        "cadence": spy_tracker.get() if spy_tracker else None,
    }

    payload = json.dumps({
        "run_id":             run_id,
        "trade_date":         today.isoformat(),
        "process_start_time": process_start.isoformat(),
        "updated_at":         now.isoformat(),
        "classification":     classification,
        "collector": {
            "past_stop":      past_stop(),
            "loop_alive":     True,
            "last_loop_time": now.isoformat(),
        },
        "feed": {
            "connected":            fh["connected"],
            "authorized":           fh["authorized"],
            "channel_open":         fh["channel_open"],
            "reconnect_count":      fh["reconnect_count"],
            "last_feed_event_time": last_event.isoformat() if last_event else None,
            "feed_stale":           feed_stale,
            "last_error":           fh["last_error"],
            "last_close_code":      fh["last_close_code"],
        },
        "uploads": {
            "prices_success_count":      ctr["prices_ok"],
            "snapshot_success_count":    ctr["snapshot_ok"],
            "csv_success_count":         ctr["csv_ok"],
            "failure_count":             ctr["failures"],
            "last_price_upload_time":    ctr["last_price_time"],
            "last_snapshot_upload_time": ctr["last_snapshot_time"],
        },
        "cadence": trk,
        "symbols": {
            "expected_price_symbols":  len(PRICE_TICKERS),
            "price_symbols_with_data": with_data,
            "no_data_symbols":         no_data,
        },
        "spy": spy_health,
    }, default=str)

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key="intraday/health.json",
            Body=payload.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.error(f"health.json upload failed: {e}")
        counters.inc_failure()


def health_loop(s3, feed: DXLinkFeed, counters: Counters, tracker: SnapshotTracker,
                run_id: str, process_start: datetime, classification: str, today: date,
                spy_context: Optional[dict] = None):
    while not past_stop():
        try:
            push_health(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context)
        except Exception as e:
            log.error(f"health loop error: {e}")
        time.sleep(HEALTH_SECS)
    log.info("health loop stopped")


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

