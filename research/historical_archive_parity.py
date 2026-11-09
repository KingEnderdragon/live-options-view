#!/usr/bin/env python3
"""Check the three reported option/underlying parity discrepancies (#146).

Read-only, bounded, and explicit about the archive's actual convention: the
`qqq-data-pipeline` historical daily chain for trade date D is written before
D's open (observed Last-Modified ~08:0x ET on D) and carries the **prior
session's** settled close, not D's intraday prices. So the matched value for a
chain's `UnderlyingPrice` is the D-1 5-minute bar close, not a same-day bar.
The earlier same-day comparison is kept in the report only to show that it does
not match; it is not the evidence.

Parity assumption (stated, not hidden): European put-call parity at zero rate
and zero time-to-expiry, same strike and expiry, no dividend adjustment:
`S_implied = mid_call - mid_put + strike`. For 0DTE index options that is the
conventional approximation, so a residual offset is a *level* difference
between the recorded underlying and the option mids within that close snapshot,
not a strike-selection or rounding error. The chain has no option-quote
timestamps, so the cause of the residual cannot be proved from it.

    python research/historical_archive_parity.py --cache ./research/archive_evidence
    python research/historical_archive_parity.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev"
DATES = ("2025-12-29", "2026-09-16", "2026-09-18")
TOLERANCES = (0.05, 0.10, 0.25)


def _get(base: str, key: str, cache: Path) -> tuple[bytes | None, dict]:
    """Bounded read with a local cache; a 404 is data, not an exception."""
    target = cache / key
    if target.exists():
        return target.read_bytes(), {"key": key, "status": 200, "cached": True}
    try:
        req = urllib.request.Request(base + "/" + key, headers={"User-Agent": "brian-parity-check/1"})
        with urllib.request.urlopen(req, timeout=40) as response:
            body = response.read()
            meta = {
                "key": key, "status": response.status,
                "etag": (response.headers.get("ETag") or "").strip('"'),
                "last_modified": response.headers.get("Last-Modified"),
                "bytes": len(body), "cached": False,
            }
    except urllib.error.HTTPError as exc:
        return None, {"key": key, "status": exc.code, "bytes": 0}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return body, meta


def _head(base: str, key: str) -> dict:
    """Headers only, for cached objects whose original write time matters."""
    try:
        req = urllib.request.Request(base + "/" + key, method="HEAD",
                                     headers={"User-Agent": "brian-parity-check/1"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return {
                "key": key, "status": response.status,
                "etag": (response.headers.get("ETag") or "").strip('"'),
                "last_modified": response.headers.get("Last-Modified"),
                "cached": True,
            }
    except urllib.error.HTTPError as exc:
        return {"key": key, "status": exc.code, "cached": True}
    except Exception:
        return {"key": key, "status": None, "cached": True}


def _rows(body: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(body.decode("utf-8", errors="replace"))))


def _prev_session(day: date) -> date:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crassus"))
        from crassus.exchange_calendar import is_holiday
        holiday = is_holiday
    except Exception:
        holiday = lambda _d: False  # noqa: E731
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5 or holiday(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _bar_candidates(body: bytes, value: float | None) -> dict:
    if value is None or not body:
        return {"value": value, "candidates": []}
    bars = []
    for row in _rows(body):
        try:
            bars.append((row["datetime"], float(row["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    within = lambda tol: [(when, round(close, 4), round(close - value, 4))
                          for when, close in bars if abs(close - value) <= tol]
    result = {"value": value, "bar_count": len(bars)}
    if bars:
        when, close = bars[-1]
        result["last_bar"] = {"bar_datetime": when, "bar_close": round(close, 4),
                              "difference": round(close - value, 4)}
    for tol in TOLERANCES:
        hits = within(tol)
        result[f"within_{tol}"] = {
            "count": len(hits),
            "first": hits[0][0] if hits else None,
            "last": hits[-1][0] if hits else None,
        }
    if bars:
        when, close = min(bars, key=lambda item: abs(item[1] - value))
        result["closest"] = {"bar_datetime": when, "bar_close": round(close, 4),
                             "difference": round(close - value, 4)}
    return result


def parity(chain_csv: bytes, prior_price_csv: bytes | None,
           same_day_price_csv: bytes | None = None) -> dict:
    rows = _rows(chain_csv)
    if not rows:
        return {"status": "empty_chain"}
    underlyings = sorted({float(r["UnderlyingPrice"]) for r in rows if r.get("UnderlyingPrice")})
    stored = underlyings[0] if len(underlyings) == 1 else None
    seen, calls, puts = set(), {}, {}
    for row in rows:
        key = (float(row["Strike"]), row["Type"])
        if key in seen:
            continue
        seen.add(key)
        (calls if row["Type"] == "call" else puts)[float(row["Strike"])] = row
    strikes = sorted(set(calls) & set(puts))
    if stored is None or not strikes:
        return {"status": "unusable_chain", "underlyings": underlyings}

    atm = min(strikes, key=lambda k: (abs(k - stored), k))

    def implied(strike: float, field: str) -> float | None:
        call, put = calls[strike], puts[strike]
        if field == "bid_ask_midpoint":
            values = [call.get("Bid"), call.get("Ask"), put.get("Bid"), put.get("Ask")]
            if any(v in (None, "") for v in values):
                return None
            cb, ca, pb, pa = (float(v) for v in values)
            return (cb + ca) / 2 - (pb + pa) / 2 + strike
        if call.get(field) in (None, "") or put.get(field) in (None, ""):
            return None
        return float(call[field]) - float(put[field]) + strike

    estimates = {field: (lambda v: None if v is None else round(v, 4))(implied(atm, field))
                 for field in ("Mid", "bid_ask_midpoint", "Last")}
    nearby = [k for k in strikes if abs(k - stored) <= 3]
    offsets = [v - stored for k in nearby if (v := implied(k, "Mid")) is not None]

    result = {
        "status": "ok",
        "stored_underlying_price": round(stored, 4),
        "atm_strike": atm,
        "implied_spot": estimates,
        "offset_vs_stored": {k: (None if v is None else round(v - stored, 4))
                             for k, v in estimates.items()},
        "offset_spread_across_nearby_strikes": (round(max(offsets) - min(offsets), 4)
                                                if offsets else None),
        "nearby_strikes_checked": len(offsets),
        "assumption": "S = C - P + K at zero rate and zero time-to-expiry, same expiry, no dividends",
    }

    prior = _bar_candidates(prior_price_csv, stored)
    result["prior_session_matched_value"] = prior
    if prior.get("last_bar"):
        result["prior_close_difference"] = prior["last_bar"]["difference"]

    # Same-day bars are reported only to show the convention is *not*
    # same-day; the discrepancy analysis does not use them.
    result["same_day_reference"] = _bar_candidates(same_day_price_csv, stored)
    implied_value = estimates.get("Mid")
    result["implied_vs_prior_bars"] = _bar_candidates(prior_price_csv, implied_value)
    return result


def run(base: str, cache: Path, dates: tuple[str, ...]) -> dict:
    report = {"origin": base, "assumption_set": "parity_v2_prior_close", "dates": {}, "artifacts": {}}
    manifest_body, manifest_meta = _get(base, "manifest.json", cache)
    report["artifacts"]["manifest.json"] = manifest_meta
    if manifest_body:
        manifest = json.loads(manifest_body)
        listed = sorted(set(manifest.get("dates", [])))
        report["manifest"] = {
            "date_count": len(listed), "first": listed[0] if listed else None,
            "last": listed[-1] if listed else None,
            "requested_dates_present": {d: d in listed for d in dates},
            "gap_note": "listed dates are the producer's index, not a promise that every object exists",
        }
    _, listing_meta = _get(base, "?list-type=2&max-keys=1", cache)
    report["listing_attempt"] = listing_meta

    for day in dates:
        d = date.fromisoformat(day)
        key = f"raw/qqq_chain_{d.strftime('%Y%m%d')}.csv"
        body, meta = _get(base, key, cache)
        if body is not None and not meta.get("last_modified"):
            meta = {**meta, **_head(base, key)}
        report["artifacts"][key] = meta
        prior = _prev_session(d)
        prior_key = f"prices/qqq_intraday_{prior.strftime('%Y%m%d')}.csv"
        same_key = f"prices/qqq_intraday_{d.strftime('%Y%m%d')}.csv"
        prior_body, prior_meta = _get(base, prior_key, cache)
        same_body, same_meta = _get(base, same_key, cache)
        report["artifacts"][prior_key] = prior_meta
        report["artifacts"][same_key] = same_meta
        if body is None:
            report["dates"][day] = {"status": "chain_missing", "http_status": meta["status"]}
            continue
        result = parity(body, prior_body, same_body)
        result["chain_etag"] = meta.get("etag")
        result["chain_last_modified"] = meta.get("last_modified")
        result["chain_bytes"] = meta.get("bytes")
        result["chain_write_date"] = _write_date(meta.get("last_modified"))
        result["prior_session"] = prior.isoformat()
        result["chain_written_before_same_day_open"] = _before_open(meta.get("last_modified"), d)
        result["implied_offset_ambiguous"] = bool(
            result.get("implied_vs_prior_bars", {}).get("within_0.1", {}).get("count", 0) > 1)
        report["dates"][day] = result
    return report


def _write_date(last_modified: str | None) -> str | None:
    if not last_modified:
        return None
    try:
        return datetime.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
    except ValueError:
        return None


def _before_open(last_modified: str | None, day: date) -> bool | None:
    """Did the producer write the chain before that trade date's open?

    A pre-open write is the observable convention behind "the chain for D is
    D-1 settled data". Compare in ET so daylight saving cannot shift the
    classification.
    """
    if not last_modified:
        return None
    try:
        written = datetime.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z")
    except ValueError:
        return None
    try:
        from zoneinfo import ZoneInfo
        et = written.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        open_at = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        return et < open_at
    except Exception:
        return None


def _self_test() -> int:
    chain = (
        "TradeDate,Expiration,Strike,Type,OptionSymbol,DTE,OpenInterest,Volume,Bid,Mid,Ask,Last,IV,Delta,Gamma,Theta,Vega,UnderlyingPrice\n"
        "2026-01-02,2026-01-02,99,call,X,0,1,1,2.4,2.5,2.6,2.5,,,,,,100\n"
        "2026-01-02,2026-01-02,99,put,X,0,1,1,0.4,0.5,0.6,0.5,,,,,,100\n"
        "2026-01-02,2026-01-02,100,call,X,0,1,1,1.9,2.0,2.1,2.0,,,,,,100\n"
        "2026-01-02,2026-01-02,100,put,X,0,1,1,0.9,1.0,1.1,1.0,,,,,,100\n"
        "2026-01-02,2026-01-02,101,call,X,0,1,1,1.4,1.5,1.6,1.5,,,,,,100\n"
        "2026-01-02,2026-01-02,101,put,X,0,1,1,1.4,1.5,1.6,1.5,,,,,,100\n"
    ).encode()
    prior_bars = (
        "datetime,open,high,low,close,volume\n"
        "2026-01-01 15:50:00-05:00,99.9,100.1,99.9,100.0,1\n"
        "2026-01-01 15:55:00-05:00,100.0,100.2,100.0,100.05,1\n"
    ).encode()
    same_bars = (
        "datetime,open,high,low,close,volume\n"
        "2026-01-02 09:30:00-05:00,105,105,105,105,1\n"
    ).encode()
    result = parity(chain, prior_bars, same_bars)
    assert result["status"] == "ok", result
    assert result["stored_underlying_price"] == 100.0, result
    assert result["atm_strike"] == 100.0, result
    assert result["implied_spot"]["Mid"] == 101.0, result
    assert result["offset_vs_stored"]["Mid"] == 1.0, result
    assert result["offset_spread_across_nearby_strikes"] == 0.0, result
    assert result["prior_session_matched_value"]["closest"]["bar_close"] == 100.0, result
    assert result["same_day_reference"]["closest"]["bar_close"] == 105.0, result
    assert result["implied_vs_prior_bars"]["within_0.05"]["count"] == 0, result
    print("historical archive parity self-test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--cache", default=str(Path(__file__).resolve().parent / "archive_evidence"))
    ap.add_argument("--dates", default=",".join(DATES))
    ap.add_argument("--json-out")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    report = run(args.base, Path(args.cache), tuple(args.dates.split(",")))
    text = json.dumps(report, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
