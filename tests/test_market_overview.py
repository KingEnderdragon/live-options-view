import math
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_overview as mo  # noqa: E402
from market_overview import Observation as Obs  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)          # Fri 15:00 ET
PREV = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)       # Thu 16:00 ET close

STOCK = {"id": "AAPL", "unit": "USD", "instrument_type": "stock"}
INDEX = {"id": "idx.spx", "unit": "index_points", "instrument_type": "cash_index"}
WTI = {"id": "fut.cl.front", "unit": "USD_per_barrel", "instrument_type": "future_dated"}
US10Y = {"id": "yld.us10y", "unit": "percent", "instrument_type": "yield", "change_unit": "bps"}
USDJPY = {"id": "fx.usdjpy", "unit": "JPY_per_USD", "instrument_type": "fx_spot"}


def pair(inst, cur, ref, **kw):
    return (Obs(inst["id"], cur, T, "fixture", session="regular", **kw),
            Obs(inst["id"], ref, PREV, "fixture", session="regular", **kw))


class ChangeArithmetic(unittest.TestCase):
    def test_positive_negative_zero(self):
        for cur, ref, pct in ((110.0, 100.0, 10.0), (90.0, 100.0, -10.0), (100.0, 100.0, 0.0)):
            c = mo.compute_change(STOCK, *pair(STOCK, cur, ref), "previous_regular_close")
            self.assertEqual(c["status"], "ok")
            self.assertAlmostEqual(c["percent"], pct)
            self.assertAlmostEqual(c["absolute"], cur - ref)
            self.assertEqual(c["absolute_unit"], "USD")

    def test_index_points_are_not_dollars(self):
        c = mo.compute_change(INDEX, *pair(INDEX, 6550.0, 6500.0), "previous_regular_close")
        self.assertEqual(c["absolute_unit"], "index_points")
        self.assertAlmostEqual(c["absolute"], 50.0)

    def test_missing_and_nonfinite_are_unavailable_not_zero(self):
        for cur, ref, reason in ((None, 100.0, "missing_current"), (100.0, None, "missing_reference"),
                                 (math.nan, 100.0, "missing_current"), (100.0, math.inf, "missing_reference")):
            c = mo.compute_change(STOCK, *pair(STOCK, cur, ref), "previous_regular_close")
            self.assertEqual(c["status"], "unavailable")
            self.assertIn(reason, c["reasons"])
            self.assertIsNone(c["percent"])
            self.assertIsNone(c["absolute"])

    def test_zero_reference_keeps_absolute_but_no_percent(self):
        c = mo.compute_change(STOCK, *pair(STOCK, 5.0, 0.0), "previous_regular_close")
        self.assertEqual(c["status"], "partial")
        self.assertEqual(c["reasons"], ["zero_reference"])
        self.assertIsNone(c["percent"])
        self.assertAlmostEqual(c["absolute"], 5.0)

    def test_split_adjustment_mismatch(self):
        cur = Obs("AAPL", 50.0, T, "fixture", session="regular", adjustment_basis="post-split-2026-10-09")
        ref = Obs("AAPL", 100.0, PREV, "fixture", session="regular", adjustment_basis="unadjusted")
        c = mo.compute_change(STOCK, cur, ref, "previous_regular_close")
        self.assertEqual(c["status"], "unavailable")
        self.assertIn("adjustment_mismatch", c["reasons"])

    def test_yield_move_in_bps_without_percent(self):
        c = mo.compute_change(US10Y, *pair(US10Y, 4.05, 4.00), "previous_observation")
        self.assertEqual(c["status"], "ok")
        self.assertAlmostEqual(c["bps"], 5.0)
        self.assertIsNone(c["percent"])

    def test_cboe_scale_normalized_once(self):
        cur = Obs("yld.us10y", mo.normalize(40.5, 10), T, "dxlink:$TNX.X")
        ref = Obs("yld.us10y", mo.normalize(40.0, 10), PREV, "dxlink:$TNX.X")
        c = mo.compute_change(US10Y, cur, ref, "previous_observation")
        self.assertAlmostEqual(cur.value, 4.05)
        self.assertAlmostEqual(c["bps"], 5.0)
        self.assertIsNone(mo.normalize(math.nan, 10))
        self.assertIsNone(mo.normalize("bad"))

    def test_fx_inversion_inverts_both_values(self):
        # /6J futures quote USD per JPY; display wants JPY per USD.
        fut_cur = Obs("fut.6j", 0.0066, T, "dxlink:/6J")
        fut_ref = Obs("fut.6j", 0.0067, PREV, "dxlink:/6J")
        c = mo.compute_change(USDJPY, mo.invert(fut_cur, "fx.usdjpy"),
                              mo.invert(fut_ref, "fx.usdjpy"), "previous_observation")
        expected = 100 * ((1 / 0.0066) - (1 / 0.0067)) / (1 / 0.0067)
        self.assertAlmostEqual(c["percent"], expected)
        naive_negation = -100 * (0.0066 - 0.0067) / 0.0067
        self.assertNotAlmostEqual(c["percent"], naive_negation, places=6)
        self.assertIsNone(mo.invert(Obs("fut.6j", 0.0, T, "x"), "fx.usdjpy").value)

    def test_incompatible_fallback_identity(self):
        # USO (an ETF) must not stand in for WTI futures.
        cur = Obs("etf.uso", 75.0, T, "dxlink:USO")
        ref = Obs("fut.cl.front", 74.0, PREV, "dxlink:/CLX26", contract="CLX26")
        c = mo.compute_change(WTI, cur, ref, "previous_settlement")
        self.assertIn("identity_mismatch", c["reasons"])
        self.assertIsNone(c["percent"])

    def test_futures_cannot_cross_contracts(self):
        cur = Obs("fut.cl.front", 75.0, T, "dxlink", contract="CLZ26")
        ref = Obs("fut.cl.front", 74.0, PREV, "dxlink", contract="CLX26")
        c = mo.compute_change(WTI, cur, ref, "previous_settlement")
        self.assertIn("contract_mismatch", c["reasons"])
        same = mo.compute_change(WTI, Obs("fut.cl.front", 75.0, T, "d", contract="CLX26"),
                                 ref, "previous_settlement")
        self.assertEqual(same["status"], "ok")
        self.assertEqual(same["absolute_unit"], "USD")

    def test_extended_reference_is_not_a_close(self):
        cur = Obs("AAPL", 101.0, T, "fixture", session="premarket")
        ref = Obs("AAPL", 100.0, PREV, "fixture", session="postmarket")
        c = mo.compute_change(STOCK, cur, ref, "previous_regular_close")
        self.assertIn("reference_not_regular_close", c["reasons"])

    def test_extended_current_names_its_session_and_basis(self):
        cur = Obs("AAPL", 101.0, T, "fixture", session="premarket")
        ref = Obs("AAPL", 100.0, PREV, "fixture", session="regular")
        c = mo.compute_change(STOCK, cur, ref, "previous_regular_close")
        self.assertEqual((c["current_session"], c["basis"]), ("premarket", "previous_regular_close"))

    def test_rolling_24h_is_not_a_basis(self):
        with self.assertRaises(ValueError):
            mo.compute_change(STOCK, *pair(STOCK, 1.0, 1.0), "rolling_24h")


