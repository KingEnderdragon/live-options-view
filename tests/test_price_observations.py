"""Offline producer contract tests: no credentials, sockets or provider calls."""
import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_data.live_market as entry
from market_data.live_market import prices as p
from market_data.live_market.feed import DXLinkFeed
from market_data import overview
from market_data.overview import INSTRUMENTS, producer_maps

UTC = timezone.utc
OLD = "2026-10-09T19:00:00+00:00"
NEW = "2026-10-12T14:00:00+00:00"


class Sink:
    def put_object(self, **kwargs):
        self.payload = json.loads(kwargs["Body"])


class Feed:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return self.state

    def get_health(self):
        return {"last_feed_event_time": datetime.now(UTC)}


def observation(label="AAPL", price=101, date="2026-10-09", **kw):
    identity = next(row["id"] for row in INSTRUMENTS if row["label"] == label)
    reference = {"instrument_id": identity, "value": 100, "source": "yfinance",
                 "source_symbol": p.YF_SYMBOL_MAP[label], "price_basis": "daily_bar_close",
                 "observed_at": None, "observation_date": "2026-10-08",
                 "observation_timezone": "America/New_York", "observation_granularity": "day",
                 "session": "daily", "adjustment_basis": "unadjusted", "resolved_contract": None}
    return dict(instrument_id=identity, price=price, bid=None, ask=None, volume=None,
                source="yfinance", source_symbol=p.YF_SYMBOL_MAP[label], price_basis="daily_bar_close",
                observed_at=None, observation_date=date, observation_timezone="America/New_York",
                observation_granularity="day", session="daily", adjustment_basis="unadjusted",
                resolved_contract=None, quote_ts=None, collector_received_at=None,
                acquired_at=OLD, last_success_at=OLD, last_attempt_at=NEW, refresh_error=None,
                reference=reference, **kw)


