"""Offline tests for the continuous price lane (#178).

No credentials, sockets, provider calls or live calendar downloads.
"""
import copy
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_data.live_market as entry
from market_data.live_market import price_history, price_lane, price_sessions, prices
from market_data.live_market.oauth import TastyAuthError
from market_data.overview import INSTRUMENTS

UTC = timezone.utc
ET = price_sessions.ET


class FakeS3:
    bucket = "test-bucket"

    def __init__(self):
        self.objects = {}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        body = self.objects[Key]

        class Body:
            def read(self_inner):
                return body

        return {"Body": Body()}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.puts.append((Key, kwargs))
        self.objects[Key] = Body

    def read(self, key):
        return json.loads(self.objects[key])


class FakeFeed:
    def __init__(self, state=None):
        self.state = state or {}
        self.option_symbols = None
        self.price_symbols = None
        self.started = False
        self.stopped = False
        self.reauth = False
        self.dead_restarts = 0

    def set_subscriptions(self, option_symbols, price_symbols):
        self.option_symbols = list(option_symbols)
        self.price_symbols = list(price_symbols)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait_ready(self, timeout=60.0):
        return True

    def restart_if_dead(self):
        self.dead_restarts += 1

    def needs_reauth(self):
        return self.reauth

    def get_state(self):
        return {k: dict(v) for k, v in self.state.items()}

    def get_health(self):
        return {"last_feed_event_time": datetime.now(UTC), "connected": True,
                "authorized": True, "channel_open": True}


class Sink:
    def __init__(self):
        self.payload = None
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.payload = json.loads(kwargs["Body"])


def dx_row(label, price, prev, observed="2026-11-09T14:30:00+00:00", session="regular"):
    return {"last": price, "last_ts": observed, "last_observed_at": observed,
            "last_session": session, "prev_close": prev,
            "prev_close_date": "2026-11-06", "bid": price - 0.1, "ask": price + 0.1,
            "bid_ts": observed, "ask_ts": observed,
            "bid_observed_at": observed, "ask_observed_at": observed}


def calendar(*days):
    return price_sessions.EquityCalendar(set(days))


class EquitySessions(unittest.TestCase):
    def test_premarket_regular_afterhours_and_close_boundaries(self):
        cal = calendar(date(2026, 11, 10), date(2026, 11, 11))
        cases = [
            ((2026, 11, 10, 3, 59), "closed", None),
            ((2026, 11, 10, 4, 0), "premarket", "09:30"),
            ((2026, 11, 10, 9, 29), "premarket", "09:30"),
            ((2026, 11, 10, 9, 30), "regular", "16:00"),
            ((2026, 11, 10, 16, 0), "afterhours", "20:00"),
            ((2026, 11, 10, 19, 59), "afterhours", "20:00"),
            ((2026, 11, 10, 20, 0), "closed", "04:00"),
        ]
        for stamp, status, transition in cases:
            state = price_sessions.equity_session(ET.localize(datetime(*stamp)), cal)
            self.assertEqual(state.status, status, stamp)
            if transition:
                self.assertEqual(state.next_transition_at.strftime("%H:%M"), transition, stamp)

    def test_holiday_and_weekend_are_distinct_and_not_eligible(self):
        cal = calendar(date(2026, 11, 9))
        holiday = price_sessions.equity_session(ET.localize(datetime(2026, 11, 26, 12)), cal)
        weekend = price_sessions.equity_session(ET.localize(datetime(2026, 11, 28, 12)), cal)
        self.assertEqual(holiday.status, "holiday")
        self.assertFalse(holiday.eligible)
        self.assertEqual(weekend.status, "closed")
        self.assertFalse(weekend.eligible)

    def test_verified_early_close_shortens_regular_session(self):
        cal = calendar(date(2026, 11, 27))
        before = price_sessions.equity_session(ET.localize(datetime(2026, 11, 27, 12, 59)), cal)
        after = price_sessions.equity_session(ET.localize(datetime(2026, 11, 27, 13, 0)), cal)
        self.assertEqual((before.status, before.early_close), ("regular", True))
        self.assertEqual((after.status, after.early_close), ("afterhours", True))

    def test_dst_keeps_wall_clock_windows(self):
        winter = price_sessions.equity_session(
            ET.localize(datetime(2026, 11, 9, 4, 0)), calendar(date(2026, 11, 9)))
        summer = price_sessions.equity_session(
            ET.localize(datetime(2027, 3, 15, 4, 0)), calendar(date(2027, 3, 15)))
        self.assertEqual(winter.status, "premarket")
        self.assertEqual(summer.status, "premarket")
        self.assertNotEqual(winter.next_transition_at.utcoffset(),
                            summer.next_transition_at.utcoffset())

    def test_missing_calendar_is_unknown_not_open(self):
        state = price_sessions.equity_session(ET.localize(datetime(2026, 11, 10, 10)), None)
        self.assertEqual(state.status, "unknown")
        self.assertFalse(state.eligible)

    def test_build_calendar_propagates_failure(self):
        def broken(start, end):
            raise RuntimeError("calendar offline")
        with self.assertRaises(price_sessions.ExchangeCalendarUnavailable):
            price_sessions.build_equity_calendar(ET.localize(datetime(2026, 11, 10, 10)), broken)


