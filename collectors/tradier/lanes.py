"""Per-underlying archive lanes and how several share one stream.

One Tradier market-data stream fans out through ``StreamRouter`` to one
``Lane`` per underlying, each with its own universe, spool, uploader,
reconciliation and stats. The first-listed underlying is the primary and keeps
the single-underlying startup path; the others are optional and are admitted
only if their whole pre-open preparation finishes before the cutoff, so it can
never delay or abort the primary's connection.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ._probe import Tradier, select_symbols
from .archive import archive_root, spool_dir_for, spool_dir_name
from .artifacts import json_artifact
from . import config
from .config import (
    LATE_START_OPTIONAL_BUDGET_SECONDS,
    OPTIONAL_LANE_POLL_SECONDS,
    OPTIONAL_LANE_RESERVE_SECONDS,
)
from .recovery import reconcile_existing_segments
from .spool import SegmentSpool, Uploader
from .stats import BoundedStats
from .universe import load_persisted_universe, persist_universe


class Lane:
    """One underlying's archive state, fed by the shared stream."""

    def __init__(self, underlying: str, expiration_policy: str, run_date: str, spool_base: Path) -> None:
        self.underlying = underlying
        self.expiration_policy = expiration_policy
        self.root = archive_root(underlying)
        self.prefix = f"{self.root}/{run_date}"
        self.base_spool_dir = spool_base / spool_dir_name(underlying)
        self.spool_dir = spool_dir_for(self.base_spool_dir, run_date)
        self.symbols: list[str] = []
        self.universe: dict[str, Any] | None = None
        self.unavailable_reason: str | None = None
        self.stats = BoundedStats()
        self.spool_cap_bytes = 0
        # Optional-lane handover: preparation publishes into this lane only
        # under ``lock`` and only while not ``frozen``.
        self.lock = threading.Lock()
        self.frozen = False
        self.prepared = threading.Event()
        self.stale_recovery: dict[str, Any] = {}
        self.reconciliation: dict[str, Any] = {"artifacts": [], "resume": [], "needs_review": []}
        self.resumed_names: set[str] = set()
        self.preflight: dict[str, Any] | None = None
        self.uploader: Uploader | None = None
        self.spool: SegmentSpool | None = None


class LaneAdmissionClosed(RuntimeError):
    """The lane was frozen (excluded) before this admission step started."""


def prepare_optional_lane(
    lane: Lane,
    client_factory: Callable[[], Tradier],
    r2: Any,
    bucket: str,
    strike_count: int,
    run_date: str,
    clock_et: Callable[[], datetime],
    session_open: datetime,
    preflight_payload: Callable[[Lane, dict[str, Any], dict[str, Any]], dict[str, Any]],
    ownership_held: Callable[[], bool],
    preflight_key: str,
) -> None:
    """Run an optional lane's whole admission path off the main thread.

    Loads or selects the universe, reconciles the local spool, then makes the
    lane durable: persists a freshly selected universe and writes its
    preflight. The main thread only waits for this until the cutoff, so slow
    or failing storage here can delay or abort nothing but this lane.

    Each write starts only while the lane is still unfrozen and the stream
    lease is still held; once frozen, no further step starts. A write already
    in flight at the cutoff may still land, but neither can overwrite
    anything: the universe is a conditional create (so a same-day restart
    reuses that pre-open selection, keeping the universe fixed) and the
    preflight key is unique to this owner. Results are handed over under the
    lane lock and discarded once frozen, so late work never changes the
    admitted subscription.
    """
    stage = "selection"
    writes: list[str] = []
    symbols: list[str] = []
    universe: dict[str, Any] = {}
    reconciliation: dict[str, Any] = {}
    preflight: dict[str, Any] | None = None
    error: str | None = None

    def admissible() -> None:
        with lane.lock:
            if lane.frozen:
                raise LaneAdmissionClosed(f"lane frozen before {stage}")
        if not ownership_held():
            raise RuntimeError("stream lease no longer held")

    try:
        loaded = load_persisted_universe(r2, bucket, lane.prefix)
        if loaded is None:
            symbols, universe = select_symbols(
                client_factory(), strike_count, 0, run_date, clock_et(), session_open=session_open,
                underlying=lane.underlying, expiration_policy=lane.expiration_policy,
            )
        else:
            symbols, universe = loaded
        stage = "reconciliation"
        reconciliation = reconcile_existing_segments(r2, bucket, lane.prefix, lane.spool_dir)
        if loaded is None:
            stage = "universe_persist"
            admissible()
            symbols, universe = persist_universe(r2, bucket, lane.prefix, symbols, universe)
            writes.append("universe")
        stage = "preflight"
        admissible()
        preflight = json_artifact(r2, bucket, preflight_key, preflight_payload(lane, universe, reconciliation))
        writes.append("preflight")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    with lane.lock:
        if lane.frozen:
            print(json.dumps({"event": "optional_lane_result_discarded", "underlying": lane.underlying,
                              "stage": stage, "error": error, "writes_completed": writes}), flush=True)
            return
        if error is not None:
            lane.unavailable_reason = f"{stage}_failed: {error}"
        else:
            lane.symbols, lane.universe = symbols, universe
            lane.reconciliation = reconciliation
            lane.preflight = preflight
        lane.prepared.set()


