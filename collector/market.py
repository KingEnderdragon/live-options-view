"""Market compute and the minute snapshot.

WHY this module: take_snapshot is the collector's output -- one minute,
one underlying, one CSV row set plus the live latest.json. Around it sit
the per-underlying market computation (VWAP/RVOL/momentum via
collector.market_signals) and the spot resolver. It reads the session
state, the quote helpers, and the session clock, and nothing else.
"""

import io
import json
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from . import market_signals as ms
from .config import (
    ET,
    MOMENTUM_LOOKBACK_MINUTES,
    MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
    MOMENTUM_NEUTRAL_BAND_PCT,
    MOMENTUM_RETAIN_MINUTES,
    R2_BUCKET,
    RVOL_BUCKET_MINUTES,
    RVOL_LOOKBACK_DAYS,
    RVOL_MIN_DAYS_REQUIRED,
    STALE_FEED_SECS,
    STRIKE_WINDOW,
    _session_bounds,
    log,
)
from .feed import DXLinkFeed
from .quotes import _fresh_spy_spot, _observed_spy_price, _spy_subscription_coverage
from .state import (
    Counters,
    QQQ_SESSION,
    SnapshotTracker,
    _last_prices,
    _log_momentum_reading,
    _persist_vwap_state,
)

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



__all__ = [
    "_compute_underlying_market",
    "_compute_and_log_momentum",
    "_resolve_underlying_spot",
    "take_snapshot",
]
