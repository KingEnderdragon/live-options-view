#!/usr/bin/env python3
"""Combine per-batch #130 window emits into one full-universe emit.

This is the combine-emit step the first-window plan recorded as missing
(docs/130-first-window-publication-plan.md): the preserved #130 full
universe is 661 underlyings in 14 batches of at most 50, and no single
bounded replay sees the whole universe. A per-batch ranking is a subset
ranking: the batch-01 qualified top (ALAB261009C00410000, pct 2.366667)
is not the full-universe top (CEG261009C00280000, pct 15.866667), so the
universe MUST be combined and ranked before the shortlist is cut.

Two entry points share one conflict rule:

* :func:`combine_rows` — evaluated rows (``windows.evaluate`` output, all
  three bases plus path evidence), as produced in-process by
  ``windows_live.build_windows`` when one symbol is seen under more than
  one underlying.
* :func:`combine_emit` — per-batch ``oa203/windows/`` emit trees on disk
  (``summary.json`` / ``rankings.csv`` / ``paths.csv.gz`` per window),
  combined into one emit tree with a conflict report.

Conflict semantics (fail closed, same as ``combine-full-universe.py`` in
the preserved #130 checkpoint, which this module follows):

A contract (OCC symbol) that appears under more than one attribution
with DIFFERING evidence — different per-basis status, reasons, values,
sessions or timestamps, different quote evidence (bid/ask, quote stamps,
sizes, ages, spreads) on ANY basis, or different identity/quality fields
— is a conflict, not a duplicate. A difference in any one of those fields
is a conflict; only rows identical in ALL of them are pure duplicates. Its identity/deliverable/path evidence is ambiguous, so it is
EXCLUDED from the combined ranked output: it is not ranked, not eligible,
and no canonical economic identity is asserted from root spelling. Its
paths are NOT stitched. Every attribution is preserved in the conflict
report (one record per attribution). A symbol whose rows are identical
in evidence is a pure duplicate and is deduped to one row. Nothing is
silently chosen.

Batch validation (fail closed): every batch emit root must exist and
hold the same set of windows with the same kind, window id, policy
version and sessions; a missing, unexpected or duplicate batch, or
windows whose session sets disagree, FAILS instead of producing a
partial result. Ranks are cut only after the combined universe is
ranked (``windows_outputs.rankings_csv`` ranks the full row set, then
cuts the shortlist); the combined emit carries all bases and the full
paths evidence for every kept row.

This module only reads local emit trees and writes a local emit tree.
Publishing the combined emit is ``windows_publish`` (offline-tested
only); live R2 publication is a release step (Chej).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import market_data.options_scanner.windows as w
import market_data.options_scanner.windows_outputs as outputs

POLICY = w.WINDOW_POLICY_VERSION

# Evidence fields that must agree for two CSV rows of the same symbol to
# be a pure duplicate rather than a conflict. Attribution (underlying /
# batch) may differ; evidence must not. Rank is recomputed globally, so
# it is not evidence.
CSV_EVIDENCE_FIELDS = ["status", "reasons", "pct", "entry", "exit",
                       "abs_change_per_contract", "entry_session", "exit_session",
                       "entry_ms", "exit_ms", "option_type", "strike",
                       "expiration", "flags"]
# Per-basis fields carried in the conflict report for every basis (the
# flat qualified-basis columns above are kept for compatibility; a report
# that only shows one basis hides the field that actually differed).
_PER_BASIS_CONFLICT_FIELDS = ["status", "reasons", "pct", "entry", "exit",
                              "abs_change_per_contract", "entry_session", "exit_session",
                              "entry_ms", "exit_ms", "entry_ask_ms", "exit_ask_ms",
                              "entry_bid_ms", "exit_bid_ms", "entry_ask", "exit_ask"]
CONFLICT_COLUMNS = ["window", "symbol", "batch", "underlying", "option_type",
                    "strike", "expiration", "status", "reasons", "pct", "entry",
                    "exit", "abs_change_per_contract", "entry_session",
                    "exit_session", "entry_ms", "exit_ms", "flags"] + [
    f"{basis}_{field}" for basis in w.BASES for field in _PER_BASIS_CONFLICT_FIELDS]


# Per-basis evidence fields: every economically relevant field the paths
# file carries for a basis (windows_outputs.BASIS_COLUMNS) — status,
# reasons, values, sessions, boundary offsets and the full quote evidence
# (bid/ask, quote stamps, sizes, ages, spreads). A difference in ANY of
# these is a conflict, never a silent dedup. This mirrors the #130
# checkpoint's EVIDENCE_FIELDS (combine-full-universe.py: status,
# reasons, pct, entry, exit, abs_change_per_contract, entry/exit session,
# entry/exit ms, option_type, strike, expiration, flags) and extends it
# from the flat CSV row to the evaluated row's full per-basis evidence;
# nothing in that list is narrowed.
def _row_evidence(row: Mapping[str, Any]) -> tuple:
    """Hashable evidence for one evaluated row (all bases + identity)."""
    parts: list[Any] = []
    for basis in w.BASES:
        b = row.get(basis) or {}
        parts.append((basis, tuple(
            tuple(b.get(c) or []) if c == "reasons" else b.get(c)
            for c in outputs.BASIS_COLUMNS)))
    parts.append((row.get("option_type"), row.get("strike"), row.get("expiration"),
                  row.get("root"), row.get("contract_size"),
                  tuple(sorted(row.get("flags") or [])),
                  tuple(row.get("contract_exclusions") or [])))
    return tuple(parts)


def _csv_evidence(row: Mapping[str, Any]) -> tuple:
    return tuple(row.get(k, "") for k in CSV_EVIDENCE_FIELDS)


def _group_symbols(attributions: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
                   evidence_fn) -> tuple[list, list[dict], dict[str, Any]]:
    """Group rows by OCC symbol; split pure duplicates from conflicts.

    Returns (kept, conflicts, stats). ``conflicts`` is a list of
    ``{"symbol", "attributions": [{"batch", "underlying", ...evidence...}]}``.
    """
    by_symbol: dict[str, list] = defaultdict(list)
    raw = 0
    for name, rows in attributions:
        for r in rows:
            by_symbol[r["symbol"]].append((name, r))
            raw += 1
    kept: list = []
    conflicts: list[dict] = []
    pure_dupe_groups = 0
    removed_dupes = 0
    for symbol, group in by_symbol.items():
        if len(group) == 1:
            kept.append(group[0][1])
            continue
        first = evidence_fn(group[0][1])
        if all(evidence_fn(r) == first for _, r in group[1:]):
            pure_dupe_groups += 1
            removed_dupes += len(group) - 1
            kept.append(group[0][1])
            continue
        conflicts.append({"symbol": symbol,
                          "attributions": [{"batch": name, **_attribution_evidence(r)}
                                           for name, r in group]})
    # Accounting: every raw row is accounted for exactly once.
    accounted = len(kept) + removed_dupes + sum(len(c["attributions"]) for c in conflicts)
    if accounted != raw:
        raise RuntimeError(f"combine accounting failed: {accounted} != {raw}")
    if len(kept) + len(conflicts) != len(by_symbol):
        raise RuntimeError("combine accounting failed: kept + conflicting != distinct symbols")
    stats = {"raw_rows": raw, "distinct_symbols": len(by_symbol), "kept": len(kept),
             "conflicting_symbols": len(conflicts),
             "pure_duplicate_groups": pure_dupe_groups,
             "pure_duplicate_rows_removed": removed_dupes}
    return kept, conflicts, stats


def _attribution_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """One attribution's evidence for the conflict report (JSON-safe)."""
    evidence: dict[str, Any] = {"underlying": row.get("underlying"),
                                "option_type": row.get("option_type"),
                                "strike": row.get("strike"),
                                "expiration": row.get("expiration"),
                                "flags": list(row.get("flags") or [])}
    for basis in w.BASES:
        b = row.get(basis) or {}
        evidence[basis] = {c: (list(b.get(c) or []) if c == "reasons" else b.get(c))
                           for c in outputs.BASIS_COLUMNS}
    # CSV-row attributions carry flat basis fields instead of nested dicts.
    if "status" in row:
        evidence["status"] = row.get("status")
        evidence["reasons"] = row.get("reasons")
        evidence["pct"] = row.get("pct")
        evidence["entry"] = row.get("entry")
        evidence["exit"] = row.get("exit")
    return evidence


