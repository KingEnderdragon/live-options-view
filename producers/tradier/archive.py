"""Where this lane writes in R2, and where it spools locally.

The archive layout is part of the data contract: the R2 keys produced here are
what the (archive-only) Tradier lane leaves behind. QQQ keeps the original
``moo144/tradier`` root so existing archives, leases and the deployed service
are unaffected; every other underlying gets a sibling namespace.
"""

from __future__ import annotations

from pathlib import Path

from ._probe import DEFAULT_UNDERLYING


# QQQ keeps the original layout so existing archives, leases and the deployed
# service are unaffected; other underlyings get a sibling namespace.
QQQ_ARCHIVE_ROOT = "moo144/tradier"


def archive_root(underlying: str) -> str:
    if underlying == DEFAULT_UNDERLYING:
        return QQQ_ARCHIVE_ROOT
    return f"{QQQ_ARCHIVE_ROOT}-{underlying.lower()}"


def spool_dir_name(underlying: str) -> str:
    if underlying == DEFAULT_UNDERLYING:
        return "moo144-collector-spool"
    return f"moo144-collector-spool-{underlying.lower()}"


def lease_key(run_date: str) -> str:
    """The day's single stream lease. It guards the one Tradier market-data
    stream, not an archive, so it stays at this fixed key whichever
    underlyings a collector is configured for: any two collectors sharing
    the token contend here and at most one can open a stream."""
    return f"{QQQ_ARCHIVE_ROOT}/{run_date}/lease.json"


def universe_key(prefix: str) -> str:
    return f"{prefix}/universe.json"


def spool_dir_for(base_spool_dir: Path, run_date: str) -> Path:
    """Per-date spool subdirectory, so segments from different sessions never
    share a directory or filename-collide, and a stale date's leftovers are
    trivially distinguishable from today's."""
    return base_spool_dir / run_date
