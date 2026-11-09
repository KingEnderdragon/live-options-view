"""Option-chain loading: OCC/DXLink symbol helpers and the chain fetch.

WHY this module: the chain is the collector's input contract -- OCC
symbols in, per-expiration strike lists out, for the 0DTE and the nearest
weekly expiration. It reads the calendar (tiers) for the weekly walk and
the auth header helper (oauth) for the fetch, and nothing else.
Its test patch point (_load_calendar, stubbed by stage2 and
verify_weekly_collection) resolves through the package entry.
"""

import re
from datetime import date, timedelta
from typing import Optional

import requests

import producers.live_market as _entry  # entry indirection for nearest_weekly_expiration's _load_calendar patch point

from .config import TASTY_BASE, TICKER, log
from .oauth import _bearer_headers
from .tiers import _CalendarLookupError, _TD_WALK_LIMIT

# -- option chain structure ---------------------------------------------------

def _strike_str(strike: float) -> str:
    """dxFeed strike format: 713.0 -> '713', 713.5 -> '71350'."""
    if strike == int(strike):
        return str(int(strike))
    return f"{strike * 100:.0f}".rstrip("0")


def _dxlink_symbol(occ_symbol: str) -> str:
    """Convert OCC symbol to dxFeed streamer format.
    QQQ260623C00713000 -> .QQQ260623C713
    The OCC strike field is 8 digits representing price * 1000.
    """
    occ = occ_symbol.replace(" ", "")
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    underlying = occ[:i]
    date_part  = occ[i:i+6]
    side       = occ[i+6]
    strike     = int(occ[i+7:]) / 1000.0
    return f".{underlying}{date_part}{side}{_strike_str(strike)}"


def _build_symbol(strike: float, exp_date: str, option_type: str, ticker: str = TICKER) -> str:
    yy, mm, dd = exp_date[2:4], exp_date[5:7], exp_date[8:10]
    side = "C" if option_type.lower() == "call" else "P"
    return f".{ticker}{yy}{mm}{dd}{side}{_strike_str(strike)}"


def _occ_root(occ_symbol: str) -> str:
    """Underlying root of an OCC symbol: 'SPY   261006C00600000' -> 'SPY'."""
    occ = occ_symbol.replace(" ", "")
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    return occ[:i]


def _contract_symbols(contract, strike: float, exp_date: str, side: str,
                      ticker: str) -> tuple[str, str] | None:
    """Accept provider symbols only when both identify this exact contract."""
    expected_streamer = _build_symbol(strike, exp_date, side, ticker)
    occ = contract if isinstance(contract, str) else contract.get("symbol", "")
    occ = occ.replace(" ", "") if isinstance(occ, str) else ""
    if occ:
        match = re.fullmatch(r"([A-Z]+)(\d{6})([CP])(\d{8})", occ)
        expected_date = exp_date[2:4] + exp_date[5:7] + exp_date[8:10]
        expected_side = "C" if side == "call" else "P"
        if (not match or match.group(1) != ticker or match.group(2) != expected_date
                or match.group(3) != expected_side
                or int(match.group(4)) != round(strike * 1000)):
            return None
    streamer = contract.get("streamer-symbol") if isinstance(contract, dict) else None
    if streamer and streamer != expected_streamer:
        return None
    return occ, expected_streamer


def nearest_weekly_expiration(as_of: date, valid_days: Optional[set[date]] = None) -> date:
    """Return the EoW expiration using qqq-data-pipeline's holiday behavior."""
    nominal_friday = as_of + timedelta(days=(4 - as_of.weekday()) % 7)
    valid = _entry._load_calendar() if valid_days is None else valid_days
    if not valid:
        # The chain lookup below still verifies that this expiration actually
        # exists. Calendar failure must not disturb the existing 0DTE path.
        return nominal_friday

    expiry = nominal_friday
    for _ in range(_TD_WALK_LIMIT):
        if expiry in valid:
            return expiry
        expiry -= timedelta(days=1)
    raise _CalendarLookupError(
        f"no trading day at or before {nominal_friday} within calendar window"
    )


