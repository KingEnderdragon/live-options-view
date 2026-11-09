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

    def test_zero_reference_retained_without_fabricating_percentage(self):
        row = self.publish({"AAPL": {"last": 101, "last_ts": NEW, "prev_close": 0}})["AAPL"]
        self.assertEqual(row["reference"]["value"], 0)
        self.assertEqual(row["prev_close"], 0)
        self.assertIsNone(row["chg_pct"])

    def test_enabled_request_set_retains_original_budget(self):
        dx, yahoo = producer_maps()
        self.assertEqual(len(dx), 17)  # SPY separate
        self.assertEqual(len(yahoo), 16)  # no yen spot
        self.assertEqual(dx, p.PRICE_TICKERS)
        self.assertEqual(yahoo, p.YF_SYMBOL_MAP)
        self.assertEqual(dx["JPY/USD"], "/6J:XCME")


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
        self.assertEqual(len(calls), 16)
        self.assertEqual([sym for sym, _ in calls], list(p.YF_SYMBOL_MAP.values()))
        self.assertEqual(set(result), set(p.YF_SYMBOL_MAP))
        self.assertEqual(factory.call_args.args, (" ".join(p.YF_SYMBOL_MAP.values()),))
        self.assertTrue(all(kwargs == {"period": "5d", "interval": "1d", "auto_adjust": False} for _, kwargs in calls))

    def test_daily_dates_timezone_holiday_reference_and_unadjusted_request(self):
        bars = pd.DataFrame({"Close": [100, 101]},
                            index=pd.DatetimeIndex(["2026-11-25", "2026-11-27"], tz="America/New_York"))
        calls = []
        ticker = SimpleNamespace(history=lambda **kw: calls.append(kw) or bars)
        with patch.object(p, "YF_SYMBOL_MAP", {"AAPL": "AAPL"}), patch.object(p.yf, "Tickers", return_value=SimpleNamespace(tickers={"AAPL": ticker})):
            row = p.fetch_yf_prices()["AAPL"]
        self.assertEqual(calls, [{"period": "5d", "interval": "1d", "auto_adjust": False}])
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
            cache["ts"] = datetime.now(UTC) - timedelta(minutes=2)
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


if __name__ == "__main__":
    unittest.main()
