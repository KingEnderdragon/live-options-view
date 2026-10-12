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
from oa203_returns import ContractInfo, Observation  # noqa: E402

ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev"
MAX_UNDERLYINGS = 50
MAX_SESSIONS = 10


def fetch(key: str, attempts: int = 4) -> bytes:
    """GET one public artifact; bounded retries with backoff on transport errors."""
    req = urllib.request.Request(f"{ORIGIN}/{key}", headers={"User-Agent": "oa203-windows-replay/1"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def load_session(day: str, underlyings: set[str], rows_by_symbol: dict, sampled: dict) -> dict:
    prefix = f"oa203/scanner/{day}/"
    summary = json.loads(fetch(prefix + "summary.json"))
    if summary.get("trade_date") != day or not summary.get("final"):
        raise RuntimeError(f"{day}: summary is not a final session")
    manifest = [json.loads(line) for line in fetch(prefix + "sweeps/manifest.jsonl").decode().splitlines() if line.strip()]
    for entry in manifest:
        for u, st in entry.get("chains", {}).items():
            if u in underlyings and st.get("status") == "ok":
                sampled[u].add(day)
    kept = 0
    for entry in manifest:
        raw = gzip.GzipFile(fileobj=io.BytesIO(fetch(prefix + "sweeps/" + entry["file"])))
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("underlying") in underlyings and r.get("symbol"):
                rows_by_symbol[r["symbol"]].append(r)
                kept += 1
    return {"day": day, "session": w.Session(day, summary["session_open_ms"], summary["session_close_ms"]),
            "sweeps": len(manifest), "rows_kept": kept, "assessment": summary["assessment"]["status"],
            "universe_digest": None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--underlyings", nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
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
    policy = w.WindowPolicy()
    weekly, skipped = w.weekly_windows(sessions, policy)
    windows = weekly + w.overnight_windows(sessions, policy) + w.multi_session_windows(sessions, policy)

    contracts = []
    for symbol, rs in rows_by_symbol.items():
        rs.sort(key=lambda r: (r["t"], r["sweep"]))
        last = rs[-1]
        info = ContractInfo(symbol=symbol, underlying=last["underlying"], option_type=last["type"],
                            strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
                            contract_size=int(last.get("contract_size") or 100))
        path = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"), ask_ms=r.get("ask_ms"),
                            underlying_price=r.get("spot"), bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                            volume=r.get("volume"), open_interest=r.get("oi")) for r in rs]
        contracts.append((info, path))

    report = {"replay": "oa203_windows_replay", "issue": 130, "read_only": True,
              "policy": policy.as_dict(), "sessions": [{k: v for k, v in m.items() if k != "session"} for m in sessions_meta],
              "underlyings": sorted(unders), "contracts": len(contracts), "skipped_weeks": skipped,
              "label": "offline validation over sampled quotes with provisional defaults; not fills, not deployed collection",
              "windows": []}
    # A contract's chain counts as sampled in a session only if its underlying's
    # chain fetch succeeded AND this contract (its expiry) appears in that session.
    def contract_days(info, path):
        seen = {s.day for s in sessions for o in path if s.open_ms <= o.t < s.close_ms}
        return seen & sampled[info.underlying]
    days_by_symbol = {info.symbol: contract_days(info, path) for info, path in contracts}
    for win in windows:
        rows = [w.evaluate(win, info, path, days_by_symbol[info.symbol], policy) for info, path in contracts]
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
    print(f"{len(windows)} windows over {len(contracts)} contracts -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
