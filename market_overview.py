"""Pure cross-market overview math for issue #132.

No I/O, no provider calls, no global state -- the same shape as
market_signals.py. Every function takes explicit inputs (observations, a
session schedule, the universe config) and returns plain values, so the
arithmetic, session and breadth rules can be pinned with hand-built
fixtures in tests/test_market_overview.py before any producer or MCP code
depends on them.

What this module decides:

- **Change arithmetic.** absolute = current - reference; percent =
  100 * absolute / reference. Missing, nonfinite or zero-denominator inputs
  produce an explicit unavailable reason, never a fabricated 0.0 move.
  Yields report their move in basis points ((current - reference) * 100 for
  values in percent units) and no percent-of-a-yield. Dollar change is only
  labeled dollars when the instrument unit is USD.
- **Identity.** A change is only computed when both observations describe
  the same instrument (and, for dated futures, the same contract) under the
  same adjustment basis. A fallback source with a different identity -- USO
  for crude, yen futures for spot USD/JPY -- is incompatible, not a
  substitute.
- **Scale and inversion.** Provider scale (e.g. CBOE's $TNX at yield x 10)
  is normalized exactly once via `normalize`. FX inversion inverts both the
  current and the reference value before recomputing change; a percentage
  is never simply negated.
- **Sessions and freshness.** US equity observations are classified as
  premarket / regular / postmarket / closed against an explicit schedule
  (holidays and early closes come from the caller's exchange calendar).
  Freshness is separate from availability: a closed market's last value is
  'closed', not fresh, and a newer failed attempt is surfaced alongside a
  cached success.
- **Breadth.** Sector/group summaries are computed over the full eligible
  curated sample before any paging or top-N, keep their denominator and
  missing/stale counts, and exclude watchlist additions unless asked.

Nothing here selects providers or symbols; see
CROSS_MARKET_OVERVIEW.md for the coverage matrix and the
open provider decisions.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UNIVERSE_PATH = Path(__file__).resolve().parent / "market_overview_universe.json"

# Units whose absolute change is a dollar amount. Everything else (index
# points, JPY per USD, percent yields...) keeps its own unit label.
USD_UNITS = frozenset({"USD", "USD_per_barrel", "USD_per_troy_ounce", "USD_per_BTC"})

# Reference bases a change may be computed against. Extended-hours values are
# compared to the previous *regular* close and say so; a rolling 24h move is
# deliberately not a supported basis.
REFERENCE_BASES = frozenset({
    "previous_regular_close",   # equities / ETFs / cash indices
    "previous_settlement",      # dated futures
    "utc_day_open",             # 24/7 crypto, documented 00:00 UTC cut
    "previous_observation",     # daily yield / FX fixings: prior observation date
})

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z])?$")


# -- observations ---------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One provider value for one instrument identity.

    `value` is already normalized to the instrument's declared unit (see
    `normalize`). `contract` is the exact dated contract for futures and None
    otherwise. `session` is the session the value was observed in
    ('regular', 'premarket', 'postmarket', 'continuous', 'settlement',
    'fixing'); `observed_at` is the provider's event/observation time --
    never retrieval time -- and is None when the provider gives none.
    """

    instrument_id: str
    value: Optional[float]
    observed_at: Optional[datetime]
    source: str
    session: Optional[str] = None
    contract: Optional[str] = None
    adjustment_basis: Optional[str] = None


def normalize(raw: Optional[float], divisor: float = 1.0) -> Optional[float]:
    """Apply a provider's documented scale exactly once.

    CBOE yield indices quote the yield x 10 ($TNX 45.0 == 4.50%), so their
    divisor is 10. Returns None for missing/nonfinite input rather than a
    number derived from it.
    """
    if raw is None or divisor in (0, None):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value / divisor


def invert(obs: Observation, instrument_id: str) -> Observation:
    """Invert an FX quote (e.g. USD-per-JPY futures -> JPY per USD).

    The inverted observation gets the *target* instrument identity so it can
    only be compared against another observation inverted the same way.
    Zero/missing values stay missing.
    """
    v = obs.value
    inverted = 1.0 / v if v is not None and math.isfinite(v) and v != 0 else None
    return replace(obs, instrument_id=instrument_id, value=inverted)


