#!/usr/bin/env python3
"""One-off, read-only DXLink coverage/entitlement probe for #132.

Approved by Owen (Oct 12, 2026) as a one-off market-hours probe. It tells
wrong symbology apart from missing entitlement for the cross-market overview's
candidate instruments (cash indices, CBOE yield indices, futures, crypto, FX
futures), with known-good controls (QQQ, /6J).

What it does, and only this:
1. Exchanges the configured read-only OAuth refresh token and obtains a DXLink
   quote token (the same path as the collector and probe_tasty_oauth.py).
2. GET-only instrument lookups to resolve exchange streamer symbols for the
   front futures contracts and crypto pairs, so a "no data" result is not just
   a guessed symbol.
3. Subscribes market-data events (Quote, Trade, TradeETH, Summary) for a fixed
   candidate list on its own short-lived DXLink connection, waits a bounded
   time, and records which symbols produced which events.
4. Writes a JSON report (no tokens, no account data) and exits.

It never touches orders, accounts, positions, R2 or the collector's state.
It does open a second DXLink connection on the same grant for at most
--seconds; run it only with the owner's go-ahead for that window.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector  # noqa: E402

# Fixed candidates. Several spellings per concept on purpose: the report says
# which spelling (if any) the feed actually serves.
CANDIDATES: dict[str, list[str]] = {
    "control.qqq": ["QQQ"],
    "control.6j": ["/6J:XCME"],
    "etf.dia": ["DIA"],
    "etf.iwm": ["IWM"],
    "idx.spx": ["SPX", "$SPX.X", "$SPX"],
    "idx.ndx": ["NDX", "$NDX.X", "$NDX"],
    "idx.dji": ["DJI", "$DJI.X", "$DJX.X"],
    "idx.rut": ["RUT", "$RUT.X", "$RUT"],
    "idx.vix": ["VIX", "$VIX.X", "$VIX"],
    "idx.ovx": ["OVX", "$OVX.X"],
    "idx.kospi": ["$KOSPI.X", "KOSPI"],
    "yld.us05y": ["FVX", "$FVX.X"],
    "yld.us10y": ["TNX", "$TNX.X"],
    "yld.us30y": ["TYX", "$TYX.X"],
    "yld.us13w": ["IRX", "$IRX.X"],
    "fut.cl": ["/CL:XNYM"],
    "fut.bz": ["/BZ:XNYM"],
    "fut.si": ["/SI:XCEC"],
    "fut.6e": ["/6E:XCME"],
    "fut.6b": ["/6B:XCME"],
    "fut.6c": ["/6C:XCME"],
    "fut.6a": ["/6A:XCME"],
    "fut.6s": ["/6S:XCME"],
    "fut.zt": ["/ZT:XCBT"],
    "fut.zf": ["/ZF:XCBT"],
    "fut.zn": ["/ZN:XCBT"],
    "fut.zb": ["/ZB:XCBT"],
    "crypto.btcusd": ["BTC/USD:CXTALP", "BTC/USD:CXERX"],
}
FUTURE_PRODUCTS = ["CL", "BZ", "SI", "6E", "6B", "6C", "6A", "6S", "6J", "ZT", "ZF", "ZN", "ZB"]
MAX_SYMBOLS = 120


def _get(access_token: str, path: str, params=None) -> dict:
    resp = requests.get(collector.TASTY_BASE + path, params=params,
                        headers=collector._bearer_headers(access_token), timeout=10)
    return {"status": resp.status_code, "json": resp.json() if resp.ok else None}


def resolve_streamer_symbols(access_token: str) -> dict:
    """GET-only: front-contract and crypto streamer symbols from tastytrade."""
    out: dict = {"futures": {}, "crypto": {}, "errors": []}
    for code in FUTURE_PRODUCTS:
        try:
            r = _get(access_token, "/instruments/futures", {"product-code[]": code})
            items = ((r["json"] or {}).get("data") or {}).get("items") or []
            active = sorted((i for i in items if i.get("active")), key=lambda i: i.get("expiration-date") or "")
            out["futures"][code] = {"http_status": r["status"], "contracts": [
                {"symbol": i.get("symbol"), "streamer_symbol": i.get("streamer-symbol"),
                 "expiration_date": i.get("expiration-date"), "is_front": i.get("active-month")}
                for i in active[:3]]}
        except Exception as exc:  # report, never retry
            out["errors"].append(f"futures {code}: {type(exc).__name__}")
    try:
        r = _get(access_token, "/instruments/cryptocurrencies")
        items = ((r["json"] or {}).get("data") or {}).get("items") or []
        out["crypto"] = {"http_status": r["status"], "pairs": [
            {"symbol": i.get("symbol"), "streamer_symbol": i.get("streamer-symbol")}
            for i in items if str(i.get("symbol", "")).startswith("BTC")]}
    except Exception as exc:
        out["errors"].append(f"crypto: {type(exc).__name__}")
    return out


def symbol_plan(resolved: dict) -> dict[str, list[str]]:
    plan = {k: list(v) for k, v in CANDIDATES.items()}
    for code, info in resolved.get("futures", {}).items():
        key = "control.6j" if code == "6J" else f"fut.{code.lower()}"
        for c in info.get("contracts", [])[:2]:
            if c.get("streamer_symbol"):
                plan.setdefault(key, []).append(c["streamer_symbol"])
    for p in resolved.get("crypto", {}).get("pairs", []):
        if p.get("symbol") == "BTC/USD" and p.get("streamer_symbol"):
            plan["crypto.btcusd"].append(p["streamer_symbol"])
    seen: set[str] = set()
    for key in plan:
        plan[key] = [s for s in plan[key] if not (s in seen or seen.add(s))]
    total = sum(len(v) for v in plan.values())
    if total > MAX_SYMBOLS:
        raise RuntimeError(f"symbol plan {total} exceeds {MAX_SYMBOLS}")
    return plan


def summarize(plan: dict[str, list[str]], state: dict) -> dict:
    report = {}
    for key, symbols in plan.items():
        rows = []
        for s in symbols:
            d = state.get(s) or {}
            rows.append({"symbol": s, "received": bool(d),
                         "quote": d.get("bid") is not None or d.get("ask") is not None,
                         "trade": d.get("last") is not None,
                         "prev_close": d.get("prev_close") is not None,
                         "sample": {k: d.get(k) for k in ("bid", "ask", "last", "prev_close")} if d else None})
        working = [r["symbol"] for r in rows if r["received"]]
        report[key] = {"working_symbols": working, "verdict": "served" if working else "no_data", "tried": rows}
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm-read-only", action="store_true",
                    help="acknowledge this makes a real read-only tastytrade/DXLink probe")
    ap.add_argument("--seconds", type=float, default=45.0, help="listen window, 15..120 (default 45)")
    ap.add_argument("--out", type=Path, required=True, help="JSON report path")
    args = ap.parse_args()
    if not args.confirm_read_only:
        ap.error("--confirm-read-only is required")
    if not 15 <= args.seconds <= 120:
        ap.error("--seconds must be between 15 and 120")

    started = datetime.now(timezone.utc)
    auth = collector.tasty_auth(collector.OAuthTokenManager.from_env())
    resolved = resolve_streamer_symbols(auth["access_token"])
    plan = symbol_plan(resolved)
    symbols = [s for v in plan.values() for s in v]

    feed = collector.DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions([], symbols)
    feed.start()
    try:
        ready = feed.wait_ready(timeout=20.0)
        deadline = time.monotonic() + args.seconds
        while ready and time.monotonic() < deadline:
            time.sleep(1.0)
        state = feed.get_state()
        health = feed.get_health()
    finally:
        feed.stop()

    report = {
        "probe": "probe_cross_market_symbols", "issue": 132, "read_only": True,
        "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "listen_seconds": args.seconds, "dxlink_ready": ready,
        "dxlink_health": {k: health.get(k) for k in ("authorized", "channel_open", "reconnect_count", "last_error")},
        "timestamps_note": "Collector state *_ts fields are receipt times; no provider event time was requested.",
        "resolved_instruments": resolved, "results": summarize(plan, state),
    }
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    served = sorted(k for k, v in report["results"].items() if v["verdict"] == "served")
    print(f"probe complete: {len(served)}/{len(plan)} concepts served; report={args.out}")
    controls_ok = all(report["results"][k]["verdict"] == "served" for k in ("control.qqq", "control.6j"))
    if not controls_ok:
        print("WARNING: a known-good control returned no data; treat no_data verdicts as inconclusive.")
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
