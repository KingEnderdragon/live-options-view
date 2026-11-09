"""Historical 132-stock/watchlist research only; not the product registry.

Accepted exact-100 membership lives in market_data.overview. Keep this legacy
proposal for explicit later-scope fixtures and the separately gated symbol probe.
Shared math has one canonical runtime home; imports remain compatible.
"""
import json
from pathlib import Path
from typing import Mapping, Sequence
from market_data.overview.calculations import *  # noqa: F401,F403

UNIVERSE_PATH = Path(__file__).with_name("universe.json")

# -- universe -------------------------------------------------------------


def load_universe(path: Path = UNIVERSE_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def baseline_members(config: Mapping) -> list[dict]:
    """Every baseline stock with its group and origin tag, in config order."""
    return [{"symbol": s, "group": g["id"], "origin": "baseline"}
            for g in config["groups"] for s in g["symbols"]]


def validate_universe(config: Mapping) -> dict:
    """Structural checks; returns counts and a list of errors (empty == valid)."""
    errors: list[str] = []
    members = baseline_members(config)
    symbols = [m["symbol"] for m in members]
    seen: set[str] = set()
    for s in symbols:
        if s in seen:
            errors.append(f"duplicate_symbol:{s}")
        seen.add(s)
        if not SYMBOL_RE.match(s):
            errors.append(f"malformed_symbol:{s}")
    aliases = config.get("share_classes", {}).get("company_aliases", {})
    companies = {aliases.get(s, s) for s in seen}
    lo, hi = config["size_bounds"]["min"], config["size_bounds"]["max"]
    if not lo <= len(companies) <= hi:
        errors.append(f"company_count_out_of_bounds:{len(companies)}")
    group_ids = [g["id"] for g in config["groups"]]
    if len(set(group_ids)) != len(group_ids):
        errors.append("duplicate_group_id")
    for g in config["groups"]:
        if not g.get("rationale"):
            errors.append(f"missing_rationale:{g['id']}")
    macro_ids = [m["id"] for m in config.get("macro", [])]
    if len(set(macro_ids)) != len(macro_ids):
        errors.append("duplicate_macro_id")
    return {"symbols": len(symbols), "companies": len(companies),
            "groups": len(group_ids), "errors": errors}


def with_watchlist(config: Mapping, additions: Sequence[str]) -> dict:
    """Baseline plus user additions, each addition with an explicit status.

    Additions supplement the baseline (never replace it), carry origin
    'watchlist', and beyond the cap are rejected rather than truncated
    silently.
    """
    members = baseline_members(config)
    baseline = {m["symbol"] for m in members}
    cap = config["watchlist"]["additions_cap"]
    statuses: list[dict] = []
    accepted: list[str] = []
    for raw in additions:
        s = str(raw).strip().upper()
        if not SYMBOL_RE.match(s):
            statuses.append({"symbol": s, "status": "malformed"})
        elif s in baseline:
            statuses.append({"symbol": s, "status": "already_in_baseline"})
        elif s in accepted:
            statuses.append({"symbol": s, "status": "duplicate_addition"})
        elif len(accepted) >= cap:
            statuses.append({"symbol": s, "status": "over_cap"})
        else:
            accepted.append(s)
            statuses.append({"symbol": s, "status": "accepted"})
    members += [{"symbol": s, "group": "watchlist", "origin": "watchlist"} for s in accepted]
    return {"members": members, "additions": statuses}