def combine_rows(window_id: str,
                 attributions: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]
                 ) -> tuple[list, list[dict], dict[str, Any]]:
    """Combine evaluated rows from several attributions for one window.

    ``attributions`` is a list of ``(name, rows)`` where ``rows`` are
    ``windows.evaluate`` outputs. Returns ``(kept, conflicts, stats)``;
    ``kept`` is the combined full universe, still unranked — callers rank
    it with ``windows.rank_window`` / ``windows_outputs.rankings_csv``,
    which rank the full set before cutting the shortlist.
    """
    kept, conflicts, stats = _group_symbols(attributions, _row_evidence)
    for c in conflicts:
        c["window"] = window_id
    return kept, conflicts, {"window_id": window_id, **stats}


def combine_csv_rows(window_id: str,
                     attributions: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]
                     ) -> tuple[list, list[dict], dict[str, Any]]:
    """Combine ranked-CSV rows (one basis, string fields) for one window.

    Same conflict rule as :func:`combine_rows`, over the CSV evidence
    fields. Used to verify the preserved #130 batch CSVs against the
    reference combine: the combined qualified ranking must reproduce the
    checkpoint's full-universe counts, not a subset ranking.
    """
    kept, conflicts, stats = _group_symbols(attributions, _csv_evidence)
    for c in conflicts:
        c["window"] = window_id
    return kept, conflicts, {"window_id": window_id, **stats}


