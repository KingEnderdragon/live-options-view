#!/usr/bin/env python3
"""Offline, read-only replay of #130 windows over published OA-203 sessions.

Reads only the public OA-203 artifacts (summary.json, universe.json,
sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz) for the given sessions,
keeps rows for a bounded set of underlyings in memory (nothing is written to
the archive, R2 or any provider), and evaluates the provisional
oa203-windows-v1 windows with oa203_windows.py.

This is offline validation over sampled quotes with provisional D1-D9
defaults. It is not deployed collection, not a fill, and not a prediction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from datetime import date, datetime, timedelta, timezone
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oa203_windows as w  # noqa: E402
import oa203_windows_outputs as outputs  # noqa: E402
from oa203_returns import ContractInfo, Observation  # noqa: E402

ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev"
MAX_UNDERLYINGS = 50
MAX_SESSIONS = 10
SOURCES: list[dict] = []  # every fetched public artifact with its SHA-256
EXPECTED_HASHES: dict[str, str] | None = None


def fetch(key: str, attempts: int = 4) -> bytes:
    """GET one public artifact; bounded retries with backoff on transport errors."""
    req = urllib.request.Request(f"{ORIGIN}/{key}", headers={"User-Agent": "oa203-windows-replay/1"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            digest = hashlib.sha256(body).hexdigest()
            if EXPECTED_HASHES is not None and EXPECTED_HASHES.get(key) != digest:
                raise RuntimeError(f"source missing from lock or changed: {key}")
            SOURCES.append({"key": key, "sha256": digest, "bytes": len(body)})
            return body
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def validate_manifest(manifest: list[dict], summary: dict, day: str,
                      selected: dict[str, dict[str, str | None]] | None = None) -> None:
    """Reject torn or amended sweep indexes before treating a replay as final."""
    if summary.get("assessment", {}).get("status") != "complete":
        raise RuntimeError(f"{day}: daily assessment is not complete")
    if not manifest or [m.get("sweep") for m in manifest] != list(range(1, len(manifest) + 1)):
        raise RuntimeError(f"{day}: non-contiguous manifest")
    files = [m.get("file") for m in manifest]
    if len(set(files)) != len(files) or any(f != f"sweep_{i:04d}.jsonl.gz" for i, f in enumerate(files, 1)):
        raise RuntimeError(f"{day}: invalid or repeated sweep file")
    if any(m.get("truncated") or not isinstance(m.get("chains"), dict) for m in manifest):
        raise RuntimeError(f"{day}: truncated or damaged sweep")
    previous_end = None
    for entry in manifest:
        start, end = entry.get("started_ms"), entry.get("ended_ms")
        if (type(start) is not int or type(end) is not int or start < 0 or end < start or
                (previous_end is not None and start < previous_end)):
            raise RuntimeError(f"{day}: invalid or reversed sweep interval {entry['sweep']}")
        previous_end = end
        for st in list(entry["chains"].values()) + [s for statuses in entry.get("additional_chains", {}).values()
                                                       for s in statuses.values()]:
            fetched = st.get("fetched_ms")
            if type(fetched) is not int or not start <= fetched <= end:
                raise RuntimeError(f"{day}: missing or invalid chain fetch time in sweep {entry['sweep']}")
        for name, expiries in (selected or {}).items():
            extra = expiries.get("additional")
            if extra:
                st = entry.get("additional_chains", {}).get(name, {}).get(extra)
                if not st or st.get("status") != "ok" or st.get("expiration") != extra or \
                        type(st.get("rows")) is not int or st["rows"] <= 0:
                    raise RuntimeError(f"{day}: incomplete additional chain {name} {extra} in sweep {entry['sweep']}")


def validate_contract_identity(symbol: str, row: dict) -> None:
    """Cross-check archive metadata against the OCC contract key."""
    suffix = symbol[-15:]
    root = symbol[:-15].strip()
    if not root or len(suffix) != 15 or not (suffix[:6] + suffix[7:]).isdigit() or suffix[6] not in "CP":
        raise RuntimeError(f"invalid OCC contract symbol: {symbol}")
    expiry = date(2000 + int(suffix[:2]), int(suffix[2:4]), int(suffix[4:6])).isoformat()
    option_type = "call" if suffix[6] == "C" else "put"
    strike = int(suffix[7:]) / 1000
    if (row.get("expiration") != expiry or row.get("type") != option_type or
            row.get("strike") != strike or (row.get("root") and row["root"].strip() != root)):
        raise RuntimeError(f"OCC symbol disagrees with archive contract metadata: {symbol}")


def load_session(day: str, underlyings: set[str], rows_by_symbol: dict, sampled: dict) -> dict:
    prefix = f"oa203/scanner/{day}/"
    summary = json.loads(fetch(prefix + "summary.json"))
    if summary.get("trade_date") != day or not summary.get("final"):
        raise RuntimeError(f"{day}: summary is not a final session")
    session_open, session_close = summary.get("session_open_ms"), summary.get("session_close_ms")
    if (type(session_open) is not int or type(session_close) is not int or
            session_open < 0 or session_open >= session_close):
        raise RuntimeError(f"{day}: invalid session bounds")
    universe = json.loads(fetch(prefix + "universe.json"))
    if universe.get("trade_date") != day:
        raise RuntimeError(f"{day}: universe date mismatch")
    selected = {u["underlying"]: {"nearest": u["expiration"], "additional": u.get("additional_expiration")}
                for u in universe.get("selected", [])}
    manifest = [json.loads(line) for line in fetch(prefix + "sweeps/manifest.jsonl").decode().splitlines() if line.strip()]
    validate_manifest(manifest, summary, day, selected)
    kept = 0
    for entry in manifest:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        in_session_counts: dict[tuple[str, str], int] = defaultdict(int)
        raw_count = 0
        raw = gzip.GzipFile(fileobj=io.BytesIO(fetch(prefix + "sweeps/" + entry["file"])))
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            if not line.strip():
                continue
            raw_count += 1
            r = json.loads(line)
            if r.get("underlying") in underlyings:
                t = r.get("t")
                if (type(r.get("sweep")) is not int or r["sweep"] != entry["sweep"] or
                        type(t) is not int or not entry["started_ms"] <= t <= entry["ended_ms"]):
                    raise RuntimeError(f"{day}: row disagrees with sweep manifest")
                choice = selected.get(r["underlying"], {})
                expiry = r.get("expiration")
                if not r.get("symbol") or expiry not in choice.values():
                    raise RuntimeError(f"{day}: row expiry absent from selected universe")
                is_extra = expiry == choice.get("additional")
                if bool(r.get("collection_role") == "additional_expiry") != is_extra:
                    raise RuntimeError(f"{day}: row collection role disagrees with selected expiry")
                st = (entry.get("additional_chains", {}).get(r["underlying"], {}).get(expiry)
                      if is_extra else entry["chains"].get(r["underlying"]))
                if not st or st.get("status") != "ok" or st.get("fetched_ms") != t or \
                        (st.get("expiration") not in (None, expiry)):
                    raise RuntimeError(f"{day}: row lacks a successful matching chain fetch")
                counts[(r["underlying"], expiry)] += 1
                if session_open <= t < session_close:
                    in_session_counts[(r["underlying"], expiry)] += 1
                    rows_by_symbol[r["symbol"]].append(r)
                    kept += 1
        if entry.get("rows") is not None and raw_count != entry["rows"]:
            raise RuntimeError(f"{day}: sweep {entry['sweep']} total row count mismatch")
        for name in underlyings & selected.keys():
            choice = selected[name]
            for role in ("nearest", "additional"):
                expiry = choice.get(role)
                if not expiry:
                    continue
                st = (entry["chains"].get(name) if role == "nearest" else
                      entry.get("additional_chains", {}).get(name, {}).get(expiry))
                actual = counts[(name, expiry)]
                expected = st.get("rows") if st and st.get("status") == "ok" else 0
                if type(expected) is not int or actual != expected:
                    raise RuntimeError(f"{day}: {name} {expiry} sweep {entry['sweep']} row count mismatch")
                if in_session_counts[(name, expiry)]:
                    sampled[(name, expiry)].add(day)
    return {"day": day, "session": w.Session(day, summary["session_open_ms"], summary["session_close_ms"]),
            "sweeps": len(manifest), "rows_kept": kept, "final": bool(summary.get("final")),
            "assessment": summary["assessment"]["status"],
            "universe_selection_version": universe.get("selection_version"),
            "universe_selected_at": universe.get("selected_at"),
            "occ_sha256": universe.get("occ_sha256")}


def validate_calendar(sessions: list[w.Session]) -> set[tuple[int, int]]:
    """Verify exact NYSE sessions and identify complete calendar weeks."""
    import pandas_market_calendars as mcal

    days = [s.day for s in sessions]
    if days != sorted(set(days)):
        raise RuntimeError("session dates must be unique and sorted")
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    weeks = {(date.fromisoformat(d).isocalendar().year, date.fromisoformat(d).isocalendar().week) for d in days}
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=4 - last.weekday())
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    expected = {}
    for day, row in schedule.iterrows():
        key = day.date().isoformat()
        expected[key] = (int(row["market_open"].timestamp() * 1000),
                         int(row["market_close"].timestamp() * 1000))
    between = [d for d in expected if days[0] <= d <= days[-1]]
    if days != between:
        raise RuntimeError(f"archive session gap or non-session date: supplied={days}, expected={between}")
    for s in sessions:
        if (s.open_ms, s.close_ms) != expected[s.day]:
            raise RuntimeError(f"{s.day}: archive bounds disagree with NYSE calendar")
    return {yw for yw in weeks if {d for d in expected if date.fromisoformat(d).isocalendar()[:2] == yw}
            <= set(days)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--underlyings", nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--emit-root", type=Path, help="also write oa203/windows/ outputs under this local directory")
    ap.add_argument("--source-lock", type=Path, help="verify every source against a previous replay's SHA-256 lock")
    a = ap.parse_args()
    global EXPECTED_HASHES
    if a.source_lock:
        lock = json.loads(a.source_lock.read_text(encoding="utf-8"))
        EXPECTED_HASHES = {s["key"]: s["sha256"] for s in lock["sources"]}
    if len(a.underlyings) > MAX_UNDERLYINGS or len(a.days) > MAX_SESSIONS:
        ap.error("bounded replay: at most 50 underlyings and 10 sessions")
    a.out.mkdir(parents=True, exist_ok=True)
    unders = set(a.underlyings)
    rows_by_symbol: dict[str, list] = defaultdict(list)
    sampled: dict[str, set] = defaultdict(set)
    sessions_meta = []
    for day in sorted(a.days):
        meta = load_session(day, unders, rows_by_symbol, sampled)
        sessions_meta.append(meta)
        print(f"{day}: {meta['sweeps']} sweeps, kept {meta['rows_kept']} rows", flush=True)
    sessions = [m["session"] for m in sessions_meta]
    complete_weeks = validate_calendar(sessions)
    import pandas_market_calendars as mcal
    calendar_version = mcal.__version__
    policy = w.WindowPolicy()
    weekly, skipped = w.weekly_windows(sessions, policy)
    weekly = [win for win in weekly if date.fromisoformat(win.sessions[0].day).isocalendar()[:2] in complete_weeks]
    for yw in sorted({date.fromisoformat(s.day).isocalendar()[:2] for s in sessions} - complete_weeks):
        skipped.append({"window_id": f"weekly-{yw[0]}-W{yw[1]:02d}", "reason": "incomplete_calendar_week"})
    windows = weekly + w.overnight_windows(sessions, policy) + w.multi_session_windows(sessions, policy)

    contracts = []
    for symbol, rs in rows_by_symbol.items():
        rs.sort(key=lambda r: (r["t"], r["sweep"]))
        last = rs[-1]
        validate_contract_identity(symbol, last)
        identity = ("underlying", "type", "strike", "expiration", "contract_size")
        if any(any(r.get(k) != last.get(k) for k in identity) or
               (r.get("root") and last.get("root") and r["root"] != last["root"]) for r in rs):
            raise RuntimeError(f"contract identity changed within path: {symbol}")
        info = ContractInfo(symbol=symbol, underlying=last["underlying"], option_type=last["type"],
                            strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
                            contract_size=int(last.get("contract_size") or 100))
        path = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"), ask_ms=r.get("ask_ms"),
                            underlying_price=r.get("spot"), bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                            volume=r.get("volume"), open_interest=r.get("oi")) for r in rs]
        contracts.append((info, path))

    report = {"replay": "oa203_windows_replay", "issue": 130, "read_only": True,
              "calendar": {"name": "NYSE", "package": "pandas_market_calendars", "version": calendar_version},
              "policy": policy.as_dict(), "sessions": [{k: v for k, v in m.items() if k != "session"} for m in sessions_meta],
              "underlyings": sorted(unders), "contracts": len(contracts), "skipped_weeks": skipped,
              "label": "offline validation over sampled quotes with provisional defaults; not fills, not deployed collection",
              "windows": []}
    # A contract's chain counts as sampled in a session only if its underlying's
    # chain fetch succeeded AND this contract (its expiry) appears in that session.
    def contract_days(info, path):
        seen = {s.day for s in sessions for o in path if s.open_ms <= o.t < s.close_ms}
        return seen & sampled.get((info.underlying, info.expiration), set())
    days_by_symbol = {info.symbol: contract_days(info, path) for info, path in contracts}
    if EXPECTED_HASHES is not None and set(EXPECTED_HASHES) != {s["key"] for s in SOURCES}:
        raise RuntimeError("source lock includes a different artifact set")
    status = {m["day"]: {"final": m["final"], "assessment": m["assessment"], "sweeps": m["sweeps"]} for m in sessions_meta}
    generated = datetime.now(timezone.utc).isoformat()
    for win in windows:
        rows = [w.evaluate(win, info, path, days_by_symbol[info.symbol], policy) for info, path in contracts]
        if a.emit_root:
            days = {s.day for s in win.sessions}
            srcs = [x for x in SOURCES if x["key"].split("/")[2] in days]
            outputs.write_window(a.emit_root, win, rows, policy, {d: status[d] for d in days}, srcs, generated,
                                 {"mode": "offline_replay", "underlyings": sorted(unders),
                                  "calendar_package_version": calendar_version,
                                  "note": "bounded subset of the daily universe; not the full producer run"})
        summary = {**win.as_dict(), "evaluated": len(rows)}
        for basis in w.BASES:
            ranked = w.rank_window(rows, basis)
            elig = [r for r in ranked if r[f"{basis}_rank"]]
            reasons: dict[str, int] = defaultdict(int)
            for r in ranked:
                if not r[f"{basis}_rank"]:
                    for why in r[basis]["reasons"][:1]:
                        reasons[why.split(":")[0]] += 1
            summary[basis] = {"eligible": len(elig), "exclusion_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
                              "top": [{"rank": r[f"{basis}_rank"], "symbol": r["symbol"], "pct": r[basis]["pct"],
                                       "entry": r[basis]["entry"], "exit": r[basis]["exit"],
                                       "abs_change_per_contract": r[basis]["abs_change_per_contract"],
                                       "entry_session": r[basis]["entry_session"], "exit_session": r[basis]["exit_session"],
                                       "flags": r["flags"]} for r in elig[:a.top]]}
            if basis == "qualified":
                with open(a.out / f"{win.window_id}.csv", "w", newline="", encoding="utf-8") as f:
                    cols = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
                            "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]
                    wr = csv.writer(f); wr.writerow(cols)
                    for r in ranked:
                        b = r[basis]
                        wr.writerow([r[f"{basis}_rank"], r["symbol"], r["underlying"], r["option_type"], r["strike"], r["expiration"],
                                     b.get("pct"), b.get("entry"), b.get("exit"), b.get("abs_change_per_contract"),
                                     b.get("entry_session"), b.get("exit_session"), b.get("entry_ms"), b.get("exit_ms"),
                                     ";".join(r["flags"]), b["status"], ";".join(b["reasons"])])
        report["windows"].append(summary)
    (a.out / "replay_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (a.out / "source_lock.json").write_text(json.dumps({"sources": SOURCES}, indent=2) + "\n", encoding="utf-8")
    print(f"{len(windows)} windows over {len(contracts)} contracts -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