def admit_optional_lanes(
    lanes: list[Lane],
    session_open: datetime,
    clock_et: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    """Wait, bounded, for optional lanes' whole admission path; then freeze
    every one of them.

    The deadline is the pre-open cutoff, or a short fixed budget when the
    primary was only ready after it. Lanes not durably admitted by then are
    excluded for this session. Nothing waits on their threads afterwards.
    """
    cutoff = session_open - timedelta(seconds=OPTIONAL_LANE_RESERVE_SECONDS)
    now = clock_et()
    deadline = cutoff if now < cutoff else now + timedelta(seconds=LATE_START_OPTIONAL_BUDGET_SECONDS)
    while not config.STOP and any(not lane.prepared.is_set() for lane in lanes):
        now = clock_et()
        if now >= deadline:
            break
        sleeper(min(OPTIONAL_LANE_POLL_SECONDS, (deadline - now).total_seconds()))
    for lane in lanes:
        with lane.lock:
            lane.frozen = True
            if not lane.prepared.is_set() and lane.unavailable_reason is None:
                lane.unavailable_reason = "missed_preparation_cutoff"
        if lane.unavailable_reason is not None:
            print(json.dumps({"event": "optional_lane_excluded", "underlying": lane.underlying,
                              "reason": lane.unavailable_reason}), flush=True)


class StreamRouter:
    """Fans the single provider stream out to per-underlying lanes.

    Provides the spool/stats calls ``capture_session`` makes. An event for a
    subscribed symbol goes only to that symbol's lane. Records without a
    routable symbol (gaps, heartbeats, malformed payloads) describe the
    shared connection, so they go to every lane: each archive stays
    self-describing about its coverage and nothing is dropped.
    """

    def __init__(self, lanes: list[Lane]) -> None:
        self.lanes = lanes
        self.by_symbol: dict[str, Lane] = {}
        for lane in lanes:
            for symbol in lane.symbols:
                if symbol in self.by_symbol:
                    raise RuntimeError(
                        f"{symbol} selected for both {self.by_symbol[symbol].underlying} and {lane.underlying}"
                    )
                self.by_symbol[symbol] = lane
        self.symbols = list(self.by_symbol)

    def lanes_for(self, event: dict[str, Any]) -> list[Lane]:
        payload = event.get("provider_payload") if event.get("type") == "excluded_timesale" else event
        symbol = payload.get("symbol") if isinstance(payload, dict) else None
        lane = self.by_symbol.get(symbol) if isinstance(symbol, str) else None
        return [lane] if lane is not None else self.lanes

    def write(self, event: dict[str, Any]) -> None:
        for lane in self.lanes_for(event):
            lane.spool.write(event)

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        for lane in self.lanes_for(event):
            lane.stats.observe(event)
        return event

    def retain_quote(self, event: dict[str, Any], *, preopen: bool = False) -> None:
        for lane in self.lanes_for(event):
            lane.stats.retain_quote(event, preopen=preopen)

    def observe_malformed(self) -> None:
        for lane in self.lanes:
            lane.stats.observe_malformed()

    def observe_excluded(self, event: dict[str, Any], reason: str) -> None:
        for lane in self.lanes_for(event):
            lane.stats.observe_excluded(event, reason)
