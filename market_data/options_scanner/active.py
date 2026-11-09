"""The intraday "active" snapshot published for the market MCP.

Built on a cadence from the sweeps captured so far, with an explicit cutoff so
a late sweep cannot change an already-published snapshot, and a size budget per
artifact because the consumer enforces one.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from collections import Counter

from . import runtime
from .archive import (
    SUMMARY,
    DayArchive,
    _content_type,
    _read_manifest,
    _r2_uploader,
    _sha256_file,
    published,
)
from .assessment import MEASUREMENT_NOTE
from .outputs import build_contract_rows, write_outputs
from .runtime import Config


ACTIVE_MEMBERS = ("summary.json", "leaderboard.csv", "qualified_ask_bid_v1.csv", "manifest.jsonl")
ACTIVE_BUILD_SECONDS = 240
# Snapshots are due by elapsed time, not sweep count: six sweeps took 57.8
# minutes on 2026-10-13 while the provider was returning 504s.
ACTIVE_INTERVAL_S = 1800
ACTIVE_LATE_FACTOR = 1.5  # observed cadence beyond this multiple is reported late
ACTIVE_MAX_SNAPSHOTS_PER_DAY = 16
ACTIVE_MAX_SWEEP = 96  # identifier bound for pointer names and the upload ledger
ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP = 3


def active_snapshot_due(ended_ms: int, since_ms: int, expected_sweep: float) -> bool:
    """Due once the interval has elapsed, to the nearest sweep boundary.

    Half a nominal sweep of tolerance keeps the healthy cadence at six
    ~306 s sweeps (1,836 s) instead of slipping to seven.
    """
    return ended_ms - since_ms >= (ACTIVE_INTERVAL_S - expected_sweep / 2) * 1000


def _active_cutoff_ms(archive: DayArchive) -> int:
    path = archive.path("inprogress/latest.json")
    try:
        cutoff = json.loads(path.read_text(encoding="utf-8")).get("cutoff_ms")
    except (OSError, ValueError, AttributeError):
        return 0
    return cutoff if type(cutoff) is int else 0


def cadence_report(manifest: list[dict[str, Any]], previous: dict[str, Any] | None,
                   observed_s: float | None) -> dict[str, Any]:
    """Describe the refresh this snapshot completes, from the sweeps it covers."""
    since = previous.get("sweep", 0) if previous else 0
    window = [m for m in manifest if m["sweep"] > since]
    durations = [(m["ended_ms"] - m["started_ms"]) / 1000 for m in window]
    stats = [m.get("request_stats_delta") or {} for m in window]
    status = ("first" if observed_s is None else
              "late" if observed_s > ACTIVE_INTERVAL_S * ACTIVE_LATE_FACTOR else "on_time")
    return {"basis": "elapsed_time", "interval_s": ACTIVE_INTERVAL_S,
            "late_after_s": ACTIVE_INTERVAL_S * ACTIVE_LATE_FACTOR, "status": status,
            "sweeps": len(window), "max_sweep_s": round(max(durations), 1) if durations else None,
            "budget_exhausted_sweeps": sum(bool(m.get("budget_exhausted")) for m in window),
            "server_errors": sum(s.get("server_errors", 0) for s in stats),
            "transport_errors": sum(s.get("transport_errors", 0) for s in stats)}
ACTIVE_MAX_MEMBER_BYTES = 4 * 1024 * 1024  # market MCP Source.LIMITS.artifact_bytes
ACTIVE_MAX_MANIFEST_LINE_BYTES = 256 * 1024  # market MCP Source.LIMITS.jsonl_line_bytes


def reserve_active_upload_attempt(archive: DayArchive, sweep: int) -> None:
    """Bound retries while the local spool survives; reserve before the first PUT."""
    name = "inprogress/upload-attempts.json"
    path = archive.path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        budget = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(budget, dict) or budget.get("schema_version") != 1 or
                budget.get("trade_date") != archive.day.isoformat() or
                not isinstance(budget.get("attempts"), dict)):
            raise ValueError("invalid active upload attempt ledger")
    else:
        budget = {"schema_version": 1, "trade_date": archive.day.isoformat(), "attempts": {}}
    attempts = budget["attempts"]
    if len(attempts) > ACTIVE_MAX_SNAPSHOTS_PER_DAY or any(
           not re.fullmatch(r"[1-9][0-9]{0,3}", key) or
           int(key) > ACTIVE_MAX_SWEEP or type(value) is not int or
           not 1 <= value <= ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP
           for key, value in attempts.items()):
        raise ValueError("invalid active upload attempt ledger")
    key = str(sweep)
    if (attempts.get(key, 0) >= ACTIVE_MAX_UPLOAD_ATTEMPTS_PER_SWEEP or
            key not in attempts and len(attempts) >= ACTIVE_MAX_SNAPSHOTS_PER_DAY):
        raise ValueError("active upload retry budget exhausted")
    attempts[key] = attempts.get(key, 0) + 1
    archive.write_json(name, budget)


def write_active_attempt(archive: DayArchive, sweep: int, status: str,
                         reason: str | None = None) -> None:
    payload = {"schema_version": 1, "trade_date": archive.day.isoformat(), "sweep": sweep,
               "status": status, "reason": reason,
               "at": datetime.now(timezone.utc).isoformat()}
    name = "inprogress/latest-attempt.json"
    archive.path("inprogress").mkdir(parents=True, exist_ok=True)
    archive.write_json(name, payload)
    if archive.upload:
        send = _r2_uploader()
        result = send(archive.path(name), f"{archive.prefix}/{name}", "application/json")
        if result.get("sha256") != _sha256_file(archive.path(name)):
            raise ValueError("active attempt upload checksum mismatch")


@contextlib.contextmanager
def active_build_lock(archive: DayArchive) -> Iterator[None]:
    """Prevent duplicate local generations across scanner restarts."""
    lock = archive.path("inprogress/.build.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(16).hex()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime <= ACTIVE_BUILD_SECONDS + 60:
            raise RuntimeError("active snapshot build already running")
        lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(token)
    try:
        yield
    finally:
        if lock.exists() and lock.read_text(encoding="utf-8") == token:
            lock.unlink()


def build_active_snapshot(archive: DayArchive, cfg: Config, sweep: int,
                          open_ms: int, close_ms: int,
                          uploader: Callable[[Path, str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
    with active_build_lock(archive):
        return _build_active_snapshot(archive, cfg, sweep, open_ms, close_ms, uploader)


def _build_active_snapshot(archive: DayArchive, cfg: Config, sweep: int,
                           open_ms: int, close_ms: int,
                           uploader: Callable[[Path, str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build an immutable cutoff generation, then publish its moving pointer last.

    The CLI may be retried after interruption. An existing generation is reused
    only when its recorded member digests still match. No source sweep is copied.
    """
    if not 1 <= sweep <= ACTIVE_MAX_SWEEP:
        raise ValueError("invalid snapshot sweep")
    if archive.upload and uploader is None:
        from market_data.tradier.probe import r2_client
        from market_data.options_scanner.active_budget import clean_and_reserve
        client, bucket = r2_client()
        if archive.prefix != f"oa203/scanner/{archive.day.isoformat()}":
            raise ValueError("active policy requires the reviewed scanner prefix")
        budget = clean_and_reserve(client, bucket, archive.day, sweep)
        runtime.log("oa203_active_budget_reserved", **budget)
    manifest_damage: list[dict[str, Any]] = []
    manifest = _read_manifest(archive.dir, manifest_damage, sweep)
    if manifest_damage:
        raise ValueError("damaged cutoff manifest; snapshot skipped")
    if [m["sweep"] for m in manifest] != list(range(1, sweep + 1)):
        raise ValueError("snapshot manifest is incomplete")
    cutoff_ms = manifest[-1].get("ended_ms")
    if type(cutoff_ms) is not int or not open_ms < cutoff_ms <= close_ms + 60_000:
        raise ValueError("invalid snapshot cutoff")
    source = []
    for m in manifest:
        path = archive.path(f"sweeps/sweep_{m['sweep']:04d}.jsonl.gz")
        if not path.is_file() or m.get("file") != path.name:
            raise FileNotFoundError(f"missing snapshot source sweep {m['sweep']}")
        entry = {**m, "sha256": _sha256_file(path)}
        if len(json.dumps(entry, sort_keys=True).encode("utf-8")) > ACTIVE_MAX_MANIFEST_LINE_BYTES:
            raise ValueError("snapshot manifest line exceeds MCP parser budget")
        source.append(entry)
    if archive.upload:
        journal = archive._journal()
        if any((journal.get(f"sweeps/sweep_{m['sweep']:04d}.jsonl.gz") or {}).get("sha256") != m["sha256"]
               for m in source):
            raise ValueError("source sweep upload is unverified; snapshot skipped")
    name = f"inprogress/sweep_{sweep:04d}"
    sweep_pointer_path = archive.path(name + ".json")
    previous = json.loads(sweep_pointer_path.read_text(encoding="utf-8")) if sweep_pointer_path.exists() else None
    if previous is not None:
        revision = previous.get("archive", "").removeprefix(archive.prefix + "/")
        if not re.fullmatch(re.escape(name) + r"/rev_[0-9a-f]{64}", revision):
            raise ValueError("invalid existing snapshot revision")
        target = archive.path(revision)
        if not target.is_dir():
            raise ValueError("published snapshot revision is missing locally")
    else:
        revisions = sorted(archive.path(name).glob("rev_*")) if archive.path(name).exists() else []
        target = revisions[0] if revisions else None
    tmp = archive.path(name + ".building")
    if target is not None:
        summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
        if (summary.get("cutoff_sweep"), summary.get("cutoff_ms")) != (sweep, cutoff_ms):
            raise ValueError("immutable snapshot identity changed")
        digest = summary.get("member_sha256") or {}
        if any(not (target / member).is_file() or _sha256_file(target / member) != digest.get(member)
               for member in ACTIVE_MEMBERS if member != "summary.json"):
            raise ValueError("immutable snapshot member changed")
        if any((target / member).stat().st_size > ACTIVE_MAX_MEMBER_BYTES for member in ACTIVE_MEMBERS):
            raise ValueError("snapshot member exceeds MCP artifact budget")
        recorded = [json.loads(line) for line in (target / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line]
        if recorded != source:
            raise ValueError("immutable snapshot source lineage changed")
        if target.name != "rev_" + _sha256_file(target / "summary.json"):
            raise ValueError("immutable snapshot revision digest changed")
        if previous and previous.get("members", {}).get("summary.json") != _sha256_file(target / "summary.json"):
            raise ValueError("published snapshot pointer digest changed")
    else:
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            damaged: list[dict[str, Any]] = []
            rows = build_contract_rows(archive.dir, cfg.policy, open_ms, min(cutoff_ms, close_ms), damaged,
                                       cfg.qualified_policy, cutoff_sweep=sweep)
            if damaged:
                raise ValueError("damaged cutoff source; snapshot skipped")
            outputs = write_outputs(tmp, rows, include_contracts=False)
            with published(tmp / "manifest.jsonl") as staged:
                staged.write_text("".join(json.dumps(m, sort_keys=True) + "\n" for m in source), encoding="utf-8")
            digests = {member: _sha256_file(tmp / member) for member in ACTIVE_MEMBERS if member != "summary.json"}
            summary = {"schema_version": 1, "trade_date": archive.day.isoformat(), "final": False,
                       "state": "in_progress", "cutoff_sweep": sweep, "cutoff_ms": cutoff_ms,
                       "session_open_ms": open_ms, "session_close_ms": close_ms,
                       "assessment": {"status": "partial", "reasons": ["session_in_progress"],
                                      "completed_sweeps": sweep,
                                      "truncated_sweeps": sum(bool(m.get("truncated")) for m in manifest),
                                      "chain_status_counts": dict(Counter(s.get("status", "unknown")
                                                                           for m in manifest for s in (
                                                                               list(m.get("chains", {}).values()) +
                                                                               [state for expiries in m.get("additional_chains", {}).values()
                                                                                for state in expiries.values()])))},
                       "contracts": len(rows), "outputs": outputs, "return_policy": cfg.policy.as_dict(),
                       "qualified_policy": cfg.qualified_policy.as_dict(),
                       "measurement": MEASUREMENT_NOTE, "member_sha256": digests}
            with published(tmp / "summary.json") as staged:
                staged.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
            if any((tmp / member).stat().st_size > ACTIVE_MAX_MEMBER_BYTES for member in ACTIVE_MEMBERS):
                raise ValueError("snapshot member exceeds MCP artifact budget")
            target = archive.path(name + "/rev_" + _sha256_file(tmp / "summary.json"))
            if target.exists():
                raise ValueError("immutable snapshot revision already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp, target)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
    hashes = {member: _sha256_file(target / member) for member in ACTIVE_MEMBERS}
    prior = archive.path("inprogress/latest.json")
    existing = json.loads(prior.read_text(encoding="utf-8")) if prior.exists() else None
    published_at = datetime.now(timezone.utc).isoformat()
    previous_at = existing.get("published_at") if existing else None
    observed_cadence = (round((datetime.fromisoformat(published_at) - datetime.fromisoformat(previous_at)).total_seconds(), 1)
                        if previous_at else None)
    pointer = {"schema_version": 1, "trade_date": archive.day.isoformat(), "state": "in_progress",
               "sweep": sweep, "cutoff_ms": cutoff_ms, "archive": f"{archive.prefix}/{target.relative_to(archive.dir).as_posix()}",
               "members": hashes, "built_at": published_at,
               "published_at": published_at, "previous_published_at": previous_at,
               "observed_cadence_s": observed_cadence,
               "expected_cadence_s": ACTIVE_INTERVAL_S,
               "cadence": cadence_report(manifest, existing, observed_cadence)}
    if existing and existing.get("sweep", 0) > sweep:
        return existing  # retries may never move the pointer backwards
    if archive.upload:
        reserve_active_upload_attempt(archive, sweep)
        send = uploader or _r2_uploader()
        for member in ACTIVE_MEMBERS:
            result = send(target / member, f"{pointer['archive']}/{member}", _content_type(member))
            if result.get("sha256") != hashes[member]:
                raise ValueError("snapshot upload checksum mismatch")
    archive.write_json(name + ".json", pointer)
    if archive.upload:
        result = send(sweep_pointer_path, f"{archive.prefix}/{name}.json", "application/json")
        if result.get("sha256") != _sha256_file(sweep_pointer_path):
            raise ValueError("snapshot sweep pointer upload checksum mismatch")
    archive.write_json("inprogress/latest.json", pointer)
    if archive.upload:
        result = send(archive.path("inprogress/latest.json"),
                      f"{archive.prefix}/inprogress/latest.json", "application/json")
        if result.get("sha256") != _sha256_file(archive.path("inprogress/latest.json")):
            raise ValueError("snapshot pointer upload checksum mismatch")
    return pointer
