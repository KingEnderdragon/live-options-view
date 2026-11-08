"""The day's archive on disk (and in R2): journal, receipt, manifest, rows.

A day directory is append-mostly: sweeps are written once, the manifest line
per sweep is appended as each sweep completes, and the upload journal records
what reached R2. A partial line from a crash is recorded as damage and
skipped, never silently accepted.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import time
import zlib
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import _probe, runtime
from ._probe import r2_client, upload_file_verified


# --------------------------------------------------------------------------
# Archive layout and storage
# --------------------------------------------------------------------------

UPLOAD_JOURNAL = "uploads.json"
UPLOAD_RECEIPT = ".uploads-receipt.json"
SUMMARY = "summary.json"
INTERRUPTIONS = "interruptions.jsonl"
RECOVERY_ERRORS = "recovery_errors.jsonl"


def _content_type(name: str) -> str:
    if name.endswith(".gz"):
        return "application/gzip"
    return "text/csv" if name.endswith(".csv") else "application/json"


def _r2_uploader() -> Callable[[Path, str, str], dict[str, Any]]:
    client, bucket = r2_client()
    return lambda path, key, content_type: upload_file_verified(client, bucket, path, key, content_type)


class DayArchive:
    """Local per-date directory, mirrored to R2 under ``<prefix>/<date>/``.

    Upload state is durable: ``uploads.json`` records the SHA-256 of each
    artifact as last verified in R2. Anything on disk whose current content
    is not recorded there is pending, whichever process wrote it, so a
    restart after an upload outage re-derives what is still owed instead of
    forgetting it. The journal is also uploaded as the remote source-hash
    manifest, after the artifacts it describes. Its own upload receipt is
    local-only so the journal never recursively hashes itself.
    """

    def __init__(self, root: Path, day: date, prefix: str, upload: bool,
                 uploader: Callable[[Path, str, str], dict[str, Any]] | None = None) -> None:
        self.day = day
        self.dir = root / day.isoformat()
        (self.dir / "sweeps").mkdir(parents=True, exist_ok=True)
        self.prefix = f"{prefix}/{day.isoformat()}"
        self.upload = upload
        self._uploader = uploader

    def artifacts(self) -> list[str]:
        return sorted(p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*")
                      if p.is_file() and not p.name.endswith(".tmp")
                      and p.name not in {UPLOAD_JOURNAL, UPLOAD_RECEIPT}
                      and "inprogress" not in p.relative_to(self.dir).parts)

    def _journal(self) -> dict[str, Any]:
        path = self.path(UPLOAD_JOURNAL)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def pending_uploads(self) -> list[str]:
        """Artifacts whose current content has not been verified in R2."""
        if not self.upload:
            return []
        journal = self._journal()
        pending = [name for name in self.artifacts()
                   if (journal.get(name) or {}).get("sha256") != _sha256_file(self.path(name))]
        if self.path(UPLOAD_JOURNAL).exists():
            try:
                receipt = json.loads(self.path(UPLOAD_RECEIPT).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                receipt = {}
            if (not isinstance(receipt, dict)
                    or receipt.get("sha256") != _sha256_file(self.path(UPLOAD_JOURNAL))
                    or receipt.get("key") != f"{self.prefix}/{UPLOAD_JOURNAL}"):
                pending.append(UPLOAD_JOURNAL)
        return pending

    def push(self, name: str) -> bool:
        if not self.upload:
            return True
        try:
            if self._uploader is None:
                self._uploader = _r2_uploader()
            result = self._uploader(self.path(name), f"{self.prefix}/{name}", _content_type(name))
        except Exception as exc:
            runtime.log("oa203_upload_failed", name=name, error=str(exc)[:300])
            return False
        receipt = {"sha256": result["sha256"], "bytes": result["bytes"], "key": result["key"],
                   "verified_at": datetime.now(timezone.utc).isoformat()}
        if name == UPLOAD_JOURNAL:
            self.write_json(UPLOAD_RECEIPT, receipt)
            return True
        journal = self._journal()
        journal[name] = receipt
        self.write_json(UPLOAD_JOURNAL, journal)
        return True

    def reconcile(self) -> list[str]:
        """Upload pending artifacts, then their hash journal; report failures."""
        for name in self.pending_uploads():
            if name != UPLOAD_JOURNAL:
                self.push(name)
        # Artifact uploads change the journal. Re-evaluate it after all of
        # them, including when the first upload created a previously absent journal.
        if UPLOAD_JOURNAL in self.pending_uploads():
            self.push(UPLOAD_JOURNAL)
        return self.pending_uploads()

    def record_interruption(self, **fields: Any) -> None:
        """Local-only, bounded shutdown checkpoint; uploaded by the next reconcile."""
        entry = {"at": datetime.now(timezone.utc).isoformat(), **fields}
        with self.path(INTERRUPTIONS).open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, sort_keys=True) + "\n")

    def interruptions(self) -> list[dict[str, Any]]:
        path = self.path(INTERRUPTIONS)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def finalized(self) -> bool:
        path = self.path(SUMMARY)
        return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("final"))

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.path(name)
        with published(path) as tmp:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                           encoding="utf-8")
        return path

    def append_manifest(self, entry: dict[str, Any]) -> None:
        # Appends are line-sized; a crash mid-append leaves at most one
        # partial trailing line, which _read_manifest skips and records.
        with self.path("sweeps/manifest.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def manifest(self, damaged: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        return _read_manifest(self.dir, damaged)

    def record_recovery_failure(self, error: str) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(), "error": error}
        with self.path(RECOVERY_ERRORS).open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, sort_keys=True) + "\n")


@contextlib.contextmanager
def published(path: Path) -> Iterator[Path]:
    """Write to a sibling ``.tmp``, fsync, then atomically rename into place.

    The published name only ever holds complete content. A crash mid-write
    leaves at most the ``.tmp`` file, which readers and the upload journal
    ignore and the next write replaces.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        yield tmp
        with open(tmp, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Truncated gzip raises EOFError, corrupt gzip OSError/zlib.error, and a
# partial final line a JSON ValueError.
READ_ERRORS = (EOFError, OSError, zlib.error, ValueError)


def _read_jsonl_gz(path: Path, day_dir: Path,
                   damaged: list[dict[str, Any]] | None) -> Iterator[dict[str, Any]]:
    """Yield records; on a damaged file keep what was readable and record it."""
    count = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as src:
            for line in src:
                if line.strip():
                    record = json.loads(line)
                    count += 1
                    yield record
    except READ_ERRORS as exc:
        entry = {"file": path.relative_to(day_dir).as_posix(), "error": f"{type(exc).__name__}: {exc}"[:300],
                 "records_salvaged": count}
        runtime.log("oa203_damaged_artifact", **entry)
        if damaged is not None:
            damaged.append(entry)


def iter_rows(day_dir: Path, damaged: list[dict[str, Any]] | None = None,
              sweep_files: list[Path] | None = None,
              strict_manifest: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
    files = sweep_files if sweep_files is not None else sorted((day_dir / "sweeps").glob("sweep_*.jsonl.gz"))
    for index, path in enumerate(files):
        if strict_manifest is None:
            yield from _read_jsonl_gz(path, day_dir, damaged)
            continue
        entry = strict_manifest[index]
        chains = entry.get("chains")
        if not isinstance(chains, dict) or type(entry.get("rows")) is not int or entry["rows"] < 0:
            raise ValueError("cutoff manifest lacks chain and row counts")
        additional = entry.get("additional_chains", {})
        if not isinstance(additional, dict) or any(not isinstance(expiries, dict) for expiries in additional.values()):
            raise ValueError("invalid cutoff additional chain status")
        start, end = entry.get("started_ms"), entry.get("ended_ms")
        if type(start) is not int or type(end) is not int or not start <= end:
            raise ValueError("invalid cutoff sweep interval")
        all_states = list(chains.values()) + [state for expiries in additional.values() for state in expiries.values()]
        for state in all_states:
            fetched = state.get("fetched_ms") if isinstance(state, dict) else None
            if fetched is not None and (type(fetched) is not int or not start <= fetched <= end):
                raise ValueError("cutoff chain fetch outside sweep interval")
        counts: Counter = Counter()
        for row in _read_jsonl_gz(path, day_dir, damaged):
            if not isinstance(row, dict) or row.get("sweep") != entry["sweep"]:
                raise ValueError("cutoff sweep row identity mismatch")
            t = row.get("t")
            if type(t) is not int or not start <= t <= end:
                raise ValueError("cutoff row outside matching sweep interval")
            for field in ("bid_ms", "ask_ms", "spot_ms", "trade_ms"):
                stamp = row.get(field)
                related = {"bid_ms": "bid", "ask_ms": "ask", "trade_ms": "last"}.get(field)
                if type(stamp) is int and stamp == 0 and related and row.get(related) is None:
                    row[field] = None  # Published missing-price rows carry a zero timestamp sentinel.
                    continue
                if stamp is not None and (type(stamp) is not int or not 946684800000 <= stamp <= t):
                    raise ValueError("cutoff row has future or non-millisecond evidence")
            underlying = row.get("underlying")
            role = row.get("collection_role")
            if role == "additional_expiry":
                state = additional.get(underlying, {}).get(row.get("expiration"))
                key = ("additional_expiry", underlying, row.get("expiration"))
                if state is not None and state.get("expiration") != row.get("expiration"):
                    raise ValueError("cutoff additional row expiration mismatch")
            elif role is None:
                state = chains.get(underlying)
                key = ("nearest", underlying)
                if state is not None and state.get("expiration") not in (None, row.get("expiration")):
                    raise ValueError("cutoff nearest row expiration mismatch")
            else:
                raise ValueError("unknown cutoff collection role")
            if not isinstance(state, dict) or state.get("status") != "ok":
                raise ValueError("cutoff row belongs to absent or failed chain")
            counts[key] += 1
            yield row
        for underlying, state in chains.items():
            expected = state.get("rows", 0) if isinstance(state, dict) else None
            if type(expected) is not int or expected < 0 or (state.get("status") != "ok" and expected):
                raise ValueError("invalid cutoff chain row count")
            if counts[("nearest", underlying)] != expected:
                raise ValueError("cutoff chain row count mismatch")
        for underlying, expiries in additional.items():
            for expiry, state in expiries.items():
                expected = state.get("rows", 0) if isinstance(state, dict) else None
                if (type(expected) is not int or expected < 0 or
                    state.get("expiration") != expiry or
                    (state.get("status") != "ok" and expected)):
                    raise ValueError("invalid cutoff additional chain row count")
                if counts[("additional_expiry", underlying, expiry)] != expected:
                    raise ValueError("cutoff additional chain row count mismatch")
        if sum(counts.values()) != entry["rows"]:
            raise ValueError("cutoff sweep row count mismatch")


def _read_manifest(day_dir: Path, damaged: list[dict[str, Any]] | None = None,
                   max_sweep: int | None = None) -> list[dict[str, Any]]:
    path = day_dir / "sweeps" / "manifest.jsonl"
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            entries.append(item)
            if max_sweep is not None and item.get("sweep") == max_sweep:
                break  # later manifest coverage cannot affect a cutoff build
        except ValueError as exc:  # e.g. a partial line from a crash mid-append
            entry = {"file": "sweeps/manifest.jsonl", "line": number, "error": str(exc)[:200]}
            runtime.log("oa203_damaged_artifact", **entry)
            if damaged is not None:
                damaged.append(entry)
    return entries


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False
