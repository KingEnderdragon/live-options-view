"""Local segment spool, background uploader and record counting.

Ingest writes closed segments to disk and hands the path to a background
thread; a slow or failing upload never blocks the stream reader. Spool growth
is bounded: passing the cap stops ingestion (``SpoolExhausted``) rather than
silently dropping an event.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ._probe import upload_file_verified


class SpoolExhausted(RuntimeError):
    """Raised when the upload spool has grown past its configured cap."""


class Uploader:
    """Background thread that uploads closed spool segments with retry/backoff.

    Ingest hands off a finished file path via ``enqueue`` and never blocks on
    the network. ``spool_bytes()`` reflects every byte not yet durably
    uploaded (queued *and* in-flight); enforcement of the cap happens in
    ``SegmentSpool.write`` so a full spool stops ingestion with an explicit,
    reported ``SpoolExhausted`` rather than growing without bound.
    """

    def __init__(
        self,
        r2: Any,
        bucket: str,
        prefix: str,
        max_spool_bytes: int,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.r2 = r2
        self.bucket = bucket
        self.prefix = prefix
        self.max_spool_bytes = max_spool_bytes
        self.sleeper = sleeper
        self.queue: "deque[Path]" = deque()
        self.lock = threading.RLock()
        self.artifacts: list[dict[str, Any]] = []
        self.failures = 0
        self.overloaded = False
        self.peak_spool_bytes = 0
        self._stop = False
        self._thread: threading.Thread | None = None

    def spool_bytes(self) -> int:
        total = 0
        with self.lock:
            for path in self.queue:
                # The upload thread unlinks a finished segment before taking the
                # lock to dequeue it; a vanished file is uploaded, not an error.
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def enqueue(self, path: Path) -> None:
        with self.lock:
            self.queue.append(path)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop or self.queue:
            with self.lock:
                path = self.queue[0] if self.queue else None
            if path is None:
                self.sleeper(0.05)
                continue
            attempt = 0
            while True:
                try:
                    key = f"{self.prefix}/{path.name}"
                    artifact = upload_file_verified(
                        self.r2, self.bucket, path, key, "application/x-ndjson", "gzip"
                    )
                    # Only read the file for a record count once the upload
                    # has actually succeeded -- not on every failed retry.
                    artifact["records"] = count_ndjson_gz_records_local(path)
                    self.artifacts.append(artifact)
                    path.unlink(missing_ok=True)
                    with self.lock:
                        self.queue.popleft()
                        if self.spool_bytes() <= self.max_spool_bytes:
                            self.overloaded = False
                    break
                except Exception:
                    self.failures += 1
                    attempt += 1
                    delay = min(2 ** attempt, 30)
                    self.sleeper(delay)

    def drain_and_stop(self, timeout: float = 60.0) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def fully_drained(self) -> bool:
        return not self.overloaded and self.spool_bytes() == 0 and len(self.queue) == 0


class SegmentSpool:
    """Writes gzip NDJSON segments to a persistent spool dir and hands closed
    ones to the uploader. Filenames are collision-free across restarts.

    Every write checks total outstanding spool bytes (queued + the segment
    currently being written) against the uploader's cap *before* writing the
    event; once the cap would be exceeded, capture must stop rather than
    keep accepting events the disk can't durably hold.
    """

    def __init__(
        self,
        spool_dir: Path,
        owner_id: str,
        uploader: Uploader,
        checkpoint_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool_dir = spool_dir
        self.owner_id = owner_id
        self.uploader = uploader
        self.checkpoint_seconds = checkpoint_seconds
        self.clock = clock
        self.index = 0
        self.records = 0
        self.opened_at = clock()
        self.path: Path | None = None
        self.handle: Any = None
        self.segment_records: dict[str, int] = {}
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self.path = self.spool_dir / f"{self.owner_id}-part-{self.index:04d}.ndjson.gz"
        self.handle = gzip.open(self.path, "wt", encoding="utf-8")
        self.records = 0
        self.opened_at = self.clock()

    def _current_size(self) -> int:
        return self.path.stat().st_size if self.path and self.path.exists() else 0

    def write(self, event: dict[str, Any]) -> None:
        total_outstanding = self.uploader.spool_bytes() + self._current_size()
        self.uploader.peak_spool_bytes = max(self.uploader.peak_spool_bytes, total_outstanding)
        if total_outstanding > self.uploader.max_spool_bytes:
            self.uploader.overloaded = True
            raise SpoolExhausted(
                f"spool backlog {total_outstanding} bytes exceeds cap "
                f"{self.uploader.max_spool_bytes} bytes; stopping ingestion"
            )
        self.handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        self.records += 1
        self.handle.flush()  # keep on-disk size current for the spool-cap check above
        if self.clock() - self.opened_at >= self.checkpoint_seconds:
            self.rotate()

    def rotate(self, final: bool = False) -> None:
        if self.handle is None or self.path is None:
            return
        self.handle.close()
        if self.records:
            self.segment_records[self.path.name] = self.records
            self.uploader.enqueue(self.path)
        elif self.path.exists():
            self.path.unlink()
        self.handle = None
        self.path = None
        if not final:
            self.index += 1
            self._open()

    def close(self) -> None:
        self.rotate(final=True)


def count_ndjson_gz_records(r2: Any, bucket: str, key: str) -> int | None:
    """Best-effort record count for a reconciled segment this process didn't
    write itself, so manifest totals can be audited against event_parts."""
    try:
        body = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
        return sum(1 for line in gzip.decompress(body).splitlines() if line.strip())
    except Exception:
        return None


def count_ndjson_gz_records_local(path: Path) -> int | None:
    """Best-effort record count for a local segment before/at upload time --
    covers both freshly-rotated and resumed-orphan segments uniformly, so
    every uploaded artifact carries a durable count, not only reconciled
    remote ones."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except Exception:
        return None