class ProducerObservations(unittest.TestCase):
    def setUp(self):
        self.old_last = copy.deepcopy(p._last_prices)
        p._last_prices.clear()

    def tearDown(self):
        p._last_prices.clear()
        p._last_prices.update(self.old_last)

    def publish(self, state, fallback=None):
        sink = Sink()
        with patch.object(entry, "fetch_yf_prices_bounded", return_value=fallback or {}):
            p.push_prices(sink, Feed(state), entry.Counters())
        self.assertEqual(sink.payload["schema_version"], 2)
        return sink.payload["prices"]

    def test_selected_trade_keeps_delayed_event_not_newer_quote(self):
        rows = self.publish({"AAPL": {"last": 102, "last_ts": NEW, "last_observed_at": OLD,
                                      "last_session": "extended", "bid": 105, "ask": 106,
                                      "bid_ts": NEW, "ask_ts": NEW,
                                      "bid_observed_at": NEW, "ask_observed_at": NEW}})
        aapl = rows["AAPL"]
        self.assertEqual((aapl["price"], aapl["observed_at"], aapl["session"]), (102, OLD, "extended"))
        self.assertEqual((aapl["quote_ts"], aapl["collector_received_at"], aapl["acquired_at"]), (NEW, NEW, NEW))

    def test_midpoint_requires_both_event_times_and_uses_older(self):
        raw = {"bid": 100, "ask": 102, "bid_ts": OLD, "ask_ts": NEW,
               "bid_observed_at": OLD, "ask_observed_at": NEW}
        row = self.publish({"AAPL": raw})["AAPL"]
        self.assertEqual((row["price"], row["quote_ts"], row["observed_at"]), (101, OLD, OLD))
        del raw["bid_observed_at"]
        self.assertIsNone(self.publish({"AAPL": raw})["AAPL"]["observed_at"])

    def test_spy_selected_midpoint_not_last_trade(self):
        raw = {"bid": 100, "ask": 102, "bid_ts": OLD, "ask_ts": NEW,
               "bid_observed_at": OLD, "ask_observed_at": NEW,
               "last": 110, "last_ts": NEW, "last_observed_at": NEW}
        row = self.publish({"SPY": raw})["SPY"]
        self.assertEqual((row["price_basis"], row["price"], row["observed_at"]), ("bid_ask_midpoint", 101, OLD))
        self.assertNotIn("SPY", p._last_prices)  # no macro CSV contract expansion

    def test_future_event_is_preserved_not_replaced_by_receipt(self):
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        row = self.publish({"AAPL": {"last": 101, "last_ts": NEW, "last_observed_at": future}})["AAPL"]
        self.assertEqual(row["observed_at"], future)
        self.assertEqual(row["quote_ts"], NEW)

    def test_unknown_provider_time_stays_unknown(self):
        row = self.publish({"AAPL": {"last": 101, "last_ts": NEW}})["AAPL"]
        self.assertIsNone(row["observed_at"])
        self.assertIsNone(row["observation_granularity"])
        self.assertEqual(row["collector_received_at"], NEW)

    def test_old_daily_fetched_now_stays_daily_with_pair_and_failed_attempt(self):
        daily = observation()
        first = self.publish({}, {"AAPL": daily})["AAPL"]
        self.assertEqual((first["observation_date"], first["observed_at"], first["quote_ts"]), ("2026-10-09", None, None))
        failure = {"AAPL": {"price": None, "last_attempt_at": NEW, "refresh_error": "timeout"}}
        carried = self.publish({}, failure)["AAPL"]
        for key in ("observation_date", "observation_timezone", "acquired_at", "last_success_at", "reference"):
            self.assertEqual(carried[key], first[key])
        self.assertEqual((carried["source"], carried["origin_source"], carried["last_attempt_at"], carried["refresh_error"]),
                         ("last-known", "yfinance", NEW, "timeout"))
        recovered = self.publish({}, {"AAPL": observation(price=103, date="2026-10-12")})["AAPL"]
        self.assertEqual(recovered["source"], "yfinance")
        self.assertEqual(recovered["observation_date"], "2026-10-12")
        self.assertIsNone(recovered["refresh_error"])
        self.assertNotIn("stale", recovered)

    def test_yen_spot_and_wrong_identity_fallback_rejected(self):
        self.assertNotIn("JPY/USD", p.YF_SYMBOL_MAP)
        rows = self.publish({}, {"JPY/USD": {"price": .0067, "instrument_id": "fx.usdjpy", "source_symbol": "JPYUSD=X"},
                                 "AAPL": {**observation(), "instrument_id": "MSFT"}})
        self.assertIsNone(rows["JPY/USD"]["price"])
        self.assertIsNone(rows["AAPL"]["price"])
        row = observation()
        row["reference"]["instrument_id"] = "MSFT"
        p._change(row)
        self.assertIsNone(row["chg_pct"])
        self.assertIsNone(row["prev_close"])

    def test_daily_fallback_keeps_strip_display_parity(self):
        # The compact strip showed no change for daily tiles before this work;
        # the dated pair is carried in reference for the overview consumer.
        row = self.publish({}, {"AAPL": observation(price=101)})["AAPL"]
        self.assertEqual(row["reference"]["value"], 100)
        self.assertEqual(row["reference"]["price_basis"], "daily_bar_close")
        self.assertIsNone(row["prev_close"])
        self.assertIsNone(row["chg_pct"])
        dx = self.publish({"AAPL": {"last": 102, "last_ts": NEW, "prev_close": 100}})["AAPL"]
        self.assertEqual((dx["prev_close"], dx["chg_pct"]), (100, 2.0))

    def test_zero_reference_retained_without_fabricating_percentage(self):
        row = self.publish({"AAPL": {"last": 101, "last_ts": NEW, "prev_close": 0}})["AAPL"]
        self.assertEqual(row["reference"]["value"], 0)
        self.assertEqual(row["prev_close"], 0)
        self.assertIsNone(row["chg_pct"])

    def test_enabled_request_set_covers_authorized_universe(self):
        dx, yahoo = producer_maps()
        self.assertEqual(len(dx), 99)  # SPY separate
        self.assertEqual(len(yahoo), 91)  # no yen spot
        self.assertEqual(dx, p.PRICE_TICKERS)
        self.assertEqual(yahoo, p.YF_SYMBOL_MAP)
        self.assertEqual(dx["JPY/USD"], None)

    def test_daily_only_instruments_request_no_streaming_symbol(self):
        dx, yahoo = producer_maps()
        rows = {row["label"]: row for row in INSTRUMENTS}
        for label in overview.DAILY_ONLY_LABELS:
            self.assertIsNone(dx[label], f"{label} must not subscribe")
            self.assertIn(label, yahoo)
            self.assertEqual(rows[label]["provider_policy"], "daily_only")
            self.assertIsNone(rows[label]["dxlink_symbol"])
            self.assertIsNotNone(rows[label]["provider_policy_reason"])

    def test_daily_only_values_publish_dated_and_never_live(self):
        # The designated source is the dated Yahoo bar: it carries a date,
        # no provider event time and no receipt, and must not claim live.
        rows = self.publish({}, {
            "VIX": observation("VIX", price=15.5),
            "10Y": observation("10Y", price=4.69),
            "KOSPI": observation("KOSPI", price=2600.0),
        })
        for label, value in (("VIX", 15.5), ("10Y", 4.69), ("KOSPI", 2600.0)):
            row = rows[label]
            self.assertEqual(row["price"], value)
            self.assertEqual(row["source"], "yfinance")
            self.assertEqual(row["observation_date"], "2026-10-09")
            self.assertIsNone(row["observed_at"])
            self.assertIsNone(row["quote_ts"])
            self.assertIsNone(row["collector_received_at"])
            self.assertNotIn("stale", row)

    def test_yield_scale_contract_makes_a_source_switch_continuous(self):
        # The registry declares each source's raw scale, so the consumer can
        # normalize without a magnitude heuristic: Cboe TNX arrives x10 over
        # DXLink and already in percent from Yahoo. Any future live mapping
        # therefore cannot step 4.69 -> 46.9.
        row = next(row for row in INSTRUMENTS if row["label"] == "10Y")
        self.assertEqual(row["scale"], {"dxlink": 10, "yfinance": 1})
        self.assertAlmostEqual(46.9 / row["scale"]["dxlink"], 4.69 / row["scale"]["yfinance"])


