"""Bounded rolling 24-hour references for continuous crypto prices.

WHY this module: the DXLink Summary supplies a previous-close-style value, which
is not a rolling 24-hour change and may be a prior UTC day's close. The
interface needs a defensible 24-hour comparison for crypto, and the producer
must not invent one when history is missing. Samples are kept in ten-minute
buckets for just over 24 hours, persisted in one small R2 object, and restored
on restart. A reference is only returned when a sample exists within a bounded
tolerance of the 24-hour mark; otherwise the comparison is explicitly
``warmup`` or ``unavailable``.

Only the three crypto identities enter this store; nothing else needs rolling
history, and the object stays a few kilobytes.
"""

import json
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

BUCKET_SECS = 600
RETENTION_BUCKETS = 150          # 25 hours: 24h target + warm-up margin
MAX_REFERENCE_GAP_BUCKETS = 3    # accept a sample up to 30 minutes early
PERSIST_SECS = 300
MAX_SAMPLES = 150 * 4            # hard bound even if a caller adds instruments

KEY = "intraday/price_references.json"
SCHEMA_VERSION = 1


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def bucket_for(moment: datetime) -> int:
    return int(moment.timestamp()) // BUCKET_SECS


class RollingReferenceStore:
    """Thread-safe ten-minute sample store with one bounded companion object."""

    def __init__(self, s3=None, *, key: str = KEY, clock=None,
                 persist_secs: int = PERSIST_SECS):
        self._s3 = s3
        self._key = key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._persist_secs = persist_secs
        self._samples: dict[str, dict[int, dict]] = {}
        self._lock = threading.Lock()
        self._loaded = False
        self._load_error: Optional[str] = None
        self._last_persist_at: Optional[datetime] = None
        self._last_persist_error: Optional[str] = None
        self._persist_count = 0

    # -- persistence ---------------------------------------------------------
    def load(self) -> bool:
        """Best-effort restore. A missing/broken object is a warm-up state."""
        if self._s3 is None:
            self._loaded = True
            self._load_error = "no_storage_client"
            return False
        try:
            payload = json.loads(self._s3.get_object(Bucket=_bucket(self._s3),
                                                     Key=self._key)["Body"].read())
        except Exception as exc:
            self._loaded = True
            self._load_error = type(exc).__name__
            return False
        try:
            self._merge_payload(payload)
        except Exception as exc:
            self._loaded = True
            self._load_error = type(exc).__name__
            return False
        self._loaded = True
        self._load_error = None
        self.prune(self._clock())
        return True

    def _merge_payload(self, payload):
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported_reference_schema")
        instruments = payload.get("instruments")
        if not isinstance(instruments, dict):
            raise ValueError("missing_instruments")
        now = self._clock()
        cutoff = bucket_for(now) - RETENTION_BUCKETS
        merged: dict[str, dict[int, dict]] = {}
        for label, entry in instruments.items():
            if not isinstance(label, str) or not isinstance(entry, dict):
                continue
            rows = entry.get("samples")
            if not isinstance(rows, list):
                continue
            kept: dict[int, dict] = {}
            for item in rows:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    bucket = int(item[0])
                    value = float(item[1])
                except (TypeError, ValueError):
                    continue
                if bucket < cutoff or not math.isfinite(value) or value <= 0:
                    continue
                kept[bucket] = {"value": value, "clock": entry.get("clock", "provider_event_clock")}
            if kept:
                merged[label] = kept
        with self._lock:
            self._samples = merged

    def persist(self, force: bool = False) -> bool:
        """Write the bounded object at most once per ``persist_secs``."""
        if self._s3 is None:
            return False
        now = self._clock()
        with self._lock:
            if (not force and self._last_persist_at is not None
                    and (now - self._last_persist_at).total_seconds() < self._persist_secs):
                return False
            snapshot = {label: dict(buckets) for label, buckets in self._samples.items()}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _iso(now),
            "bucket_seconds": BUCKET_SECS,
            "retention_buckets": RETENTION_BUCKETS,
            "instruments": {
                label: {
                    "clock": next(iter(buckets.values())).get("clock", "provider_event_clock"),
                    "samples": [[bucket, round(row["value"], 8)]
                                for bucket, row in sorted(buckets.items())],
                }
                for label, buckets in snapshot.items()
            },
        }
        try:
            self._s3.put_object(Bucket=_bucket(self._s3), Key=self._key,
                                Body=json.dumps(payload, separators=(",", ":")).encode(),
                                ContentType="application/json",
                                CacheControl="no-cache, max-age=0")
        except Exception as exc:
            with self._lock:
                self._last_persist_error = type(exc).__name__
            return False
        with self._lock:
            self._last_persist_at = now
            self._last_persist_error = None
            self._persist_count += 1
        return True

    # -- sampling ------------------------------------------------------------
    def observe(self, label: str, price, observed_at=None, *,
                clock: str = "provider_event_clock") -> bool:
        """Record one valid positive value into its ten-minute bucket."""
        try:
            value = float(price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0:
            return False
        anchor = _parse(observed_at) or self._clock()
        if anchor > self._clock() + timedelta(minutes=5):
            # A future provider timestamp is not usable history.
            return False
        bucket = bucket_for(anchor)
        with self._lock:
            buckets = self._samples.setdefault(label, {})
            buckets[bucket] = {"value": value, "clock": clock}
            if len(buckets) > RETENTION_BUCKETS + MAX_REFERENCE_GAP_BUCKETS:
                for stale in sorted(buckets)[:-RETENTION_BUCKETS]:
                    buckets.pop(stale, None)
            total = sum(len(rows) for rows in self._samples.values())
            if total > MAX_SAMPLES:
                # Keep the newest buckets across all instruments.
                newest = sorted(
                    ((bucket, label) for label, rows in self._samples.items() for bucket in rows),
                    reverse=True,
                )[:MAX_SAMPLES]
                keep = {(label, bucket) for bucket, label in newest}
                for label in list(self._samples):
                    self._samples[label] = {
                        bucket: row for bucket, row in self._samples[label].items()
                        if (label, bucket) in keep
                    }
                    if not self._samples[label]:
                        self._samples.pop(label)
        return True

    def prune(self, now: datetime | None = None):
        now = now or self._clock()
        cutoff = bucket_for(now) - RETENTION_BUCKETS
        with self._lock:
            for label in list(self._samples):
                kept = {b: row for b, row in self._samples[label].items() if b >= cutoff}
                if kept:
                    self._samples[label] = kept
                else:
                    self._samples.pop(label)

    # -- reads ---------------------------------------------------------------
    def reference(self, label: str, now: datetime | None = None,
                  instrument_id: str | None = None,
                  source_symbol: str | None = None) -> dict | None:
        """Return a usable 24-hour reference or None (caller records warm-up)."""
        now = now or self._clock()
        target = bucket_for(now) - (24 * 3600 // BUCKET_SECS)
        with self._lock:
            buckets = dict(self._samples.get(label, {}))
        if not buckets:
            return None
        # Prefer the exact 24-hour mark, then equal-width lookback/lookahead.
        candidates = [target]
        for offset in range(1, MAX_REFERENCE_GAP_BUCKETS + 1):
            candidates.extend([target - offset, target + offset])
        for candidate in candidates:
            row = buckets.get(candidate)
            if row is None:
                continue
            sample_at = datetime.fromtimestamp(candidate * BUCKET_SECS, timezone.utc)
            return {
                "instrument_id": instrument_id,
                "value": row["value"],
                "source": "rolling_history",
                "source_symbol": source_symbol,
                "price_basis": "rolling_24h",
                "observed_at": _iso(sample_at),
                "observation_time_reason": (
                    None if row.get("clock") == "provider_event_clock"
                    else "sample_clock_is_collector_receipt"
                ),
                "observation_date": None,
                "observation_timezone": "UTC",
                "observation_granularity": "event",
                "session": "continuous",
                "adjustment_basis": "unadjusted",
                "resolved_contract": None,
                "reference_age_seconds": round((now - sample_at).total_seconds(), 1),
                "sample_clock": row.get("clock", "provider_event_clock"),
            }
        return None

    def status(self, now: datetime | None = None) -> dict:
        now = now or self._clock()
        target = bucket_for(now) - (24 * 3600 // BUCKET_SECS)
        with self._lock:
            ready = sorted(label for label, buckets in self._samples.items()
                           if any(target - MAX_REFERENCE_GAP_BUCKETS <= b <= target + MAX_REFERENCE_GAP_BUCKETS
                                  for b in buckets))
            warming = sorted(label for label in self._samples if label not in ready)
            counts = {label: len(buckets) for label, buckets in self._samples.items()}
            last_persist = self._last_persist_at
            persist_error = self._last_persist_error
            load_error = self._load_error
            persist_count = self._persist_count
        return {
            "bucket_seconds": BUCKET_SECS,
            "retention_buckets": RETENTION_BUCKETS,
            "sample_counts": counts,
            "ready": ready,
            "warming_up": warming,
            "loaded": self._loaded,
            "load_error": load_error,
            "last_persist_at": _iso(last_persist) if last_persist else None,
            "last_persist_error": persist_error,
            "persist_count": persist_count,
        }

    def sample_count(self, label: str) -> int:
        with self._lock:
            return len(self._samples.get(label, {}))


def _bucket(s3) -> str:
    """Tests inject a bucket on the fake client; production reads config."""
    bucket = getattr(s3, "bucket", None)
    if bucket:
        return bucket
    from .config import R2_BUCKET
    return R2_BUCKET


def change_percent(price, reference) -> float | None:
    """Percent change against a valid positive reference, never a zero fill."""
    try:
        current = float(price)
        base = float((reference or {}).get("value"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not math.isfinite(base) or current <= 0 or base <= 0:
        return None
    return round((current - base) / base * 100, 2)


__all__ = [
    "BUCKET_SECS", "RETENTION_BUCKETS", "MAX_REFERENCE_GAP_BUCKETS",
    "PERSIST_SECS", "KEY", "RollingReferenceStore", "bucket_for",
    "change_percent",
]