class FuturesSessions(unittest.TestCase):
    def test_sunday_open_friday_close_and_maintenance(self):
        cases = [
            ((2026, 11, 8, 17, 59), "closed"),
            ((2026, 11, 8, 18, 0), "overnight"),
            ((2026, 11, 9, 2, 0), "overnight"),
            ((2026, 11, 9, 10, 0), "regular"),
            ((2026, 11, 9, 17, 0), "maintenance"),
            ((2026, 11, 9, 17, 59), "maintenance"),
            ((2026, 11, 9, 18, 0), "overnight"),
            ((2026, 11, 13, 16, 59), "overnight"),
            ((2026, 11, 13, 17, 0), "closed"),
            ((2026, 11, 14, 12, 0), "closed"),
        ]
        for stamp, status in cases:
            self.assertEqual(price_sessions.futures_session(ET.localize(datetime(*stamp))).status,
                             status, stamp)

    def test_full_holiday_and_early_close(self):
        closed = price_sessions.futures_session(ET.localize(datetime(2026, 12, 25, 12)))
        early = price_sessions.futures_session(ET.localize(datetime(2026, 11, 27, 14)))
        self.assertEqual(closed.status, "holiday")
        self.assertEqual((early.status, early.early_close), ("closed", True))


class LaneMode(unittest.TestCase):
    def test_weekend_crypto_overnight_futures_and_equity_precedence(self):
        weekend = price_sessions.lane_summary(
            ET.localize(datetime(2026, 11, 14, 12)), calendar())
        overnight = price_sessions.lane_summary(
            ET.localize(datetime(2026, 11, 9, 2)), calendar(date(2026, 11, 9)))
        regular = price_sessions.lane_summary(
            ET.localize(datetime(2026, 11, 9, 10)), calendar(date(2026, 11, 9)))
        self.assertEqual(weekend["mode"], "crypto")
        self.assertTrue(weekend["groups_open"]["crypto"])
        self.assertEqual(overnight["mode"], "futures")
        self.assertEqual(regular["mode"], "equities")

    def test_calendar_failure_still_yields_a_consistent_summary(self):
        summary = price_sessions.lane_summary_payload(
            ET.localize(datetime(2026, 11, 14, 12)), None)
        self.assertEqual(summary["mode"], "crypto")
        self.assertTrue(summary["eligible"])
        self.assertEqual(summary["sessions"]["equities"]["status"], "unknown")

    def test_session_for_families(self):
        cal = calendar(date(2026, 11, 9))
        instruments = {row["label"]: row for row in INSTRUMENTS}
        self.assertEqual(price_sessions.session_for(instruments["BTC/USD"], ET.localize(
            datetime(2026, 11, 14, 12)), cal).status, "continuous")
        self.assertEqual(price_sessions.session_for(instruments["/CL"], ET.localize(
            datetime(2026, 11, 9, 2)), cal).status, "overnight")
        self.assertEqual(price_sessions.session_for(instruments["QQQ"], ET.localize(
            datetime(2026, 11, 9, 5)), cal).status, "premarket")