class Sessions(unittest.TestCase):
    def at(self, day, hh, mm):
        return datetime.combine(datetime.fromisoformat(day).date(), time(hh, mm), mo.ET)

    def test_regular_premarket_postmarket(self):
        s = mo.us_equity_schedule("2026-10-09")
        self.assertEqual(mo.classify_session(self.at("2026-10-09", 8, 0), s), "premarket")
        self.assertEqual(mo.classify_session(self.at("2026-10-09", 9, 30), s), "regular")
        self.assertEqual(mo.classify_session(self.at("2026-10-09", 16, 0), s), "postmarket")
        self.assertEqual(mo.classify_session(self.at("2026-10-09", 20, 0), s), "closed")
        self.assertEqual(mo.classify_session(self.at("2026-10-09", 3, 59), s), "closed")

    def test_holiday_and_early_close(self):
        thanksgiving = mo.us_equity_schedule("2026-11-26", regular_open=None)
        self.assertEqual(mo.classify_session(self.at("2026-11-26", 11, 0), thanksgiving), "closed")
        black_friday = mo.us_equity_schedule("2026-11-27", regular_close=time(13, 0))
        self.assertTrue(black_friday.early_close)
        self.assertEqual(mo.classify_session(self.at("2026-11-27", 13, 30), black_friday), "postmarket")

    def test_utc_input_is_converted_to_exchange_time(self):
        s = mo.us_equity_schedule("2026-10-09")
        self.assertEqual(mo.classify_session(datetime(2026, 10, 9, 13, 45, tzinfo=UTC), s), "regular")

    def test_dst_boundary(self):
        # 2026-11-02 is the first Monday after the US DST end: 9:30 ET == 14:30 UTC.
        s = mo.us_equity_schedule("2026-11-02")
        self.assertEqual(mo.classify_session(datetime(2026, 11, 2, 14, 29, tzinfo=UTC), s), "premarket")
        self.assertEqual(mo.classify_session(datetime(2026, 11, 2, 14, 30, tzinfo=UTC), s), "regular")


