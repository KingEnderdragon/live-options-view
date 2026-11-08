"""Universe selection and one capture sweep across it.

A sweep is one pass over the selected contracts: for each underlying, fetch the
chain and turn its rows into the day's archive. Requests are budgeted, so one
slow contract cannot starve the rest of the pass.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import runtime
from .archive import published
from .runtime import ET, SELECTION_VERSION
from .tradier import BudgetExhausted, Tradier


# --------------------------------------------------------------------------
# Universe selection
# --------------------------------------------------------------------------

def nearest_expiration(dates: Iterable[str], today: date) -> str | None:
    upcoming = sorted(d for d in dates if d >= today.isoformat())
    return upcoming[0] if upcoming else None


def second_friday_expiration(dates: Iterable[str], today: date, nearest: str) -> str | None:
    """Keep the next Friday cohort across Friday-to-Monday, within two chains.

    A rolling >=5-calendar-day threshold drops the next Friday cohort on
    Monday. The next listed Friday strictly after the nearest expiry remains the same
    cohort from the previous Friday through the following week's Thursday.
    """
    candidates = sorted(d for d in dates if d > nearest
                        and date.fromisoformat(d).weekday() == 4)
    return candidates[0] if candidates else None


def select_universe(client: Tradier, ranked: list[tuple[str, dict[str, int]]], today: date,
                    target: int, workers: int = 3, spy_qqq_second_expiry: bool = False) -> dict[str, Any]:
    """First ``target`` ranked underlyings with an unexpired expiry on Tradier."""
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    position = 0

    def lookup(item: tuple[int, str, dict[str, int]]) -> dict[str, Any]:
        rank_no, symbol, vol = item
        base = {"occ_rank": rank_no, "underlying": symbol, **{f"occ_{k}": v for k, v in vol.items()}}
        try:
            dates = client.expirations(symbol)
        except Exception as exc:  # recorded per name, never a silent pass
            return {**base, "skip_reason": "provider_error", "detail": str(exc)[:300]}
        if not dates:
            return {**base, "skip_reason": "no_listed_expirations"}
        expiry = nearest_expiration(dates, today)
        if expiry is None:
            return {**base, "skip_reason": "no_unexpired_expiration"}
        extra = (second_friday_expiration(dates, today, expiry)
                 if spy_qqq_second_expiry and symbol in {"SPY", "QQQ"} else None)
        return {**base, "expiration": expiry,
                "days_to_expiration": (date.fromisoformat(expiry) - today).days,
                **({"additional_expiration": extra} if extra else {})}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while len(selected) < target and position < len(ranked):
            need = target - len(selected)
            batch = [(position + i + 1, *ranked[position + i])
                     for i in range(min(need, len(ranked) - position))]
            position += len(batch)
            for result in pool.map(lookup, batch):
                (skipped if "skip_reason" in result else selected).append(result)
    selected = sorted(selected, key=lambda r: r["occ_rank"])[:target]
    return {
        "selection_version": "oa203-universe-v2-second-expiry" if spy_qqq_second_expiry else SELECTION_VERSION,
        "rule": ("rank underlyings by previous-session OCC cleared contracts "
                 "(sum over account types / 2); keep the first N with an unexpired "
                 "Tradier expiration; chain = nearest expiration on/after the trade date"),
        "target": target,
        "selected_count": len(selected),
        "shortfall": max(0, target - len(selected)),
        "candidates_examined": position,
        "selected": selected,
        "additional_expiry_rule": ("next listed Friday strictly after nearest expiry for SPY/QQQ only; "
                                   "at most one extra expiry" if spy_qqq_second_expiry else "disabled"),
        "skipped": skipped,
        "skip_counts": dict(Counter(s["skip_reason"] for s in skipped)),
    }


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drop NaN


def _spot(quote: dict[str, Any] | None) -> tuple[float | None, str | None, int | None]:
    if not quote:
        return None, None, None
    bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
    if bid and ask and bid > 0 and ask >= bid:
        stamps = [int(s) for s in (quote.get("bid_date"), quote.get("ask_date")) if s]
        return round((bid + ask) / 2, 6), "mid", (min(stamps) if stamps else None)
    last = _num(quote.get("last"))
    return (last, "last", int(quote["trade_date"]) if quote.get("trade_date") else None) if last else (None, None, None)


def chain_rows(sweep: int, fetched_ms: int, underlying: str, contracts: list[dict[str, Any]],
               quote: dict[str, Any] | None) -> list[dict[str, Any]]:
    spot, spot_source, spot_ms = _spot(quote)
    rows = []
    for c in contracts:
        bid, ask, last = (_num(c.get(field)) for field in ("bid", "ask", "last"))
        rows.append({
            "sweep": sweep, "t": fetched_ms, "underlying": underlying,
            "spot": spot, "spot_source": spot_source, "spot_ms": spot_ms,
            "symbol": c.get("symbol"), "root": c.get("root_symbol"),
            "type": c.get("option_type"), "strike": _num(c.get("strike")),
            "expiration": c.get("expiration_date"),
            "bid": bid, "ask": ask,
            "bid_ms": None if bid is None and c.get("bid_date") == 0 else c.get("bid_date"),
            "ask_ms": None if ask is None and c.get("ask_date") == 0 else c.get("ask_date"),
            "bid_size": c.get("bidsize"), "ask_size": c.get("asksize"),
            "last": last, "trade_ms": None if last is None and c.get("trade_date") == 0 else c.get("trade_date"),
            "volume": c.get("volume"), "oi": c.get("open_interest"),
            "contract_size": c.get("contract_size") or 100,
        })
    return rows


BUDGET_SKIP = "sweep budget exhausted before chain request"


def run_sweep(client: Tradier, universe: list[dict[str, Any]], sweep: int, out_path: Path,
              block_size: int, workers: int, deadline: datetime | None = None,
              budget_s: float | None = None,
              clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Sample every chain once; write rows to ``out_path``; return the manifest entry.

    With ``budget_s``, no chain request starts after the sweep has run that
    long, and failed requests stop retrying. Chains not requested are recorded
    as ``skipped_budget``, so a degraded provider shortens coverage visibly
    instead of stretching the sweep. In-flight requests can overrun the budget
    by at most one request timeout.
    """
    started = runtime.now_ms()
    budget_end = clock() + budget_s if budget_s else None
    over_budget = lambda: budget_end is not None and clock() >= budget_end
    from capture_diagnostics import exception_details
    def safe_error(exc):
        return json.dumps(exception_details(exc, (getattr(client, "token", ""),)))
    chains: dict[str, dict[str, Any]] = {}
    additional_chains: dict[str, dict[str, dict[str, Any]]] = {}
    truncated = budget_exhausted = False
    if isinstance(client, Tradier):
        client.budget_deadline = budget_end
    with published(out_path) as tmp, gzip.open(tmp, "wt", encoding="utf-8") as out,             ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(universe), block_size):
            if runtime.STOP or (deadline and datetime.now(ET) >= deadline):
                truncated = True
                break
            block = universe[start:start + block_size]
            if over_budget():
                budget_exhausted = True
                for u in block:
                    chains[u["underlying"]] = {"fetched_ms": None, "expiration": u["expiration"],
                                               "status": "skipped_budget"}
                    if u.get("additional_expiration"):
                        additional_chains[u["underlying"]] = {u["additional_expiration"]: {
                            "expiration": u["additional_expiration"], "fetched_ms": None,
                            "status": "skipped_budget"}}
                continue
            symbols = [u["underlying"] for u in block]
            try:
                quotes = client.quotes(symbols)
                quote_error = None
            except Exception as exc:
                quotes, quote_error = {}, safe_error(exc)

            def fetch(u: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None, int, str | None]:
                if runtime.STOP or (deadline and datetime.now(ET) >= deadline):
                    return u, None, runtime.now_ms(), "session closed or stop requested before chain"
                if over_budget():
                    return u, None, runtime.now_ms(), BUDGET_SKIP
                try:
                    contracts = client.chain(u["underlying"], u["expiration"])
                    return u, contracts, runtime.now_ms(), None
                except BudgetExhausted:
                    return u, None, runtime.now_ms(), BUDGET_SKIP
                except Exception as exc:
                    return u, None, runtime.now_ms(), safe_error(exc)

            for u, contracts, fetched, error in pool.map(fetch, block):
                name = u["underlying"]
                status: dict[str, Any] = {"fetched_ms": fetched, "expiration": u["expiration"]}
                if error == BUDGET_SKIP:
                    budget_exhausted = True
                    status.update(status="skipped_budget", fetched_ms=None)
                elif error is not None:
                    status.update(status="error", error=error)
                elif not contracts:
                    status.update(status="empty")
                else:
                    rows = chain_rows(sweep, fetched, name, contracts, quotes.get(name))
                    for row in rows:
                        out.write(json.dumps(row, separators=(",", ":")) + "\n")
                    status.update(status="ok", rows=len(rows))
                if name not in quotes:
                    status["spot_missing"] = quote_error or "not returned"
                chains[name] = status
                extra = u.get("additional_expiration")
                if extra:
                    extra_status: dict[str, Any] = {"expiration": extra, "fetched_ms": runtime.now_ms()}
                    if runtime.STOP or (deadline and datetime.now(ET) >= deadline):
                        extra_status["status"] = "skipped_deadline"
                        truncated = True
                    elif over_budget():
                        extra_status.update(status="skipped_budget", fetched_ms=None)
                        budget_exhausted = True
                    else:
                        try:
                            extra_contracts = client.chain(name, extra)
                            extra_status["fetched_ms"] = runtime.now_ms()
                            if not extra_contracts:
                                extra_status["status"] = "empty"
                            elif any(c.get("expiration_date") != extra for c in extra_contracts):
                                extra_status["status"] = "expiry_mismatch"
                            else:
                                extra_rows = chain_rows(sweep, extra_status["fetched_ms"], name,
                                                        extra_contracts, quotes.get(name))
                                for row in extra_rows:
                                    row["collection_role"] = "additional_expiry"
                                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
                                extra_status.update(status="ok", rows=len(extra_rows))
                        except BudgetExhausted:
                            extra_status.update(status="skipped_budget", fetched_ms=None)
                            budget_exhausted = True
                        except Exception as exc:
                            extra_status.update(status="error", error=safe_error(exc))
                    additional_chains[name] = {extra: extra_status}
    if isinstance(client, Tradier):
        client.budget_deadline = None
    counts = Counter(s["status"] for s in chains.values())
    truncated = truncated or bool(deadline and datetime.now(ET) >= deadline)
    return {"sweep": sweep, "file": out_path.name, "started_ms": started, "ended_ms": runtime.now_ms(),
            "truncated": truncated, "budget_s": budget_s or None, "budget_exhausted": budget_exhausted,
            "status_counts": dict(counts),
            "rows": sum(s.get("rows", 0) for s in chains.values()) +
                    sum(s.get("rows", 0) for e in additional_chains.values() for s in e.values()),
            "chains": chains, "additional_chains": additional_chains}
