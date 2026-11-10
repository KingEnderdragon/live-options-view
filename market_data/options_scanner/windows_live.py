"""Build and publish #130 windows from local finalized day archives.

This is the producer-to-publisher link for live window products:

* It reads ONLY local day directories (the scanner host's spool, the same
  files ``finalize`` wrote: summary.json, universe.json,
  sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz). It never touches a
  provider and never reads R2.
* ``build_windows`` writes a local emit root (``oa203/windows/`` tree).
  ``publish_windows`` publishes an emit root — or, with ``window_ids``,
  exactly the windows one cycle built — to an injected backend.
  The CLI's ``windows-publish`` only offers the local-filesystem backend;
  R2 publication is a release step (Chej) via ``windows_publish.r2_backend``.
* The production hook is ``run_after_finalize`` (called from
  ``session.finalize``): it runs only when the env flag
  ``OA203_WINDOWS_LIVE_ENABLED == "1"`` (disabled by default). Enabling
  the flag is live activation (Chej alone, after review); the code only
  implements the hook.
* ``build_windows`` defaults to a bounded 50-underlying subset
  (``universe_scope: bounded_subset`` — always served partial, never
  final). ``full_universe=True`` loads the complete selected universe
  (at most 500 underlyings PER DAY — the cap is daily, not on the
  union: the per-day selected set may change day to day, so the union
  over the window may exceed 500; the union is bounded by 10 sessions
  x 500) and combines it before ranking (see ``windows_combine``); the
  passed underlyings must cover every selected underlying of every
  loaded day, or the build fails closed.

The loader reuses ``windows_replay.load_session`` exactly (same manifest,
calendar, row and contract-identity validation) by pointing its ``fetch``
at the local day directory for one call. That is the point: one
implementation of the validation, no fork. A final day assessed
``partial`` is admitted with explicit ``session_partial:<day>`` reasons
(see ``windows_replay.validate_manifest``); a torn or invalid manifest
is still refused fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import market_data.options_scanner.windows as w
import market_data.options_scanner.windows_combine as combine
import market_data.options_scanner.windows_outputs as outputs
import market_data.options_scanner.windows_publish as pub
import market_data.options_scanner.windows_replay as replay
from market_data.options_scanner.returns import ContractInfo, Observation

MAX_UNDERLYINGS = 50
MAX_UNDERLYINGS_FULL = 500  # live universe size (Config.universe_size default)
MAX_SESSIONS = 10
WINDOWS_LIVE_ENV = "OA203_WINDOWS_LIVE_ENABLED"


def load_local_session(day_dir: Path, underlyings: set[str],
                       rows_by_symbol: dict, sampled: dict) -> dict:
    """Load one finalized local day archive through the replay loader.

    ``day_dir`` must be named for its ISO date (e.g. ``2026-10-05``) and
    hold summary.json, universe.json and sweeps/ exactly as ``finalize``
    wrote them. Returns the replay session metadata plus ``sources`` (the
    local files read, each with its SHA-256). Raises on any missing,
    torn, non-final or invalid day; a final day assessed partial is
    admitted (see ``windows_replay.validate_manifest``): nothing is
    invented.
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


def selected_underlyings(day_dirs: Sequence[Path]) -> set[str]:
    """Union of selected underlyings across local day archives."""
    out: set[str] = set()
    for d in day_dirs:
        universe = json.loads((Path(d) / "universe.json").read_text(encoding="utf-8"))
        out.update(u["underlying"] for u in universe.get("selected", []))
    return out


def finalized_day_dirs(spool: Path, *, through_day: date | None = None,
                       limit: int = MAX_SESSIONS) -> list[Path]:
    """Finalized local day directories in the spool, oldest first.

    A directory counts as finalized when it holds a summary.json with
    ``final: true``; a final-but-partial day counts (its partiality is
    carried as session_partial reasons by the loader). Torn or non-final
    days are skipped here and would still fail closed if passed to
    build_windows directly.
    """
    spool = Path(spool)
    dirs = []
    for day_dir in sorted(p for p in spool.iterdir() if p.is_dir()):
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if through_day is not None and day > through_day:
            continue
        summary_path = day_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            if not json.loads(summary_path.read_text(encoding="utf-8")).get("final"):
                continue
        except ValueError:
            continue
        dirs.append(day_dir)
    return dirs[-limit:] if limit else dirs


def windows_live_enabled() -> bool:
    """Live window hook enablement: env flag only, default off."""
    return os.getenv(WINDOWS_LIVE_ENV) == "1"


