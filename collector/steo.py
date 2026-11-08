"""EIA STEO feed: the monthly crude-calibration publication.

WHY this module: the STEO loop is independent of the market session -- it
polls EIA on its own cadence, keeps a vintage log for revision diffs, and
publishes macro/eia_steo.json. It reads the crude-calibration math
(collector.crude_calibration), the shared OVX price from the last-known
strip, and nothing else from the collector. Its test patch points
(fetch_eia_steo_rows, stubbed by verify_eia_steo_calibration, and the
FrozenDatetime freeze on datetime) resolve through the package entry.
"""

import json
import time
from datetime import timezone

import requests

import collector as _entry  # entry indirection for push_eia_steo's fetch + datetime patch points

from . import crude_calibration as cc
from .config import (
    EIA_API_KEY,
    EIA_STEO_KEY,
    EIA_STEO_LOG_KEY,
    EIA_STEO_MAX_VINTAGES,
    EIA_STEO_PAGE_LENGTH,
    EIA_STEO_POLL_SECS,
    EIA_STEO_URL,
    R2_BUCKET,
    log,
)
from .state import _last_prices

# -- eia_steo.json upload (every EIA_STEO_POLL_SECS, optional feature) -------


def fetch_eia_steo_rows(api_key: str) -> list[dict]:
    """Raw rows from EIA's v2 STEO API for the series crude_calibration.py
    tracks (see BRENT_SERIES_ID/WTI_SERIES_ID/BALANCE_SERIES_ID there),
    most-recent-period-first. Network I/O only -- parsing/normalization is
    crude_calibration.parse_steo_rows.

    Series IDs and the response shape assumed here (`response.data`, each row
    a flat dict with `period`/`seriesId`/`value`) should be verified against
    a live call before this is trusted -- see crude_calibration.py's module
    docstring.

    An earlier version passed `length=36` intending "36 periods of history
    per series" -- but EIA v2's `length` caps *total rows in the response*,
    and with 3 series requested together that capped the combined response
    at 36 rows total (~12 periods across 3 series), silently truncating
    history without any error. Flagged in review. Fixed two ways: request a
    `length` (`EIA_STEO_PAGE_LENGTH`) with real margin over what 3 series x
    36 periods needs, and verify the server's own `response.total` count
    against what was actually returned -- raising rather than silently
    returning a truncated page if EIA's per-request cap or a future
    additional series ever exceeds it. `total` is only checked when present;
    an unexpected response schema degrades to "can't verify," not a crash.
    """
    params = {
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[seriesId][]": [cc.BRENT_SERIES_ID, cc.WTI_SERIES_ID, cc.BALANCE_SERIES_ID],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": EIA_STEO_PAGE_LENGTH,
    }
    resp = requests.get(EIA_STEO_URL, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()["response"]
    data = payload["data"]
    total = payload.get("total")
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        # A missing/malformed `total` means we cannot verify the response
        # wasn't truncated -- fail closed rather than silently persisting
        # unverifiable partial data as a new vintage. Flagged in review: the
        # original `if total is not None:` guard skipped verification
        # entirely (and swallowed unparseable values into a no-op) whenever
        # EIA's response didn't carry a clean int `total`, which is exactly
        # the case this check exists to catch.
        raise RuntimeError(
            f"EIA STEO response missing or malformed 'total' field ({total!r}) -- cannot "
            f"verify the {len(data)} returned row(s) are complete, refusing to treat them "
            f"as a trustworthy vintage."
        )
    if total_int > len(data):
        raise RuntimeError(
            f"EIA STEO response truncated: server reports {total_int} total row(s) but "
            f"only {len(data)} were returned (length={EIA_STEO_PAGE_LENGTH}). Increase "
            f"EIA_STEO_PAGE_LENGTH or add real offset-based pagination rather than "
            f"silently using a partial history."
        )
    return data


def load_steo_vintage_log(s3) -> list[dict]:
    """Rolling log of past STEO releases this collector has itself observed
    -- EIA's live API serves the current published values, not a queryable
    history of what a *prior* release forecast for the same future month, so
    that vintage-over-vintage comparison has to be built up locally over
    time, one release per stored entry (same reasoning as RVOL's own
    baselines/ history, see market_signals.py).

    Raises on anything other than the log genuinely never having been
    written yet (`NoSuchKey` -- a fresh deployment/bucket). A transient R2
    read failure, an auth error, or corrupt JSON must NOT be silently
    treated as "no history exists": `push_eia_steo` appends to whatever
    this returns and overwrites the R2 object with the result, so treating
    a *read* failure as an empty log would destroy every real prior
    vintage on the next write. Flagged in review; let the caller's own
    exception handling (`eia_steo_loop`'s per-cycle try/except) skip this
    poll and retry later instead.
    """
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return []
    return json.loads(body)


def save_steo_vintage_log(s3, entries: list[dict]) -> None:
    payload = json.dumps(entries[-EIA_STEO_MAX_VINTAGES:]).encode()
    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_LOG_KEY,
        Body=payload, ContentType="application/json",
    )