class Freshness(unittest.TestCase):
    NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)   # Sunday

    def test_sunday_btc_does_not_freshen_friday_equity(self):
        btc = mo.freshness(self.NOW - timedelta(seconds=30), self.NOW, True, timedelta(minutes=2))
        eq = mo.freshness(T, self.NOW, False, timedelta(minutes=2))
        self.assertEqual(btc["status"], "fresh")
        self.assertEqual(eq["status"], "closed")

    def test_stale_and_unknown_and_future(self):
        self.assertEqual(mo.freshness(self.NOW - timedelta(minutes=10), self.NOW, True,
                                      timedelta(minutes=2))["status"], "stale")
        self.assertEqual(mo.freshness(None, self.NOW, True, timedelta(minutes=2))["status"],
                         "unknown_time")
        self.assertEqual(mo.freshness(self.NOW + timedelta(minutes=1), self.NOW, True,
                                      timedelta(minutes=2))["status"], "future_dated")

    def test_cached_success_keeps_age_and_newer_failure(self):
        obs_at = self.NOW - timedelta(minutes=5)
        f = mo.freshness(obs_at, self.NOW, True, timedelta(minutes=2),
                         newer_failed_attempt_at=self.NOW - timedelta(seconds=10))
        self.assertEqual(f["status"], "stale")
        self.assertEqual(f["age_seconds"], 300)
        self.assertIn("newer_attempt_failed", f["warnings"])
        older = mo.freshness(obs_at, self.NOW, True, timedelta(minutes=10),
                             newer_failed_attempt_at=obs_at - timedelta(minutes=1))
        self.assertEqual(older["warnings"], [])


class Universe(unittest.TestCase):
    def setUp(self):
        self.cfg = mo.load_universe()

    def test_candidate_universe_is_valid(self):
        v = mo.validate_universe(self.cfg)
        self.assertEqual(v["errors"], [])
        self.assertEqual((v["symbols"], v["companies"], v["groups"]), (132, 132, 11))
        self.assertTrue(all(len(g["symbols"]) == 12 for g in self.cfg["groups"]))

    def test_duplicates_and_bounds_are_reported(self):
        bad = {**self.cfg, "groups": [dict(g) for g in self.cfg["groups"]]}
        bad["groups"][0] = {**bad["groups"][0], "symbols": bad["groups"][0]["symbols"] + ["MSFT"]}
        self.assertIn("duplicate_symbol:MSFT", mo.validate_universe(bad)["errors"])
        tiny = {**self.cfg, "groups": self.cfg["groups"][:2]}
        self.assertTrue(any(e.startswith("company_count_out_of_bounds")
                            for e in mo.validate_universe(tiny)["errors"]))

    def test_share_classes_count_once(self):
        cfg = {**self.cfg, "groups": [dict(g) for g in self.cfg["groups"]],
               "share_classes": {"company_aliases": {"GOOG": "GOOGL"}}}
        cfg["groups"][1] = {**cfg["groups"][1], "symbols": cfg["groups"][1]["symbols"] + ["GOOG"]}
        v = mo.validate_universe(cfg)
        self.assertEqual((v["symbols"], v["companies"]), (133, 132))

    def test_watchlist_supplements_with_explicit_statuses(self):
        cap = self.cfg["watchlist"]["additions_cap"]
        extra = [f"ZZ{i:02d}" for i in range(cap + 1)]
        w = mo.with_watchlist(self.cfg, ["pltr", "AAPL", "PLTR", "not a symbol!"] + extra)
        status = {a["symbol"]: a["status"] for a in w["additions"]}
        self.assertEqual(status["PLTR"], "duplicate_addition")   # second occurrence wins the dict
        self.assertEqual(w["additions"][0], {"symbol": "PLTR", "status": "accepted"})
        self.assertEqual(status["AAPL"], "already_in_baseline")
        self.assertEqual(status["NOT A SYMBOL!"], "malformed")
        self.assertEqual(sum(a["status"] == "over_cap" for a in w["additions"]), 2)
        origins = [m["origin"] for m in w["members"]]
        self.assertEqual(origins.count("baseline"), 132)
        self.assertEqual(origins.count("watchlist"), cap)