def _finite(v: Optional[float]) -> bool:
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


def compute_change(instrument: Mapping, current: Observation, reference: Observation,
                   basis: str) -> dict:
    """Change of `current` versus `reference` for one instrument.

    `instrument` is a universe/macro entry (needs `id`, `unit`,
    `instrument_type`; yields carry `change_unit: "bps"`). Returns a dict
    with status 'ok' or 'unavailable', the reasons, and only the change
    fields that are valid -- an unavailable percent is None, never 0.0.
    """
    if basis not in REFERENCE_BASES:
        raise ValueError(f"unsupported reference basis: {basis}")
    iid, unit = instrument["id"], instrument["unit"]
    is_yield = instrument.get("change_unit") == "bps"
    reasons: list[str] = []

    if current.instrument_id != iid or reference.instrument_id != iid:
        reasons.append("identity_mismatch")
    if instrument.get("instrument_type") == "future_dated":
        if not current.contract or current.contract != reference.contract:
            reasons.append("contract_mismatch")
    if (current.adjustment_basis is not None and reference.adjustment_basis is not None
            and current.adjustment_basis != reference.adjustment_basis):
        reasons.append("adjustment_mismatch")
    if basis == "previous_regular_close" and reference.session not in (None, "regular", "settlement"):
        # An extended-hours print is not a close; comparing to it would mix sessions.
        reasons.append("reference_not_regular_close")
    if not _finite(current.value):
        reasons.append("missing_current")
    if not _finite(reference.value):
        reasons.append("missing_reference")

    result = {
        "instrument_id": iid,
        "unit": unit,
        "basis": basis,
        "current": current.value if _finite(current.value) else None,
        "reference": reference.value if _finite(reference.value) else None,
        "current_session": current.session,
        "current_observed_at": current.observed_at,
        "reference_observed_at": reference.observed_at,
        "absolute": None,
        "absolute_unit": None,
        "percent": None,
        "bps": None,
    }
    if reasons:
        return {**result, "status": "unavailable", "reasons": reasons}

    absolute = current.value - reference.value
    result["absolute"] = absolute
    result["absolute_unit"] = "USD" if unit in USD_UNITS else unit
    if is_yield:
        result["bps"] = absolute * 100.0
        return {**result, "status": "ok", "reasons": []}
    if reference.value == 0:
        return {**result, "status": "partial", "reasons": ["zero_reference"]}
    result["percent"] = 100.0 * absolute / reference.value
    return {**result, "status": "ok", "reasons": []}


# -- sessions and freshness -----------------------------------------------


@dataclass(frozen=True)
class SessionSchedule:
    """One exchange trading day. None-valued opens mean the day is closed."""

    day: str                       # ISO date in exchange time
    regular_open: Optional[datetime]
    regular_close: Optional[datetime]
    extended_open: Optional[datetime] = None
    extended_close: Optional[datetime] = None
    early_close: bool = False


def us_equity_schedule(day: str, regular_open: Optional[time] = time(9, 30),
                       regular_close: Optional[time] = time(16, 0)) -> SessionSchedule:
    """Build a US equity schedule from explicit times (fixtures, calendar adapter).

    Pass regular_open=None for a holiday; pass regular_close=time(13, 0) for an
    early close. Extended hours are 04:00-20:00 ET on trading days.
    """
    d = datetime.fromisoformat(day).date()
    if regular_open is None or regular_close is None:
        return SessionSchedule(day, None, None)
    at = lambda t: datetime.combine(d, t, ET)  # noqa: E731
    return SessionSchedule(day, at(regular_open), at(regular_close),
                           at(time(4, 0)), at(time(20, 0)),
                           early_close=regular_close < time(16, 0))


def classify_session(observed_at: datetime, schedule: SessionSchedule) -> str:
    """premarket / regular / postmarket / closed for one US equity observation."""
    if schedule.regular_open is None:
        return "closed"
    t = observed_at.astimezone(ET)
    if schedule.regular_open <= t < schedule.regular_close:
        return "regular"
    if schedule.extended_open and schedule.extended_open <= t < schedule.regular_open:
        return "premarket"
    if schedule.extended_close and schedule.regular_close <= t < schedule.extended_close:
        return "postmarket"
    return "closed"


