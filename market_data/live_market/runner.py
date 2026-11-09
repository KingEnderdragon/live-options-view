"""Session runner: one market session end to end, plus the entry point.

WHY this module now: _run_session (263 lines) is the second-largest unit
in the collector and the orchestrator -- it wires auth, the feed, the
snapshot/price/health loops and the weekly loop into one session. It moved
out early (with DXLinkFeed) so the biggest units landed first; its callees
now live in their final homes in the package (see the responsibility map).
main() moves with it: it is the Railway entry point and its test patch
points (start_live_quote_server, make_s3, wait_for_premarket, _run_session)
resolve through the package entry, as they did in the flat file.
"""

import secrets
import threading
import time
from datetime import date, datetime, timezone

import market_data.live_market as _entry  # entry indirection for main()'s test patch points

from .config import (
    COLLECT_SPY,
    ET,
    HEALTH_SECS,
    PRICES_SECS,
    PRICE_TICKERS,
    SNAPSHOT_SECS,
    STOP_HOUR,
    STOP_MIN,
    WEEKLY_SNAPSHOT_SECS,
    log,
    past_stop,
)
from .feed import DXLinkFeed
from .oauth import OAuthTokenManager, TastyAuthError, tasty_auth
from .tiers import classify_tier
from .chain import load_chain
from .steo import eia_steo_loop
from .health import health_loop, push_health
from .market import take_snapshot
from .prices import (
    _classify_startup,
    _log_ticker_health,
    make_s3,
    prices_loop,
)
from .quotes import (
    LiveQuoteRegistry,
    _fresh_spy_spot,
    _observed_spy_price,
    _select_spy_strikes,
)
from .state import (
    Counters,
    QQQ_SESSION,
    SnapshotTracker,
    UnderlyingSession,
    UNDERLYING_SESSIONS,
    finalize_rvol_baseline,
    load_rvol_baseline,
    restore_state,
)
from .sessions import weekly_snapshot_loop

