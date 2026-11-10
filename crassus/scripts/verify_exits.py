#!/usr/bin/env python3
"""Verify configurable profit-taking and protective exits (#86 / B3).

The fixtures cover the decision table and the lifecycle edges the quest asks
for: inclusive threshold boundaries, long and short holdings, the
liquidation-side price (bid for a long, ask for a short), every held contract in
a multi-position book with stop-over-profit precedence, stale and missing
quotes, the Phelps window's two configurable bypasses, configuration
validation, the re-entry cooldown (including across a restart), the audit
metadata that records trigger/quote/threshold/fill, and the runner wiring that
makes an exit a real submitted order attributed to `exit_policy`.

    python scripts/verify_exits.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))

if sys.platform == "win32":  # POSIX directory fsync has no meaning here
    import crassus.audit as _audit
    import crassus.durability as _durability

    _durability.sync_directory = lambda path: None
    _audit.sync_directory = lambda path: None

from crassus import exits  # noqa: E402
from crassus.client import AccountState, Book  # noqa: E402
from crassus.exits import (  # noqa: E402
    ExitConfigError,
    ExitPolicy,
    ExitStateStore,
    evaluate,
    pnl_pct,
    reentry_blocked,
    resolve_policy,
)
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategy import Decision, StrategyContext  # noqa: E402

SYMBOL = "QQQ240101C00400000"
NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _scratch_root() -> Path:
    candidate = Path(os.environ.get("CRASSUS_TEST_TMPDIR") or ROOT.parent / ".test-tmp") / "exits"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tmpdir() -> Path:
    base = _scratch_root()
    for attempt in range(100):
        candidate = base / f"run-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not create a scratch directory under {base}")


def quote(bid: float | None, ask: float | None, *, age_s: float = 5.0) -> Quote:
    server = NOW.replace(second=30)
    quote_ts = server - timedelta(seconds=age_s)
    return Quote(
        symbol=SYMBOL,
        bid=bid,
        ask=ask,
        quote_ts=quote_ts.replace(tzinfo=None).isoformat(),
        server_ts=server.replace(tzinfo=None).isoformat(),
    )


def book(*trades) -> Book:
    return Book(list(trades))


def long_trade(price: float = 1.00, symbol: str = SYMBOL, ts: str = "2024-01-01T10:00:00Z"):
    return {"sym": symbol, "side": "buy", "qty": 1, "price": price, "ts": ts}


def short_trade(price: float = 1.00, symbol: str = SYMBOL, ts: str = "2024-01-01T10:00:00Z"):
    return {"sym": symbol, "side": "sell", "qty": 1, "price": price, "ts": ts}


def ctx(book_obj, quotes, *, params=None, now_et=NOW) -> StrategyContext:
    return StrategyContext(
        snapshot=None,
        account_state={"username": "crassus_test"},
        book=book_obj,
        now_et=now_et,
        session_phase="open",
        quotes=lambda symbols: {s: quotes[s] for s in symbols if s in quotes},
        params=params or {},
        account_alias="Test",
    )


class BoundaryTests(unittest.TestCase):
    def test_profit_boundary_is_inclusive(self):
        policy = ExitPolicy()
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.50, 1.55)}), policy)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.metadata["exit_trigger"], "profit_target")

        below = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.49, 1.54)}), policy)
        self.assertIsNone(below)

    def test_stop_boundary_is_inclusive(self):
        policy = ExitPolicy()
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.50, 0.55)}), policy)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

        above = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.51, 0.56)}), policy)
        self.assertIsNone(above)

    def test_long_liquidates_at_the_bid_not_the_ask(self):
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(1.50, 3.00)}), ExitPolicy())
        self.assertEqual(decision.action, "sell")
        self.assertEqual(decision.quantity, 1)
        self.assertEqual(decision.metadata["exit_liquidation_price"], 1.50)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_short_liquidates_at_the_ask(self):
        # Short 1.00; buying back at 0.50 is a 50% gain.
        short_book = book(short_trade(1.00))
        decision = evaluate(ctx(short_book, {SYMBOL: quote(0.40, 0.50)}), ExitPolicy())
        self.assertEqual(decision.action, "buy")
        self.assertEqual(decision.quantity, 1)
        self.assertEqual(decision.metadata["exit_liquidation_price"], 0.50)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_short_stop_uses_the_ask(self):
        short_book = book(short_trade(1.00))
        decision = evaluate(ctx(short_book, {SYMBOL: quote(1.40, 1.50)}), ExitPolicy())
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")
        self.assertEqual(decision.metadata["exit_liquidation_price"], 1.50)

    def test_cost_basis_comes_from_the_documented_book_average(self):
        # Two buys at 1.00 and 2.00 -> average 1.50; bid 2.25 is +50%.
        scaled = book(
            {"sym": SYMBOL, "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": SYMBOL, "side": "buy", "qty": 1, "price": 2.00, "ts": "2024-01-01T10:05:00Z"},
        )
        decision = evaluate(ctx(scaled, {SYMBOL: quote(2.25, 2.30)}), ExitPolicy())
        self.assertEqual(decision.quantity, 2)
        self.assertAlmostEqual(decision.metadata["exit_cost_basis"], 1.5)
        self.assertAlmostEqual(decision.metadata["exit_pnl_pct"], 0.5)

    def test_pnl_sign_convention(self):
        self.assertAlmostEqual(pnl_pct(1.0, 1.5, 1), 0.5)
        self.assertAlmostEqual(pnl_pct(1.0, 0.5, 1), -0.5)
        self.assertAlmostEqual(pnl_pct(1.0, 0.5, -1), 0.5)
        with self.assertRaises(ValueError):
            pnl_pct(0.0, 1.0, 1)


class QuoteTests(unittest.TestCase):
    def test_stale_quote_blocks_the_exit_and_holds_the_cycle(self):
        stale = quote(0.40, 0.45, age_s=30.0)
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: stale}), ExitPolicy())
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_pending_symbols"], [SYMBOL])

    def test_missing_quote_blocks_the_exit(self):
        decision = evaluate(ctx(book(long_trade()), {}), ExitPolicy())
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_pending_symbols"], [SYMBOL])

    def test_quote_missing_the_liquidation_side_is_not_usable(self):
        # No bid, so a long cannot be valued even though an ask exists.
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(None, 1.5)}), ExitPolicy())
        self.assertEqual(decision.action, "no_trade")

    def test_quote_error_becomes_a_holding_no_trade(self):
        def broken(_symbols):
            raise RuntimeError("quote service down")

        context = ctx(book(long_trade()), {})
        context.quotes = broken
        decision = evaluate(context, ExitPolicy())
        self.assertEqual(decision.action, "no_trade")
        self.assertEqual(decision.metadata["exit_quotes_error"], "RuntimeError")

    def test_flat_book_produces_no_exit(self):
        self.assertIsNone(evaluate(ctx(book(), {}), ExitPolicy()))


class PrecedenceTests(unittest.TestCase):
    def test_stop_outranks_profit_in_a_multi_position_book(self):
        winners = book(
            {"sym": "WIN", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "LOSS", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        quotes = {
            "WIN": Quote("WIN", 1.70, 1.75, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
            "LOSS": Quote("LOSS", 0.40, 0.45, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
        }
        decision = evaluate(ctx(winners, quotes), ExitPolicy())
        self.assertEqual(decision.symbol, "LOSS")
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

    def test_largest_loss_wins_between_two_stops(self):
        two = book(
            {"sym": "A", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "B", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        quotes = {
            "A": Quote("A", 0.45, 0.50, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
            "B": Quote("B", 0.20, 0.25, "2024-01-01T12:00:25", "2024-01-01T12:00:30"),
        }
        decision = evaluate(ctx(two, quotes), ExitPolicy())
        self.assertEqual(decision.symbol, "B")

    def test_every_held_contract_is_considered_each_cycle(self):
        two = book(
            {"sym": "A", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
            {"sym": "B", "side": "buy", "qty": 1, "price": 1.00, "ts": "2024-01-01T10:00:00Z"},
        )
        # A has no quote; B is comfortably profitable and must still exit.
        quotes = {"B": Quote("B", 1.60, 1.65, "2024-01-01T12:00:25", "2024-01-01T12:00:30")}
        decision = evaluate(ctx(two, quotes), ExitPolicy())
        self.assertEqual(decision.symbol, "B")
        self.assertEqual(decision.metadata["exit_trigger"], "profit_target")

    def test_phelps_stop_bypasses_the_window_by_default(self):
        params = {"phelps_minutes": 27.5}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))  # 5 minutes ago
        decision = evaluate(
            ctx(recent, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "sell")
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")

    def test_phelps_profit_waits_for_the_window_by_default(self):
        params = {"phelps_minutes": 27.5}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))
        policy = resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True)
        deferred = evaluate(
            ctx(recent, {SYMBOL: quote(1.60, 1.65)}, params=params),
            policy,
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(deferred.action, "no_trade")
        self.assertEqual(deferred.metadata["exit_pending_symbols"], [SYMBOL])

        mature = book(long_trade(ts="2024-01-01T11:00:00Z"))  # 60 minutes ago
        fired = evaluate(
            ctx(mature, {SYMBOL: quote(1.60, 1.65)}, params=params),
            policy,
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(fired.action, "sell")

    def test_phelps_stop_can_be_made_to_respect_the_window(self):
        params = {"phelps_minutes": 27.5, "exits": {"stop_bypasses_phelps": False}}
        recent = book(long_trade(ts="2024-01-01T11:55:00Z"))
        decision = evaluate(
            ctx(recent, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "no_trade")

    def test_unknown_entry_time_does_not_hold_a_stop_open(self):
        params = {"phelps_minutes": 27.5}
        untimed = book({"sym": SYMBOL, "side": "buy", "qty": 1, "price": 1.00})
        decision = evaluate(
            ctx(untimed, {SYMBOL: quote(0.40, 0.45)}, params=params),
            resolve_policy("reddit_sentiment_qqq_phelps", params, activated=True),
            strategy_id="reddit_sentiment_qqq_phelps",
        )
        self.assertEqual(decision.action, "sell")


class PolicyTests(unittest.TestCase):
    def test_covered_strategy_enables_the_layer(self):
        self.assertTrue(resolve_policy("reddit_sentiment_qqq", {}, activated=True).enabled)

    def test_layer_is_inert_without_deployment_activation(self):
        # Neither the covered-strategy default nor an explicit per-account
        # opt-in can bypass the deployment switch: this branch must change no
        # trading behaviour until CRASSUS_EXITS_ENABLED is set.
        self.assertFalse(resolve_policy("reddit_sentiment_qqq", {}).enabled)
        self.assertFalse(
            resolve_policy("reddit_sentiment_qqq", {"exits": {"enabled": True}}).enabled
        )
        decision = evaluate(
            ctx(book(long_trade()), {SYMBOL: quote(0.10, 0.15)}),
            resolve_policy("reddit_sentiment_qqq", {}),
        )
        self.assertIsNone(decision)

    def test_activation_parsing_requires_an_explicit_affirmative(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(
                exits.activation_from_environment({"CRASSUS_EXITS_ENABLED": value}), value
            )
        self.assertFalse(exits.activation_from_environment({}))
        for value in ("0", "false", "no", "off", "maybe", ""):
            self.assertFalse(
                exits.activation_from_environment({"CRASSUS_EXITS_ENABLED": value}), value
            )

    def test_uncovered_strategy_stays_off_unless_it_opts_in(self):
        self.assertFalse(resolve_policy("smoke_atm_roundtrip", {}, activated=True).enabled)
        self.assertFalse(resolve_policy("looking_glass_straddle", {}, activated=True).enabled)
        self.assertTrue(resolve_policy("smoke_atm_roundtrip", {"exits": {"enabled": True}}, activated=True).enabled)

    def test_disabled_policy_never_decides(self):
        policy = resolve_policy("reddit_sentiment_qqq", {"exits": {"enabled": False}}, activated=True)
        self.assertIsNone(evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.10, 0.15)}), policy))

    def test_thresholds_are_configurable(self):
        policy = resolve_policy("reddit_sentiment_qqq", {"exits": {
            "profit_target_pct": 0.25, "stop_loss_pct": -0.15,
        }}, activated=True)
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.84, 0.90)}), policy)
        self.assertEqual(decision.metadata["exit_trigger"], "protective_stop")
        self.assertEqual(decision.metadata["exit_threshold_pct"], -0.15)

    def test_malformed_config_raises_per_account(self):
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"profit_target_pct": "lots"}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"stop_loss_pct": 0.2}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"profit_target_pct": float("nan")}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": {"stop_los_pct": -0.5}})
        with self.assertRaises(ExitConfigError):
            resolve_policy("reddit_sentiment_qqq", {"exits": []})

    def test_audit_metadata_carries_the_whole_decision(self):
        decision = evaluate(ctx(book(long_trade()), {SYMBOL: quote(0.40, 0.45)}), ExitPolicy())
        metadata = decision.metadata
        for key in ("exit_trigger", "exit_symbol", "exit_held_quantity", "exit_cost_basis",
                    "exit_liquidation_price", "exit_bid", "exit_ask", "exit_quote_age_s",
                    "exit_pnl_pct", "exit_threshold_pct", "exit_policy"):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["exit_symbol"], SYMBOL)
        self.assertEqual(metadata["exit_held_quantity"], 1)
        self.assertEqual(metadata["exit_quote_age_s"], 5.0)


class ReentryTests(unittest.TestCase):
    def policy(self, minutes=15.0):
        return ExitPolicy(reentry_cooldown_minutes=minutes)

    def test_exit_blocks_the_same_contract_until_the_cooldown_ends(self):
        store = ExitStateStore(_tmpdir())
        now = [datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)]
        store._now = lambda: now[0]
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)

        buy = Decision(action="buy", symbol=SYMBOL, quantity=1, reason="signal",
                       strategy_id="reddit_sentiment_qqq", strategy_version="1")
        blocked = reentry_blocked(buy, username="crassus_test", store=store,
                                  policy=self.policy(), book=book())
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.action, "no_trade")
        self.assertEqual(blocked.metadata["blocked_symbol"], SYMBOL)
        self.assertAlmostEqual(blocked.metadata["exit_cooldown_remaining_s"], 900.0, places=0)

        now[0] = now[0] + timedelta(minutes=16)
        self.assertIsNone(reentry_blocked(buy, username="crassus_test", store=store,
                                          policy=self.policy(), book=book()))

    def test_cooldown_survives_a_restart(self):
        directory = _tmpdir()
        store = ExitStateStore(directory)
        store.record("crassus_test", SYMBOL, trigger="profit_target", quantity=1)
        reopened = ExitStateStore(directory)
        self.assertGreater(reopened.cooldown_remaining_s("crassus_test", SYMBOL, 15.0), 0)

    def test_reentry_guard_never_blocks_a_reducing_trade(self):
        store = ExitStateStore(_tmpdir())
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)
        held = book(long_trade())
        sell = Decision(action="sell", symbol=SYMBOL, quantity=1, reason="reduce",
                        strategy_id="reddit_sentiment_qqq", strategy_version="1")
        self.assertIsNone(reentry_blocked(sell, username="crassus_test", store=store,
                                          policy=self.policy(), book=held))

    def test_old_entries_are_pruned(self):
        directory = _tmpdir()
        now = [datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)]
        store = ExitStateStore(directory, now=lambda: now[0])
        store.record("crassus_test", SYMBOL, trigger="protective_stop", quantity=1)
        now[0] = now[0] + timedelta(hours=30)
        store.record("crassus_test", "OTHER", trigger="profit_target", quantity=1)
        saved = json.loads((directory / "exits.json").read_text())
        self.assertNotIn(SYMBOL, saved["accounts"]["crassus_test"])
        self.assertIn("OTHER", saved["accounts"]["crassus_test"])


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RunnerWiringTests(unittest.TestCase):
    """The exit must become a real submitted order, attributed correctly."""

    def setUp(self):
        # Pin the runner's clock mid-session: the mandatory flatten must not be
        # the thing that closes the position in these scenarios.
        import crassus.clock as clock_module

        self._clock = clock_module
        self._original_now_et = clock_module.now_et
        clock_module.now_et = lambda: datetime(
            2024, 1, 2, 12, 0, tzinfo=timezone(timedelta(hours=-5))
        )

    def tearDown(self):
        self._clock.now_et = self._original_now_et

    def _runner(self, directory: Path, *, params=None, trades=None, outcome="filled",
               pending=None):
        from crassus.audit import DecisionLedger, Outcome
        from crassus.runner import Runner

        runner = object.__new__(Runner)
        account = SimpleNamespace(
            alias="Luigi", username="crassus_luigi",
            strategy_id="reddit_sentiment_qqq", params=params or {},
        )
        state = AccountState(username="crassus_luigi", balance_cash=10000.0,
                             trades=list(trades) if trades is not None else [long_trade()])
        runner.accounts = [account]
        runner.dry_run = False
        runner.ledger = DecisionLedger(directory)
        runner.retired = set()
        runner._stop = None
        runner.exit_state = ExitStateStore(directory)
        runner.exits_activated = True
        runner.quotes = SimpleNamespace(
            quotes=lambda symbols: {s: quote(0.40, 0.45) for s in symbols},
            last_retry_note=None,
        )
        runner.sessions = {"Luigi": SimpleNamespace(me=lambda: state,
                                                   sync_metadata=lambda **kw: None)}
        submitted = []
        outcome_class = getattr(Outcome, outcome.upper())

        class _Executor:
            def pending_intent(self):
                return pending

            def submit(self, **kwargs):
                submitted.append(kwargs)
                return SimpleNamespace(outcome_class=outcome_class,
                                       execution_request_id=f"req-{len(submitted)}",
                                       http_status=500 if outcome_class == Outcome.REJECTED else 200,
                                       server_response={}, latency_ms=12,
                                       state_after=state, note=None)

            def finalize(self, request_id):
                return None

        runner.executors = {"Luigi": _Executor()}
        runner._recover_pending = lambda acct: pending is not None
        return runner, account, submitted

    def test_exit_becomes_a_sell_attributed_to_the_exit_policy(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory)
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-01T17:00:00+00:00",
                     "snapshot_time": "2024-01-01T17:00:00+00:00",
                     "expiration": "2024-01-01", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")

        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["side"], "sell")
        self.assertEqual(submitted[0]["symbol"], SYMBOL)
        self.assertEqual(submitted[0]["strategy_id"], "exit_policy")
        self.assertEqual(submitted[0]["decision"]["metadata"]["exit_trigger"], "protective_stop")
        # The cooldown was recorded before submission.
        self.assertGreater(
            runner.exit_state.cooldown_remaining_s("crassus_luigi", SYMBOL, 15.0), 0
        )

    def test_rejected_exit_is_recorded_and_retried_on_the_next_cycle(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(directory, outcome="rejected")
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(len(submitted), 1)
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        self.assertEqual(json.loads(ledger[-1])["outcome_class"], "rejected")

        # The position is still held and still below its stop: the next cycle
        # must propose the exit again rather than treat the rejection as done.
        runner._run_account(account, snapshot, "open")
        self.assertEqual(len(submitted), 2)
        self.assertNotEqual(submitted[1]["decision_id"], submitted[0]["decision_id"])

    def test_settled_position_produces_no_second_exit(self):
        # Long then sold: the book is flat, so a settled position cannot be
        # exited twice.
        directory = _tmpdir()
        settled = [
            long_trade(),
            {"sym": SYMBOL, "side": "sell", "qty": 1, "price": 0.55,
             "ts": "2024-01-02T11:00:00Z"},
        ]
        runner, account, submitted = self._runner(directory, trades=settled)
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(submitted, [])

    def test_pending_exit_intent_is_reconciled_before_any_new_exit(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(
            directory, pending={"execution_request_id": "req-0", "decision_id": "d-0"}
        )
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-02T17:00:00+00:00",
                     "snapshot_time": "2024-01-02T17:00:00+00:00",
                     "expiration": "2024-01-02", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        # Recovery handled the in-flight exit; no second exit was proposed and
        # no cooldown entry was written for a trade that has not been re-sent.
        self.assertEqual(submitted, [])
        self.assertEqual(
            runner.exit_state.cooldown_remaining_s("crassus_luigi", SYMBOL, 15.0), 0.0
        )

    def test_invalid_exit_config_records_an_error_instead_of_trading(self):
        directory = _tmpdir()
        runner, account, submitted = self._runner(
            directory, params={"exits": {"stop_loss_pct": 0.5}}
        )
        snapshot = MarketSnapshot.from_payload(
            url="test://snapshot",
            payload={"timestamp": "2024-01-01T17:00:00+00:00",
                     "snapshot_time": "2024-01-01T17:00:00+00:00",
                     "expiration": "2024-01-01", "underlying_price": 400.0, "rows": []},
            raw=b"{}",
        )
        runner._run_account(account, snapshot, "open")
        self.assertEqual(submitted, [])
        ledger = sorted(directory.glob("decisions-*.jsonl"))[-1].read_text().strip().splitlines()
        record = json.loads(ledger[-1])
        self.assertEqual(record["outcome_class"], "runner_error")
        self.assertIn("Invalid exit configuration", record["reason"])


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (BoundaryTests, QuoteTests, PrecedenceTests, PolicyTests,
                 ReentryTests, RunnerWiringTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
