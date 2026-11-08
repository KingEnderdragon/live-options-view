"""Was this session good enough to trust?

Coverage is judged from what was actually captured -- start grace, universe and
chain success, sweep cadence -- so a session that merely reached the close is
not reported as complete.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from .runtime import Config


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------

START_GRACE_MS = 120_000   # first sweep must start within this of the open
MIN_UNIVERSE_COVERAGE = 0.98
MIN_CHAIN_SUCCESS = 0.98
SLOW_SWEEP_FACTOR = 1.5


def expected_sweep_s(universe_size: int, cfg: "Config") -> float:
    """Request-budget floor for one sweep: chains plus one quote batch per block."""
    blocks = -(-universe_size // max(1, cfg.block_size))
    return (universe_size + blocks) / max(1, cfg.max_rpm) * 60.0


def assess_session(universe: dict[str, Any], manifest: list[dict[str, Any]], open_ms: int,
                   close_ms: int, pending_uploads: list[str], backfill: dict[str, Any] | None,
                   rate_stats: dict[str, int], expected_sweep_ms: float,
                   interruptions: list[dict[str, Any]] | None = None,
                   damaged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Label a session complete or partial; every check stands on its own.

    Temporal coverage (start, gaps, end) uses the larger of the observed
    median complete sweep and the request-budget floor, so it still applies
    when every sweep was truncated. Universe coverage counts selected
    underlyings with at least one successful in-session fetch, so chains
    that were never attempted count as missing rather than disappearing
    from a success-rate denominator.
    """
    reasons: list[str] = []
    if universe.get("shortfall"):
        reasons.append(f"universe_shortfall:{universe['shortfall']}")
    in_session = [m for m in manifest if m["ended_ms"] > open_ms and m["started_ms"] < close_ms]
    if not in_session:
        reasons.append("no_sweeps_in_session")
    complete_sweeps = [m for m in in_session if not m.get("truncated")]
    if in_session and not complete_sweeps:
        reasons.append("no_complete_sweep")

    # The nearest-chain daily contract remains unchanged. Additional chains
    # are separate expected fetches: an omitted status is a failure, not a
    # smaller denominator. Report their coverage independently so two missed
    # SPY/QQQ chains cannot disappear inside 500 successful nearest chains.
    expected_extra = {(u["underlying"], u["additional_expiration"])
                      for u in universe.get("selected") or [] if u.get("additional_expiration")}
    nearest_total = sum(len(m["chains"]) for m in in_session)
    nearest_ok = sum(m["status_counts"].get("ok", 0) for m in in_session)
    extra_statuses = [m.get("additional_chains", {}).get(name, {}).get(expiry)
                      for m in in_session for name, expiry in sorted(expected_extra)]
    extra_ok = sum(bool(st and st.get("status") == "ok" and st.get("expiration") == expiry
                        and type(st.get("rows")) is int and st["rows"] > 0)
                   for m in in_session for name, expiry in expected_extra
                   for st in [m.get("additional_chains", {}).get(name, {}).get(expiry)])
    extra_expected = len(extra_statuses)
    extra_recorded = sum(st is not None for st in extra_statuses)
    extra_failures = extra_expected - extra_ok
    chain_total = nearest_total + extra_expected
    chain_ok = nearest_ok + extra_ok
    ok_rate = chain_ok / chain_total if chain_total else 0.0
    if chain_total and ok_rate < MIN_CHAIN_SUCCESS:
        reasons.append(f"chain_success_rate:{ok_rate:.4f}")
    if extra_failures:
        reasons.append(f"additional_chain_failures:{extra_failures}")

    selected = {u["underlying"] for u in universe.get("selected") or []}
    covered = {name for m in in_session for name, status in m["chains"].items()
               if status.get("status") == "ok"}
    coverage = len(covered & selected) / len(selected) if selected else 0.0
    if coverage < MIN_UNIVERSE_COVERAGE:
        reasons.append(f"universe_coverage:{coverage:.4f}")
    extra_covered = {(name, expiry) for m in in_session for name, expiry in expected_extra
                     if (st := m.get("additional_chains", {}).get(name, {}).get(expiry))
                     and st.get("status") == "ok" and st.get("expiration") == expiry
                     and type(st.get("rows")) is int and st["rows"] > 0}
    extra_coverage = len(extra_covered) / len(expected_extra) if expected_extra else None
    if expected_extra and extra_coverage < 1:
        reasons.append(f"additional_expiry_coverage:{extra_coverage:.4f}")

    durations = [m["ended_ms"] - m["started_ms"] for m in complete_sweeps]
    typical = statistics.median(durations) if durations else None
    max_gap = 2 * max(typical or 0, expected_sweep_ms) + 60_000
    # The median hides a degraded stretch, so slow and budget-cut sweeps are
    # counted separately against the request-budget floor.
    all_durations = [m["ended_ms"] - m["started_ms"] for m in in_session]
    slow_sweeps = sum(d > SLOW_SWEEP_FACTOR * expected_sweep_ms for d in all_durations)
    budget_cut = sum(bool(m.get("budget_exhausted")) for m in in_session)
    if budget_cut:
        reasons.append(f"sweeps_over_budget:{budget_cut}")
    gaps: list[int] = []
    if in_session:
        starts = sorted(m["started_ms"] for m in in_session)
        late = starts[0] - open_ms
        if late > START_GRACE_MS:
            reasons.append(f"late_start:{late // 1000}s")
        gaps = [b - a for a, b in zip(starts, starts[1:]) if b - a > max_gap]
        if gaps:
            reasons.append(f"sweep_gaps:{len(gaps)}")
        if close_ms - max(m["ended_ms"] for m in in_session) > max_gap:
            reasons.append("stopped_before_close")
    if interruptions:
        reasons.append(f"interrupted:{len(interruptions)}")
    if damaged:
        reasons.append(f"damaged_artifacts:{len(damaged)}")
    if pending_uploads:
        reasons.append(f"uploads_pending:{len(pending_uploads)}")
    if backfill is None:
        reasons.append("backfill_not_run")
    elif backfill.get("errors"):
        reasons.append(f"backfill_errors:{backfill['errors']}")
    if rate_stats.get("rate_limited"):
        reasons.append(f"rate_limited_responses:{rate_stats['rate_limited']}")
    # Status covers session-level checks (start, gaps, end, universe and chain
    # coverage). A sweep cut short, normally the last one at the close, does
    # not change it but is reported here so "complete" is not read as "every
    # sweep finished".
    truncated = [m for m in in_session if m.get("truncated")]
    final = max(in_session, key=lambda m: m["started_ms"]) if in_session else None
    final_sweep = None if final is None else {
        "sweep": final.get("sweep"), "truncated": bool(final.get("truncated")),
        "ended_ms": final["ended_ms"], "chains_recorded": len(final.get("chains") or {}),
        "chains_selected": len(selected), "status_counts": final.get("status_counts", {})}
    return {
        "status": "partial" if reasons else "complete",
        "status_basis": "session-level checks; see truncated_sweeps and final_sweep for per-sweep completion",
        "reasons": reasons,
        "sweeps_in_session": len(in_session),
        "complete_sweeps": len(complete_sweeps),
        "truncated_sweeps": len(truncated),
        "final_sweep": final_sweep,
        "chain_fetches": nearest_total + extra_recorded,
        "chain_expected": chain_total,
        "chain_success_rate": round(ok_rate, 6),
        "universe_coverage": round(coverage, 6),
        "additional_chain_expected": extra_expected,
        "additional_chain_statuses_recorded": extra_recorded,
        "additional_chain_successes": extra_ok,
        "additional_chain_success_rate": (round(extra_ok / extra_expected, 6) if extra_expected else None),
        "additional_expiry_coverage": (round(extra_coverage, 6) if extra_coverage is not None else None),
        "typical_sweep_s": round(typical / 1000, 1) if typical else None,
        "max_sweep_s": round(max(all_durations) / 1000, 1) if all_durations else None,
        "slow_sweeps": slow_sweeps,
        "slow_sweep_threshold_s": round(SLOW_SWEEP_FACTOR * expected_sweep_ms / 1000, 1),
        "budget_exhausted_sweeps": budget_cut,
        "max_gap_s": round(max_gap / 1000, 1),
        "gaps_ms": gaps,
        "pending_uploads": pending_uploads,
    }


MEASUREMENT_NOTE = (
    "Sampled-return leaderboard: quote returns come from ~5-minute chain samples. "
    "The trade-bar backfill only enriches contracts that already rank in those "
    "samples; a brief spike in a contract that never ranks is not discovered."
)