def push_eia_steo(s3) -> None:
    rows = _entry.fetch_eia_steo_rows(EIA_API_KEY)
    # EIA's v2 API doesn't hand back "which monthly release this row came
    # from" directly, so the calendar month we happen to be polling in is
    # used as the vintage *label* -- but whether to mint a new vintage entry
    # at all is decided by whether the fetched data actually changed from
    # the most recently logged entry, not by the calendar alone. Labeling by
    # calendar month unconditionally (the original approach) creates a
    # phantom release right at a boundary: a poll in the first few days of a
    # month, before that month's STEO has actually been published, would log
    # a new entry under the new month's label containing last month's
    # unchanged data -- and anything reading macro/eia_steo.json in that
    # window (or a vintage-over-vintage comparison run against it) would see
    # a fabricated "this month's release" that was never actually published.
    # Flagged in review. Content-equality against the last logged entry is
    # the real signal for "did a new release land"; the calendar month is
    # only the label attached once that's true.
    release_period = _entry.datetime.now(timezone.utc).strftime("%Y-%m")
    current = cc.parse_steo_rows(rows, release_period=release_period)
    current_points_payload = [
        {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
        for p in current.points
    ]

    entries = load_steo_vintage_log(s3)  # raises on a real read failure -- see that function's docstring
    if entries and entries[-1].get("points") == current_points_payload:
        # Byte-identical to the most recent logged vintage: EIA hasn't
        # actually published a new STEO since we last saw one, even if the
        # calendar month has rolled over. Reuse the existing label instead
        # of minting a phantom one, and skip the R2 write entirely.
        release_period = entries[-1]["release_period"]
        # Flagged in review: `current` was built above with the pre-
        # canonicalization calendar guess, so leaving it as-is would let
        # compare_vintages() below stamp each SteoRevision's current_release
        # with that stale guess while the top-level payload's current_release
        # uses the canonicalized label -- the two would disagree. Rebuild
        # `current` with the final label before it's used for anything else.
        current = cc.SteoVintage(release_period=release_period, points=current.points)
    else:
        entries = [e for e in entries if e.get("release_period") != release_period]
        entries.append({"release_period": release_period, "points": current_points_payload})
        save_steo_vintage_log(s3, entries)

    prior_vintage = None
    if len(entries) >= 2:
        prior_entry = entries[-2]
        prior_vintage = cc.SteoVintage(
            release_period=prior_entry["release_period"],
            points=tuple(cc.SteoPricePoint(**p) for p in prior_entry["points"]),
        )

    revisions = []
    if prior_vintage is not None:
        for point in current.points:
            revision = cc.compare_vintages(prior_vintage, current, point.period)
            if revision is not None:
                revisions.append(revision.__dict__)

    ovx_value = _last_prices.get("OVX", {}).get("price")

    payload = json.dumps({
        "timestamp":       _entry.datetime.now(timezone.utc).isoformat(),
        "current_release": release_period,
        "prior_release":   prior_vintage.release_period if prior_vintage else None,
        "points":          [
            {"period": p.period, "brent": p.brent, "wti": p.wti, "balance": p.balance}
            for p in current.points
        ],
        "revisions":       revisions,
        "ovx": {
            "value":  ovx_value,
            "regime": cc.classify_ovx_regime(ovx_value),
        },
    }, default=str).encode()

    s3.put_object(
        Bucket=R2_BUCKET, Key=EIA_STEO_KEY,
        Body=payload, ContentType="application/json",
        CacheControl="no-cache, max-age=300",
    )


def eia_steo_loop(s3) -> None:
    if not EIA_API_KEY:
        log.info("EIA_API_KEY not set -- skipping macro/eia_steo.json (crude calibration) feed")
        return
    while True:
        try:
            push_eia_steo(s3)
        except Exception as e:
            log.warning(f"eia_steo.json error: {e}")
        time.sleep(EIA_STEO_POLL_SECS)



__all__ = [
    "fetch_eia_steo_rows",
    "load_steo_vintage_log",
    "save_steo_vintage_log",
    "push_eia_steo",
    "eia_steo_loop",
]
