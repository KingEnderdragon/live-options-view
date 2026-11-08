"""Durable, fail-closed bounds for provisional snapshots only.

No raw sweeps, daily final rankings or pre-policy snapshots are deleted.
One scanner replica is required; conditional reservations survive spool loss.
"""
from datetime import date, datetime, timedelta, timezone
import json
import re

POLICY_START = date(2026, 11, 9)
RETENTION_DAYS = 30
MAX_BYTES = 8 * 1024**3
RESERVATION_BYTES = 17 * 1024**2
MAX_DAILY_CUTOFFS = 16
MAX_DAILY_ATTEMPTS = 16
MAX_ATTEMPTS = 3
PREFIX = "oa203/scanner/"
MEMBER = re.compile(r"(?:latest(?:-attempt)?\.json|sweep_\d{4}\.json|sweep_\d{4}/rev_[0-9a-f]{64}/(?:summary\.json|leaderboard\.csv|qualified_ask_bid_v1\.csv|manifest\.jsonl)|budget/\d{4}-[123]\.json)$")
TOKEN = re.compile(r"budget/(\d{4})-([123])\.json$")


def inventory(client, bucket):
    dates = client.list_objects_v2(Bucket=bucket, Prefix=PREFIX, Delimiter="/", MaxKeys=1000)
    if dates.get("IsTruncated"):
        raise ValueError("active date inventory exceeds reviewed bound")
    objects = []
    for item in dates.get("CommonPrefixes", []):
        prefix = item["Prefix"]
        day_text = prefix.removeprefix(PREFIX).rstrip("/")
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if prefix != PREFIX + day.isoformat() + "/":
            continue
        active = prefix + "inprogress/"
        result = client.list_objects_v2(Bucket=bucket, Prefix=active, MaxKeys=1000)
        if result.get("IsTruncated"):
            raise ValueError("active object inventory exceeds reviewed bound")
        for obj in result.get("Contents", []):
            if not obj["Key"].startswith(active):
                raise ValueError("unexpected storage prefix")
            rel = obj["Key"][len(active):]
            if not MEMBER.fullmatch(rel):
                raise ValueError("unrecognized active artifact; manual reconciliation required")
            objects.append({**obj, "day": day, "relative": rel})
        if len(objects) > 32000:
            raise ValueError("active inventory exceeds reviewed bound")
    return objects


def clean_and_reserve(client, bucket, day, sweep, now=None):
    now = now or datetime.now(timezone.utc)
    if day < POLICY_START or day != now.date() or not 1 <= sweep <= 96:
        raise ValueError("active publication requires current policy session")
    objects = inventory(client, bucket)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    expired = [o for o in objects if o["day"] >= POLICY_START
               and o["day"] < cutoff.date() and o["LastModified"] < cutoff]
    for offset in range(0, len(expired), 1000):
        result = client.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": o["Key"]} for o in expired[offset:offset+1000]], "Quiet": True})
        if result.get("Errors"):
            raise ValueError("active retention cleanup failed")
    if expired:
        objects = inventory(client, bucket)
        if any(o["Key"] in {x["Key"] for x in expired} for o in objects):
            raise ValueError("active retention deletion not verified")
    tokens = [o for o in objects if TOKEN.fullmatch(o["relative"])]
    # Charge the full permitted generation even if a failed upload left no bytes.
    # Plus actual bytes: conservative double accounting also covers legacy data.
    charged = sum(o["Size"] for o in objects) + len(tokens) * RESERVATION_BYTES
    if charged + RESERVATION_BYTES > MAX_BYTES:
        raise ValueError("active remote storage budget exhausted")
    today = [o for o in tokens if o["day"] == day]
    used = {int(TOKEN.fullmatch(o["relative"]).group(1)) for o in today}
    if sweep not in used and len(used) >= MAX_DAILY_CUTOFFS:
        raise ValueError("active remote cutoff budget exhausted")
    if len(today) >= MAX_DAILY_ATTEMPTS:
        raise ValueError("active remote daily attempt budget exhausted")
    root = PREFIX + day.isoformat() + "/inprogress/"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        key = root + f"budget/{sweep:04d}-{attempt}.json"
        try:
            client.put_object(Bucket=bucket, Key=key, IfNoneMatch="*",
                              ContentType="application/json",
                              Body=json.dumps({"policy": 1, "reserved_at": now.isoformat(),
                                               "reserved_bytes": RESERVATION_BYTES}).encode())
        except Exception as exc:
            if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                continue
            raise
        return {"reservation": key, "retention_days": RETENTION_DAYS,
                "charged_bytes_before": charged, "max_bytes": MAX_BYTES,
                "expired_objects": len(expired), "attempt": attempt}
    raise ValueError("active remote retry budget exhausted")

def constrain_builder():
    """Linux child only: leave host headroom for the collector."""
    import os
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (230, 230))
    os.nice(10)
    if hasattr(os, "sched_getaffinity"):
        allowed = os.sched_getaffinity(0)
        if allowed:
            os.sched_setaffinity(0, {min(allowed)})
