#!/usr/bin/env python3
"""Prove explicit strategy-state lifecycle for independent replays (#69).

Hermetic: hand-built snapshots and contexts only, no network, no R2, no
provider access. `put_call_ratio_qqq` and `momentum_qqq` keep one trailing
observation window per process. A second independent replay in the same
interpreter used to score its first day against the previous replay's last
day, so a parameter sweep silently turned the final configuration into
`no_trade` for every day. The fix is an explicit boundary
(`crassus/replay.py`) plus a backward-date epoch guard, and this suite proves
the boundary, the guard, within-run continuity, and that a stateless strategy
is untouched. Per-account OI-skew state is proved separately in
`scripts/verify_oi_skew_accounts.py`.

    python scripts/verify_strategy_state.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot, Quote  # noqa: E402
from crassus.replay import registered_strategy_ids, reset_strategy_state  # noqa: E402
from crassus.strategies import momentum_qqq as moq  # noqa: E402
from crassus.strategies import put_call_ratio as pcr  # noqa: E402
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


# -- fixtures ---------------------------------------------------------------

def _ts(date_str: str, minute: int) -> str:
    base = datetime.fromisoformat(f"{date_str}T09:00:00+00:00")
    return (base + timedelta(minutes=minute)).isoformat()


def pcr_snapshot(timestamp: str, ratio: float, price: float = 400.0) -> MarketSnapshot:
    call_oi, put_oi = 1000, int(round(1000 * ratio))
    rows = [
        {"OptionSymbol": "QQQ260101C00400000", "Strike": price, "Type": "call",
         "Bid": 1.0, "Ask": 1.1, "OpenInterest": call_oi},
        {"OptionSymbol": "QQQ260101P00400000", "Strike": price, "Type": "put",
         "Bid": 1.0, "Ask": 1.1, "OpenInterest": put_oi},
    ]
    return MarketSnapshot.from_payload(
        "test://pcr", {"timestamp": timestamp, "snapshot_time": timestamp,
                       "expiration": "2026-01-01", "underlying_price": price,
                       "rows": rows}, raw=b"{}",
    )


def price_snapshot(timestamp: str, price: float) -> MarketSnapshot:
    return MarketSnapshot.from_payload(
        "test://price", {"timestamp": timestamp, "snapshot_time": timestamp,
                         "expiration": "2026-01-01", "underlying_price": price,
                         "rows": []}, raw=b"{}",
    )


def ctx_for(snapshot: MarketSnapshot, params: dict | None = None,
            alias: str | None = None, username: str | None = None) -> StrategyContext:
    symbols = {r["OptionSymbol"] for r in snapshot.rows}
    ts = snapshot.timestamp

    def quotes(syms):
        return {
            s: Quote(symbol=s, bid=1.0, ask=1.1, quote_ts=ts, server_ts=ts)
            for s in syms if s in symbols
        }

    return StrategyContext(
        snapshot=snapshot,
        account_state={"username": username} if username else {},
        book=Book([]),
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


def run(strategy_id: str, snapshots: list[MarketSnapshot], params: dict | None = None,
        alias: str | None = None) -> list[dict]:
    strategy = REGISTRY[strategy_id]
    return [strategy(ctx_for(s, params, alias)).to_dict() for s in snapshots]


DAY1, DAY2 = "2026-01-05", "2026-01-06"

# -- 1. explicit replay boundary -------------------------------------------

reset_strategy_state()
fresh = run("put_call_ratio_qqq", pcr_day(DAY1))
check("PCR fresh run reaches a decision", len(fresh) == len(pcr_day(DAY1)))
check("PCR extreme reading buys a call",
      any(d["action"] == "buy" and d["symbol"] == "QQQ260101C00400000" for d in fresh),
      f"actions={[d['action'] for d in fresh]}")

reset_strategy_state()
again = run("put_call_ratio_qqq", pcr_day(DAY1))
check("identical run repeated after reset matches", again == fresh)

# The #69 reproduction in miniature: a sweep value first, the default value
# second, no explicit reset between them. The second run starts on an earlier
# calendar date, so the epoch guard must drop the first run's future baseline.
reset_strategy_state()
run("put_call_ratio_qqq", pcr_day(DAY2), {"extreme_z_threshold": 0.75})
after_sweep = run("put_call_ratio_qqq", pcr_day(DAY1))
check("sweep-then-default run matches a fresh default run", after_sweep == fresh)

# Out-of-order replay: a later day, then an earlier day, no reset.
reset_strategy_state()
run("put_call_ratio_qqq", pcr_day(DAY2))
out_of_order = run("put_call_ratio_qqq", pcr_day(DAY1))
check("out-of-order earlier day matches a fresh run", out_of_order == fresh)

# Chronological continuity must survive: day 2 inside one run still sees
# day 1's trailing window, so its sample counts are strictly larger than a
# fresh day-2 pass with no earlier history.
reset_strategy_state()
continuous = run("put_call_ratio_qqq", pcr_day(DAY1) + pcr_day(DAY2))
day2_continuous = continuous[len(pcr_day(DAY1)):]
reset_strategy_state()
day2_alone = run("put_call_ratio_qqq", pcr_day(DAY2))
check("within-run history carries across days",
      day2_continuous[0]["metadata"]["sample_count"] > day2_alone[0]["metadata"]["sample_count"],
      f"continuous={day2_continuous[0]['metadata']['sample_count']} alone={day2_alone[0]['metadata']['sample_count']}")

# Same-day replay is only correct with a declared boundary; both halves of
# that statement are explicit here so the contract is testable rather than
# implied.
reset_strategy_state()
first_half = run("put_call_ratio_qqq", pcr_day(DAY1))
reset_strategy_state()
second_half = run("put_call_ratio_qqq", pcr_day(DAY1))
check("same-day replay with reset matches", first_half == second_half)

# -- 2. momentum tracker has the same lifecycle ----------------------------

reset_strategy_state()
moq._decide(ctx_for(price_snapshot(_ts(DAY1, 35), 400.0)))
check("momentum records one observation", len(moq._tracker.snapshot()) == 1)
reset_strategy_state()
check("momentum reset clears history", len(moq._tracker.snapshot()) == 0)
moq._decide(ctx_for(price_snapshot(_ts(DAY2, 35), 400.0)))
moq._decide(ctx_for(price_snapshot(_ts(DAY1, 35), 400.0)))
check("momentum backward date starts a new epoch", len(moq._tracker.snapshot()) == 1,
      f"points={len(moq._tracker.snapshot())}")

# -- 3. registry and negative control --------------------------------------

check("PCR and momentum register a state resetter",
      {"put_call_ratio_qqq", "momentum_qqq"} <= set(registered_strategy_ids()),
      str(registered_strategy_ids()))
check("unknown/stateless strategy reset is a no-op", reset_strategy_state("max_pain_qqq") == ())

max_pain = REGISTRY["max_pain_qqq"]
before = max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), 1.0))).to_dict()
reset_strategy_state()
after = max_pain(ctx_for(pcr_snapshot(_ts(DAY1, 35), 1.0))).to_dict()
check("stateless strategy is unaffected by the reset", before == after)


print()
print("=" * 66)
print(f"{passed} passed, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
