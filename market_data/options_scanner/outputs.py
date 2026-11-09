"""Leaderboard rows and the CSV/JSON artifacts built from them.

Offline readout, the post-close leaderboard and the intraday active snapshot
all rank the same per-contract rows, so the row builder lives here once.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

from . import runtime
from .archive import _read_jsonl_gz, _read_manifest, iter_rows, published
from .returns import (
    ContractInfo,
    Observation,
    QUALIFIED,
    QualifiedPolicy,
    ReturnPolicy,
    contract_metrics,
    rank,
    trade_bar_metrics,
)


# --------------------------------------------------------------------------
# Leaderboard build (live and offline share this path)
# --------------------------------------------------------------------------

def _contract_paths(day_dir: Path, start_ms: int | None, end_ms: int | None,
                    buckets: int = 64, damaged: list[dict[str, Any]] | None = None,
                    sweep_files: list[Path] | None = None,
                    strict_manifest: list[dict[str, Any]] | None = None
                    ) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Group archived rows by contract with bounded memory (hash-partitioned temp files)."""
    with tempfile.TemporaryDirectory(prefix="oa203-build-") as tmp:
        handles = [open(Path(tmp) / f"b{i:02d}.jsonl", "w", encoding="utf-8") for i in range(buckets)]
        try:
            for row in iter_rows(day_dir, damaged, sweep_files, strict_manifest):
                if not row.get("symbol"):
                    continue
                # Preserve the established daily leaderboard and backfill
                # universe. The extra raw rows are reserved for windows.
                if row.get("collection_role") == "additional_expiry":
                    continue
                if (start_ms and row["t"] < start_ms) or (end_ms and row["t"] >= end_ms):
                    continue
                bucket = int(hashlib.md5(row["symbol"].encode()).hexdigest(), 16) % buckets
                handles[bucket].write(json.dumps(row, separators=(",", ":")) + "\n")
        finally:
            for h in handles:
                h.close()
        for i in range(buckets):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            with open(Path(tmp) / f"b{i:02d}.jsonl", encoding="utf-8") as src:
                for line in src:
                    row = json.loads(line)
                    grouped[row["symbol"]].append(row)
            for symbol in sorted(grouped):
                yield symbol, sorted(grouped[symbol], key=lambda r: (r["t"], r["sweep"]))


def chain_ok_sweeps(manifest: list[dict[str, Any]]) -> Counter:
    ok: Counter = Counter()
    for entry in manifest:
        for underlying, status in entry.get("chains", {}).items():
            if status.get("status") == "ok":
                ok[underlying] += 1
    return ok


def chain_ok_prefixes(manifest: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    """Successful chain sweeps known by each sweep, never a day-end denominator."""
    counts: Counter = Counter()
    prefixes = {}
    expected_sweep = 1
    complete_prefix = True
    for entry in sorted(manifest, key=lambda m: m.get("sweep") if type(m.get("sweep")) is int else -1):
        sweep = entry.get("sweep")
        if not isinstance(sweep, int):
            continue
        if sweep != expected_sweep:
            complete_prefix = False  # a missing/corrupt manifest line cannot certify coverage
        expected_sweep = sweep + 1
        for underlying, status in entry.get("chains", {}).items():
            if status.get("status") == "ok":
                counts[underlying] += 1
            if complete_prefix:
                prefixes[(underlying, sweep)] = counts[underlying]
    return prefixes


def load_backfill(day_dir: Path, damaged: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    path = day_dir / "backfill_timesales.jsonl.gz"
    if not path.exists():
        return {}
    return {record["symbol"]: record for record in _read_jsonl_gz(path, day_dir, damaged)
            if isinstance(record, dict) and record.get("symbol")}


def build_contract_rows(day_dir: Path, policy: ReturnPolicy, start_ms: int | None = None,
                        end_ms: int | None = None,
                        damaged: list[dict[str, Any]] | None = None,
                        qualified_policy: QualifiedPolicy = QualifiedPolicy(),
                        cutoff_sweep: int | None = None) -> list[dict[str, Any]]:
    """Rank every archived contract; damaged inputs are salvaged and recorded in ``damaged``."""
    manifest = _read_manifest(day_dir, damaged, cutoff_sweep)
    if cutoff_sweep is not None:
        manifest = [m for m in manifest if type(m.get("sweep")) is int and 1 <= m["sweep"] <= cutoff_sweep]
        if [m["sweep"] for m in manifest] != list(range(1, cutoff_sweep + 1)):
            raise ValueError("cutoff manifest is incomplete or out of order")
        sweep_files = [day_dir / "sweeps" / f"sweep_{m['sweep']:04d}.jsonl.gz" for m in manifest]
        if any(not p.is_file() for p in sweep_files):
            raise FileNotFoundError("cutoff sweep file is missing")
    else:
        sweep_files = None
    ok = chain_ok_sweeps(manifest)
    prefix_ok = chain_ok_prefixes(manifest)
    backfill = {} if cutoff_sweep is not None else load_backfill(day_dir, damaged)
    rows = []
    for symbol, path_rows in _contract_paths(day_dir, start_ms, end_ms, damaged=damaged,
                                             sweep_files=sweep_files, strict_manifest=manifest if cutoff_sweep is not None else None):
        last = path_rows[-1]
        info = ContractInfo(
            symbol=symbol, underlying=last["underlying"], option_type=last["type"],
            strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
            contract_size=int(last.get("contract_size") or 100),
            volume=last.get("volume"), open_interest=path_rows[0].get("oi"),
            chain_ok_sweeps=ok.get(last["underlying"], 0),
        )
        observations = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"),
                                    ask_ms=r.get("ask_ms"), underlying_price=r.get("spot"),
                                    bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                                    volume=r.get("volume"), open_interest=r.get("oi"),
                                    chain_ok_sweeps_so_far=prefix_ok.get((r["underlying"], r.get("sweep"))))
                        for r in path_rows]
        row = contract_metrics(info, observations, policy, qualified_policy)
        record = backfill.get(symbol)
        if record is not None:
            row["backfill_status"] = "error" if record.get("error") else "ok"
            if not record.get("error"):
                row.update(trade_bar_metrics(record.get("bars") or [], info.contract_size, policy))
        rows.append(row)
    return rank(rows)


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, float):
        return repr(round(value, 8))
    return value


def write_outputs(day_dir: Path, rows: list[dict[str, Any]], top: int = 200,
                  include_contracts: bool = True) -> dict[str, str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    if include_contracts:
        with published(day_dir / "contracts.csv.gz") as tmp, gzip.open(tmp, "wt", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _cell(row.get(k)) for k in columns})
    board = [r for r in rows if (r.get("rank") or 10**9) <= top or (r.get("clean_rank") or 10**9) <= top]
    with published(day_dir / "leaderboard.csv") as tmp,             open(tmp, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in board:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    qualified_board = sorted((r for r in rows if r.get(f"{QUALIFIED}_rank") is not None),
                             key=lambda r: r[f"{QUALIFIED}_rank"])[:top]
    with published(day_dir / "qualified_ask_bid_v1.csv") as tmp, open(tmp, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in qualified_board:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    return {**({"contracts": "contracts.csv.gz"} if include_contracts else {}),
            "leaderboard": "leaderboard.csv", "qualified_ask_bid_v1": "qualified_ask_bid_v1.csv"}