class DailyOnlyRegistry(unittest.TestCase):
    def test_registry_rejects_a_daily_only_instrument_with_a_stream(self):
        cfg = copy.deepcopy(overview.load_universe())
        next(row for row in cfg["instruments"] if row["label"] == "VIX")["dxlink_symbol"] = "$VIX.X"
        with self.assertRaises(ValueError):
            overview._validate(cfg)

    def test_registry_rejects_a_mislabelled_streaming_instrument(self):
        cfg = copy.deepcopy(overview.load_universe())
        next(row for row in cfg["instruments"] if row["label"] == "KOSPI")["provider_policy"] = "streaming"
        with self.assertRaises(ValueError):
            overview._validate(cfg)


class RegistryGate(unittest.TestCase):
    def test_activation_preserves_exact_universe_and_compact_strip(self):
        cfg = overview.load_universe()
        self.assertEqual(overview.ACTIVATION_ALLOWLIST,
                         {r["label"] for r in cfg["instruments"] if r["enabled"]})
        widened = copy.deepcopy(cfg)
        next(r for r in widened["instruments"] if r["label"] == "MSFT")["enabled"] = False
        with self.assertRaises(ValueError):
            overview._validate(widened)
        missing = copy.deepcopy(cfg)
        missing["instruments"] = missing["instruments"][:-1]
        with self.assertRaises(ValueError):
            overview._validate(missing)
        spot_yen = copy.deepcopy(cfg)
        next(r for r in spot_yen["instruments"] if r["label"] == "JPY/USD")["yahoo_symbol"] = "JPYUSD=X"
        with self.assertRaises(ValueError):
            overview._validate(spot_yen)