class RollingReferences(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
        self.store = price_history.RollingReferenceStore(clock=lambda: self.now)

    def test_reference_requires_a_sample_near_the_24_hour_mark(self):
        old = self.now - timedelta(hours=24)
        self.assertTrue(self.store.observe("BTC/USD", 100.0, old.isoformat()))
        ref = self.store.reference("BTC/USD", self.now, instrument_id="crypto.btcusd")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["value"], 100.0)
        self.assertEqual(ref["price_basis"], "rolling_24h")
        self.assertLessEqual(abs(ref["reference_age_seconds"] - 86400), 600)

    def test_partial_history_is_warmup_not_a_borrowed_baseline(self):
        self.store.observe("BTC/USD", 100.0, (self.now - timedelta(hours=2)).isoformat())
        self.assertIsNone(self.store.reference("BTC/USD", self.now))
        status = self.store.status(self.now)
        self.assertEqual(status["warming_up"], ["BTC/USD"])

    def test_tolerance_is_bounded_to_three_buckets(self):
        too_far = self.now - timedelta(hours=24) + timedelta(minutes=41)
        self.store.observe("BTC/USD", 100.0, too_far.isoformat())
        self.assertIsNone(self.store.reference("BTC/USD", self.now))
        old_ok = self.now - timedelta(hours=24) + timedelta(minutes=29)
        self.store.observe("ETH/USD", 10.0, old_ok.isoformat())
        self.assertIsNotNone(self.store.reference("ETH/USD", self.now))

    def test_invalid_and_future_samples_are_ignored(self):
        self.assertFalse(self.store.observe("BTC/USD", 0, self.now.isoformat()))
        self.assertFalse(self.store.observe("BTC/USD", float("nan"), self.now.isoformat()))
        self.assertFalse(self.store.observe("BTC/USD", 100,
                                            (self.now + timedelta(minutes=10)).isoformat()))

    def test_persist_and_restore_round_trip_is_bounded(self):
        old = self.now - timedelta(hours=24)
        self.store.observe("BTC/USD", 100.0, old.isoformat())
        sink = FakeS3()
        self.store._s3 = sink
        self.assertTrue(self.store.persist(force=True))
        payload = sink.read(price_history.KEY)
        self.assertEqual(payload["bucket_seconds"], 600)
        self.assertLessEqual(len(payload["instruments"]["BTC/USD"]["samples"]), 151)
        self.now = self.now + timedelta(minutes=5)
        restored = price_history.RollingReferenceStore(sink, clock=lambda: self.now)
        self.assertTrue(restored.load())
        self.assertIsNotNone(restored.reference("BTC/USD", self.now))

    def test_change_percent_never_invents_a_zero_move(self):
        self.assertIsNone(price_history.change_percent(100, None))
        self.assertIsNone(price_history.change_percent(100, {"value": 0}))
        self.assertEqual(price_history.change_percent(110, {"value": 100}), 10.0)


class LanePayload(unittest.TestCase):
    def setUp(self):
        self.old = copy.deepcopy(prices._last_prices)
        prices._last_prices.clear()

    def tearDown(self):
        prices._last_prices.clear()
        prices._last_prices.update(self.old)

    def publish(self, lane_context=None, rolling_store=None):
        sink = Sink()
        btc_symbol = prices.PRICE_TICKERS["BTC/USD"]
        state = {
            "QQQ": dx_row("QQQ", 656.2, 655.0),
            btc_symbol: dx_row("BTC/USD", 90000.0, 89500.0, session="extended"),
        }
        with patch.object(entry, "fetch_yf_prices_bounded", return_value={}):
            prices.push_prices(sink, FakeFeed(state), entry.Counters(),
                               lane_context=lane_context, rolling_store=rolling_store,
                               now_utc=datetime(2026, 11, 9, 15, 0, tzinfo=UTC))
        return sink.payload

    def test_legacy_payload_keeps_its_exact_fields(self):
        payload = self.publish()
        self.assertNotIn("price_lane", payload)
        self.assertNotIn("market_session", payload["prices"]["QQQ"])

    def test_lane_payload_adds_sessions_and_bounded_rolling_reference(self):
        now = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
        store = price_history.RollingReferenceStore(clock=lambda: now)
        store.observe("BTC/USD", 85000.0, (now - timedelta(hours=24)).isoformat())
        payload = self.publish({
            "writer_id": "test-writer",
            "summary": {"mode": "equities", "eligible": True},
            "sessions": {"QQQ": "premarket", "BTC/USD": "continuous", "/CL": "overnight"},
        }, store)
        self.assertEqual(payload["price_lane"]["writer_id"], "test-writer")
        self.assertEqual(payload["price_lane"]["mode"], "equities")
        self.assertEqual(payload["prices"]["QQQ"]["market_session"], "premarket")
        btc = payload["prices"]["BTC/USD"]
        self.assertEqual(btc["market_session"], "continuous")
        self.assertEqual(btc["rolling_reference"]["value"], 85000.0)
        self.assertEqual(btc["rolling_change_pct"], round((90000 - 85000) / 85000 * 100, 2))
        self.assertEqual(btc["change_basis"], "rolling_24h")
        self.assertEqual(payload["prices"]["/CL"]["change_basis"],
                         "contract_reference_unverified")

    def test_crypto_without_history_is_unavailable_not_zero(self):
        payload = self.publish({
            "writer_id": "test-writer", "summary": {"mode": "crypto"},
            "sessions": {"BTC/USD": "continuous"},
        }, None)
        btc = payload["prices"]["BTC/USD"]
        self.assertIsNone(btc["rolling_change_pct"])
        self.assertEqual(btc["change_basis"], "unavailable")


