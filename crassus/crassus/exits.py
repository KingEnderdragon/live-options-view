"""Configurable profit-taking and protective exits for held option positions.

Every option strategy in this repo closes *reactively*: it waits for its own
signal to flip (see `reddit_sentiment`, `trump_whisperer`, `momentum_qqq`,
`oi_skew`, `max_pain`, `put_call_ratio`). Nothing closes a position because it
has made enough money, because it has lost too much, or because a contract
that is decaying toward expiration should be salvaged. The mandatory end-of-day
flatten (`flatten.py`) only bounds *time*, and Guideline Phelps (`phelps.py`)
deliberately withholds a signal-driven exit for one window. Between those two
controls sits the entire range of ordinary position management, and issue #86
asks for it explicitly.

This module adds that layer as a pre-strategy decision -- the same shape
`maybe_flatten` already uses -- so it can see the held book, the account's
documented average cost, and a fresh liquidation-side quote without any
strategy having to change:

* **Long** positions are valued at the quote's **bid** (what a sale would
  actually receive); **short** positions at the **ask** (what buying back would
  actually cost). Using the mid or the entry-side price would systematically
  overstate an exit's value. The quote must pass `Quote.is_executable`, the
  same 15-second, both-sides, server-clock freshness rule the Worker enforces
  on submission -- an exit is only proposed against a price that could actually
  be hit this cycle.
* **Cost basis** is `Book`'s average cost per contract, reconstructed from the
  server's authoritative trade history (see `client.Book`). Percentage P&L is
  therefore premium P&L; the Worker settles paper options in premium terms, so
  no contract multiplier enters the threshold arithmetic.
* **Triggers** are inclusive at the boundary: `pnl_pct >= profit_target_pct` and
  `pnl_pct <= stop_loss_pct`. A position sitting exactly on its threshold has
  reached it; the alternative makes the boundary depend on float noise.

Precedence, highest first, is fixed here and documented for review:

1. **Session** -- the mandatory end-of-day flatten runs before this layer and
   always wins. Exits never preempt it, never duplicate it, and never open a
   position in the flatten window.
2. **Protective stop** -- the one exit whose purpose is bounding loss. By
   default it fires even inside a Guideline Phelps window (`stop_bypasses_phelps`
   defaults to true), because Phelps deliberately tolerates discomfort and has
   no loss bound of its own. An account that wants strict doctrine can set
   `stop_bypasses_phelps: false`, which defers the stop to the end of the
   window -- a choice with a real tail cost, so it is opt-in and visible.
3. **Profit target** -- by default deferred inside a Phelps window
   (`profit_bypasses_phelps` defaults to false): taking a profit is not the
   capitulation Phelps protects against, but releasing the position early also
   ends the convexity purchase the method paid for. Deferral is the
   doctrine-consistent default and can be switched off per account.
4. **Signal reversal** -- the base strategy's own sell, with Phelps's existing
   deferral unchanged. This layer never suppresses it.
5. **Re-entry** -- after an exit is submitted, the same contract may not be
   re-opened for `reentry_cooldown_minutes` (default 15). Without this, a
   strategy whose signal is still bullish buys straight back into the contract
   it just stopped out of, usually at a worse price, and the stop never
   actually bounds the day.

Persistence and duplicate protection are handled by machinery that already
exists and is already tested: the runner writes the decision envelope to a
durable intent before submitting, reconciles any pending intent on restart, and
records one ledger row per decision. This module adds only the re-entry guard's
own state (`ExitStateStore`, a small JSON file under the state directory); a
position that is already flat produces no exit, so settlement and a restart
after a fill cannot re-sell a position that no longer exists.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .strategy import Decision, StrategyContext

STRATEGY_ID = "exit_policy"
STRATEGY_VERSION = "1.1.0"

#: Strategies whose exits this layer owns by default: the signal-reversal
#: option books. `smoke_*` is excluded deliberately -- it is a pipeline
#: instrument whose round trip is the point, and an exit layer that closed it
#: on a 50% move would corrupt the fixtures that prove the lifecycle.
#: `canopus_down_day_14` and `looking_glass_straddle` are excluded because they
#: already carry their own terminal/profit exits, and duplicating them would
#: make one position subject to two independent policy owners.
DEFAULT_COVERED = frozenset({
    "reddit_sentiment_qqq",
    "reddit_sentiment_qqq_phelps",
    "reddit_sentiment_qqq_fixed_window",
    "trump_whisperer_qqq",
    "trump_whisperer_qqq_phelps",
    "trump_whisperer_qqq_fixed_window",
    "momentum_qqq",
    "momentum_qqq_phelps",
    "momentum_qqq_fixed_window",
    "momentum_puts_only_qqq",
    "momentum_puts_only_qqq_phelps",
    "max_pain_qqq",
    "oi_skew_qqq",
    "put_call_ratio_qqq",
    "phelps_pure_qqq",
})

PHELPS_SUFFIX = "_phelps"

#: Activation switch. The layer is implemented, tested and *inert* until an
#: operator sets this after the thresholds and precedence have been reviewed;
#: that is the difference between "the code exists" and "the bots trade a new
#: exit policy". Default off means a deploy of this branch changes no trading
#: behaviour, and rollback is the same single variable.
ACTIVATION_ENV = "CRASSUS_EXITS_ENABLED"

#: Proposed numeric defaults, every one an explicit review question rather than
#: a tuned result. Rationale, for the review packet:
#:
#: * `profit_target_pct = 0.50` -- a long 0DTE ATM contract that doubles its
#:   premium is a large move for an intraday hold; 50% is the conservative half
#:   of that, and well above the 20% the Cheshire straddle already uses as its
#:   core-profit threshold (`looking_glass_straddle.DEFAULT_CORE_PROFIT_THRESHOLD_PCT`),
#:   which keeps a signal-driven book from being nickeled out by its own stop.
#: * `stop_loss_pct = -0.50` -- the same magnitude on the loss side. Half the
#:   debit is a wide stop for a same-day contract but a real one: the current
#:   alternatives are an unbounded hold (today) or a stop so tight that Phelps's
#:   own window cannot resolve. Symmetry keeps the pair explainable; nothing in
#:   the repo establishes a better number.
#: * `reentry_cooldown_minutes = 15` -- one runner cycle is five minutes, so 15
#:   covers three cycles: long enough that the same stale signal cannot buy the
#:   same contract back immediately, short enough that a genuinely new setup
#:   later in the session is not blocked.
#: * `quote_max_age_s` is not configurable: it is the Worker's own 15-second
#:   execution freshness contract, and a looser local value would only produce
#:   409s.
DEFAULT_PROFIT_TARGET_PCT = 0.50
DEFAULT_STOP_LOSS_PCT = -0.50
DEFAULT_REENTRY_COOLDOWN_MINUTES = 15.0
DEFAULT_STOP_BYPASSES_PHELPS = True
DEFAULT_PROFIT_BYPASSES_PHELPS = False

STATE_SCHEMA = "exit_state.v1"
STATE_FILE_NAME = "exits.json"
#: An exit older than this cannot plausibly still be cooling down; prune rather
#: than let the state file grow without bound on a long-lived runtime.
STATE_RETENTION_HOURS = 24.0


class ExitConfigError(ValueError):
    """A malformed `exits` block. Surfaced per account, never process-wide."""


def _finite(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExitConfigError(f"exits.{field_name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ExitConfigError(f"exits.{field_name} must be finite, got {value!r}")
    return number


@dataclass(frozen=True)
class ExitPolicy:
    profit_target_pct: float = DEFAULT_PROFIT_TARGET_PCT
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    reentry_cooldown_minutes: float = DEFAULT_REENTRY_COOLDOWN_MINUTES
    stop_bypasses_phelps: bool = DEFAULT_STOP_BYPASSES_PHELPS
    profit_bypasses_phelps: bool = DEFAULT_PROFIT_BYPASSES_PHELPS
    enabled: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def activation_from_environment(environ: dict[str, str] | None = None) -> bool:
    """Whether the exit layer is switched on for this process.

    Anything other than an explicit affirmative keeps it inert; a typo must not
    be able to start trading a policy that has not been reviewed.
    """
    raw = (os.environ if environ is None else environ).get(ACTIVATION_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def resolve_policy(
    strategy_id: str,
    params: dict[str, Any] | None,
    *,
    covered: frozenset[str] = DEFAULT_COVERED,
    activated: bool = False,
) -> ExitPolicy:
    """The policy for one account, or a disabled policy.

    `params["exits"]` may set any field; `params["exits"]["enabled"] = false`
    turns the layer off for that account. An account whose strategy is not
    covered is off unless it opts in explicitly, so adding a strategy to the
    repo does not silently acquire an exit policy. In every case the layer also
    requires `activated` -- the deployment-level switch -- so shipping this
    code changes nothing until `CRASSUS_EXITS_ENABLED` is set.
    """
    params = params or {}
    block = params.get("exits")
    if block is None:
        return ExitPolicy(enabled=bool(activated and strategy_id in covered))
    if not isinstance(block, dict):
        raise ExitConfigError("exits must be an object")
    unknown = set(block) - set(ExitPolicy.__dataclass_fields__)
    if unknown:
        raise ExitConfigError(
            f"unknown exits field(s) {sorted(unknown)}; allowed: "
            f"{sorted(ExitPolicy.__dataclass_fields__)}"
        )
    configured = block.get("enabled", strategy_id in covered)
    if not isinstance(configured, bool):
        raise ExitConfigError(f"exits.enabled must be true or false, got {configured!r}")
    enabled = bool(configured and activated)
    policy = ExitPolicy(
        profit_target_pct=_finite(block.get("profit_target_pct", DEFAULT_PROFIT_TARGET_PCT),
                                  "profit_target_pct"),
        stop_loss_pct=_finite(block.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT),
                              "stop_loss_pct"),
        reentry_cooldown_minutes=_finite(
            block.get("reentry_cooldown_minutes", DEFAULT_REENTRY_COOLDOWN_MINUTES),
            "reentry_cooldown_minutes",
        ),
        stop_bypasses_phelps=bool(block.get("stop_bypasses_phelps", DEFAULT_STOP_BYPASSES_PHELPS)),
        profit_bypasses_phelps=bool(
            block.get("profit_bypasses_phelps", DEFAULT_PROFIT_BYPASSES_PHELPS)
        ),
        enabled=enabled,
    )
    if policy.profit_target_pct <= 0:
        raise ExitConfigError("exits.profit_target_pct must be > 0")
    if policy.stop_loss_pct >= 0:
        raise ExitConfigError("exits.stop_loss_pct must be < 0")
    if policy.reentry_cooldown_minutes < 0:
        raise ExitConfigError("exits.reentry_cooldown_minutes must be >= 0")
    return policy


def is_phelps(strategy_id: str) -> bool:
    return strategy_id.endswith(PHELPS_SUFFIX) or strategy_id == "phelps_pure_qqq"


def _elapsed_minutes(ctx: StrategyContext, symbol: str) -> float | None:
    """Minutes since the position's own fill, when the trade history says so.

    Imported lazily and defensively: `phelps` is a strategy module, and this
    layer must keep working (by not applying the window) if that helper ever
    changes shape.
    """
    try:
        from .phelps import position_entry_time
    except ImportError:  # pragma: no cover - defensive
        return None
    entry = position_entry_time(ctx, symbol)
    if entry is None:
        return None
    now = ctx.now_et
    if entry.tzinfo is None or now.tzinfo is None:
        return None
    return (now - entry).total_seconds() / 60.0


def _phelps_window_minutes(params: dict[str, Any]) -> float:
    from .phelps import resolve_phelps_minutes

    return resolve_phelps_minutes(params)


def liquidation_price(quote: Any, quantity: int) -> float | None:
    """The price this position would actually transact at, or None.

    Long exits hit the bid; short exits pay the ask. A quote missing the needed
    side is not usable for this position even if the other side is present.
    """
    if quote is None:
        return None
    price = quote.bid if quantity > 0 else quote.ask
    return float(price) if price is not None else None


def pnl_pct(cost_basis: float, price: float, quantity: int) -> float:
    """Premium P&L as a fraction of the documented average cost."""
    if cost_basis <= 0:
        raise ValueError("cost basis must be positive")
    if quantity > 0:
        return (price - cost_basis) / cost_basis
    return (cost_basis - price) / cost_basis


@dataclass
class Candidate:
    symbol: str
    quantity: int
    cost_basis: float
    price: float
    pnl_pct: float
    trigger: str  # "protective_stop" | "profit_target"
    bid: float | None
    ask: float | None
    quote_age_s: float | None

    @property
    def priority(self) -> tuple[int, float]:
        # Lower sorts first: stops always outrank profit targets; within a
        # trigger class the larger absolute move wins, so the most urgent
        # position exits first.
        return (0 if self.trigger == "protective_stop" else 1, -abs(self.pnl_pct))

    def metadata(self, policy: ExitPolicy) -> dict[str, Any]:
        return {
            "exit_trigger": self.trigger,
            "exit_symbol": self.symbol,
            "exit_held_quantity": self.quantity,
            "exit_cost_basis": round(self.cost_basis, 6),
            "exit_liquidation_price": self.price,
            "exit_bid": self.bid,
            "exit_ask": self.ask,
            "exit_quote_age_s": None if self.quote_age_s is None else round(self.quote_age_s, 3),
            "exit_pnl_pct": round(self.pnl_pct, 6),
            "exit_threshold_pct": (
                policy.stop_loss_pct if self.trigger == "protective_stop"
                else policy.profit_target_pct
            ),
            "exit_policy": policy.as_metadata(),
        }


def _classify(position: Any, quote: Any, policy: ExitPolicy) -> Candidate | None:
    price = liquidation_price(quote, position.quantity)
    if price is None or position.average_cost <= 0:
        return None
    change = pnl_pct(position.average_cost, price, position.quantity)
    if change <= policy.stop_loss_pct:
        trigger = "protective_stop"
    elif change >= policy.profit_target_pct:
        trigger = "profit_target"
    else:
        return None
    return Candidate(
        symbol=position.symbol,
        quantity=position.quantity,
        cost_basis=position.average_cost,
        price=price,
        pnl_pct=change,
        trigger=trigger,
        bid=getattr(quote, "bid", None),
        ask=getattr(quote, "ask", None),
        quote_age_s=getattr(quote, "age_seconds", None),
    )


def evaluate(
    ctx: StrategyContext,
    policy: ExitPolicy,
    *,
    strategy_id: str | None = None,
) -> Decision | None:
    """A close decision for the most urgent triggered position, else None.

    Returns a blocking `no_trade` when a trigger has fired but no held position
    with a trigger has an executable quote: the position is waiting to be
    closed, and letting the strategy open something else in the meantime is
    exactly the "unintended re-entry" this layer exists to prevent.
    """
    if not policy.enabled or ctx.book is None:
        return None
    positions = [
        position for position in ctx.book.positions.values()
        if position.quantity != 0
    ]
    if not positions:
        return None

    wanted = (strategy_id or "") and is_phelps(strategy_id or "")
    quotes: dict[str, Any] = {}
    try:
        quotes = ctx.quotes([p.symbol for p in positions]) or {}
    except Exception as exc:  # quote failures are a finding, not a crash
        return Decision.no_trade(
            reason=f"Exit check could not read quotes: {type(exc).__name__}; holding.",
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata={"exit_quotes_error": type(exc).__name__},
        )

    executable: list[Candidate] = []
    pending: list[str] = []
    for position in positions:
        quote = quotes.get(position.symbol)
        # No quote, or no price on the side this exit would actually transact
        # at: the position cannot be valued at all this cycle, which is a
        # different condition from "valued but too stale to submit".
        if liquidation_price(quote, position.quantity) is None:
            pending.append(position.symbol)
            continue
        candidate = _classify(position, quote, policy)
        if candidate is None:
            continue
        if not getattr(quote, "is_executable", False):
            pending.append(position.symbol)
            continue
        if wanted and _deferred_by_phelps(ctx, candidate, policy):
            pending.append(position.symbol)
            continue
        executable.append(candidate)

    if not executable:
        if pending:
            return Decision.no_trade(
                reason=(
                    f"Exit triggered but no executable liquidation quote for {pending}; "
                    f"waiting rather than holding or adding to the book."
                ),
                strategy_id=STRATEGY_ID,
                strategy_version=STRATEGY_VERSION,
                metadata={
                    "exit_pending_symbols": pending,
                    "exit_quote_max_age_s": 15.0,
                },
            )
        return None

    best = min(executable, key=lambda c: c.priority)
    action = "sell" if best.quantity > 0 else "buy"
    window_note = ""
    if wanted:
        window_note = (
            f" (Phelps window {'bypassed' if best.trigger == 'protective_stop' else 'respected'})"
        )
    return Decision(
        action=action,
        symbol=best.symbol,
        quantity=abs(best.quantity),
        reason=(
            f"Exit policy: {best.trigger} at {best.pnl_pct:+.1%} of {best.cost_basis:.2f} "
            f"cost basis, liquidating at {best.price:.2f}{window_note}."
        ),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata=best.metadata(policy),
    )


def _deferred_by_phelps(ctx: StrategyContext, candidate: Candidate, policy: ExitPolicy) -> bool:
    """Whether the Phelps window withholds this exit this cycle.

    If the fill time cannot be established the window is *not* applied: an
    unparseable trade record must not be able to hold a losing position open
    past its stop.
    """
    if candidate.trigger == "protective_stop" and policy.stop_bypasses_phelps:
        return False
    if candidate.trigger == "profit_target" and policy.profit_bypasses_phelps:
        return False
    elapsed = _elapsed_minutes(ctx, candidate.symbol)
    if elapsed is None:
        return False
    return elapsed < _phelps_window_minutes(ctx.params)


# --------------------------------------------------------------------------
# Re-entry guard
# --------------------------------------------------------------------------


class ExitStateStore:
    """Small durable record of recent exits, used to block immediate re-entry.

    Keyed by account username and contract symbol. Persisted because the guard
    must survive the restarts the runner already recovers positions across;
    without that, a crash immediately after a stop-out would re-open the
    contract on the next boot.
    """

    def __init__(self, state_dir: Path, *, now: Callable[[], datetime] | None = None):
        self.path = Path(state_dir) / STATE_FILE_NAME
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
            return
        accounts = raw.get("accounts")
        if isinstance(accounts, dict):
            self._data = {
                str(account): {
                    str(symbol): dict(record)
                    for symbol, record in (entries or {}).items()
                    if isinstance(record, dict)
                }
                for account, entries in accounts.items()
                if isinstance(entries, dict)
            }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema": STATE_SCHEMA, "accounts": self._data}
            self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except OSError:
            # A read-only state volume must not turn an exit into a crash.
            pass

    def record(self, username: str, symbol: str, *, trigger: str, quantity: int,
               decision_id: str | None = None) -> None:
        account = self._data.setdefault(str(username), {})
        account[symbol] = {
            "exited_at": self._now().isoformat(timespec="seconds"),
            "trigger": trigger,
            "quantity": quantity,
            "decision_id": decision_id,
        }
        self._prune()
        self._save()

    def exited_at(self, username: str, symbol: str) -> datetime | None:
        record = self._data.get(str(username), {}).get(symbol)
        if not record:
            return None
        try:
            return datetime.fromisoformat(str(record.get("exited_at")))
        except ValueError:
            return None

    def cooldown_remaining_s(self, username: str, symbol: str, minutes: float) -> float:
        if minutes <= 0:
            return 0.0
        exited = self.exited_at(username, symbol)
        if exited is None:
            return 0.0
        elapsed = (self._now() - exited).total_seconds()
        return max(0.0, minutes * 60.0 - elapsed)

    def _prune(self) -> None:
        cutoff = self._now() - timedelta(hours=STATE_RETENTION_HOURS)
        for account in list(self._data):
            for symbol in list(self._data[account]):
                try:
                    exited = datetime.fromisoformat(
                        str(self._data[account][symbol].get("exited_at"))
                    )
                except ValueError:
                    del self._data[account][symbol]
                    continue
                if exited < cutoff:
                    del self._data[account][symbol]
            if not self._data[account]:
                del self._data[account]


def reentry_blocked(
    decision: Decision,
    *,
    username: str,
    store: ExitStateStore,
    policy: ExitPolicy,
    book: Any,
) -> Decision | None:
    """Convert an entry into a no_trade while an exit cooldown is live.

    Only entries are blocked -- a decision that reduces an existing position is
    never suppressed by this guard. Returns None when the decision may proceed.
    """
    if not policy.enabled or policy.reentry_cooldown_minutes <= 0:
        return None
    if not decision.is_trade or not decision.symbol:
        return None
    position = book.position(decision.symbol) if book is not None else None
    held = position.quantity if position is not None else 0
    opens_or_adds = (
        (held == 0) or (held > 0 and decision.action == "buy") or
        (held < 0 and decision.action == "sell")
    )
    if not opens_or_adds:
        return None
    remaining = store.cooldown_remaining_s(username, decision.symbol,
                                           policy.reentry_cooldown_minutes)
    if remaining <= 0:
        return None
    return Decision.no_trade(
        reason=(
            f"Exit cooldown: {decision.symbol} was exited less than "
            f"{policy.reentry_cooldown_minutes:.0f}m ago "
            f"({remaining:.0f}s remaining); not re-entering on the same signal."
        ),
        strategy_id=decision.strategy_id,
        strategy_version=decision.strategy_version,
        metadata={
            "exit_cooldown_remaining_s": round(remaining, 1),
            "blocked_action": decision.action,
            "blocked_symbol": decision.symbol,
            "blocked_strategy_id": decision.strategy_id,
        },
    )