class FeedObservations(unittest.TestCase):
    def test_provider_time_receipt_and_summary_date_separate(self):
        feed = DXLinkFeed("unused", "unused")
        millis = int(datetime.fromisoformat(OLD).timestamp() * 1000)
        feed._ingest([{"eventType": "TradeETH", "eventSymbol": "AAPL", "price": 101, "time": millis},
                      {"eventType": "Quote", "eventSymbol": "AAPL", "bidPrice": 100, "askPrice": 102,
                       "bidTime": millis, "askTime": millis + 1000},
                      {"eventType": "Summary", "eventSymbol": "AAPL", "prevDayClosePrice": 100,
                       "prevDayId": (datetime(2026, 10, 8) - datetime(1970, 1, 1)).days}])
        raw = feed.get_state()["AAPL"]
        self.assertEqual(raw["last_observed_at"], OLD)
        self.assertNotEqual(raw["last_ts"], OLD)
        self.assertEqual(raw["last_session"], "extended")
        self.assertEqual(raw["prev_close_date"], "2026-10-08")
        # New event with unknown provider time must not inherit prior time.
        feed._ingest([{"eventType": "Trade", "eventSymbol": "AAPL", "price": 103}])
        self.assertIsNone(feed.get_state()["AAPL"]["last_observed_at"])

    def test_invalid_event_times_and_nonfinite_values_not_invented(self):
        for val in (None, "bad", 0, float("nan"), float("inf")):
            self.assertIsNone(DXLinkFeed._event_time(val))
        self.assertIsNone(DXLinkFeed._to_float(float("inf")))
        self.assertIsNone(DXLinkFeed._day_id(float("inf")))

    def test_setup_requests_event_fields_without_more_subscriptions(self):
        feed = DXLinkFeed("unused", "unused")
        sent = []
        feed._send = sent.append
        feed._on_message(None, json.dumps({"type": "CHANNEL_OPENED"}))
        fields = sent[0]["acceptEventFields"]
        self.assertIn("time", fields["Trade"])
        self.assertIn("bidTime", fields["Quote"])
        self.assertIn("prevDayId", fields["Summary"])
        self.assertEqual(feed._subs, [])