class BreadthAndMovers(unittest.TestCase):
    def setUp(self):
        cfg = mo.load_universe()
        self.members = mo.with_watchlist(cfg, ["PLTR"])["members"]
        tech = cfg["groups"][0]["symbols"]
        pcts = [3.0, 2.0, 1.0, 0.0, -1.0, -2.0, 0.5, 0.25, -0.5]  # 9 of 12 tech names
        self.changes = {s: {"percent": p, "basis": "previous_regular_close", "current_session": "regular"}
                        for s, p in zip(tech, pcts)}
        self.changes["PLTR"] = {"percent": 25.0, "basis": "previous_regular_close",
                                "current_session": "regular"}
        self.changes["JPM"] = {"percent": 9.0, "basis": "previous_regular_close",
                               "current_session": "regular"}

    def test_full_denominator_with_missing_and_stale(self):
        b = mo.breadth(self.members, self.changes, group="technology", stale={"NVDA"})
        self.assertEqual(b["denominator"], 12)
        self.assertEqual(b["missing"], 3)
        self.assertEqual(b["stale"], 1)               # NVDA (+1.0) excluded as stale
        self.assertEqual((b["advancing"], b["declining"], b["unchanged"]), (4, 3, 1))
        self.assertEqual(b["label"], "breadth within the curated sample")

    def test_watchlist_does_not_change_baseline_summary(self):
        base = mo.breadth(self.members, self.changes)
        self.assertEqual(base["denominator"], 132)
        withw = mo.breadth(self.members, self.changes, include_watchlist=True)
        self.assertEqual(withw["denominator"], 133)
        self.assertNotEqual(base["mean_percent"], withw["mean_percent"])

    def test_mixed_sessions_are_flagged(self):
        changes = dict(self.changes)
        changes["AAPL"] = {**changes["AAPL"], "current_session": "premarket"}
        self.assertIn("mixed_sessions", mo.breadth(self.members, changes)["warnings"])

    def test_top_movers_rank_full_sample_before_paging(self):
        up = mo.top_movers(self.members, self.changes, "up", limit=2)
        self.assertEqual([r["symbol"] for r in up["rows"]], ["JPM", "AAPL"])  # PLTR is watchlist
        self.assertEqual((up["eligible"], up["ranked"], up["missing"]), (132, 10, 122))
        page2 = mo.top_movers(self.members, self.changes, "up", limit=2, offset=2)
        self.assertEqual(page2["rows"][0]["rank"], 3)
        down = mo.top_movers(self.members, self.changes, "down", limit=1)
        self.assertEqual(down["rows"][0]["percent"], -2.0)
        last = mo.top_movers(self.members, self.changes, "up", limit=50)
        self.assertIsNone(last["next_offset"])
        withw = mo.top_movers(self.members, self.changes, "up", limit=1, include_watchlist=True)
        self.assertEqual(withw["rows"][0]["symbol"], "PLTR")


class Artifact(unittest.TestCase):
    NOW = datetime(2026, 10, 9, 19, 0, 30, tzinfo=UTC)

    def setUp(self):
        self.cfg = mo.load_universe()

    def row(self, symbol, cur, ref, **kw):
        inst = mo.stock_instrument(symbol)
        return mo.overview_row(inst, group="technology", origin="baseline",
                               current=Obs(symbol, cur, T, "dxlink", session="regular"),
                               reference=Obs(symbol, ref, PREV, "dxlink", session="regular"),
                               basis="previous_regular_close", now=self.NOW, market_open=True,
                               max_age=timedelta(minutes=2), provider_symbol=symbol, **kw)

    def test_row_shape_and_values(self):
        r = self.row("AAPL", 110.0, 100.0)
        self.assertEqual(r["change"]["status"], "ok")
        self.assertAlmostEqual(r["change"]["percent"], 10.0)
        self.assertEqual(r["current"]["observed_at"], T.isoformat())
        self.assertEqual(r["freshness"]["status"], "fresh")
        self.assertEqual(r["provider"], {"source": "dxlink", "symbol": "AAPL", "contract": None})
        import json
        json.dumps(r)  # serializable

    def test_uncollected_instruments_become_gaps(self):
        p = mo.build_overview(self.cfg, [self.row("AAPL", 1.0, 1.0)], generated_at=self.NOW,
                              cycle_started_at=self.NOW - timedelta(seconds=5), cycle_status="partial")
        self.assertEqual(p["schema_version"], 1)
        self.assertEqual(len(p["instruments"]), 1)
        expected = 132 + len(self.cfg["macro"])
        self.assertEqual(len(p["gaps"]), expected - 1)
        self.assertNotIn("AAPL", {g["id"] for g in p["gaps"]})

    def test_unknown_and_duplicate_rows_rejected(self):
        with self.assertRaises(ValueError):
            mo.build_overview(self.cfg, [self.row("ZZZZ", 1.0, 1.0)], generated_at=self.NOW,
                              cycle_started_at=self.NOW, cycle_status="complete")
        with self.assertRaises(ValueError):
            mo.build_overview(self.cfg, [self.row("AAPL", 1.0, 1.0)] * 2, generated_at=self.NOW,
                              cycle_started_at=self.NOW, cycle_status="complete")


if __name__ == "__main__":
    unittest.main()