# -- paths.csv.gz round-trip -------------------------------------------------

def _num(value: Any, cast):
    if value in ("", None):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _split(value: Any) -> list[str]:
    return [x for x in str(value or "").split(";") if x]


def rows_from_paths(paths_bytes: bytes, window: w.Window,
                    policy: w.WindowPolicy | None = None) -> list[dict[str, Any]]:
    """Rebuild evaluated rows from one window's ``paths.csv.gz`` content.

    Every basis column in the paths file is restored (status, reasons,
    pct, entry/exit, timestamps, quote evidence); ``abs_change`` is
    recomputed from entry/exit, and the entry/exit boundary offsets are
    recomputed from the window sessions. Rank fields are NOT carried over:
    the combined universe is re-ranked from scratch.
    """
    policy = policy or w.WindowPolicy()
    sessions = {s.day: s for s in window.sessions}
    text = gzip.decompress(paths_bytes).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for r in csv.DictReader(io.StringIO(text)):
        row: dict[str, Any] = {**window.as_dict(),
                               "policy_version": POLICY,
                               "decisions_status": policy.decisions_status,
                               "symbol": r["symbol"], "underlying": r["underlying"],
                               "root": r["root"], "option_type": r["option_type"],
                               "strike": _num(r["strike"], float),
                               "expiration": r["expiration"],
                               "contract_size": _num(r["contract_size"], int) or 100,
                               "contract_exclusions": _split(r.get("contract_exclusions")),
                               "flags": _split(r.get("flags")),
                               "hindsight": window.kind in ("weekly_cheapest",
                                                            "weekly_cheapest_best_exit"),
                               "label": "quote comparison over sampled observations; not an executed fill"}
        for basis in w.BASES:
            entry_ms = _num(r.get(f"{basis}_entry_ms"), int)
            exit_ms = _num(r.get(f"{basis}_exit_ms"), int)
            entry = _num(r.get(f"{basis}_entry"), float)
            exit_ = _num(r.get(f"{basis}_exit"), float)
            entry_session = r.get(f"{basis}_entry_session") or None
            exit_session = r.get(f"{basis}_exit_session") or None
            delay = None
            if entry_ms is not None and entry_session in sessions:
                delay = (entry_ms - sessions[entry_session].open_ms) / 1000
            lead = None
            if exit_ms is not None and exit_session in sessions:
                lead = (sessions[exit_session].close_ms - exit_ms) / 1000
            row[basis] = {
                "status": r.get(f"{basis}_status") or "excluded",
                "reasons": _split(r.get(f"{basis}_reasons")),
                "pct": _num(r.get(f"{basis}_pct"), float),
                "entry": entry, "exit": exit_,
                "abs_change": (round(exit_ - entry, 6) if entry is not None and exit_ is not None else None),
                "abs_change_per_contract": _num(r.get(f"{basis}_abs_change_per_contract"), float),
                "entry_ms": entry_ms, "exit_ms": exit_ms,
                "entry_session": entry_session, "exit_session": exit_session,
                "entry_delay_from_boundary_s": delay,
                "exit_lead_before_close_s": lead,
                "entry_bid": _num(r.get(f"{basis}_entry_bid"), float),
                "entry_ask": _num(r.get(f"{basis}_entry_ask"), float),
                "entry_bid_ms": _num(r.get(f"{basis}_entry_bid_ms"), int),
                "entry_ask_ms": _num(r.get(f"{basis}_entry_ask_ms"), int),
                "entry_ask_size": _num(r.get(f"{basis}_entry_ask_size"), int),
                "entry_bid_quote_age_s": _num(r.get(f"{basis}_entry_bid_quote_age_s"), float),
                "entry_ask_quote_age_s": _num(r.get(f"{basis}_entry_ask_quote_age_s"), float),
                "entry_quote_age_s": _num(r.get(f"{basis}_entry_quote_age_s"), float),
                "entry_relative_spread": _num(r.get(f"{basis}_entry_relative_spread"), float),
                "exit_bid": _num(r.get(f"{basis}_exit_bid"), float),
                "exit_ask": _num(r.get(f"{basis}_exit_ask"), float),
                "exit_bid_ms": _num(r.get(f"{basis}_exit_bid_ms"), int),
                "exit_ask_ms": _num(r.get(f"{basis}_exit_ask_ms"), int),
                "exit_bid_size": _num(r.get(f"{basis}_exit_bid_size"), int),
                "exit_bid_quote_age_s": _num(r.get(f"{basis}_exit_bid_quote_age_s"), float),
                "exit_ask_quote_age_s": _num(r.get(f"{basis}_exit_ask_quote_age_s"), float),
                "exit_quote_age_s": _num(r.get(f"{basis}_exit_quote_age_s"), float),
                "exit_relative_spread": _num(r.get(f"{basis}_exit_relative_spread"), float),
            }
        rows.append(row)
    return rows


