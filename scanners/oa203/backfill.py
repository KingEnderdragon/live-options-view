"""Post-close backfill for the leaderboard's top symbols.

The capture sweeps cover the whole universe sparsely; the backfill fills in
trade bars for the handful of contracts that end up at the top of the board.
"""

from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from . import runtime
from .runtime import now_ms  # noqa: F401  (kept importable; calls use runtime.now_ms)
from .archive import published
from .tradier import Tradier


# --------------------------------------------------------------------------
# After-close backfill
# --------------------------------------------------------------------------

def backfill_symbols(rows: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    chosen = [r for r in rows if (r.get("rank") or 10**9) <= top
              or (r.get("clean_rank") or 10**9) <= top]
    return sorted(chosen, key=lambda r: (r.get("rank") or 10**9))


def run_backfill(client: Tradier, day: date, day_dir: Path, rows: list[dict[str, Any]],
                 top: int, workers: int) -> dict[str, Any]:
    targets = backfill_symbols(rows, top)

    def fetch(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"symbol": row["symbol"], "fetched_ms": runtime.now_ms(),
                    "bars": client.timesales(row["symbol"], day)}
        except Exception as exc:
            return {"symbol": row["symbol"], "fetched_ms": runtime.now_ms(), "error": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(fetch, targets))
    with published(day_dir / "backfill_timesales.jsonl.gz") as tmp,             gzip.open(tmp, "wt", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
    return {"requested": len(targets), "errors": sum(1 for r in records if r.get("error"))}
