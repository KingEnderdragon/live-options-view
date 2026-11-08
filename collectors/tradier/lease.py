"""The day's single stream lease.

Tradier permits one simultaneous market-data stream per account, so the run is
fenced by a renewable lease under the day's key: a heartbeat renews it
conditionally against this owner's own copy, and ownership is only given up
once the last *confirmed* deadline actually elapses. A transient renewal
failure is retried, never swallowed into a false loss.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .archive import lease_key


class LeaseLost(RuntimeError):
    """Base class: this process can no longer prove it holds the lease.

    Never raised directly by renew_lease -- always one of the two specific
    subclasses below, since "confirmed takeover" and "confirmed absence"
    require different recovery behavior (see run_lease_heartbeat). Kept as
    a common base so any code that only needs "should ingestion stop" can
    still catch it broadly.
    """


class LeaseTakenByAnotherOwner(LeaseLost):
    """A verified, live competing owner now holds the lease. This process
    is definitively no longer the owner -- restarting would only compete
    with the real new owner, so this must never trigger a restart."""


class LeaseMissing(LeaseLost):
    """The lease object is confirmed absent (deleted, or expired and never
    recreated) -- NOT evidence that another owner exists. Unlike a
    confirmed takeover, restarting here is the correct recovery: nobody
    is known to be collecting, so a fresh process should reacquire."""


class LeaseOwnershipUncertain(LeaseLost):
    """Stop intake and recover; no live competing owner has been verified."""


class LeaseUnavailable(RuntimeError):
    """Raised when the lease object could not be read due to a transient
    storage/service failure -- distinct from a *confirmed* absence
    (never created) or a confirmed takeover (another owner_id present).
    Ownership is neither proven lost nor proven held in this case."""


def _is_confirmed_absent(exc: Exception) -> bool:
    """True only for a response that positively confirms the object doesn't
    exist (a 404/NoSuchKey) -- never for a 5xx or other service/transport
    failure, which proves nothing about whether the lease exists."""
    response = getattr(exc, "response", {}) or {}
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    code = (response.get("Error") or {}).get("Code")
    return status == 404 or code in {"NoSuchKey", "404", "NotFound"}


def _read_lease(client: Any, bucket: str, run_date: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the lease object, or (None, None) for a *confirmed* absence.

    A transient storage/service failure (a 5xx, timeout, or any ClientError
    that isn't a positive 404) raises ``LeaseUnavailable`` instead of being
    silently treated as "no lease" -- that conflation is what let a single
    InternalError look identical to a genuinely deleted/never-created lease
    to every caller.
    """
    key = lease_key(run_date)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body), head.get("ETag")
    except client.exceptions.NoSuchKey:
        return None, None
    except client.exceptions.ClientError as exc:
        if _is_confirmed_absent(exc):
            return None, None
        raise LeaseUnavailable(f"transient error reading lease for {run_date}") from exc