# -- emit-tree combine -------------------------------------------------------

def _read_emit_tree(root: Path) -> dict[str, dict[str, Path]]:
    """{kind: {window_id: dir}} for one local emit root (fail closed)."""
    tree = root / "oa203" / "windows" / POLICY
    if not tree.is_dir():
        raise ValueError(f"no windows emit tree at {tree}")
    found: dict[str, dict[str, Path]] = {}
    for kind_dir in sorted(p for p in tree.iterdir() if p.is_dir()):
        if kind_dir.name not in w.KINDS:
            raise ValueError(f"{root}: unexpected window kind {kind_dir.name}")
        for win_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            for member in ("summary.json", "rankings.csv", "paths.csv.gz"):
                if not (win_dir / member).is_file():
                    raise ValueError(f"{root}: {kind_dir.name}/{win_dir.name} missing {member}")
            found.setdefault(kind_dir.name, {})[win_dir.name] = win_dir
    if not found:
        raise ValueError(f"no windows found under {tree}")
    return found


def _window_from_summary(summary: Mapping[str, Any]) -> w.Window:
    sessions = [w.Session(s["day"], s["open_ms"], s["close_ms"], s.get("early_close", False))
                for s in summary["sessions"]]
    by_day = {s.day: s for s in sessions}
    entry = tuple(by_day[d] for d in summary["entry_sessions"])
    return w.Window(summary["kind"], summary["window_id"], tuple(sessions),
                    entry, by_day[summary["exit_session"]])