def _run_session(token_manager: OAuthTokenManager, quote_registry: LiveQuoteRegistry):
    run_id        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    process_start = datetime.now(timezone.utc)
    log.info(f"session start  run_id={run_id}")

    s3             = make_s3()
    classification = _classify_startup(s3, process_start)
    log.info(f"startup classification: {classification}")

    today   = datetime.now(ET).date()
    restore_state(s3, today, QQQ_SESSION)
    QQQ_SESSION.rvol_baseline = load_rvol_baseline(s3, QQQ_SESSION)
    QQQ_SESSION.rvol_today = {}

    auth    = tasty_auth(token_manager)
    tier    = classify_tier(today)
    log.info(f"session date={today}  tier={tier}")

    strikes, exp_date, weekly_strikes, weekly_exp_date = load_chain(
        auth["access_token"], today
    )

    # Extra 0DTE chains (#121). Each is optional: a failure here disables
    # only that underlying for this session and never touches QQQ.
    extra_chains: list[tuple[UnderlyingSession, list[dict], str]] = []
    pending_spy: tuple[UnderlyingSession, list[dict], str] | None = None
    spy_context = {"status": "disabled" if not COLLECT_SPY else "unavailable",
                   "reason": "COLLECT_SPY=0" if not COLLECT_SPY else "chain_not_loaded"}
    for sym, us in UNDERLYING_SESSIONS.items():
        if us is QQQ_SESSION or (sym == "SPY" and not COLLECT_SPY):
            continue
        try:
            us_strikes, us_exp, _, _ = load_chain(auth["access_token"], today, ticker=sym, include_weekly=False)
        except Exception as e:
            log.error(f"{sym} chain load failed ({e}) -- {sym} collection disabled this session")
            if sym == "SPY":
                spy_context.update(status="missing_today_expiration" if "SPY 0DTE unavailable" in str(e) else "chain_load_failed",
                                   reason=str(e))
            continue
        restore_state(s3, today, us)
        us.rvol_baseline = load_rvol_baseline(s3, us)
        us.rvol_today = {}
        if sym == "SPY":
            us.subscription_coverage = None
            pending_spy = (us, us_strikes, us_exp)
            spy_context.update(status="awaiting_spot", reason="waiting_for_fresh_spy_spot")
        else:
            extra_chains.append((us, us_strikes, us_exp))

    option_syms = []
    contracts = {}
    for s in strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])
        if s["call_occ"]:
            contracts[s["call_occ"].replace(" ", "")] = {
                "streamer_symbol": s["call_sym"], "strike": s["strike"],
                "type": "call", "exp": exp_date,
            }
        if s["put_occ"]:
            contracts[s["put_occ"].replace(" ", "")] = {
                "streamer_symbol": s["put_sym"], "strike": s["strike"],
                "type": "put", "exp": exp_date,
            }

    # Weekly contracts share the same DXLink connection but are intentionally
    # not added to the live quote registry/UI contract map.
    for s in weekly_strikes:
        option_syms.append(s["call_sym"])
        option_syms.append(s["put_sym"])
    for us, us_strikes, us_exp in extra_chains:
        for s in us_strikes:
            for side in ("call", "put"):
                option_syms.append(s[f"{side}_sym"])
                if s[f"{side}_occ"]:
                    contracts[s[f"{side}_occ"].replace(" ", "")] = {
                        "streamer_symbol": s[f"{side}_sym"], "strike": s["strike"],
                        "type": side, "exp": us_exp,
                    }
    option_syms = list(dict.fromkeys(option_syms))

    # Extra underlyings need their own spot/volume stream, but are not
    # added to PRICE_TICKERS (the price strip and its health counts).
    price_syms = list(dict.fromkeys(
        list(PRICE_TICKERS.values()) + [us.symbol for us, _, _ in extra_chains]
        + (["SPY"] if COLLECT_SPY else [])
    ))
    log.info(f"subscribing to {len(option_syms)} option symbols + {len(price_syms)} price tickers")
    for label, sym in PRICE_TICKERS.items():
        log.info(f"  price ticker  {label:<10} -> {sym}")

    feed = DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions(option_syms, price_syms)
    quote_registry.set_session(feed, contracts)
    feed.start()

    if not feed.wait_ready(timeout=30):
        log.warning("DXLink channel not open after 30s -- proceeding anyway")

    log.info("waiting for first option data event (up to 15s)...")
    if feed.wait_first_data(timeout=15):
        log.info("option data flowing -- proceeding to snapshot")
    else:
        log.warning("no feed data within 15s -- proceeding anyway")

    if pending_spy:
        us, us_strikes, us_exp = pending_spy
        deadline = time.monotonic() + 15
        spot, spot_ts = None, None
        while time.monotonic() < deadline:
            spot, spot_ts = _fresh_spy_spot(feed.get_state().get("SPY", {}), datetime.now(timezone.utc))
            if spot is not None:
                break
            time.sleep(0.5)
        if spot is None:
            observed, observed_ts = _observed_spy_price(feed.get_state().get("SPY", {}))
            spy_context.update(status="stale_startup_spot" if observed_ts else "spot_unavailable",
                               reason="fresh_spy_spot_not_observed_within_15s")
            log.warning("SPY options unavailable: no fresh timed startup spot within 15s")
        else:
            selected = _select_spy_strikes(us_strikes, spot)
            if not selected:
                spy_context.update(status="empty_chain", reason="no_valid_spy_strikes")
                log.warning("SPY options unavailable: no valid strikes in today's chain")
            else:
                coverage = {
                    "policy": "fixed_startup_nearest_67_strikes",
                    "fixed_at_startup": True,
                    "reference_spot": spot,
                    "reference_spot_observed_at": spot_ts,
                    "total_chain_strikes": len(us_strikes),
                    "selected_strikes": len(selected),
                    "selected_option_symbols": 2 * len(selected),
                    "min_strike": selected[0]["strike"],
                    "max_strike": selected[-1]["strike"],
                }
                us.subscription_coverage = coverage
                spy_context["subscription_coverage"] = coverage
                extra_chains.append((us, selected, us_exp))
                for row in selected:
                    for side in ("call", "put"):
                        occ = row[f"{side}_occ"]
                        if occ:
                            contracts[occ.replace(" ", "")] = {
                                "streamer_symbol": row[f"{side}_sym"],
                                "strike": row["strike"], "type": side, "exp": us_exp,
                            }
                quote_registry.set_session(feed, contracts)
                symbols = list(dict.fromkeys(row[f"{side}_sym"] for row in selected for side in ("call", "put")))
                added = feed.add_option_subscriptions(symbols)
                pending = feed.get_subscription_delivery()["pending_event_pairs"]
                spy_context.update(status="subscription_pending" if pending else "collecting",
                                   reason="subscription_delivery_incomplete" if pending else None)
                log.info(f"SPY fixed startup window: {len(us_strikes)} chain strikes, "
                         f"{len(selected)} selected strikes, {len(symbols)} option symbols, "
                         f"{added} added event/symbol pairs; spot={spot} at {spot_ts}")

    _log_ticker_health(feed)

    counters = Counters()
    tracker  = SnapshotTracker()
    # Separate counters/trackers so extra underlyings can never mask a missed
    # QQQ snapshot in health.json (which stays QQQ-only).
    extra_trackers = {us.symbol: (Counters(), SnapshotTracker()) for us, _, _ in extra_chains}
    if "SPY" in extra_trackers:
        spy_context["counters"], spy_context["tracker"] = extra_trackers["SPY"]

    prices_thread = threading.Thread(
        target=prices_loop, args=(s3, feed, counters, quote_registry), daemon=True)
    prices_thread.start()
    log.info(f"prices thread started (every {PRICES_SECS}s)")

    health_thread = threading.Thread(
        target=health_loop,
        args=(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context),
        daemon=True,
    )
    health_thread.start()
    log.info(f"health thread started (every {HEALTH_SECS}s)")

    if weekly_strikes:
        weekly_thread = threading.Thread(
            target=weekly_snapshot_loop,
            args=(s3, feed, weekly_strikes, weekly_exp_date, today),
            daemon=True,
        )
        weekly_thread.start()
        log.info(
            f"weekly snapshot thread started (expiration {weekly_exp_date}, "
            f"every {WEEKLY_SNAPSHOT_SECS // 60}m during 09:30-16:00 ET)"
        )

    log.info(f"snapshot loop started (every {SNAPSHOT_SECS}s, stop {STOP_HOUR:02d}:{STOP_MIN:02d} ET)")

    reauth_attempted = False
    while not past_stop():
        if feed.needs_reauth():
            if reauth_attempted:
                feed.stop()
                quote_registry.clear_session()
                raise TastyAuthError(
                    "streamer-reauth-circuit",
                    detail="one-reauth-chain-already-used-this-session",
                )
            reauth_attempted = True
            log.warning("DXLink auth failed 3+ times -- one streamer reauth attempt permitted")
            try:
                new_auth = tasty_auth(token_manager)
                feed.update_token(new_auth["streamer_token"])
                log.info("streamer token refreshed")
            except TastyAuthError:
                feed.stop()
                quote_registry.clear_session()
                raise
        feed.restart_if_dead()

        if spy_context.get("status") == "subscription_pending":
            feed.retry_pending_option_subscriptions()
            if not feed.get_subscription_delivery()["pending_event_pairs"]:
                spy_context.update(status="collecting", reason=None)

        tracker.check_missed()
        try:
            take_snapshot(s3, feed, strikes, exp_date, tier, today, counters, tracker)
        except Exception as e:
            log.error(f"snapshot error: {e}")
        for us, us_strikes, us_exp in extra_chains:
            if us.symbol == "SPY" and spy_context.get("status") != "collecting":
                continue
            us_counters, us_tracker = extra_trackers[us.symbol]
            us_tracker.check_missed()
            try:
                take_snapshot(s3, feed, us_strikes, us_exp, tier, today, us_counters, us_tracker, us)
            except Exception as e:
                log.error(f"{us.symbol} snapshot error: {e}")
        time.sleep(SNAPSHOT_SECS)

    trk = tracker.get()
    ctr = counters.get()
    log.info(
        f"session complete  run_id={run_id}  "
        f"snapshots={trk['snapshot_sequence']}  "
        f"missed={trk['missed_snapshot_count']}  "
        f"failures={ctr['failures']}"
    )

    # Write final health.json with past_stop=True so next startup classifies as clean_start
    try:
        push_health(s3, feed, counters, tracker, run_id, process_start, classification, today, spy_context)
    except Exception:
        pass

    # Fold today's RVOL bucket readings into the cross-day baseline. Done here
    # (once, at confirmed session end) rather than incrementally, so a
    # mid-session crash can never leave the baseline holding a partial day.
    finalize_rvol_baseline(s3, today, QQQ_SESSION)
    for us, _, _ in extra_chains:
        finalize_rvol_baseline(s3, today, us)

    feed.stop()
    quote_registry.clear_session()


