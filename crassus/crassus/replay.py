"""Explicit lifecycle ownership for process-wide strategy observation state.

A strategy is a plain function from an observation to an intent, but three of
them keep rolling observation history at module scope: `put_call_ratio_qqq`,
`momentum_qqq` and `oi_skew_qqq`. That history is correct for one continuous
chronological replay and for the live runner, where wall-clock time only
moves forward and a snapshot older than the newest recorded one is a stalled
or out-of-order feed read.

It is not correct for an *independent* replay that begins again at an earlier
date inside the same interpreter -- a parameter sweep, a walk-forward fold, a
repeated CI run. The carried-over baseline then silently suppresses every
later trade (#69), which looks like "the strategy stopped trading" rather
than "state bled across an unrelated run."

This module is the single place that declares and performs a boundary. A
harness that owns a replay calls `reset_strategy_state()` once at the start
of each independent run; each stateful strategy registers what "forget the
observation history" means for it. Nothing in the live runner calls this, so
single-run behavior is unchanged.

Two safeguards sit beside the explicit boundary rather than replacing it:

- A snapshot whose calendar date is *earlier* than the newest recorded one
  cannot belong to the run that recorded it, so the single-history
  strategies drop their history before recording it. That catches a replay
  harness that forgot to declare its boundary.
- `oi_skew_qqq` already resets its per-session tracker whenever the snapshot's
  calendar date changes; its state is additionally keyed per account and
  parameter band (see that module), so one account cannot consume another's
  observation.

Neither safeguard makes an identical same-day replay correct without a
declared boundary; callers that rerun the same day must call
`reset_strategy_state()`.
"""

from __future__ import annotations

import threading
from typing import Callable

_RESETTERS: dict[str, list[Callable[[], None]]] = {}
_lock = threading.Lock()


def register_state_resetter(strategy_id: str, resetter: Callable[[], None]) -> None:
    """Register what an independent replay must forget for `strategy_id`."""
    with _lock:
        _RESETTERS.setdefault(strategy_id, []).append(resetter)


def registered_strategy_ids() -> tuple[str, ...]:
    """Strategy ids that carry process-wide observation state."""
    with _lock:
        return tuple(sorted(_RESETTERS))


def reset_strategy_state(strategy_id: str | None = None) -> tuple[str, ...]:
    """Drop observation history for one strategy, or for all of them.

    Returns the ids actually reset, in sorted order, so a harness can record
    which boundaries it declared. Unknown ids are a no-op, not an error:
    a sweep over the whole registry can reset defensively without having to
    know which strategies are stateful.
    """
    with _lock:
        targets = (
            {strategy_id: _RESETTERS.get(strategy_id, [])}
            if strategy_id is not None
            else _RESETTERS
        )
        resetters = [(sid, list(fns)) for sid, fns in targets.items() if fns]
    for sid, fns in resetters:
        for resetter in fns:
            resetter()
    return tuple(sorted(sid for sid, _ in resetters))


__all__ = ["register_state_resetter", "registered_strategy_ids", "reset_strategy_state"]