def acquire_lease(
    client: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Acquire or take over the day's collection lease.

    Raises RuntimeError if another owner's lease is still live. A lease
    that exists but has expired may be taken over by a new owner.
    """
    key = lease_key(run_date)
    current, existing_etag = _read_lease(client, bucket, run_date)
    if current is not None:
        expires_at = datetime.fromisoformat(current["expires_at"])
        if expires_at > now() and current.get("owner_id") != owner_id:
            raise RuntimeError(
                f"MOO-144 collection lease for {run_date} is held by "
                f"{current.get('owner_id')!r} until {current['expires_at']}"
            )

    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    put_kwargs: dict[str, Any] = {"IfMatch": existing_etag} if existing_etag else {"IfNoneMatch": "*"}
    body = (json.dumps(lease, indent=2, sort_keys=True) + "\n").encode()
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", **put_kwargs)
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            raise RuntimeError(
                f"MOO-144 collection lease for {run_date} was taken concurrently"
            ) from exc
        raise
    return lease


def _require_live_lease_owner(
    current: dict[str, Any] | None, owner_id: str, run_date: str,
    now: Callable[[], datetime],
) -> None:
    if current is None:
        raise LeaseMissing(f"MOO-144 lease for {run_date} is confirmed absent")
    try:
        current_owner = current["owner_id"]
        expires_at = datetime.fromisoformat(current["expires_at"])
        if not isinstance(current_owner, str) or not current_owner or expires_at.tzinfo is None:
            raise ValueError("invalid lease identity or expiry")
        live = expires_at > now()
    except (KeyError, TypeError, ValueError) as exc:
        raise LeaseOwnershipUncertain(f"MOO-144 lease for {run_date} is invalid") from exc
    if not live:
        raise LeaseOwnershipUncertain(f"MOO-144 lease for {run_date} has expired")
    if current_owner != owner_id:
        raise LeaseTakenByAnotherOwner(
            f"MOO-144 lease for {run_date} is now owned by {current_owner!r}"
        )


def renew_lease(
    client: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Renew this owner's lease, fenced against having lost ownership.

    Unlike acquisition, a renewal must never be able to clobber a newer
    owner's lease: it reads the current object first and requires both a
    matching owner_id and a conditional ``IfMatch`` on that exact version.

    Raises ``LeaseMissing`` if the lease object is confirmed absent (never
    proof of a competing owner), or ``LeaseTakenByAnotherOwner`` if a live
    competing owner is actually present -- these require different
    recovery behavior and must never be conflated (see run_lease_heartbeat).
    A transient read failure propagates as ``LeaseUnavailable`` from
    ``_read_lease`` unchanged.
    """
    current, etag = _read_lease(client, bucket, run_date)
    _require_live_lease_owner(current, owner_id, run_date, now)
    lease = {
        "run_date": run_date,
        "owner_id": owner_id,
        "acquired_at": now().isoformat(),
        "expires_at": (now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    body = (json.dumps(lease, indent=2, sort_keys=True) + "\n").encode()
    try:
        client.put_object(
            Bucket=bucket, Key=lease_key(run_date), Body=body,
            ContentType="application/json", IfMatch=etag,
        )
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status in (412, 409) or code in {"PreconditionFailed", "412"}:
            # A failed conditional write proves only that renewal failed.
            # Deletion also causes this response; verify ownership afresh.
            try:
                current, _ = _read_lease(client, bucket, run_date)
            except Exception as read_exc:
                raise LeaseOwnershipUncertain(
                    f"MOO-144 lease for {run_date} could not be verified after renewal conflict"
                ) from read_exc
            _require_live_lease_owner(current, owner_id, run_date, now)
            raise LeaseOwnershipUncertain(
                f"MOO-144 lease for {run_date} renewal conflicted without a verified takeover"
            ) from exc
        raise
    return lease


def run_lease_heartbeat(
    r2: Any,
    bucket: str,
    run_date: str,
    owner_id: str,
    ttl_seconds: int,
    confirmed_until_ref: dict[str, datetime],
    stop: threading.Event,
    lease_lost: threading.Event,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    wait: Callable[[float], bool] | None = None,
    loss_reason: dict[str, str] | None = None,
) -> None:
    """Renew the collection lease every ``ttl_seconds/3`` until told to stop.

    This function does NOT enforce the confirmed-ownership deadline itself
    -- that used to happen here (checked only after a renewal request
    returned), which meant a slow/blocked request could carry the process
    past its deadline before the check ever ran. Deadline enforcement is
    now ``run_lease_deadline_watchdog``'s job, on its own tight polling
    cadence that is never blocked by a network call. This function's only
    responsibilities are: attempt renewals, keep ``confirmed_until_ref``
    current on success (read by the watchdog), and record *why* ownership
    was confirmed lost when that happens -- three genuinely different
    outcomes, via ``loss_reason``:

    - ``LeaseTakenByAnotherOwner`` (a verified, live competing owner) means
      this process is definitively no longer the owner. Restarting would
      only compete with the real new owner.
      ``loss_reason["reason"] = "confirmed_takeover"``.
    - ``LeaseMissing`` (the lease object is confirmed absent -- deleted or
      never recreated) is NOT evidence of a competing owner. Restarting is
      the correct recovery here: nobody is known to be collecting.
      ``loss_reason["reason"] = "confirmed_absence"``.
    - Any other failure (``LeaseUnavailable``, or an unexpected exception)
      proves nothing about ownership either way and is simply retried;
      the watchdog is what eventually stops intake if this keeps failing
      long enough to cross the confirmed deadline.
    """
    wait = wait if wait is not None else stop.wait
    while not wait(ttl_seconds / 3):
        try:
            lease = renew_lease(r2, bucket, run_date, owner_id, ttl_seconds, now=now)
            confirmed_until_ref["value"] = datetime.fromisoformat(lease["expires_at"])
        except LeaseTakenByAnotherOwner:
            if loss_reason is not None:
                loss_reason["reason"] = "confirmed_takeover"
            lease_lost.set()
            return
        except LeaseMissing:
            if loss_reason is not None:
                loss_reason["reason"] = "confirmed_absence"
            lease_lost.set()
            return
        except LeaseLost:
            # Unclassified loss is not proof of a live competing owner.
            # Reacquisition remains conditional, so recovery cannot steal
            # a lease from a verified live owner.
            if loss_reason is not None:
                loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost.set()
            return
        except Exception:
            # Transient failure -- keep retrying. The confirmed deadline is
            # enforced independently by run_lease_deadline_watchdog.
            pass


def run_lease_deadline_watchdog(
    confirmed_until_ref: dict[str, datetime],
    stop: threading.Event,
    lease_lost: threading.Event,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    wait: Callable[[float], bool] | None = None,
    poll_seconds: float = 1.0,
    loss_reason: dict[str, str] | None = None,
) -> None:
    """Independently enforce the confirmed-ownership deadline.

    Runs on its own short, fixed polling cadence -- never blocked by a
    network call -- so a renewal request that hangs or takes far longer
    than expected can never postpone stopping intake past the deadline
    this process last actually proved it held. ``confirmed_until_ref`` is
    the same shared reference ``run_lease_heartbeat`` updates on every
    successful renewal, so an extended deadline is picked up on this
    thread's very next poll.

    Uses ``loss_reason.setdefault`` rather than overwriting: if the
    heartbeat has already recorded a more specific reason (a confirmed
    takeover or confirmed absence) for the same ``lease_lost`` signal,
    that stays authoritative over this fail-safe's generic
    ``"ownership_uncertain"``.
    """
    wait = wait if wait is not None else stop.wait
    while not wait(poll_seconds):
        if now() >= confirmed_until_ref["value"]:
            if loss_reason is not None:
                loss_reason.setdefault("reason", "ownership_uncertain")
            lease_lost.set()
            return
