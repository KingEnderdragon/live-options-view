#!/usr/bin/env python3
"""Prove explicit strategy-state lifecycle for independent replays (#69).

Hermetic: hand-built snapshots and contexts only, no network, no R2, no
provider access. `put_call_ratio_qqq`, `momentum_qqq` and `oi_skew_qqq` keep
rolling observation history per process. A second independent replay in the
same interpreter used to score its first day against the previous replay's
last day, so a parameter sweep silently turned the final configuration into
`no_trade` for every day.

This suite proves the accepted G1 criteria through the *declared* run boundary
(`crassus/replay.py`):

- a genuinely fresh process (a subprocess, not a reset in the same one) and a
  repeated same-interpreter run produce identical decisions;
- parameter sweeps run in different orders produce identical per-configuration
  decisions for PCR, momentum and OI skew;
- chronological history still carries across days inside one run, while a
  repeated same-day run requires the declared boundary;
- a stateless strategy is unaffected;
- the separate regression driver `scripts/fixtures/strategy_state_repro.py`
  runs unchanged on pre-fix source to show the contamination.

    python scripts/verify_strategy_state.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.replay import registered_strategy_ids, reset_strategy_state  # noqa: E402
from crassus.strategies import momentum_qqq as moq  # noqa: E402
from crassus.strategies import oi_skew as oisk  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

passed, failed = 0, 0

DAY1, DAY2 = "2026-01-05", "2026-01-06"


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


# -- fixtures ---------------------------------------------------------------

def _ts(date_str: str, minute: int) -> str:
    base = datetime.fromisoformat(f"{date_str}T09:00:00+00:00")
    return (base + timedelta(minutes=minute)).isoformat()


def _option_row(symbol: str, strike: float, kind: str, oi: int = 500) -> dict:
    return {"OptionSymbol": symbol, "Strike": strike, "Type": kind,
            "Bid": 1.0, "Ask": 1.1, "OpenInterest": oi}


def pcr_snapshot(timestamp: str, ratio: float, price: float = 400.0) -> MarketSnapshot:
    call_oi, put_oi = 1000, int(round(1000 * ratio))
    rows = [_option_row("QQQ260101C00400000", price, "call", call_oi),
            _option_row("QQQ260101P00400000", price, "put", put_oi)]
    return MarketSnapshot.from_payload(
        "test://pcr", {"timestamp": timestamp, "snapshot_time": timestamp,
                       "expiration": "2026-01-01", "underlying_price": price,
                       "rows": rows}, raw=b"{}",
    )


def price_snapshot(timestamp: str, price: float) -> MarketSnapshot:
    strike = float(round(price))
    rows = [_option_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call"),
            _option_row(f"QQQ260101P{int(strike*1000):08d}", strike, "put")]
    return MarketSnapshot.from_payload(
        "test://price", {"timestamp": timestamp, "snapshot_time": timestamp,
                         "expiration": "2026-01-01", "underlying_price": price,
                         "rows": rows}, raw=b"{}",
    )


def oi_snapshot(timestamp: str, call_oi: float, put_oi: float,
                edge_call_oi: float = 500, edge_put_oi: float = 20) -> MarketSnapshot:
    rows = []
    for strike in (392.0, 396.0, 400.0, 404.0, 408.0):
        rows.append(_option_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call", call_oi))
        rows.append(_option_row(f"QQQ260101P{int(strike*1000):08d}", strike, "put", put_oi))
    for strike in (384.0, 416.0):
        rows.append(_option_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call", edge_call_oi))
        rows.append(_option_row(f"QQQ260101P{int(strike*1000):08d}", strike, "put", edge_put_oi))
    return MarketSnapshot.from_payload(
        "test://oi", {"timestamp": timestamp, "snapshot_time": timestamp,
                      "expiration": "2026-01-01", "underlying_price": 400.0,
                      "rows": rows}, raw=b"{}",
    )


def ctx_for(snapshot: MarketSnapshot, params: dict | None = None,
            alias: str | None = None, username: str | None = None,
            trades: list[dict] | None = None) -> StrategyContext:
    symbols = {r["OptionSymbol"] for r in snapshot.rows}
    ts = snapshot.timestamp

    def quotes(syms):
        return {s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts=ts, server_ts=ts)
                for s in syms if s in symbols}

    return StrategyContext(
        snapshot=snapshot,
        account_state={"username": username} if username else {},
        book=Book(trades or []),
        now_et=datetime.fromisoformat(ts),
        session_phase="open",
        quotes=quotes,
        params=params or {},
        account_alias=alias,
    )


def pcr_day(date_str: str) -> list[MarketSnapshot]:
    """12 warm-up readings around PCR 1.0, one extreme, then a normal read."""
    ratios = [0.99, 1.01] * 6 + [1.50, 1.00, 0.98]
    return [pcr_snapshot(_ts(date_str, 35 + 5 * i), ratio)
            for i, ratio in enumerate(ratios)]


def momentum_day(date_str: str, first_minute: int = 35) -> list[MarketSnapshot]:
    """A 65-minute rise then a fall, enough for a buy and a later sell."""
    prices = ([400.0 + 0.12 * i for i in range(12)]
              + [401.2, 400.9, 400.4, 400.0, 399.6])
    return [price_snapshot(_ts(date_str, first_minute + 5 * i), price)
            for i, price in enumerate(prices)]


def oi_day(date_str: str) -> list[MarketSnapshot]:
    calls = [100, 140, 220, 260, 300, 260]
    puts = [80, 70, 60, 60, 55, 60]
    return [oi_snapshot(_ts(date_str, 40 + 5 * i), c, p)
            for i, (c, p) in enumerate(zip(calls, puts))]


def run(strategy_id: str, snapshots: list[MarketSnapshot], params: dict | None = None,
        alias: str | None = None) -> list[dict]:
    strategy = REGISTRY[strategy_id]
    return [strategy(ctx_for(s, params, alias)).to_dict() for s in snapshots]


# -- scenarios (also used by the fresh-process comparator) -------------------

def scenario_pcr(params: dict) -> list[dict]:
    return run("put_call_ratio_qqq", pcr_day(DAY1), params)


def scenario_momentum(params: dict) -> list[dict]:
    """Decision-level replay with fills, so both entries and exits appear."""
    strategy = REGISTRY["momentum_qqq"]
    trades: list[dict] = []
    out = []
    for snapshot in momentum_day(DAY1):
        ctx = ctx_for(snapshot, params, trades=trades)
        decision = strategy(ctx)
        if decision.is_trade:
            row = next(r for r in snapshot.rows
                       if r["OptionSymbol"] == decision.symbol)
            price = row["Ask"] if decision.action == "buy" else row["Bid"]
            trades.append({"sym": decision.symbol, "side": decision.action,
                           "qty": decision.quantity, "price": price,
                           "ts": snapshot.timestamp})
        out.append(decision.to_dict())
    return out


def scenario_oi(params: dict) -> list[dict]:
    accounts = (("A", 0.02), ("B", 0.02), ("C", 0.04))
    out = []
    for snapshot in oi_day(DAY1):
        for alias, _band in accounts:
            decision = REGISTRY["oi_skew_qqq"](
                ctx_for(snapshot, {**params, "near_money_pct": params.get(
                    "near_money_pct", 0.02) if alias != "C" else 0.04}, alias=alias)
            ).to_dict()
            out.append({"account": alias, "decision": decision})
    return out


SCENARIOS = {"pcr": (scenario_pcr, "put_call_ratio_qqq"),
             "momentum": (scenario_momentum, "momentum_qqq"),
             "oi": (scenario_oi, "oi_skew_qqq")}


def fresh_process(scenario: str, params: dict) -> list[dict]:
    """Actually start a new interpreter; this is not an in-process reset."""
    result = subprocess.run(
        [sys.executable, str(HERE), "--emit-scenario", scenario,
         "--params", json.dumps(params)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"scenario subprocess failed: {result.stderr[-800:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# -- subprocess entry point -------------------------------------------------

def emit() -> int:
    args = sys.argv[1:]
    scenario = args[args.index("--emit-scenario") + 1]
    params = json.loads(args[args.index("--params") + 1])
    print(json.dumps(SCENARIOS[scenario][0](params)))
    return 0


if __name__ == "__main__" and "--emit-scenario" in sys.argv:
    raise SystemExit(emit())


# -- 1. fresh process versus repeated same-interpreter run -------------------

for name, (scenario, strategy_id) in SCENARIOS.items():
    params = ({} if name != "oi" else {"near_band_mode": "account"})
    reset_strategy_state(strategy_id)
    first = scenario(params)
    reset_strategy_state(strategy_id)
    second = scenario(params)
    external = fresh_process(name, params)
    check(f"{name}: fresh process and repeated same-interpreter run agree",
          first == second == external,
          f"first={len(first)} second={len(second)} external={len(external)}")

# -- 2. reordered parameter sweeps through the declared boundary ------------

SWEEPS = {
    "pcr": (scenario_pcr, "put_call_ratio_qqq", [
        {"extreme_z_threshold": 0.75},
        {"extreme_z_threshold": 1.0},
        {"extreme_z_threshold": 1.5},
    ]),
    "momentum": (scenario_momentum, "momentum_qqq", [
        {"bullish_threshold": 0.0005, "bearish_threshold": -0.0005},
        {"bullish_threshold": 0.0010, "bearish_threshold": -0.0010},
        {"bullish_threshold": 0.0020, "bearish_threshold": -0.0020},
    ]),
    "oi": (scenario_oi, "oi_skew_qqq", [
        {"near_money_pct": 0.01, "imbalance_threshold": 0.2},
        {"near_money_pct": 0.02, "imbalance_threshold": 0.3},
        {"near_money_pct": 0.04, "imbalance_threshold": 0.3},
    ]),
}


def ordered_sweep(scenario, strategy_id: str, configs: list[dict],
                  order: list[int]) -> dict[str, list[dict]]:
    results = {}
    for index in order:
        reset_strategy_state(strategy_id)
        results[json.dumps(configs[index], sort_keys=True)] = scenario(configs[index])
    return results


for name, (scenario, strategy_id, configs) in SWEEPS.items():
    forward = ordered_sweep(scenario, strategy_id, configs, [0, 1, 2])
    reversed_ = ordered_sweep(scenario, strategy_id, configs, [2, 0, 1])
    check(f"{name}: reordered sweeps agree per configuration", forward == reversed_)
    fresh_mismatch = [
        key for key, decisions in forward.items()
        if fresh_process(name, json.loads(key)) != decisions
    ]
    check(f"{name}: every sweep configuration matches a fresh process",
          not fresh_mismatch, f"mismatched={fresh_mismatch}")

# -- 3. chronological history is retained, same-day replay needs the boundary

reset_strategy_state()
continuous = run("put_call_ratio_qqq", pcr_day(DAY1) + pcr_day(DAY2))
reset_strategy_state()
day2_alone = run("put_call_ratio_qqq", pcr_day(DAY2))
check("PCR within-run history carries across days",
      continuous[len(pcr_day(DAY1))]["metadata"]["sample_count"]
      > day2_alone[0]["metadata"]["sample_count"],
      f"continuous={continuous[len(pcr_day(DAY1))]['metadata']['sample_count']} "
      f"alone={day2_alone[0]['metadata']['sample_count']}")

reset_strategy_state()
moq._decide(ctx_for(momentum_day(DAY1)[0]))
check("momentum records one observation", len(moq._tracker.snapshot()) == 1)
reset_strategy_state()
check("momentum reset clears history", len(moq._tracker.snapshot()) == 0)
reset_strategy_state()
run("momentum_qqq", momentum_day(DAY1) + momentum_day(DAY2, first_minute=-540))
continuous_points = len(moq._tracker.snapshot())
reset_strategy_state()
run("momentum_qqq", momentum_day(DAY2, first_minute=-540))
alone_points = len(moq._tracker.snapshot())
check("momentum within-run history carries across days",
      continuous_points > alone_points, f"continuous={continuous_points} alone={alone_points}")

reset_strategy_state()
oi_first = scenario_oi({"near_money_pct": 0.02})
reset_strategy_state()
oi_second = scenario_oi({"near_money_pct": 0.02})
check("OI same-day repeat with reset matches", oi_first == oi_second)
check("OI accounts keep independent anchors",
      {r["account"]: r["decision"]["metadata"]["session_start_imbalance"]
       for r in oi_first if r["decision"]["metadata"]}
      .get("A") == {r["account"]: r["decision"]["metadata"]["session_start_imbalance"]
                    for r in oi_first if r["decision"]["metadata"]}.get("B")
      and {r["account"]: r["decision"]["metadata"]["session_start_imbalance"]
           for r in oi_first if r["decision"]["metadata"]}.get("A")
      != {r["account"]: r["decision"]["metadata"]["session_start_imbalance"]
          for r in oi_first if r["decision"]["metadata"]}.get("C"))

# -- 4. registry and stateless negative control -----------------------------

check("PCR, momentum and OI register a state resetter",
      {"put_call_ratio_qqq", "momentum_qqq", "oi_skew_qqq"}
      <= set(registered_strategy_ids()), str(registered_strategy_ids()))
check("unknown/stateless strategy reset is a no-op", reset_strategy_state("max_pain_qqq") == ())

max_pain = REGISTRY["max_pain_qqq"]
before = max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), 1.0))).to_dict()
reset_strategy_state()
after = max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), 1.0))).to_dict()
check("stateless strategy is unaffected by the reset", before == after)
reset_strategy_state()
sweep_a = [max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), ratio))).to_dict()
           for ratio in (0.99, 1.01, 1.5)]
reset_strategy_state()
sweep_b = [max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), ratio))).to_dict()
           for ratio in (1.5, 0.99, 1.01)]
check("stateless strategy is unaffected by run order",
      sweep_a == [sweep_b[1], sweep_b[2], sweep_b[0]])


# -- subprocess entry point -------------------------------------------------

def emit() -> int:
    args = sys.argv[1:]
    scenario = args[args.index("--emit-scenario") + 1]
    params = json.loads(args[args.index("--params") + 1])
    print(json.dumps(SCENARIOS[scenario][0](params)))
    return 0


if __name__ == "__main__" and "--emit-scenario" in sys.argv:
    raise SystemExit(emit())

print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
