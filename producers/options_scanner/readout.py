"""Offline readout of an archived day: one contract, or the whole board.

Read-only: these commands answer "what did the scanner see" from the archive
alone, without touching the provider.
"""

from __future__ import annotations

import csv
import gzip
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .archive import _is_date, _read_manifest, iter_rows
from .outputs import chain_ok_prefixes, chain_ok_sweeps, load_backfill
from .returns import (
    ContractInfo,
    Observation,
    QualifiedPolicy,
    ReturnPolicy,
    contract_metrics,
    trade_bar_metrics,
)
from .runtime import ET


# --------------------------------------------------------------------------
# Inspection and readout
# --------------------------------------------------------------------------

def inspect_contract(day_dir: Path, symbol: str, policy: ReturnPolicy,
                     qualified_policy: QualifiedPolicy = QualifiedPolicy()) -> dict[str, Any]:
    path = [r for r in iter_rows(day_dir) if r.get("symbol") == symbol]
    path.sort(key=lambda r: r["t"])
    if not path:
        raise SystemExit(f"{symbol} not found in {day_dir}")
    summary = json.loads((day_dir / "summary.json").read_text()) if (day_dir / "summary.json").exists() else {}
    open_ms, close_ms = summary.get("session_open_ms"), summary.get("session_close_ms")
    rows = [r for r in path if (not open_ms or r["t"] >= open_ms) and (not close_ms or r["t"] < close_ms)]
    manifest = _read_manifest(day_dir)
    prefix_ok = chain_ok_prefixes(manifest)
    info = ContractInfo(symbol=symbol, underlying=rows[-1]["underlying"], option_type=rows[-1]["type"],
                        strike=rows[-1]["strike"], expiration=rows[-1]["expiration"],
                        contract_size=int(rows[-1].get("contract_size") or 100),
                        volume=rows[-1].get("volume"), open_interest=rows[0].get("oi"),
                        chain_ok_sweeps=chain_ok_sweeps(manifest).get(rows[-1]["underlying"], 0))
    obs = [Observation(r["t"], r["bid"], r["ask"], r.get("bid_ms"), r.get("ask_ms"), r.get("spot"),
                       r.get("bid_size"), r.get("ask_size"), r.get("volume"), r.get("oi"),
                       prefix_ok.get((r["underlying"], r.get("sweep")))) for r in rows]
    metrics = contract_metrics(info, obs, policy, qualified_policy)
    record = load_backfill(day_dir).get(symbol)
    if record and not record.get("error"):
        metrics.update(trade_bar_metrics(record.get("bars") or [], info.contract_size, policy))
    return {
        "metrics": metrics,
        "path": [{"time_et": datetime.fromtimestamp(r["t"] / 1000, ET).strftime("%H:%M:%S"),
                  "t": r["t"], "bid": r["bid"], "ask": r["ask"], "mid": o.mid,
                  "bid_ms": r.get("bid_ms"), "ask_ms": r.get("ask_ms"),
                  "bid_size": r.get("bid_size"), "ask_size": r.get("ask_size"),
                  "quote_age_s": o.quote_age_s, "spot": r.get("spot"),
                  "volume": r.get("volume"), "oi": r.get("oi")}
                 for r, o in zip(rows, obs)],
        "trade_bars": (record or {}).get("bars"),
    }


def _hour_bucket(ms: Any) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, ET).strftime("%H:00")


def _moneyness_bucket(row: dict[str, Any]) -> str | None:
    pct = row.get("strike_vs_spot_pct")
    if pct is None:
        return None
    # Signed so positive means out of the money for both calls and puts.
    otm = pct if row["option_type"] == "call" else -pct
    for edge, label in ((-0.05, "ITM >5%"), (-0.01, "ITM 1-5%"), (0.01, "ATM ±1%"),
                        (0.05, "OTM 1-5%"), (0.10, "OTM 5-10%")):
        if otm < edge:
            return label
    return "OTM >10%"


def readout(day_dirs: list[Path], top: int = 100, clean_only: bool = True) -> str:
    """Where outsized moves concentrate, across one or more archived days."""
    winners: list[dict[str, Any]] = []
    universe_rows = 0
    for day_dir in day_dirs:
        with gzip.open(day_dir / "contracts.csv.gz", "rt", encoding="utf-8") as src:
            for row in csv.DictReader(src):
                universe_rows += 1
                key = "clean_rank" if clean_only else "rank"
                if row.get(key) and int(row[key]) <= top:
                    row["strike_vs_spot_pct"] = float(row["strike_vs_spot_pct"]) if row.get("strike_vs_spot_pct") else None
                    row["days_to_expiration"] = (date.fromisoformat(row["expiration"]) - date.fromisoformat(day_dir.name)).days \
                        if _is_date(day_dir.name) else None
                    winners.append(row)
    lines = [f"# OA-203 readout — {', '.join(d.name for d in day_dirs)}", "",
             f"Top {top} {'clean ' if clean_only else ''}contracts per day by mid first-to-max return "
             f"({len(winners)} rows out of {universe_rows} contracts sampled).", ""]
    dims = [
        ("Underlying", lambda r: r["underlying"]),
        ("Call / put", lambda r: r["option_type"]),
        ("Strike vs spot at entry", _moneyness_bucket),
        ("Days to expiration", lambda r: r.get("days_to_expiration")),
        ("Entry hour (ET)", lambda r: _hour_bucket(r.get("mid_first_to_max_entry_ms"))),
        ("Exit hour (ET)", lambda r: _hour_bucket(r.get("mid_first_to_max_exit_ms"))),
    ]
    for title, key in dims:
        counts = Counter(key(r) for r in winners)
        lines += [f"## {title}", "", "| Value | Count | Share |", "|---|---:|---:|"]
        for value, count in counts.most_common(15):
            lines.append(f"| {value} | {count} | {count / max(1, len(winners)):.0%} |")
        lines.append("")
    return "\n".join(lines)