def freshness(observed_at: Optional[datetime], now: datetime, market_open: bool,
              max_age: timedelta, newer_failed_attempt_at: Optional[datetime] = None,
              future_skew: timedelta = timedelta(seconds=5)) -> dict:
    """Freshness of one observation, independent of whether it is available.

    market_open is per instrument (crypto is always open; an equity on a
    weekend is not), so a fresh Sunday BTC print never makes Friday's equity
    close fresh. A closed market's last value is 'closed', not 'fresh'.
    """
    warnings: list[str] = []
    if newer_failed_attempt_at is not None and (
            observed_at is None or newer_failed_attempt_at > observed_at):
        warnings.append("newer_attempt_failed")
    if observed_at is None:
        return {"status": "unknown_time", "age_seconds": None, "warnings": warnings}
    age = (now - observed_at).total_seconds()
    if observed_at - now > future_skew:
        return {"status": "future_dated", "age_seconds": age, "warnings": warnings}
    if not market_open:
        status = "closed"
    elif age > max_age.total_seconds():
        status = "stale"
    else:
        status = "fresh"
    return {"status": status, "age_seconds": age, "warnings": warnings}


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


# -- breadth and movers ---------------------------------------------------


def breadth(members: Iterable[Mapping], changes: Mapping[str, Mapping],
            group: Optional[str] = None, include_watchlist: bool = False,
            stale: Iterable[str] = ()) -> dict:
    """Advancing/declining/unchanged within the curated sample.

    The denominator is every eligible member (filtered by group/origin),
    including those with missing or stale data, so a 60%-covered group can't
    read as complete. Mean/median are unweighted over available percent
    changes. Mixed reference bases or sessions are reported, not hidden.
    """
    stale = set(stale)
    eligible = [m for m in members
                if (include_watchlist or m["origin"] == "baseline")
                and (group is None or m["group"] == group)]
    adv = dec = unch = missing = n_stale = 0
    pcts: list[float] = []
    bases: set[str] = set()
    sessions: set[str] = set()
    for m in eligible:
        ch = changes.get(m["symbol"])
        if ch is None or ch.get("percent") is None:
            missing += 1
            continue
        if m["symbol"] in stale:
            n_stale += 1
            continue
        p = ch["percent"]
        pcts.append(p)
        bases.add(ch["basis"])
        if ch.get("current_session"):
            sessions.add(ch["current_session"])
        if p > 0:
            adv += 1
        elif p < 0:
            dec += 1
        else:
            unch += 1
    warnings = []
    if len(bases) > 1:
        warnings.append("mixed_reference_basis")
    if len(sessions) > 1:
        warnings.append("mixed_sessions")
    return {
        "label": "breadth within the curated sample",
        "group": group,
        "include_watchlist": include_watchlist,
        "weighting": "equal (unweighted)",
        "denominator": len(eligible),
        "advancing": adv, "declining": dec, "unchanged": unch,
        "missing": missing, "stale": n_stale,
        "mean_percent": statistics.fmean(pcts) if pcts else None,
        "median_percent": statistics.median(pcts) if pcts else None,
        "reference_bases": sorted(bases),
        "sessions": sorted(sessions),
        "warnings": warnings,
    }


def _iso(t: Optional[datetime]) -> Optional[str]:
    return t.isoformat() if t is not None else None


# -- published artifact (intraday/overview.json, schema 1) ----------------
#
# The producer computes change and publication-time freshness with the rules
# above; the market MCP consumer (market_mcp/consumer.js) preserves those
# values, recomputes only query-time age, and never re-derives a change.

OVERVIEW_KEY = "intraday/overview.json"
OVERVIEW_SCHEMA_VERSION = 1
ROW_STATUSES = ("ok", "partial", "unavailable")
CYCLE_STATUSES = ("complete", "partial", "failed")


