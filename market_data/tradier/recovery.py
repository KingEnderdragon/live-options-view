"""Crash/restart reconciliation against R2.

A restarted process must know, per segment name, whether R2 already holds
exactly this content before deleting local evidence. Content identity is
verified (downloading and hashing when the listing ETag alone cannot establish
it), never assumed. A stale date's leftover spool is recovered under its own
archive prefix, never under today's.
"""

from __future__ import annotations

import gzip
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .archive import QQQ_ARCHIVE_ROOT
from .spool import Uploader


def _is_readable_gzip_ndjson(path: Path) -> bool:
    """True if ``path`` is a complete, uncorrupted gzip NDJSON segment.

    A segment truncated mid-write by a crash raises ``EOFError``/``OSError``
    when fully decompressed; that distinguishes a resumable finished segment
    from one that must be flagged for manual review instead of re-uploaded.
    """
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def _remote_content_matches(
    r2: Any, bucket: str, key: str, local_path: Path, remote_item: dict[str, Any],
) -> bool:
    """Verify local file content against the durable remote object.

    Always returns a definitive True/False -- unknown identity must never
    authorize deletion. When the listing's ETag is a plain MD5 (the normal
    case for our single-part uploads) it's compared directly. When it
    isn't (e.g. a multipart upload, or missing), the listing alone can't
    establish identity, so the actual remote bytes are fetched and hashed
    instead of trusting the upload's own historical verification.
    """
    import hashlib
    local_digest = hashlib.md5()
    with local_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            local_digest.update(chunk)
    local_hex = local_digest.hexdigest()

    etag = str(remote_item.get("ETag") or "").strip('"')
    if etag and "-" not in etag:
        return local_hex == etag

    try:
        remote_bytes = r2.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return False  # can't verify -- never delete on an unverifiable identity
    return local_hex == hashlib.md5(remote_bytes).hexdigest()


def reconcile_existing_segments(
    r2: Any, bucket: str, prefix: str, spool_dir: Path,
) -> dict[str, Any]:
    """Reconcile local spool state against the durable archive before resuming.

    - Segments already durably uploaded (verified by content, not just name)
      are reported as artifacts and their local copy is removed.
    - Segments that exist locally but are NOT yet on R2, and are readable,
      are queued for the uploader to resume -- a crash must not strand data.
    - Segments that are neither confirmed-uploaded-with-matching-content nor
      cleanly readable are reported separately for manual review; they are
      never silently deleted or silently re-uploaded under an assumed identity.

    Listing failures propagate (they are not treated as "the archive is
    empty") so a caller never certifies completeness against state it
    could not actually verify.
    """
    uploaded: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = r2.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item.get("Key", "")
            if key.endswith(".ndjson.gz"):
                uploaded[Path(key).name] = item
        if response.get("IsTruncated") and response.get("NextContinuationToken"):
            token = response["NextContinuationToken"]
            continue
        break

    artifacts = [
        {"key": f"{prefix}/{name}", "bytes": item.get("Size"), "reconciled": True}
        for name, item in uploaded.items()
    ]

    resume: list[Path] = []
    needs_review: list[str] = []
    if spool_dir.exists():
        for path in sorted(spool_dir.glob("*.ndjson.gz")):
            remote = uploaded.get(path.name)
            if remote is not None:
                key = f"{prefix}/{path.name}"
                if _remote_content_matches(r2, bucket, key, path, remote):
                    path.unlink(missing_ok=True)
                else:
                    needs_review.append(f"{path.name}: local content differs from archived object of the same name")
                continue
            if _is_readable_gzip_ndjson(path):
                resume.append(path)
            else:
                needs_review.append(f"{path.name}: truncated/corrupt, not resumable")

    return {"artifacts": artifacts, "resume": resume, "needs_review": needs_review}


def recover_stale_sessions(
    base_spool_dir: Path,
    current_run_date: str,
    r2: Any,
    bucket: str,
    uploader_sleeper: Callable[[float], None] = time.sleep,
    drain_timeout_seconds: float = 30.0,
    *,
    root: str = QQQ_ARCHIVE_ROOT,
    isolate_failures: bool = False,
) -> dict[str, Any]:
    """Recover any prior day's spool left behind by a crash, before today's
    session starts.

    Each stale date subdirectory is reconciled and resumed against its OWN
    archive prefix (``<root>/<that date>``) -- never today's -- so a
    leftover segment from a previous session is never misfiled under the
    current one. Best-effort and time-bounded: a date that doesn't finish
    draining within ``drain_timeout_seconds`` is left on disk for the next
    run rather than blocking today's collection indefinitely.

    With ``isolate_failures`` (optional lanes), an error recovering one date is
    recorded in that date's entry (``recovery_failed``) and its files are left
    on disk for a later run, instead of propagating.
    """
    report: dict[str, Any] = {}
    if not base_spool_dir.exists():
        return report
    for entry in sorted(base_spool_dir.iterdir()):
        if not entry.is_dir() or entry.name == current_run_date:
            continue
        try:
            date.fromisoformat(entry.name)
        except ValueError:
            continue  # not a session date directory -- leave it alone
        stale_prefix = f"{root}/{entry.name}"
        try:
            reconciliation = reconcile_existing_segments(r2, bucket, stale_prefix, entry)
        except Exception as exc:
            if not isolate_failures:
                raise
            report[entry.name] = {
                "prefix": stale_prefix,
                "recovery_failed": f"{type(exc).__name__}: {exc}",
                "retained_local_files": sorted(path.name for path in entry.glob("*.ndjson.gz")),
            }
            continue
        resumed_artifacts: list[dict[str, Any]] = []
        if reconciliation["resume"]:
            stale_uploader = Uploader(
                r2, bucket, stale_prefix, max_spool_bytes=1 << 62, sleeper=uploader_sleeper,
            )
            for path in reconciliation["resume"]:
                stale_uploader.enqueue(path)
            stale_uploader.start()
            stale_uploader.drain_and_stop(timeout=drain_timeout_seconds)
            resumed_artifacts = list(stale_uploader.artifacts)
        report[entry.name] = {
            "prefix": stale_prefix,
            "reconciled_artifacts": len(reconciliation["artifacts"]),
            "resumed_artifacts": resumed_artifacts,
            "resume_incomplete": len(resumed_artifacts) < len(reconciliation["resume"]),
            "needs_review": reconciliation["needs_review"],
        }
    return report