class LaneRuntime(unittest.TestCase):
    def setUp(self):
        self._futures = {row["label"]: entry.PRICE_TICKERS.get(row["label"])
                         for row in INSTRUMENTS if row["profile"] == "FU"}

    def tearDown(self):
        for label, symbol in self._futures.items():
            entry.PRICE_TICKERS[label] = symbol

    def make_lane(self, auth=None, feed=None, publish=None, clock=None,
                  feed_factory=None, resolve=None, **kwargs):
        self.feed = feed or FakeFeed()
        self.sink = FakeS3()
        self.published = []
        self.auth_calls = []
        self.feeds = []

        def fake_auth(_manager):
            self.auth_calls.append(1)
            if isinstance(auth, Exception):
                raise auth
            return {"access_token": "a", "streamer_token": "s", "streamer_url": "ws://test"}

        def fake_publish(s3, feed, counters, registry, **kw):
            self.published.append(kw)
            s3.put_object(Bucket="b", Key="intraday/prices.json", Body=b"{}")

        def make_feed(url, token):
            built = feed_factory(url, token) if feed_factory else self.feed
            self.feeds.append(built)
            return built

        def fake_resolve(token, force=False):
            if resolve is not None:
                return resolve(token, force=force)
            return {}

        lane = price_lane.PriceLane(
            object(), None, self.sink, writer_id="test-writer",
            clock=clock or (lambda: datetime(2026, 11, 9, 15, 0, tzinfo=UTC)),
            sleep=lambda secs: None, auth=fake_auth,
            feed_factory=make_feed,
            resolve=fake_resolve,
            publish=publish or fake_publish,
            calendar_builder=lambda now: calendar(now.date()),
            rolling_store=kwargs.pop("rolling_store", None), **kwargs)
        return lane

    def test_cycle_subscribes_to_prices_only_and_publishes_once(self):
        lane = self.make_lane()
        self.assertEqual(lane.cycle(), price_lane.PRICES_SECS if hasattr(price_lane, "PRICES_SECS") else 10)
        self.assertEqual(self.feed.option_symbols, [])
        self.assertIn("QQQ", self.feed.price_symbols)
        self.assertIn("SPY", self.feed.price_symbols)
        allowed = set(entry.PRICE_TICKERS.values()) | {"SPY"}
        self.assertTrue(set(self.feed.price_symbols).issubset(allowed),
                        "the lane subscribes only to price identities")
        self.assertEqual(len(self.published), 1)
        context = self.published[0]["lane_context"]
        self.assertEqual(context["writer_id"], "test-writer")
        self.assertIn("QQQ", context["sessions"])
        health = self.sink.read(entry.PRICE_LANE_HEALTH_KEY)
        self.assertEqual(health["state"], "streaming")
        self.assertEqual(health["option_symbols_subscribed"], 0)
        self.assertEqual(health["feed"]["connected"], True)

    def test_auth_failure_backs_off_and_respects_the_cycle_budget(self):
        frozen = [datetime(2026, 11, 9, 15, 0, tzinfo=UTC)]
        lane = self.make_lane(auth=TastyAuthError("oauth-token", detail="denied"),
                              clock=lambda: frozen[0], max_auth_cycles=4)
        delays = []
        for _ in range(6):
            delays.append(lane.cycle())
        self.assertEqual(len(self.auth_calls), 4, "auth attempts are capped per window")
        self.assertEqual(delays[:2], [60, 120])
        self.assertTrue(all(delay <= 1800 for delay in delays), "backoff is capped")
        self.assertEqual(lane.health()["state"], "backing_off")

    def test_feed_needing_reauth_is_replaced_with_a_bounded_budget(self):
        lane = self.make_lane()
        lane.cycle()
        old_feed = self.feed
        old_feed.reauth = True
        lane.cycle()
        self.assertTrue(old_feed.stopped)
        self.assertEqual(len(self.auth_calls), 2)

    def test_cycle_survives_calendar_failure_and_still_publishes(self):
        lane = self.make_lane()

        def broken(now):
            raise price_sessions.ExchangeCalendarUnavailable("offline")
        lane._calendar_builder = broken
        self.assertEqual(lane.cycle(), 10)
        context = self.published[0]["lane_context"]
        self.assertEqual(context["summary"]["mode"], "futures")
        self.assertEqual(context["sessions"].get("QQQ"), "unknown")

    def test_rolling_history_is_persisted_after_publish(self):
        now = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
        lane = self.make_lane(clock=lambda: now)
        store = price_history.RollingReferenceStore(self.sink, clock=lambda: now)
        store.observe("BTC/USD", 85000.0, (now - timedelta(hours=24)).isoformat())
        lane._rolling = store
        lane.cycle()
        payload = self.sink.read(price_history.KEY)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("BTC/USD", payload["instruments"])
        self.assertEqual(payload["instruments"]["BTC/USD"]["samples"][0][1], 85000.0)

    def test_cycle_uses_eastern_clock_for_session_labels(self):
        # 21:00Z is 16:00 ET in winter (EST) and would look closed if read as UTC.
        lane = self.make_lane(clock=lambda: datetime(2026, 11, 9, 21, 0, tzinfo=UTC))
        lane.cycle()
        context = self.published[0]["lane_context"]
        self.assertEqual(context["summary"]["mode"], "equities")
        self.assertEqual(context["sessions"]["QQQ"], "afterhours")

        # Same wall-clock instant in summer (EDT) must also be after-hours.
        summer = self.make_lane(clock=lambda: datetime(2027, 7, 15, 20, 0, tzinfo=UTC))
        summer.cycle()
        summer_context = self.published[0]["lane_context"]
        self.assertEqual(summer_context["summary"]["mode"], "equities")
        self.assertEqual(summer_context["sessions"]["QQQ"], "afterhours")

    def test_daily_roll_reconciles_the_lane_subscription(self):
        now = [datetime(2026, 11, 9, 15, 0, tzinfo=UTC)]
        contract = {"symbol": "/GCZ26:XCEC"}
        force_calls = []

        def resolve(_token, force=False):
            force_calls.append(force)
            entry.PRICE_TICKERS["/GC"] = contract["symbol"]
            return {}

        def feed_factory(_url, _token):
            return FakeFeed()

        lane = self.make_lane(clock=lambda: now[0], resolve=resolve, feed_factory=feed_factory)
        lane.cycle()
        self.assertIn("/GCZ26:XCEC", self.feeds[0].price_symbols)
        self.assertEqual(len(self.feeds), 1)

        # A new active month must replace the subscription even though the feed
        # stayed healthy and no authentication happened again.
        contract["symbol"] = "/GCG27:XCEC"
        now[0] += timedelta(seconds=price_lane.PRICE_LANE_CONTRACT_REFRESH_SECS + 1)
        lane.cycle()
        self.assertTrue(force_calls[-1], "the periodic reconciliation forces a resolution")
        self.assertTrue(self.feeds[0].stopped)
        self.assertEqual(len(self.feeds), 2)
        self.assertIn("/GCG27:XCEC", self.feeds[1].price_symbols)
        self.assertNotIn("/GCZ26:XCEC", self.feeds[1].price_symbols)
        self.assertEqual(lane.health()["contract_reconciliations"], 1)

    def test_concurrent_chain_resolution_reconciles_without_a_new_auth(self):
        now = [datetime(2026, 11, 9, 15, 0, tzinfo=UTC)]
        force_calls = []

        def resolve(_token, force=False):
            force_calls.append(force)
            entry.PRICE_TICKERS["/GC"] = "/GCZ26:XCEC"
            return {}

        lane = self.make_lane(clock=lambda: now[0], resolve=resolve,
                              feed_factory=lambda _url, _token: FakeFeed())
        lane.cycle()
        # Simulate another owner (the chain session) publishing the new month.
        entry.PRICE_TICKERS["/GC"] = "/GCG27:XCEC"
        now[0] += timedelta(seconds=1)
        lane.cycle()
        self.assertEqual(len(self.feeds), 2)
        self.assertTrue(self.feeds[0].stopped)
        self.assertIn("/GCG27:XCEC", self.feeds[1].price_symbols)
        self.assertNotIn(True, force_calls, "the drift check does not need another auth or provider call")

    def test_writer_plan_is_single_valued(self):
        from market_data.live_market import runner
        with patch.object(runner, "PRICE_LANE_ENABLED", True):
            self.assertEqual(runner.price_writer_plan(), "continuous_price_lane")
        with patch.object(runner, "PRICE_LANE_ENABLED", False):
            self.assertEqual(runner.price_writer_plan(), "in_session_legacy")


if __name__ == "__main__":
    unittest.main()