class YahooObservations(unittest.TestCase):
    def test_one_serial_history_call_per_approved_symbol(self):
        bars = pd.DataFrame({"Close": [100, 101]},
                            index=pd.DatetimeIndex(["2026-10-08", "2026-10-09"], tz="America/New_York"))
        calls = []
        tickers = {}
        for symbol in p.YF_SYMBOL_MAP.values():
            def history(symbol=symbol, **kwargs):
                calls.append((symbol, kwargs))
                return bars
            tickers[symbol] = SimpleNamespace(history=history)
        with patch.object(p.yf, "Tickers", return_value=SimpleNamespace(tickers=tickers)) as factory:
            result = p.fetch_yf_prices()
        self.assertEqual(len(calls), len(p.YF_SYMBOL_MAP))
        self.assertEqual({sym for sym, _ in calls}, set(p.YF_SYMBOL_MAP.values()))
        self.assertEqual(set(result), set(p.YF_SYMBOL_MAP))
        self.assertEqual(factory.call_args.args, (" ".join(p.YF_SYMBOL_MAP.values()),))
        self.assertTrue(all(kwargs == {"period": "5d", "interval": "1d", "auto_adjust": False, "timeout": 10} for _, kwargs in calls))

    def test_daily_dates_timezone_holiday_reference_and_unadjusted_request(self):
        bars = pd.DataFrame({"Close": [100, 101]},
                            index=pd.DatetimeIndex(["2026-11-25", "2026-11-27"], tz="America/New_York"))
        calls = []
        ticker = SimpleNamespace(history=lambda **kw: calls.append(kw) or bars)
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL"}), patch.object(p.yf, "Tickers", return_value=SimpleNamespace(tickers={"AAPL": ticker})):
            row = p.fetch_yf_prices()["AAPL"]
        self.assertEqual(calls, [{"period": "5d", "interval": "1d", "auto_adjust": False, "timeout": 10}])
        self.assertEqual(row["observation_date"], "2026-11-27")
        self.assertEqual(row["reference"]["observation_date"], "2026-11-25")
        self.assertEqual(row["observation_timezone"], "America/New_York")
        self.assertEqual(row["reference"]["instrument_id"], row["instrument_id"])
        self.assertEqual(row["adjustment_basis"], "unadjusted")
        self.assertIsNone(row["observed_at"])
        self.assertIsNone(row["collector_received_at"])

    def test_naive_dates_rejected_instead_of_assuming_timezone(self):
        bars = pd.DataFrame({"Close": [100]}, index=pd.DatetimeIndex(["2026-10-09"]))
        ticker = SimpleNamespace(history=lambda **kw: bars)
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL"}), patch.object(p.yf, "Tickers", return_value=SimpleNamespace(tickers={"AAPL": ticker})):
            row = p.fetch_yf_prices()["AAPL"]
        self.assertIsNone(row["price"])
        self.assertEqual(row["refresh_error"], "daily_observation_timezone_unknown")

    def test_cache_failure_cooldown_deep_copy_and_recovery(self):
        cache = {"ts": None, "data": None}
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL"}), patch.object(p, "_yf_cache", cache), patch.object(p, "_yf_cooldown_until", [None]), patch.object(p, "_yf_inflight", None), patch.object(p, "fetch_yf_prices") as fetch:
            fetch.return_value = {"AAPL": observation()}
            first = p.fetch_yf_prices_bounded()["AAPL"]
            first["reference"]["value"] = 999
            self.assertEqual(p.fetch_yf_prices_bounded()["AAPL"]["reference"]["value"], 100)
            self.assertEqual(fetch.call_count, 1)
            cache["ts"] = datetime.now(UTC) - timedelta(minutes=6)
            fetch.return_value = {"AAPL": p._failure("timeout", NEW)}
            failed = p.fetch_yf_prices_bounded()["AAPL"]
            self.assertEqual((failed["observation_date"], failed["acquired_at"], failed["refresh_error"]), ("2026-10-09", OLD, "timeout"))
            self.assertEqual(p.fetch_yf_prices_bounded()["AAPL"], failed)
            self.assertEqual(fetch.call_count, 2)
            cache["ts"] = datetime.now(UTC) - timedelta(minutes=10)
            p._yf_cooldown_until[0] = None
            fetch.return_value = {"AAPL": observation(price=105, date="2026-10-12")}
            recovered = p.fetch_yf_prices_bounded()["AAPL"]
            self.assertEqual(recovered["price"], 105)
            self.assertIsNone(recovered["refresh_error"])

    def test_timeout_uses_timedelta_and_does_not_erase_cached_pair(self):
        class HungThread:
            def __init__(self, **kw): pass
            def start(self): pass
            def join(self, **kw): pass
            def is_alive(self): return True
        cache = {"ts": None, "data": {"AAPL": observation()}}
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL"}), patch.object(p, "_yf_cache", cache), patch.object(p, "_yf_cooldown_until", [None]), patch.object(p, "_yf_inflight", None), patch.object(p.threading, "Thread", HungThread):
            row = p.fetch_yf_prices_bounded()["AAPL"]
            self.assertEqual(row["price"], 101)
            self.assertEqual(row["reference"]["value"], 100)
            self.assertEqual(row["refresh_error"], "daily_observation_timeout")
            self.assertIsNotNone(p._yf_cooldown_until[0])
            cache["ts"] = None
            p._yf_cooldown_until[0] = None
            row = p.fetch_yf_prices_bounded()["AAPL"]
            self.assertEqual(row["refresh_error"], "daily_observation_fetch_still_running")
            self.assertEqual(row["acquired_at"], OLD)

    def test_partial_failure_retains_only_failed_symbol_and_records_attempt(self):
        cache = {"ts": None, "data": {"AAPL": observation()}}
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL", "QQQ": "QQQ"}), patch.object(p, "_yf_cache", cache), patch.object(p, "_yf_cooldown_until", [None]), patch.object(p, "_yf_inflight", None), patch.object(p, "fetch_yf_prices", return_value={"AAPL": p._failure("unavailable", NEW), "QQQ": observation("QQQ", price=500)}):
            result = p.fetch_yf_prices_bounded()
            self.assertEqual(result["AAPL"]["price"], 101)
            self.assertEqual(result["AAPL"]["last_attempt_at"], NEW)
            self.assertEqual(result["AAPL"]["refresh_error"], "unavailable")
            self.assertEqual(result["QQQ"]["price"], 500)
            self.assertIsNone(result["QQQ"]["refresh_error"])


