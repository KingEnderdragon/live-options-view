#!/usr/bin/env python3
"""Clyde: strategy discovery and account assignment CLI (issue #25).

Standalone, offline. Clyde classifies strategy names into four categories
and assigns only REGISTRY-registered strategy_ids to accounts in the
gitignored accounts.json. It never fetches, clones, vendors, or executes
upstream strategy code, and it never changes runner.py, client.py, or the
strategy execution contract.

Categories (descriptive metadata only; only REGISTRY authorizes assignment):
  registered  -- implemented and currently registered in REGISTRY.
  candidate   -- fits the present single-instrument execution model but is
                 not implemented/registered yet.
  overlay     -- a reusable position-management policy, not a complete
                 entry strategy; not assignable until composition exists.
  research    -- useful source material that does not fit the current
                 runtime without materially changing the strategy.

Usage (from crassus/):
    python scripts/clyde.py catalogue
    python scripts/clyde.py accounts [--accounts FILE]
    python scripts/clyde.py assign --strategy ID --account ALIAS [--account ALIAS ...]
        [--all] [--accounts FILE] [--dry-run] [--backup FILE]
    python scripts/clyde.py recover --backup FILE --accounts FILE

Assignment rules: only a strategy_id currently in REGISTRY can be assigned.
Implementation candidates, overlays, research-only names, and unknown names
are refused and the file is left unchanged. Only the selected accounts'
strategy_id fields change; aliases, ordering, params, credentials, and all
unrelated data are preserved as values, in their original order.
Serialization may reformat whitespace, so what is preserved is values and
ordering, not exact bytes.
Writes are validated and atomic (temp file + rename); a backup is written
before replacement unless --no-backup is given. Missing or malformed
configuration leaves the file unchanged. Passwords are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus import strategies as _strategies  # noqa: E402,F401  (import registers)
from crassus.strategy import REGISTRY  # noqa: E402

# ---------------------------------------------------------------------------
# Catalogue (descriptive metadata from issue #25; not authorization).
# Registered entries are discovered from REGISTRY at runtime; the names
# below for candidate/overlay/research are the issue's curated list.
# ---------------------------------------------------------------------------

CATALOGUE: list[dict[str, str]] = [
    # Implementation candidates (issue Step 1A): single-instrument signal
    # concepts reasonably compatible with the present decision shape.
    {"name": "HourBasedStrategy", "category": "candidate",
     "fit": "Can use the context already available today (clock/session); no rolling OHLC history needed."},
    {"name": "Supertrend", "category": "candidate",
     "fit": "Requires a shared, tested rolling OHLC-history facility before implementation."},
    {"name": "UniversalMACD", "category": "candidate",
     "fit": "Requires a shared, tested rolling OHLC-history facility before implementation."},
    {"name": "TrendRiderStrategy", "category": "candidate",
     "fit": "Requires a shared, tested rolling OHLC-history facility before implementation."},
    {"name": "HLHB", "category": "candidate",
     "fit": "Requires a shared, tested rolling OHLC-history facility before implementation."},
    {"name": "PatternRecognition", "category": "candidate",
     "fit": "Requires a shared, tested rolling OHLC-history facility before implementation."},
    # Risk/exit overlays (issue Step 1B): position-management policies.
    {"name": "BreakEven", "category": "overlay",
     "fit": "Position-management policy, not a complete entry strategy; non-assignable until strategy composition is supported."},
    {"name": "FixedRiskRewardLoss", "category": "overlay",
     "fit": "Position-management policy, not a complete entry strategy; non-assignable until strategy composition is supported."},
    {"name": "CustomStoplossWithPSAR", "category": "overlay",
     "fit": "Position-management policy, not a complete entry strategy; non-assignable until strategy composition is supported."},
    # Research-only references (issue Step 1C).
    {"name": "Bandtastic", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "Diamond", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "GodStra", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "Heracles", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "InformativeSample", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "MultiMa", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "PowerTower", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "SwingHighToSky", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "MabStra", "category": "research",
     "fit": "Research-only reference; no Crassus-compatible design approved."},
    {"name": "Time Series Momentum Effect", "category": "research",
     "fit": "Research-only reference; cross-sectional/multi-asset systems are not portable as the same strategy."},
    {"name": "Asset Class Trend-Following", "category": "research",
     "fit": "Research-only reference; cross-sectional/multi-asset systems are not portable as the same strategy."},
    {"name": "Paired Switching", "category": "research",
     "fit": "Research-only reference; replacing a pair with QQQ does not preserve pairs trading."},
    {"name": "Asset Growth Effect", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Short Term Reversal Effect in Stocks", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Reversal During Earnings-Announcements", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Low Volatility Factor Effect in Stocks", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Volatility Risk Premium Effect", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Pairs Trading with Stocks", "category": "research",
     "fit": "Research-only reference; replacing a stock pair with QQQ does not preserve pairs trading."},
    {"name": "Betting Against Beta Factor in Stocks", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Trend-following Effect in Stocks", "category": "research",
     "fit": "Research-only reference; cross-sectional equity-factor strategy needing a stock universe."},
    {"name": "Overnight Seasonality in Bitcoin", "category": "research",
     "fit": "Research-only reference; transferring Bitcoin seasonality to QQQ creates a new hypothesis, not a port."},
    {"name": "FX Carry Trade", "category": "research",
     "fit": "Research-only reference; needs FX spot/forwards, not a single-underlying options loop."},
]

DEFAULT_ACCOUNTS = Path(__file__).resolve().parent.parent / "accounts.json"


def registered_strategy_ids() -> list[str]:
    """Executable IDs discovered from the current REGISTRY (the truth)."""
    return sorted(REGISTRY)


def catalogue_entries() -> list[dict[str, str]]:
    """Full catalogue: registered IDs first, then candidate/overlay/research."""
    entries = [
        {"name": sid, "category": "registered",
         "fit": "Implemented and currently registered in REGISTRY; assignable."}
        for sid in registered_strategy_ids()
    ]
    return entries + list(CATALOGUE)


def catalogue_category(name: str) -> str | None:
    """Category of a name, or None if it is not in the catalogue at all."""
    for entry in catalogue_entries():
        if entry["name"] == name:
            return entry["category"]
    return None


def load_accounts_file(path: Path) -> dict:
    """Read and parse an accounts JSON file. Raises on missing/malformed."""
    if not path.exists():
        raise FileNotFoundError(f"No accounts file at {path}.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed accounts file {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError(f"Malformed accounts file {path}: expected an object with an 'accounts' list.")
    return data


def validate_accounts(data: dict) -> list[str]:
    """Return a list of structural problems (empty when the file is usable)."""
    problems: list[str] = []
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        return ["'accounts' is not a list"]
    seen_aliases: set[str] = set()
    for i, entry in enumerate(accounts):
        if not isinstance(entry, dict):
            problems.append(f"accounts[{i}] is not an object")
            continue
        for key in ("alias", "username", "strategy_id"):
            if not entry.get(key):
                problems.append(f"accounts[{i}] missing {key!r}")
        alias = entry.get("alias")
        if alias:
            if alias in seen_aliases:
                problems.append(f"duplicate alias {alias!r}")
            seen_aliases.add(alias)
    return problems


def select_accounts(data: dict, aliases: list[str] | None, all_accounts: bool) -> list[dict]:
    """Resolve one/multiple/all selection to account entries (by alias)."""
    accounts = data["accounts"]
    if all_accounts:
        return list(accounts)
    wanted = set(aliases or [])
    selected = [a for a in accounts if a.get("alias") in wanted]
    missing = wanted - {a.get("alias") for a in selected}
    if missing:
        raise ValueError(f"Unknown account alias(es): {sorted(missing)}")
    return selected


def assign_strategy(data: dict, strategy_id: str, aliases: list[str] | None,
                    all_accounts: bool = False) -> tuple[dict, list[str]]:
    """Return (new_data, changed_aliases). Only strategy_id changes.

    Refuses anything not currently in REGISTRY (candidates, overlays,
    research-only names, and unknown names). The input dict is not mutated.
    """
    category = catalogue_category(strategy_id)
    if strategy_id not in REGISTRY:
        if category in ("candidate", "overlay", "research"):
            raise ValueError(
                f"Refusing {strategy_id!r}: it is a {category} entry, not a registered "
                "strategy. Catalogue classification is descriptive metadata, never "
                "authorization to assign.")
        raise ValueError(
            f"Refusing unknown strategy {strategy_id!r}: not in REGISTRY "
            f"(registered: {', '.join(registered_strategy_ids())}).")
    selected = select_accounts(data, aliases, all_accounts)
    if not selected:
        raise ValueError("No accounts selected.")
    new_data = json.loads(json.dumps(data))  # deep copy preserving order
    by_alias = {a["alias"]: a for a in new_data["accounts"]}
    changed: list[str] = []
    for entry in selected:
        target = by_alias[entry["alias"]]
        if target.get("strategy_id") != strategy_id:
            target["strategy_id"] = strategy_id
            changed.append(entry["alias"])
    return new_data, changed


def preview_changes(data: dict, strategy_id: str, aliases: list[str] | None,
                    all_accounts: bool = False) -> list[str]:
    """Human-readable dry-run lines (alias, username, old -> new). No secrets."""
    selected = select_accounts(data, aliases, all_accounts)
    lines = []
    for entry in selected:
        old = entry.get("strategy_id")
        mark = " (unchanged)" if old == strategy_id else ""
        lines.append(f"  {entry.get('alias')} ({entry.get('username')}): {old} -> {strategy_id}{mark}")
    return lines


def write_accounts_atomic(path: Path, data: dict, backup_path: Path | None = None) -> None:
    """Validate, then atomically replace path (temp file + rename).

    A backup of the pre-change bytes is written first when backup_path is
    given. The rewritten file is re-read and validated before replacement.
    """
    problems = validate_accounts(data)
    if problems:
        raise ValueError(f"Refusing to write invalid accounts data: {problems}")
    text = json.dumps(data, indent=2) + "\n"
    # Validate the exact bytes we are about to write.
    json.loads(text)
    if backup_path is not None and path.exists():
        shutil.copyfile(path, backup_path)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    try:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        # Close the handle first: on Windows an open file cannot be
        # deleted, so cleanup must close before unlinking or the temp
        # file is left behind after a flush/fsync failure.
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def recover_from_backup(backup_path: Path, accounts_path: Path) -> None:
    """Restore accounts_path from a backup file (validated before replace)."""
    if not backup_path.exists():
        raise FileNotFoundError(f"No backup file at {backup_path}.")
    data = load_accounts_file(backup_path)
    problems = validate_accounts(data)
    if problems:
        raise ValueError(f"Backup is invalid, refusing to restore: {problems}")
    write_accounts_atomic(accounts_path, data, backup_path=None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _accounts_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS,
                        help="accounts JSON file (default: gitignored accounts.json)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clyde",
        description="Clyde: offline strategy catalogue and account assignment (issue #25).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cat = sub.add_parser("catalogue", help="print the classified catalogue")
    p_cat.add_argument("--registered-only", action="store_true",
                       help="show only REGISTRY-registered (assignable) entries")

    p_acc = sub.add_parser("accounts", help="list accounts and their strategy_id")
    _accounts_arg(p_acc)

    p_asn = sub.add_parser("assign", help="assign a registered strategy to accounts")
    p_asn.add_argument("--strategy", required=True, help="strategy_id to assign")
    p_asn.add_argument("--account", action="append", default=[],
                       help="account alias to change (repeatable)")
    p_asn.add_argument("--all", action="store_true", help="change all accounts")
    p_asn.add_argument("--dry-run", action="store_true",
                       help="preview only; do not write")
    p_asn.add_argument("--backup", type=Path, default=None,
                       help="backup path (default: <accounts>.bak)")
    p_asn.add_argument("--no-backup", action="store_true",
                       help="do not write a backup before replacing")
    _accounts_arg(p_asn)

    p_rec = sub.add_parser("recover", help="restore accounts from a backup file")
    p_rec.add_argument("--backup", type=Path, required=True)
    _accounts_arg(p_rec)

    args = parser.parse_args(argv)

    try:
        if args.command == "catalogue":
            for entry in catalogue_entries():
                if args.registered_only and entry["category"] != "registered":
                    continue
                print(f"[{entry['category']}] {entry['name']} -- {entry['fit']}")
            return 0

        if args.command == "accounts":
            data = load_accounts_file(args.accounts)
            problems = validate_accounts(data)
            if problems:
                print(f"accounts file has problems: {problems}", file=sys.stderr)
                return 2
            for entry in data["accounts"]:
                print(f"{entry.get('alias')} ({entry.get('username')}): "
                      f"{entry.get('strategy_id')}")
            return 0

        if args.command == "assign":
            if not args.all and not args.account:
                print("assign needs --account ALIAS (repeatable) or --all.", file=sys.stderr)
                return 2
            if args.all and args.account:
                print("assign: --account and --all are mutually exclusive.",
                      file=sys.stderr)
                return 2
            data = load_accounts_file(args.accounts)
            problems = validate_accounts(data)
            if problems:
                print(f"accounts file has problems: {problems}", file=sys.stderr)
                return 2
            if args.dry_run:
                # Dry-run runs the SAME registry/selection validation as
                # apply (assign_strategy raises for unassignable/unknown
                # strategy or unknown alias); the file is never written.
                assign_strategy(data, args.strategy, args.account, args.all)
                for line in preview_changes(data, args.strategy, args.account, args.all):
                    print(line)
                print("(dry run: no file written)")
                return 0
            new_data, changed = assign_strategy(data, args.strategy, args.account, args.all)
            if not changed:
                print("No changes: selected accounts already use "
                      f"{args.strategy}.")
                return 0
            backup = None if args.no_backup else (
                args.backup or args.accounts.with_suffix(".json.bak"))
            write_accounts_atomic(args.accounts, new_data, backup_path=backup)
            print(f"Assigned {args.strategy} to {len(changed)} account(s): "
                  f"{', '.join(changed)}")
            if backup is not None:
                print(f"Backup written to {backup}")
            return 0

        if args.command == "recover":
            recover_from_backup(args.backup, args.accounts)
            print(f"Restored {args.accounts} from {args.backup}")
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"clyde: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