def run_after_finalize(spool_dir: Path | str, day, universe: Mapping[str, Any], *,
                       backend: pub.StorageBackend | None = None,
                       emit_root: Path | str | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Build + publish windows for the finalized day (hook implementation).

    Builds from the finalized day archives in the spool (up to 10
    sessions ending at ``day``) over the complete selected universe,
    then publishes to ``backend`` (default: the R2 backend — constructed
    only here, i.e. only when the hook actually runs). Callers must gate
    this on ``windows_live_enabled()``; this function itself performs
    the build + publish and must never be called from an unflagged path.
    Returns the build report plus per-window publish results.

    Per-cycle output: one cycle (one finalized day) writes to its own
    emit root (``<emit_root>/<day>``, or ``<spool>/windows_emit/<day>``
    by default) and records exactly which windows it built in
    ``cycle.json`` there. ``publish_windows`` is then given that
    explicit list -- never a directory scan of an accumulated emit root --
    so stale windows from earlier cycles are never republished and an
    unchanged re-run of the same cycle is idempotent: each window's
    summary bytes are a function of that window's own inputs only
    (stable per-window generated_at from its exit-session close,
    window-scoped scope and rows), so the rebuild is byte-identical
    for unchanged inputs and the publisher returns
    ``already_published`` with no new revision and no re-upload. A
    retry at a later wall-clock time, or a next-day cycle overlapping
    the same windows, republishes only genuinely changed/added window
    products; unchanged historical windows stay already_published.
    """
    spool = Path(spool_dir)
    day = date.fromisoformat(day) if isinstance(day, str) else day
    dirs = finalized_day_dirs(spool, through_day=day)
    if not dirs:
        raise ValueError(f"no finalized day archives in spool {spool}")
    today_selected = {u["underlying"] for u in universe.get("selected", [])}
    unders = selected_underlyings(dirs)
    if today_selected and not today_selected <= unders:
        raise ValueError(f"finalized day {day.isoformat()}: universe disagrees with its own archive")
    cycle = day.isoformat()
    emit = (Path(emit_root) / cycle) if emit_root is not None else spool / "windows_emit" / cycle
    report = build_windows(dirs, unders, emit, full_universe=True, generated_at=now)
    window_ids = sorted({(w["kind"], w["window_id"]) for w in report["windows"]})
    manifest = {"cycle": cycle, "day": day.isoformat(), "days": [d.name for d in dirs],
                "underlyings": sorted(unders), "universe_scope": report["universe_scope"],
                "generated_at": report["generated_at"],
                "windows": [[kind, window_id] for kind, window_id in window_ids]}
    (emit / "cycle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    results = publish_windows(emit, backend or pub.r2_backend(),
                              window_ids={(kind, window_id) for kind, window_id in window_ids},
                              now=now)
    return {"enabled": True, "cycle": cycle, "day": day.isoformat(), "days": [d.name for d in dirs],
            "underlyings": sorted(unders), "emit_root": str(emit),
            "build": report, "publish": results}


def build_windows(day_dirs: list[Path] | tuple[Path, ...], underlyings: set[str],
                  emit_root: Path, *, top: int = 15, full_universe: bool = False,
                  generated_at: datetime | str | None = None) -> dict[str, Any]:
    """Evaluate every supported window kind over local finalized days.

    ``day_dirs`` are finalized day-archive directories (sorted by date,
    at most 10 sessions); ``underlyings`` is an explicit set of
    underlyings. By default at most 50 (a bounded subset, served
    partial as ``bounded_universe_subset`` — it never claims
    full-universe completion). With ``full_universe=True`` the cap is
    per day, not on the union: each loaded day selects at most 500
    underlyings (the live universe size) and the passed set must cover
    every selected underlying of every loaded day; the per-day set may
    rotate, so the union over the window may exceed 500 (bounded by 10
    sessions x 500). The combined full universe is ranked before the
    shortlist is cut (see ``windows_combine``).
    Writes the ``oa203/windows/`` tree under ``emit_root`` with scope
    ``live_archive``. A full-universe build keeps a genuinely final
    window final; a bounded-subset build is served partial as
    ``bounded_universe_subset`` (replay emits stay forced partial as
    ``bounded_universe_replay``). Returns the build report. Writes
    nothing outside ``emit_root`` and touches no provider or R2.

    Stable product identity: each window's summary bytes are a function
    of that window's own inputs only (its sessions, source locks,
    per-basis rows/counts/evidence, state and reasons). generated_at is
    the window's exit-session close, scope lists only the window's own
    sessions' selected underlyings, and rows cover only contracts with
    observations in the window's sessions. The cycle wall clock
    (``generated_at`` parameter / report ``generated_at``) is cycle
    metadata and never enters a window summary, so a retry at a later
    wall-clock time rebuilds byte-identical summaries for unchanged
    windows and the publisher returns already_published with no fresh
    revision. A genuinely changed window still gets a new revision.
    """
    dirs = [Path(d) for d in day_dirs]
    if not 1 <= len(dirs) <= MAX_SESSIONS:
        raise ValueError(f"bounded build: 1..{MAX_SESSIONS} sessions, got {len(dirs)}")
    unders = set(underlyings)
    if full_universe:
        # Per-day cap, not a union cap: MAX_UNDERLYINGS_FULL is the live
        # universe size for ONE day. The per-day selected set may rotate
        # (a replacement underlying enters on a later day), so the union
        # over the window may exceed 500 — 500 + one replacement is 501
        # and must NOT fail. The union is bounded by 10 sessions x 500.
        if not unders:
            raise ValueError("bounded build: at least 1 underlying")
        if len(unders) > MAX_SESSIONS * MAX_UNDERLYINGS_FULL:
            raise ValueError(f"bounded build: union over at most {MAX_SESSIONS} sessions x "
                             f"{MAX_UNDERLYINGS_FULL} underlyings, got {len(unders)}")
        for d in dirs:
            selected = len(selected_underlyings([d]))
            if selected > MAX_UNDERLYINGS_FULL:
                raise ValueError(f"full_universe: {d.name} selects {selected} underlyings, "
                                 f"over the per-day cap {MAX_UNDERLYINGS_FULL}")
    elif not 1 <= len(underlyings) <= MAX_UNDERLYINGS:
        raise ValueError(f"bounded build: 1..{MAX_UNDERLYINGS} underlyings, got {len(underlyings)}")
    if [d.name for d in dirs] != sorted(d.name for d in dirs) or len({d.name for d in dirs}) != len(dirs):
        raise ValueError("day directories must be unique and sorted by date")
    rows_by_symbol: dict[str, list] = defaultdict(list)
    sampled: dict[str, set] = defaultdict(set)
    sessions_meta = [load_local_session(d, unders, rows_by_symbol, sampled) for d in dirs]
    sessions = [m["session"] for m in sessions_meta]
    if full_universe:
        for d in dirs:
            missing = selected_underlyings([d]) - unders
            if missing:
                raise ValueError(f"full_universe: {d.name} selected underlyings not covered: {sorted(missing)[:5]}")
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

    # One candidate path per (symbol, underlying) attribution. A symbol
    # seen under more than one underlying is NOT stitched into a single
    # path: each attribution is evaluated separately and resolved by
    # windows_combine (pure duplicate -> one row; differing evidence ->
    # all attributions excluded, conflict recorded).
    candidates: list[tuple[ContractInfo, list[Observation], str]] = []
    for symbol, rs in rows_by_symbol.items():
        by_under: dict[str, list] = defaultdict(list)
        for r in rs:
            by_under[r["underlying"]].append(r)
        for under, urs in by_under.items():
            urs.sort(key=lambda r: (r["t"], r["sweep"]))
            last = urs[-1]
            replay.validate_contract_identity(symbol, last)
            identity = ("underlying", "type", "strike", "expiration", "contract_size")
            if any(any(r.get(k) != last.get(k) for k in identity) or
                   (r.get("root") and last.get("root") and r["root"] != last["root"]) for r in urs):
                raise RuntimeError(f"contract identity changed within path: {symbol} ({under})")
            info = ContractInfo(symbol=symbol, underlying=last["underlying"], option_type=last["type"],
                                strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
                                contract_size=int(last.get("contract_size") or 100))
            path = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"),
                                ask_ms=r.get("ask_ms"), underlying_price=r.get("spot"),
                                bid_size=r.get("bid_size"), ask_size=r.get("ask_size"),
                                volume=r.get("volume"), open_interest=r.get("oi")) for r in urs]
            candidates.append((info, path, under))

    def contract_days(info: ContractInfo, path: list[Observation]) -> set[str]:
        seen = {s.day for s in sessions for o in path if s.open_ms <= o.t < s.close_ms}
        return seen & sampled.get((info.underlying, info.expiration), set())

    candidate_days = [contract_days(info, path) for info, path, _ in candidates]
    all_sources = [s for m in sessions_meta for s in m["sources"]]
    status = {m["day"]: {"final": m["final"], "assessment": m["assessment"],
                         "assessment_reasons": m.get("assessment_reasons", []),
                         "sweeps": m["sweeps"]} for m in sessions_meta}
    if generated_at is None:
        generated = datetime.now(timezone.utc).isoformat()
    elif isinstance(generated_at, datetime):
        generated = generated_at.isoformat()
    else:
        generated = str(generated_at)
    universe_scope = "full_universe" if full_universe else "bounded_subset"
    all_conflicts: list[dict] = []
    kept_total = 0
    report: dict[str, Any] = {"builder": "windows_live", "mode": "live_archive",
                              "generated_at": generated,
                              "universe_scope": universe_scope,
                              "calendar": {"name": "NYSE", "package": "pandas_market_calendars",
                                           "version": calendar_version},
                              "policy": policy.as_dict(),
                              "sessions": [{k: v for k, v in m.items() if k not in ("session", "sources")}
                                           for m in sessions_meta],
                              "underlyings": sorted(unders), "contracts": len(candidates),
                              "contracts_kept": 0, "conflicts": all_conflicts,
                              "conflicting_symbols": 0,
                              "skipped_weeks": skipped, "windows": []}
    dir_by_day = {d.name: d for d in dirs}
    for win in windows:
        days = {s.day for s in win.sessions}
        # Window-scoped rows: only contracts with in-session observations
        # in THIS window's own sessions are evaluated for it. A contract
        # first sampled on an unrelated later day (or rotated out of the
        # window's days) contributes no row here, so unrelated universe
        # rotation cannot leak rows into a historical window.
        evaluated = [(under, w.evaluate(win, info, path, cd, policy))
                     for (info, path, under), cd in zip(candidates, candidate_days)
                     if cd & days]
        rows, conflicts, _stats = combine.combine_rows(
            win.window_id, [(under, [row]) for under, row in evaluated])
        for c in conflicts:
            all_conflicts.append({"window_id": win.window_id, "symbol": c["symbol"],
                                  "attributions": c["attributions"]})
        kept_total += len(rows)
        srcs = [x for x in all_sources if x["key"].split("/")[2] in days]
        # Stable canonical summary bytes (retry fix): the summary is a
        # function of THIS window's own inputs only, so an unchanged
        # window rebuilds byte-identically and the released
        # rev_<sha256(summary.json)> genuinely denotes the same content
        # (no protocol change: the revision is still the SHA-256 of the
        # summary bytes). generated_at is the window's exit-session close
        # -- its data-as-of time -- NOT the cycle wall clock; scope lists
        # only underlyings selected on the window's own sessions and in
        # this build's universe; the calendar package version is build
        # metadata in the report, not a window input.
        win_generated = datetime.fromtimestamp(
            win.sessions[-1].close_ms / 1000, tz=timezone.utc).isoformat()
        win_unders = sorted(selected_underlyings([dir_by_day[d] for d in days]) & unders)
        outputs.write_window(Path(emit_root), win, rows, policy,
                             {d: status[d] for d in days}, srcs, win_generated,
                             {"mode": "live_archive", "universe_scope": universe_scope,
                              "underlyings": win_unders,
                              "note": ("built from the complete selected universe on the scanner host; "
                                       "no provider, no R2 read") if full_universe else
                                      ("built from a bounded subset of the selected universe on the scanner host; "
                                       "not the full universe; no provider, no R2 read")})
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
    report["contracts_kept"] = kept_total
    report["conflicting_symbols"] = len({c["symbol"] for c in all_conflicts})
    return report


def publish_windows(emit_root: Path, backend: pub.StorageBackend, *,
                    budget: pub.WindowBudget | None = None,
                    window_ids: set[tuple[str, str]] | None = None,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Publish windows in a local emit root to ``backend``.

    With ``window_ids`` given, publishes ONLY those ``(kind, window_id)``
    pairs — the explicit list of windows one cycle built — and refuses
    if a listed window is missing from the emit root (checked before
    anything is published). Without it, every window in the emit root
    is published in sorted order (the manual CLI behaviour). Either way
    each window goes through ``windows_publish.publish_window``
    (validation, budget reservation, readback, pointer last). Returns
    the per-window pointer payloads. The caller chooses the backend:
    tests and the CLI use ``LocalFilesystemBackend``; R2 is a release
    step (Chej) via ``windows_publish.r2_backend``. Raises (and
    publishes nothing further) on the first invalid, over-budget or
    failed window.
    """
    root = Path(emit_root) / "oa203" / "windows" / w.WINDOW_POLICY_VERSION
    if not root.is_dir():
        raise ValueError(f"no windows emit root at {root}")
    planned: list[tuple[str, str]] = []
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for win_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            if window_ids is not None and (kind_dir.name, win_dir.name) not in window_ids:
                continue
            planned.append((kind_dir.name, win_dir.name))
    if window_ids is not None:
        missing = sorted(window_ids - set(planned))
        if missing:
            raise ValueError(f"emit root is missing cycle windows: {missing}")
    if not planned:
        raise ValueError(f"no windows found under {root}")
    results: list[dict[str, Any]] = []
    for kind, window_id in planned:
        results.append(pub.publish_window(Path(emit_root), kind, window_id,
                                          backend, budget=budget, now=now))
    return results


def _report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
