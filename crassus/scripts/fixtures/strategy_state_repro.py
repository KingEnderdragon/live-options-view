#!/usr/bin/env python3
"""Cross-revision reproduction driver for the #69 / #107 tracker defects.

This file deliberately uses only APIs that exist on pre-fix source: the
strategy registry, `MarketSnapshot`, `Quote`, `Book`, `StrategyContext` and
`prices`. It does not import `crassus.replay`, so it can be run unchanged in a
worktree at the parent of the fix and in the fixed worktree. That is the
regression proof: the same driver shows contamination before and clean
repetition after.

Reported here:

- PCR and momentum: decisions from a fresh subprocess versus the same
  configuration run after two other parameter values in one interpreter;
- OI skew: one cycle, three accounts on one snapshot (2%, 2%, 4%) -- the old
  shared tracker makes every account after the first a stale no-trade.

    python scripts/fixtures/strategy_state_repro.py           # report
    python scripts/fixtures/strategy_state_repro.py --only pcr # one scenario
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
import crassus.strategies  # noqa: E402,F401  (registration)
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

DAY1, DAY2 = "2026-01-05", "2026-01-06"


def _ts(date_str: str, minute: int) -> str:
    return (datetime.fromisoformat(f"{date_str}T09:00:00+00:00")
            + timedelta(minutes=minute)).isoformat()


def _row(symbol: str, strike: float, kind: str, oi: int) -> dict:
    return {"OptionSymbol": symbol, "Strike": strike, "Type": kind,
            "Bid": 1.0, "Ask": 1.1, "OpenInterest": oi}


def pcr_snapshot(timestamp: str, ratio: float) -> MarketSnapshot:
    rows = [_row("QQQ260101C00400000", 400.0, "call", 1000),
            _row("QQQ260101P00400000", 400.0, "put", int(round(1000 * ratio)))]
    return MarketSnapshot.from_payload(
        "test://pcr", {"timestamp": timestamp, "snapshot_time": timestamp,
                       "expiration": "2026-01-01", "underlying_price": 400.0,
                       "rows": rows}, raw=b"{}")


def price_snapshot(timestamp: str, price: float) -> MarketSnapshot:
    strike = float(round(price))
    rows = [_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call", 500),
            _row(f"QQQ260101P{int(strike*1000):08d}", strike, "put", 500)]
    return MarketSnapshot.from_payload(
        "test://price", {"timestamp": timestamp, "snapshot_time": timestamp,
                         "expiration": "2026-01-01", "underlying_price": price,
                         "rows": rows}, raw=b"{}")


def oi_snapshot(timestamp: str, call_oi: float, put_oi: float) -> MarketSnapshot:
    rows = []
    for strike in (392.0, 396.0, 400.0, 404.0, 408.0):
        rows.append(_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call", call_oi))
        rows.append(_row(f"QQQ260101P{int(strike*1000):08d}", strike, "put", put_oi))
    for strike in (384.0, 416.0):
        rows.append(_row(f"QQQ260101C{int(strike*1000):08d}", strike, "call", 500))
        rows.append(_row(f"QQQ260101P{int(strike*1000):08d}", strike, "put", 20))
    return MarketSnapshot.from_payload(
        "test://oi", {"timestamp": timestamp, "snapshot_time": timestamp,
                      "expiration": "2026-01-01", "underlying_price": 400.0,
                      "rows": rows}, raw=b"{}")


def ctx_for(snapshot, params=None, username="acct"):
    symbols = {r["OptionSymbol"] for r in snapshot.rows}
    ts = snapshot.timestamp

    def quotes(syms):
        return {s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts=ts, server_ts=ts)
                for s in syms if s in symbols}

    return StrategyContext(
        snapshot=snapshot, account_state={"username": username}, book=Book([]),
        now_et=datetime.fromisoformat(ts), session_phase="open", quotes=quotes,
        params=params or {},
    )


def pcr_days():
    ratios = [0.99, 1.01] * 6 + [1.50, 1.00, 0.98]
    out = []
    for day in (DAY1, DAY2):
        out.extend(pcr_snapshot(_ts(day, 35 + 5 * i), ratio)
                   for i, ratio in enumerate(ratios))
    return out


def momentum_days():
    prices = ([400.0 + 0.12 * i for i in range(12)]
              + [401.2, 400.9, 400.4, 400.0, 399.6])
    out = []
    for day in (DAY1, DAY2):
        out.extend(price_snapshot(_ts(day, 35 + 5 * i), price)
                   for i, price in enumerate(prices))
    return out


def run(strategy_id: str, snapshots: list, params: dict) -> list[dict]:
    strategy = REGISTRY[strategy_id]
    return [strategy(ctx_for(s, params)).to_dict() for s in snapshots]


def pcr_run(threshold: float) -> list[dict]:
    return run("put_call_ratio_qqq", pcr_days(), {"extreme_z_threshold": threshold})


def momentum_run(threshold: float) -> list[dict]:
    return run("momentum_qqq", momentum_days(),
               {"bullish_threshold": threshold, "bearish_threshold": -threshold})


def oi_cycle() -> list[dict]:
    snapshots = [oi_snapshot(_ts(DAY1, 40 + 5 * i), call, put)
                 for i, (call, put) in enumerate(
                     [(100, 80), (140, 70), (220, 60), (260, 60), (300, 55)])]
    out = []
    for snapshot in snapshots:
        for alias, band in (("A", 0.02), ("B", 0.02), ("C", 0.04)):
            decision = REGISTRY["oi_skew_qqq"](
                ctx_for(snapshot, {"near_money_pct": band}, username=alias)).to_dict()
            out.append({"account": alias,
                        "stale": "not newer" in decision["reason"],
                        "action": decision["action"]})
    return out


def fresh(kind: str) -> dict:
    result = subprocess.run([sys.executable, str(HERE), "--only", kind],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-800:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    if "--only" in sys.argv:
        kind = sys.argv[sys.argv.index("--only") + 1]
        print(json.dumps({"pcr": pcr_run(1.5), "momentum": momentum_run(0.001),
                          "oi": oi_cycle()}[kind]))
        return 0

    report = {"fresh": {k: fresh(k) for k in ("pcr", "momentum", "oi")},
              "after_sweep": {}}
    # No reset call anywhere: the fixed source's backward-date epoch guard has
    # to make each configuration start clean; the old source has no guard.
    pcr_run(0.75)
    pcr_run(1.0)
    report["after_sweep"]["pcr"] = pcr_run(1.5)
    momentum_run(0.0005)
    momentum_run(0.0020)
    report["after_sweep"]["momentum"] = momentum_run(0.001)
    report["oi"] = oi_cycle()
    report["equal"] = {k: report["fresh"][k] == report["after_sweep"].get(k)
                       for k in ("pcr", "momentum")}
    report["oi_stale_accounts"] = sorted(
        {r["account"] for r in report["oi"] if r["stale"]})
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