def main():
    try:
        token_manager = OAuthTokenManager.from_env()
    except TastyAuthError as exc:
        log.critical(f"{exc}; collector remains fail-closed")
        return

    quote_registry = LiveQuoteRegistry()
    _entry.start_live_quote_server(quote_registry)

    # Independent of the QQQ market session (unlike prices_loop/health_loop,
    # started fresh inside every _run_session): EIA STEO has nothing to do
    # with market hours, so this runs continuously off its own s3 client for
    # the life of the process. No-ops immediately if EIA_API_KEY is unset.
    steo_thread = threading.Thread(target=eia_steo_loop, args=(_entry.make_s3(),), daemon=True)
    steo_thread.start()

    blocked_session_date: date | None = None
    while True:
        _entry.wait_for_premarket(blocked_session_date)
        try:
            _entry._run_session(token_manager, quote_registry)
        except TastyAuthError as exc:
            quote_registry.clear_session()
            blocked_session_date = datetime.now(ET).date()
            log.critical(
                f"{exc}; auth circuit open for exchange session {blocked_session_date}"
            )
        except Exception as e:
            quote_registry.clear_session()
            blocked_session_date = datetime.now(ET).date()
            log.error(
                f"session failed: {e}; startup circuit open for exchange session "
                f"{blocked_session_date}",
                exc_info=True,
            )

__all__ = ["_run_session", "main"]
