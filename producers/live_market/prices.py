"""Prices: the R2 client, the Yahoo fallback, and the prices.json loop.

WHY this module: prices.json is the macro price strip the site renders
every 10 seconds -- one concern with one output key. It owns the S3
client construction, the yfinance fallback (with its cache/cooldown, so
Yahoo is hit at most once a minute), and the loop that merges DXLink
quotes, the yfinance fallback, and the last-known strip into one payload.
Its test patch point (fetch_yf_prices_bounded, stubbed by stage2)
resolves through the package entry.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
import yfinance as yf

import producers.live_market as _entry  # entry indirection for push_prices' fetch_yf_prices_bounded patch point

from .config import (
    COLLECT_SPY,
    ET,
    PRICES_SECS,
    PRICE_TICKERS,
    R2_BUCKET,
    STALE_FEED_SECS,
    YF_SYMBOL_MAP,
    log,
    past_stop,
)
from .feed import DXLinkFeed
from .quotes import LiveQuoteRegistry, _observed_spy_price_with_basis
from .state import Counters, QQQ_SESSION, _last_prices

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



__all__ = [
    "make_s3",
    "_classify_startup",
    "_log_ticker_health",
    "YF_FETCH_TIMEOUT_SECS",
    "YF_COOLDOWN_SECS",
    "YF_MIN_INTERVAL_SECS",
    "_yf_cooldown_until",
    "_yf_cache",
    "fetch_yf_prices",
    "fetch_yf_prices_bounded",
    "push_prices",
    "prices_loop",
]