def overview_row(instrument: Mapping, *, group: Optional[str], origin: str,
                 current: Observation, reference: Observation, basis: str,
                 now: datetime, market_open: bool, max_age: timedelta,
                 provider_symbol: Optional[str] = None,
                 newer_failed_attempt_at: Optional[datetime] = None) -> dict:
    """One published row: identity, both observations, change and freshness."""
    if origin not in ("baseline", "watchlist", "macro"):
        raise ValueError(f"unknown origin: {origin}")
    change = compute_change(instrument, current, reference, basis)
    return {
        "id": instrument["id"],
        "symbol": instrument.get("symbol", instrument["id"]),
        "label": instrument.get("label", instrument["id"]),
        "asset_class": instrument["asset_class"],
        "instrument_type": instrument["instrument_type"],
        "group": group,
        "origin": origin,
        "unit": instrument["unit"],
        "provider": {"source": current.source, "symbol": provider_symbol,
                     "contract": current.contract},
        "current": {"value": change["current"], "observed_at": _iso(current.observed_at),
                    "session": current.session},
        "reference": {"value": change["reference"], "observed_at": _iso(reference.observed_at),
                      "basis": basis, "session": reference.session},
        "change": {k: change[k] for k in ("status", "reasons", "absolute", "absolute_unit",
                                          "percent", "bps")},
        "freshness": {**freshness(current.observed_at, now, market_open, max_age,
                                  newer_failed_attempt_at),
                      "market_open": market_open,
                      "max_age_seconds": max_age.total_seconds(),
                      "evaluated_at": _iso(now)},
    }


def stock_instrument(symbol: str) -> dict:
    return {"id": symbol, "symbol": symbol, "label": symbol, "asset_class": "equity",
            "instrument_type": "stock", "unit": "USD"}


def build_overview(config: Mapping, rows: Sequence[Mapping], *, generated_at: datetime,
                   cycle_started_at: datetime, cycle_status: str,
                   newer_failed_attempt_at: Optional[datetime] = None) -> dict:
    """Assemble the schema-1 overview payload.

    Every configured instrument appears exactly once: a row the cycle did not
    produce is listed under `gaps` with reason 'not_collected', so missing
    coverage is never silent. Rows for unknown ids are rejected.
    """
    if cycle_status not in CYCLE_STATUSES:
        raise ValueError(f"unknown cycle status: {cycle_status}")
    expected = {m["symbol"] for m in baseline_members(config)} | {m["id"] for m in config["macro"]}
    seen: set[str] = set()
    for r in rows:
        if r["id"] in seen:
            raise ValueError(f"duplicate row: {r['id']}")
        if r["origin"] != "watchlist" and r["id"] not in expected:
            raise ValueError(f"row is not in the universe: {r['id']}")
        seen.add(r["id"])
    gaps = [{"id": i, "reason": "not_collected"} for i in sorted(expected - seen)]
    return {
        "schema_version": OVERVIEW_SCHEMA_VERSION,
        "dataset": "cross_market_overview",
        "universe_version": config["universe_version"],
        "universe_status": config["status"],
        "generated_at": _iso(generated_at),
        "cycle": {"status": cycle_status, "started_at": _iso(cycle_started_at),
                  "finished_at": _iso(generated_at),
                  "newer_failed_attempt_at": _iso(newer_failed_attempt_at)},
        "groups": [{"id": g["id"], "label": g["label"]} for g in config["groups"]],
        "instruments": list(rows),
        "gaps": gaps,
        "notes": [
            "An as-of collection of asynchronous observations, not a simultaneous snapshot.",
            "Breadth over this artifact describes the curated sample, not the whole market.",
            "Price moves do not establish a news cause.",
        ],
    }


def top_movers(members: Iterable[Mapping], changes: Mapping[str, Mapping],
               direction: str, limit: int, offset: int = 0,
               include_watchlist: bool = False) -> dict:
    """Biggest up/down movers, ranked over the full eligible sample, then paged."""
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    eligible = [m for m in members if include_watchlist or m["origin"] == "baseline"]
    ranked = [(m["symbol"], changes[m["symbol"]]["percent"]) for m in eligible
              if m["symbol"] in changes and changes[m["symbol"]].get("percent") is not None]
    ranked.sort(key=lambda r: (-r[1] if direction == "up" else r[1], r[0]))
    page = ranked[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(ranked) else None
    return {
        "direction": direction,
        "eligible": len(eligible),
        "ranked": len(ranked),
        "missing": len(eligible) - len(ranked),
        "rows": [{"rank": offset + i + 1, "symbol": s, "percent": p}
                 for i, (s, p) in enumerate(page)],
        "next_offset": next_offset,
    }