def _parse_expiration_strikes(expiration: dict, ticker: str = TICKER) -> list[dict]:
    exp_date = expiration["expiration-date"]
    strikes = []
    foreign = 0
    for s in expiration.get("strikes", []):
        strike = float(s.get("strike-price", 0))
        call = _contract_symbols(s.get("call", {}), strike, exp_date, "call", ticker)
        put = _contract_symbols(s.get("put", {}), strike, exp_date, "put", ticker)
        if call is None or put is None:
            foreign += 1
            continue
        call_occ, call_sym = call
        put_occ, put_sym = put
        strikes.append({
            "strike":   strike,
            "call_sym": call_sym,
            "put_sym":  put_sym,
            "call_occ": call_occ,
            "put_occ":  put_occ,
        })
    if foreign:
        log.warning(f"{ticker} chain {exp_date}: dropped {foreign} strikes with mismatched contract identity")
    return strikes


def load_chain(access_token: str, today: date, ticker: str = TICKER,
               include_weekly: bool = True) -> tuple[list[dict], str, list[dict], str]:
    """Load the current expiration and the pipeline-compatible EoW expiration.

    `include_weekly=False` (SPY) returns only the current expiration.
    """
    resp = requests.get(
        f"{TASTY_BASE}/option-chains/{ticker}/nested",
        headers=_bearer_headers(access_token),
        timeout=30,
    )
    resp.raise_for_status()

    items = resp.json().get("data", {}).get("items", [])
    if not items:
        raise RuntimeError("empty option chain response")

    today_str   = today.isoformat()
    expirations = items[0].get("expirations", [])

    ordered_expirations = sorted(expirations, key=lambda e: e.get("expiration-date", ""))
    target = None
    if ticker == "SPY":
        target = next((exp for exp in ordered_expirations
                       if exp.get("expiration-date") == today_str), None)
        if target is None:
            raise RuntimeError(f"SPY 0DTE unavailable: no expiration for {today_str}")
    else:
        for exp in ordered_expirations:
            if exp.get("expiration-date", "") >= today_str:
                target = exp
                break
    if target is None:
        raise RuntimeError(f"no upcoming expiration found in chain for {today_str}")

    exp_date = target["expiration-date"]
    log.info(f"{ticker} chain expiration: {exp_date}  ({len(target.get('strikes', []))} strikes)")

    strikes = _parse_expiration_strikes(target, ticker)
    if not include_weekly:
        return strikes, exp_date, [], ""

    try:
        weekly_exp_date = nearest_weekly_expiration(today).isoformat()
    except Exception as exc:
        log.error(
            f"weekly expiration selection failed ({exc}) -- "
            "weekly collection disabled for this session"
        )
        return strikes, exp_date, [], ""
    weekly_target = next(
        (exp for exp in ordered_expirations
         if exp.get("expiration-date") == weekly_exp_date),
        None,
    )
    if weekly_target is None:
        log.error(
            f"nearest weekly expiration {weekly_exp_date} not found in chain -- "
            "weekly collection disabled for this session"
        )
        return strikes, exp_date, [], weekly_exp_date

    weekly_strikes = strikes if weekly_exp_date == exp_date else _parse_expiration_strikes(weekly_target)
    log.info(
        f"weekly chain expiration: {weekly_exp_date}  "
        f"({len(weekly_target.get('strikes', []))} strikes)"
    )
    return strikes, exp_date, weekly_strikes, weekly_exp_date



__all__ = [
    "_strike_str",
    "_dxlink_symbol",
    "_build_symbol",
    "_occ_root",
    "_contract_symbols",
    "nearest_weekly_expiration",
    "_parse_expiration_strikes",
    "load_chain",
]
