"""Build and publish #130 windows from local finalized day archives.

This is the missing producer-to-publisher link for live window products,
and it is deliberately dormant:

* It reads ONLY local day directories (the scanner host's spool, the same
  files ``finalize`` wrote: summary.json, universe.json,
  sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz). It never touches a
  provider, never reads R2, and never uploads.
* ``build_windows`` writes a local emit root (``oa203/windows/`` tree).
  ``publish_windows`` publishes that emit root to an injected backend.
  The CLI's ``windows-publish`` only offers the local-filesystem backend;
  R2 publication is a release step (Chej) via ``windows_publish.r2_backend``.
* NOTHING here runs on a schedule, hooks into ``run_day``/``finalize``,
  or sets any flag. The live trigger (call this after finalize, on what
  cadence, under what flag, on which Railway service setting) is a
  separate, unapproved step: see docs/130-live-window-wiring.md.

The loader reuses ``windows_replay.load_session`` exactly (same manifest,
calendar, row and contract-identity validation) by pointing its ``fetch``
at the local day directory for one call. That is the point: one
implementation of the validation, no fork. A day whose daily assessment
is not ``complete`` is refused by that validation (fail-closed); see the
wiring doc for the partial-day gap that leaves open.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import market_data.options_scanner.windows as w
import market_data.options_scanner.windows_outputs as outputs
import market_data.options_scanner.windows_publish as pub
import market_data.options_scanner.windows_replay as replay
from market_data.options_scanner.returns import ContractInfo, Observation

MAX_UNDERLYINGS = 50
MAX_SESSIONS = 10


def load_local_session(day_dir: Path, underlyings: set[str],
                       rows_by_symbol: dict, sampled: dict) -> dict:
    """Load one finalized local day archive through the replay loader.

    ``day_dir`` must be named for its ISO date (e.g. ``2026-10-05``) and
    hold summary.json, universe.json and sweeps/ exactly as ``finalize``
    wrote them. Returns the replay session metadata plus ``sources`` (the
    local files read, each with its SHA-256). Raises on any missing,
    torn, non-final or non-complete day: nothing is invented.
    """
    day = day_dir.name
    date.fromisoformat(day)  # ValueError on a non-date directory name
    prefix = f"oa203/scanner/{day}/"
    sources: list[dict] = []

    def fetch(key: str) -> bytes:
        if not key.startswith(prefix):
            raise RuntimeError(f"{day}: unexpected archive key: {key}")
        path = day_dir / key[len(prefix):]
        if not path.is_file():
            raise RuntimeError(f"{day}: missing local archive file: {key[len(prefix):]}")
        body = path.read_bytes()
        sources.append({"key": key, "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body), "local": True})
        return body

    original_fetch = replay.fetch
    replay.fetch = fetch  # type: ignore[method-assign]
    try:
        meta = replay.load_session(day, underlyings, rows_by_symbol, sampled)
    finally:
        replay.fetch = original_fetch
    meta["sources"] = sources
    return meta


def build_windows(day_dirs: list[Path] | tuple[Path, ...], underlyings: set[str],
                  emit_root: Path, *, top: int = 15) -> dict[str, Any]:
    """Evaluate every supported window kind over local finalized days.

    ``day_dirs`` are finalized day-archive directories (sorted by date,
    at most 10 sessions); ``underlyings`` is an explicit bounded set
    (at most 50, mirroring the replay bound — a full-universe emit needs
    the combine-emit step, see docs/130-live-window-wiring.md).
    Writes the ``oa203/windows/`` tree under ``emit_root`` with scope
    ``live_archive`` (unlike replay emits, a genuinely final window
    stays final: ``write_window`` only forces partial for
    ``offline_replay`` scope). Returns the build report. Writes nothing
    outside ``emit_root`` and touches no provider or R2.
    """
    dirs = [Path(d) for d in day_dirs]
    if not 1 <= len(dirs) <= MAX_SESSIONS:
        raise ValueError(f"bounded build: 1..{MAX_SESSIONS} sessions, got {len(dirs)}")
    if not 1 <= len(underlyings) <= MAX_UNDERLYINGS:
        raise ValueError(f"bounded build: 1..{MAX_UNDERLYINGS} underlyings, got {len(underlyings)}")
    if [d.name for d in dirs] != sorted(d.name for d in dirs) or len({d.name for d in dirs}) != len(dirs):
        raise ValueError("day directories must be unique and sorted by date")
    unders = set(underlyings)
    rows_by_symbol: dict[str, list] = defaultdict(list)
    sampled: dict[str, set] = defaultdict(set)
    sessions_meta = [load_local_session(d, unders, rows_by_symbol, sampled) for d in dirs]
    sessions = [m["session"] for m in sessions_meta]
    complete_weeks = replay.validate_calendar(sessions)
    import pandas_market_calendars as mcal
    calendar_version = mcal.__version__
    policy = w.WindowPolicy()
    weekly, skipped = w.weekly_windows(sessions, policy)
    weekly = [win for win in weekly
              if date.fromisoformat(win.sessions[0].day).isocalendar()[:2] in complete_weeks]
    for yw in sorted({date.fromisoformat(s.day).isocalendar()[:2] for s in sessions} - complete_weeks):
        skipped.append({"window_id": f"weekly-{yw[0]}-W{yw[1]:02d}", "reason": "incomplete_calendar_week"})
    windows = weekly + w.overnight_windows(sessions, policy) + w.multi_session_windows(sessions, policy)

    contracts = []
    for symbol, rs in rows_by_symbol.items():
        rs.sort(key=lambda r: (r["t"], r["sweep"]))
        last = rs[-1]
        replay.validate_contract_identity(symbol, last)
        identity = ("underlying", "type", "strike", "expiration", "contract_size")
        if any(any(r.get(k) != last.get(k) for k in identity) or
               (r.get("root") and last.get("root") and r["root"] != last["root"]) for r in rs):
            raise RuntimeError(f"contract identity changed within path: {symbol}")
        info = ContractInfo(symbol=symbol, underlying=last["underlying"], option_type=last["type"],
                            strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
                            contract_size=int(last.get("contract_size") or 100))
        path = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"),
                            ask_ms=r.get("ask_ms"), underlying_price=r.get("spot"),
                            bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                            volume=r.get("volume"), open_interest=r.get("oi")) for r in rs]
        contracts.append((info, path))

    def contract_days(info: ContractInfo, path: list[Observation]) -> set[str]:
        seen = {s.day for s in sessions for o in path if s.open_ms <= o.t < s.close_ms}
        return seen & sampled.get((info.underlying, info.expiration), set())

    days_by_symbol = {info.symbol: contract_days(info, path) for info, path in contracts}
    all_sources = [s for m in sessions_meta for s in m["sources"]]
    status = {m["day"]: {"final": m["final"], "assessment": m["assessment"],
                         "sweeps": m["sweeps"]} for m in sessions_meta}
    generated = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {"builder": "windows_live", "mode": "live_archive",
                              "calendar": {"name": "NYSE", "package": "pandas_market_calendars",
                                           "version": calendar_version},
                              "policy": policy.as_dict(),
                              "sessions": [{k: v for k, v in m.items() if k not in ("session", "sources")}
                                           for m in sessions_meta],
                              "underlyings": sorted(unders), "contracts": len(contracts),
                              "skipped_weeks": skipped, "windows": []}
    for win in windows:
        rows = [w.evaluate(win, info, path, days_by_symbol[info.symbol], policy)
                for info, path in contracts]
        days = {s.day for s in win.sessions}
        srcs = [x for x in all_sources if x["key"].split("/")[2] in days]
        outputs.write_window(Path(emit_root), win, rows, policy,
                             {d: status[d] for d in days}, srcs, generated,
                             {"mode": "live_archive", "underlyings": sorted(unders),
                              "calendar_package_version": calendar_version,
                              "note": "built from local finalized day archives on the scanner host; "
                                      "no provider, no R2 read"})
        summary: dict[str, Any] = {**win.as_dict(), "evaluated": len(rows)}
        for basis in w.BASES:
            ranked = w.rank_window(rows, basis)
            elig = [r for r in ranked if r[f"{basis}_rank"]]
            reasons: dict[str, int] = defaultdict(int)
            for r in ranked:
                if not r[f"{basis}_rank"]:
                    for why in r[basis]["reasons"][:1]:
                        reasons[why.split(":")[0]] += 1
            summary[basis] = {"eligible": len(elig),
                              "exclusion_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
                              "top": [{"rank": r[f"{basis}_rank"], "symbol": r["symbol"],
                                       "pct": r[basis]["pct"]} for r in elig[:top]]}
        report["windows"].append(summary)
    return report


def publish_windows(emit_root: Path, backend: pub.StorageBackend, *,
                    budget: pub.WindowBudget | None = None,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Publish every window in a local emit root to ``backend``.

    Walks ``emit_root/oa203/windows/<policy>/<kind>/<window_id>/`` in
    sorted order and calls ``windows_publish.publish_window`` for each
    (validation, budget reservation, readback, pointer last). Returns the
    per-window pointer payloads. The caller chooses the backend: tests
    and the CLI use ``LocalFilesystemBackend``; R2 is a release step
    (Chej) via ``windows_publish.r2_backend``. Raises (and publishes
    nothing further) on the first invalid, over-budget or failed window.
    """
    root = Path(emit_root) / "oa203" / "windows" / w.WINDOW_POLICY_VERSION
    if not root.is_dir():
        raise ValueError(f"no windows emit root at {root}")
    results: list[dict[str, Any]] = []
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for win_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            results.append(pub.publish_window(Path(emit_root), kind_dir.name, win_dir.name,
                                              backend, budget=budget, now=now))
    if not results:
        raise ValueError(f"no windows found under {root}")
    return results


def _report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
