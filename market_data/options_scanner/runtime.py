"""Process-level services shared by the OA-203 modules.

The event log, the wall clock, the stop flag and the run settings live here so
there is one owner for each. Other modules reach them through this module
(``runtime.log``, ``runtime.now_ms()``, ``runtime.STOP``) rather than copying a
binding, so a test or an operator changes behaviour once.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .returns import QualifiedPolicy, ReturnPolicy


API = "https://api.tradier.com/v1"
OCC_VOLUME_URL = "https://marketdata.theocc.com/volume-query"
ET = ZoneInfo("America/New_York")
SELECTION_VERSION = "oa203-universe-v1"
STOP = False


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "at": datetime.now(timezone.utc).isoformat(), **fields},
                     sort_keys=True, default=str), flush=True)


def on_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def now_ms() -> int:
    return int(time.time() * 1000)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Config:
    universe_size: int = 500
    max_rpm: int = 100          # own cap, below Tradier's 120/min market-data limit
    reserve: int = 10           # pause when the token-wide remaining budget drops this low
    block_size: int = 50
    # Dispatch is paced by the rate limiter, not by workers. Workers only
    # bound requests in flight: at 100/min, 3 workers become the bottleneck
    # once mean provider latency exceeds 1.8 s (October 13: 0.7-2.7 s).
    workers: int = 6
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 20.0   # October 13 504s returned at ~10 s or hung ~55 s
    sweep_budget_s: float = 600.0  # ~2x the 306 s rate-budget floor; 0 disables
    backfill_top: int = 200
    universe_lead_min: int = 30  # start selection this long before the open
    spool_dir: Path = Path("/data/oa203")
    r2_prefix: str = "oa203/scanner"
    upload: bool = True
    spy_qqq_second_expiry: bool = False  # separately activated after review; at most two chains each
    policy: ReturnPolicy = field(default_factory=ReturnPolicy)
    qualified_policy: QualifiedPolicy = field(default_factory=QualifiedPolicy)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            universe_size=_env_int("OA203_UNIVERSE_SIZE", 500),
            max_rpm=_env_int("OA203_MAX_RPM", 100),
            reserve=_env_int("OA203_RATE_RESERVE", 10),
            block_size=_env_int("OA203_BLOCK_SIZE", 50),
            workers=_env_int("OA203_WORKERS", 6),
            connect_timeout_s=float(os.getenv("OA203_CONNECT_TIMEOUT_S", "5")),
            read_timeout_s=float(os.getenv("OA203_READ_TIMEOUT_S", "20")),
            sweep_budget_s=float(os.getenv("OA203_SWEEP_BUDGET_S", "600")),
            backfill_top=_env_int("OA203_BACKFILL_TOP", 200),
            universe_lead_min=_env_int("OA203_UNIVERSE_LEAD_MIN", 30),
            spool_dir=Path(os.getenv("OA203_SPOOL_DIR", "/data/oa203")),
            r2_prefix=os.getenv("OA203_R2_PREFIX", "oa203/scanner"),
            spy_qqq_second_expiry=os.getenv("OA203_SPY_QQQ_SECOND_EXPIRY", "false").lower() == "true",
            qualified_policy=QualifiedPolicy(
                min_entry_premium=float(os.getenv("OA203_QUAL_MIN_PREMIUM", "0.10")),
                max_relative_spread=float(os.getenv("OA203_QUAL_MAX_REL_SPREAD", "0.30")),
                max_quote_age_s=float(os.getenv("OA203_QUAL_MAX_AGE_S", "1800")),
                min_ask_size=_env_int("OA203_QUAL_MIN_ASK_SIZE", 1),
                min_bid_size=_env_int("OA203_QUAL_MIN_BID_SIZE", 1),
                min_entry_volume=_env_int("OA203_QUAL_MIN_ENTRY_VOLUME", 1),
                min_samples=_env_int("OA203_QUAL_MIN_SAMPLES", 3),
                min_coverage=float(os.getenv("OA203_QUAL_MIN_COVERAGE", "0.5")),
                min_open_interest=(int(os.environ["OA203_QUAL_MIN_OI"])
                                   if "OA203_QUAL_MIN_OI" in os.environ else None)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_size": self.universe_size, "max_rpm": self.max_rpm,
            "reserve": self.reserve, "block_size": self.block_size, "workers": self.workers,
            "connect_timeout_s": self.connect_timeout_s, "read_timeout_s": self.read_timeout_s,
            "sweep_budget_s": self.sweep_budget_s,
            "backfill_top": self.backfill_top, "universe_lead_min": self.universe_lead_min,
            "r2_prefix": self.r2_prefix, "return_policy": self.policy.as_dict(),
            "spy_qqq_second_expiry": self.spy_qqq_second_expiry,
            "qualified_policy": self.qualified_policy.as_dict(),
        }


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def nyse_session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Exchange hours for ``day`` including early closes; None if closed."""
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (row["market_open"].to_pydatetime().astimezone(ET),
            row["market_close"].to_pydatetime().astimezone(ET))


def previous_session(day: date) -> date:
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day - timedelta(days=14),
                                                  end_date=day - timedelta(days=1))
    if schedule.empty:
        raise RuntimeError(f"no NYSE session in the 14 days before {day}")
    return schedule.index[-1].date()
