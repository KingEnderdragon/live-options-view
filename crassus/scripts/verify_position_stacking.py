#!/usr/bin/env python3
"""Reproduce #86's changing-at-the-money-strike stacking case, hermetically.

The reported Phelps/smoke failure: `smoke_atm_roundtrip` checked flatness only
for the *current* ATM symbol, so once QQQ drifted to the next strike it bought
that strike while the previous one was still held, and `phelps_wrap` deferred
the old position's exit for its 27.5-minute window. The result was several
simultaneous untracked positions.

This drives the shipped strategy functions against a hand-built session whose
ATM moves 711 -> 712 while the first position's Phelps window is still open,
using the same `run_strategy_on_day` bridge the synthetic-day backtests use. No
network, no R2, no account. Run it against a pre-fix revision to see the
stacked buy; against current source it must show one position at a time.

    python scripts/verify_position_stacking.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
# The synthetic-day harness moved to research/synthetic_days in the folder
# reorganization; keep this reproducible against the earlier layout too.
for _relative in ("research/synthetic_days", "synthetic_days"):
    _candidate = ROOT / _relative
    if (_candidate / "backtest_bridge.py").exists():
        sys.path.insert(0, str(_candidate))
        break

from backtest_bridge import run_strategy_on_day  # noqa: E402
import crassus.strategies  # noqa: E402,F401  (registers the strategies)
from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.strategy import REGISTRY, StrategyContext  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def occ(strike: float, side: str) -> str:
    return f"QQQ260903{side}{int(strike * 1000):08d}"


def rows_for(spot: float) -> list[dict]:
    rows = []
    for strike in (711.0, 712.0, 713.0):
        for side in ("C", "P"):
            rows.append({
                "OptionSymbol": occ(strike, side),
                "Strike": strike,
                "Type": "call" if side == "C" else "put",
                "Bid": 1.0 if strike == round(spot) else 0.8,
                "Ask": 1.2 if strike == round(spot) else 1.0,
                "OpenInterest": 100,
            })
    return rows


def session() -> dict:
    """13:35 buy 711C; 13:40 ATM drifts to 712 inside the Phelps window;
    14:05 the 30-minute window has elapsed; 14:06 the account is flat again."""
    points = [
        ("2026-09-03T13:35:00-04:00", 711.2),
        ("2026-09-03T13:40:00-04:00", 712.1),
        ("2026-09-03T14:05:00-04:00", 712.2),
        ("2026-09-03T14:06:00-04:00", 712.1),
    ]
    return {
        "session_date": "2026-09-03",
        "donor_day": "fixture",
        "snapshots": [
            {"timestamp": ts, "snapshot_time": ts, "expiration": "2026-09-03",
             "underlying_price": spot, "rows": rows_for(spot)}
            for ts, spot in points
        ],
    }


def context_at(snapshot: dict, held_symbol: str | None) -> StrategyContext:
    snap = MarketSnapshot.from_payload("fixture://snapshot", snapshot, raw=b"{}")
    entry = {"sym": held_symbol, "side": "buy", "qty": 1, "price": 1.2,
             "ts": "2026-09-03T13:35:00-04:00"} if held_symbol else None
    book = Book([entry] if entry else [])
    ts = snapshot["timestamp"]
    by_symbol = {r["OptionSymbol"]: r for r in snapshot["rows"]}
    from datetime import datetime
    return StrategyContext(
        snapshot=snap, account_state={"username": "fixture"}, book=book,
        now_et=datetime.fromisoformat(ts), session_phase="open",
        quotes=lambda syms: {
            s: Quote(symbol=s, bid=by_symbol[s]["Bid"], ask=by_symbol[s]["Ask"],
                     quote_ts=ts, server_ts=ts)
            for s in syms if s in by_symbol
        },
        params={},
    )


day = session()
strategy = REGISTRY["smoke_atm_roundtrip_phelps"]
result = run_strategy_on_day("smoke_atm_roundtrip_phelps", day, {})
trades = result["trades"]
sequence = [(t["side"], t["sym"]) for t in trades]
print(f"  trade sequence: {sequence}")

# No second buy while a first position is still open.
held = 0
stacked = False
for side, _symbol in sequence:
    if side == "buy":
        if held:
            stacked = True
        held += 1
    else:
        held = max(0, held - 1)
check("no buy while a position is already held", not stacked, f"held={held}")

check("first entry is the 711 call",
      sequence[:1] == [("buy", occ(711.0, "C"))] or sequence[:1] == [("buy", occ(711.0, "C"))],
      str(sequence[:1]))
check("the 711 call is closed after its Phelps window",
      ("sell", occ(711.0, "C")) in sequence,
      str(sequence))
check("no simultaneous 711/712 position remains",
      sum(1 for side, _ in sequence if side == "buy")
      - sum(1 for side, _ in sequence if side == "sell") <= 1,
      str(sequence))

# Decision-level proof that the drift cycle defers the exit rather than
# buying the new ATM: holding 711C at the 13:40 snapshot must be a no_trade
# with the Phelps deferral metadata, not a buy of 712C.
drift = context_at(day["snapshots"][1], held_symbol=occ(711.0, "C"))
drift_decision = strategy(drift).to_dict()
check("drifting ATM defers the old position instead of stacking",
      drift_decision["action"] == "no_trade"
      and (drift_decision["metadata"] or {}).get("phelps_elapsed_minutes") is not None,
      drift_decision["reason"][:90])

# A contaminated book is refused outright rather than force-traded.
contaminated = context_at(day["snapshots"][2], held_symbol=occ(711.0, "C"))
contaminated.book = Book([
    {"sym": occ(711.0, "C"), "side": "buy", "qty": 1, "price": 1.2, "ts": "2026-09-03T13:35:00-04:00"},
    {"sym": occ(712.0, "C"), "side": "buy", "qty": 1, "price": 1.2, "ts": "2026-09-03T13:36:00-04:00"},
])
contaminated_decision = strategy(contaminated).to_dict()
check("a two-position book stands down",
      contaminated_decision["action"] == "no_trade",
      contaminated_decision["reason"][:90])


print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