class ExtendedSessionSelection(unittest.TestCase):
    """#178 repair: an active extended session is not last session's close.

    The live November 9 artifact carried the 15:59:59 ET closing print for all
    71 streaming equity rows across 2.7 hours of after-hours while two-sided
    quotes advanced. These fixtures pin the selection rules that replace it
    without ever promoting a receipt time into observation time.
    """

    def setUp(self):
        self.old_last = copy.deepcopy(p._last_prices)
        p._last_prices.clear()
        self.now = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)       # 16:30 ET
        self.afterhours_start = "2026-11-09T21:00:00+00:00"       # 16:00 ET
        self.premarket_start = "2026-11-09T09:00:00+00:00"        # 04:00 ET
        self.regular_start = "2026-11-09T14:30:00+00:00"          # 09:30 ET

    def tearDown(self):
        p._last_prices.clear()
        p._last_prices.update(self.old_last)

    def publish_lane(self, state, sessions, starts, now=None):
        sink = Sink()
        context = {"writer_id": "test-writer", "summary": {"mode": "equities"},
                   "sessions": sessions, "session_starts": starts}
        with patch.object(entry, "fetch_yf_prices_bounded", return_value={}):
            p.push_prices(sink, Feed(state), entry.Counters(), lane_context=context,
                          now_utc=now or self.now)
        return sink.payload["prices"]

    def extended_state(self, last="2026-11-09T20:59:59.916000+00:00", **quote):
        raw = {"last": 678.78, "last_ts": "2026-11-09T20:59:59.916000+00:00",
               "last_observed_at": last, "last_session": "regular",
               "bid": 679.58, "ask": 679.61,
               "bid_ts": "2026-11-09T21:29:59+00:00", "ask_ts": "2026-11-09T21:29:59.500000+00:00",
               "prev_close": 675.0, "prev_close_date": "2026-11-06"}
        raw.update(quote)
        return raw

    def test_afterhours_stale_close_is_replaced_by_a_qualified_quote(self):
        row = self.publish_lane({"QQQ": self.extended_state()},
                                {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(row["price_basis"], "bid_ask_midpoint")
        self.assertAlmostEqual(row["price"], 679.595)
        self.assertIsNone(row["observed_at"], "a quote without provider time is not observed now")
        self.assertEqual(row["observation_time_reason"], "provider_quote_time_unavailable")
        self.assertEqual(row["quote_ts"], "2026-11-09T21:29:59.500000+00:00")
        self.assertIsNone(row["session"], "a Quote has no trustworthy session discriminator")
        self.assertEqual(row["market_session"], "afterhours")

    def test_extended_quote_provider_times_make_the_row_live(self):
        row = self.publish_lane({"QQQ": self.extended_state(
            bid_observed_at="2026-11-09T21:29:58+00:00",
            ask_observed_at="2026-11-09T21:29:59+00:00")},
            {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(row["price_basis"], "bid_ask_midpoint")
        self.assertEqual(row["observed_at"], "2026-11-09T21:29:58+00:00",
                         "the older side wins; never the receipt clock")
        self.assertIsNone(row["observation_time_reason"])

    def test_premarket_trade_inside_the_session_still_wins(self):
        row = self.publish_lane({"QQQ": self.extended_state(
            last="2026-11-09T13:59:00+00:00")},
            {"QQQ": "premarket"}, {"QQQ": self.premarket_start})["QQQ"]
        self.assertEqual(row["price_basis"], "last_trade")
        self.assertEqual(row["price"], 678.78)
        self.assertEqual(row["observed_at"], "2026-11-09T13:59:00+00:00")
        self.assertEqual(row["session"], "regular")

    def test_wide_afterhours_quote_keeps_the_honest_stale_close(self):
        # IGV's live 13% after-hours spread is a placeholder, not a price.
        row = self.publish_lane({"QQQ": self.extended_state(bid=97.0, ask=110.55)},
                                {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(row["price_basis"], "last_trade")
        self.assertEqual(row["price"], 678.78)
        self.assertEqual(row["observed_at"], "2026-11-09T20:59:59.916000+00:00")

    def test_half_updated_quote_receipts_are_rejected(self):
        row = self.publish_lane({"QQQ": self.extended_state(
            ask_ts="2026-11-09T21:00:00+00:00")},  # 30 minutes old on one side
            {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(row["price_basis"], "last_trade")
        self.assertEqual(row["price"], 678.78)

    def test_regular_session_keeps_the_last_trade(self):
        row = self.publish_lane({"QQQ": self.extended_state(
            last="2026-11-09T21:29:50+00:00")},
            {"QQQ": "regular"}, {"QQQ": self.regular_start})["QQQ"]
        self.assertEqual(row["price_basis"], "last_trade")
        self.assertEqual(row["observed_at"], "2026-11-09T21:29:50+00:00")

    def test_reference_rolls_with_the_provider_day_id(self):
        first = self.publish_lane({"QQQ": self.extended_state()},
                                  {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(first["reference"]["observation_date"], "2026-11-06")
        rolled = self.publish_lane({"QQQ": self.extended_state(
            prev_close=679.0, prev_close_date="2026-11-09")},
            {"QQQ": "afterhours"}, {"QQQ": self.afterhours_start})["QQQ"]
        self.assertEqual(rolled["reference"]["observation_date"], "2026-11-09")
        self.assertEqual(rolled["reference"]["value"], 679.0)
        self.assertAlmostEqual(rolled["chg_pct"], round((rolled["price"] - 679.0) / 679.0 * 100, 2))


class FeedEventEvidence(unittest.TestCase):
    def test_provider_event_and_quote_time_counts_are_observable(self):
        feed = DXLinkFeed("unused", "unused")
        millis = int(datetime.fromisoformat(OLD).timestamp() * 1000)
        feed._ingest([
            {"eventType": "Quote", "eventSymbol": "QQQ", "bidPrice": 100, "askPrice": 101},
            {"eventType": "TradeETH", "eventSymbol": "QQQ", "price": 100.5, "time": millis},
            {"eventType": "Quote", "eventSymbol": "QQQ", "bidPrice": 100, "askPrice": 101,
             "bidTime": millis, "askTime": millis},
        ])
        health = feed.get_health()
        self.assertEqual(health["event_counts"]["Quote"], 2)
        self.assertEqual(health["event_counts"]["TradeETH"], 1)
        self.assertEqual(health["quote_time_counts"],
                         {"with_provider_time": 1, "without_provider_time": 1})


if __name__ == "__main__":
    unittest.main()
