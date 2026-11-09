"""The JSON evidence this lane publishes to R2.

Every artifact is written through the verified-put helper and carries a
content hash, so a manifest can be audited against the objects it names.
"""

from __future__ import annotations

import json
from typing import Any

from ._probe import put_bytes_verified, utc_now
from .spool import Uploader
from .stats import BoundedStats


def json_artifact(r2: Any, bucket: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    return put_bytes_verified(r2, bucket, key, body, "application/json")


def write_health(
    r2: Any,
    bucket: str,
    prefix: str,
    stats: BoundedStats,
    uploader: Uploader,
    reconnects: int,
    gap_seconds: float,
) -> None:
    payload = {
        "updated_at": utc_now(),
        "first_receipt_timestamp": stats.first_receipt_ts,
        "last_receipt_timestamp": stats.last_receipt_ts,
        "event_counts": dict(stats.counts),
        "reconnects": reconnects,
        "gap_seconds": round(gap_seconds, 3),
        "malformed_payloads": stats.malformed,
        "duplicate_count": stats.duplicate_count,
        "spool_backlog_bytes": uploader.spool_bytes(),
        "peak_spool_bytes": uploader.peak_spool_bytes,
        "trade_delivery_lag_ms": stats.summary()["trade_delivery_lag_ms"],
        "spool_overloaded": uploader.overloaded,
        "upload_failures": uploader.failures,
    }
    json_artifact(r2, bucket, f"{prefix}/health.json", payload)