def combine_emit(batch_emit_roots: Sequence[Path | str], out_root: Path | str,
                 *, policy: w.WindowPolicy | None = None) -> dict[str, Any]:
    """Combine per-batch emit trees into one full-universe emit tree.

    ``batch_emit_roots`` are distinct local emit roots (one per batch);
    the same root twice, a missing root, or roots whose window sets
    disagree all fail closed. Writes the combined ``oa203/windows/``
    tree under ``out_root`` plus ``conflicts.csv`` and
    ``combine_report.json`` at the root. Returns the report.
    """
    policy = policy or w.WindowPolicy()
    if policy.as_dict()["version"] != POLICY:
        raise ValueError(f"combine requires policy {POLICY}")
    roots = [Path(r) for r in batch_emit_roots]
    if len({str(r) for r in roots}) != len(roots):
        raise ValueError("duplicate batch emit root")
    trees = [_read_emit_tree(r) for r in roots]
    window_set = {(kind, wid) for kind in trees[0] for wid in trees[0][kind]}
    for root, tree in zip(roots, trees):
        have = {(kind, wid) for kind in tree for wid in tree[kind]}
        if have != window_set:
            raise ValueError(f"{root}: window set differs from first batch "
                             f"(missing {sorted(window_set - have)}, "
                             f"unexpected {sorted(have - window_set)})")
    out_root = Path(out_root)
    generated_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {"combine": "windows_combine", "policy": POLICY,
                              "batches": [str(r) for r in roots],
                              "generated_at": generated_at, "windows": {},
                              "conflicts": {"symbols": 0, "rows": 0, "report": "conflicts.csv"}}
    all_conflicts: list[dict] = []
    for kind, window_id in sorted(window_set):
        summaries = []
        for root, tree in zip(roots, trees):
            summaries.append(json.loads((tree[kind][window_id] / "summary.json").read_text(encoding="utf-8")))
        first = summaries[0]
        for s in summaries[1:]:
            if (s["kind"], s["window_id"]) != (first["kind"], first["window_id"]):
                raise ValueError(f"{window_id}: batch summaries disagree on kind/window_id")
            if s.get("policy", {}).get("version") != POLICY:
                raise ValueError(f"{window_id}: batch policy version disagrees")
            if s["sessions"] != first["sessions"]:
                raise ValueError(f"{window_id}: batch session sets disagree")
        window = _window_from_summary(first)
        session_status = {s["day"]: {"final": s.get("final"), "assessment": s.get("assessment"),
                                     "sweeps": s.get("sweeps")} for s in first["sessions"]}
        attributions = []
        sources: list[dict] = []
        seen_sources: set = set()
        for root, tree in zip(roots, trees):
            win_dir = tree[kind][window_id]
            attributions.append((root.name, rows_from_paths(
                (win_dir / "paths.csv.gz").read_bytes(), window, policy)))
            for src in json.loads((win_dir / "summary.json").read_text(encoding="utf-8")).get("sources", []):
                key = (src.get("key"), src.get("sha256"))
                if key not in seen_sources:
                    seen_sources.add(key)
                    sources.append(src)
        kept, conflicts, stats = combine_rows(window_id, attributions)
        scope = {"mode": "full_universe", "universe_scope": "full_universe",
                 "batches": [r.name for r in roots], "conflicts": len(conflicts),
                 "note": "combined full universe: batches ranked together before the shortlist cut; "
                         "conflicting symbols excluded fail-closed, see conflicts.csv"}
        outputs.write_window(out_root, window, kept, policy, session_status, sources,
                             generated_at, scope)
        # Conflict report rows are flattened on the qualified basis (the
        # published ranking basis); the full per-basis evidence lives in
        # combine_report.json.
        for c in conflicts:
            for a in c["attributions"]:
                q = a.get("qualified") or {}
                flat: dict[str, Any] = {
                    "window": window_id, "symbol": c["symbol"], "batch": a["batch"],
                    "underlying": a.get("underlying"), "option_type": a.get("option_type"),
                    "strike": a.get("strike"), "expiration": a.get("expiration"),
                    "status": q.get("status"), "reasons": ";".join(q.get("reasons") or []),
                    "pct": q.get("pct"), "entry": q.get("entry"), "exit": q.get("exit"),
                    "abs_change_per_contract": q.get("abs_change_per_contract"),
                    "entry_session": q.get("entry_session") or "",
                    "exit_session": q.get("exit_session") or "",
                    "entry_ms": q.get("entry_ms"), "exit_ms": q.get("exit_ms"),
                    "flags": ";".join(a.get("flags") or [])}
                for basis in w.BASES:
                    b = a.get(basis) or {}
                    for field in _PER_BASIS_CONFLICT_FIELDS:
                        value = b.get(field)
                        if field == "reasons":
                            value = ";".join(value or [])
                        elif field in ("entry_session", "exit_session"):
                            value = value or ""
                        flat[f"{basis}_{field}"] = value
                all_conflicts.append(flat)
        report["windows"][f"{kind}/{window_id}"] = stats
        report["windows"][f"{kind}/{window_id}"]["conflicts"] = [
            {"symbol": c["symbol"], "attributions": c["attributions"]}
            for c in conflicts]
    with open(out_root / "conflicts.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=CONFLICT_COLUMNS, lineterminator="\n")
        wr.writeheader()
        for r in sorted(all_conflicts, key=lambda x: (x["window"], x["symbol"], x["batch"])):
            wr.writerow({c: (r.get(c, "") if not isinstance(r.get(c), dict) else "") for c in CONFLICT_COLUMNS})
    report["conflicts"] = {"symbols": len({r["symbol"] for r in all_conflicts}),
                           "rows": len(all_conflicts), "report": "conflicts.csv"}
    (out_root / "combine_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
