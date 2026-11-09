"""The Tradier market-data client used by the scanner.

One place for the provider's retry policy, rate limits and per-sweep budget:
a request that would exceed the sweep's remaining time raises before it is
sent, so one slow contract cannot starve the rest of the sweep.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter, deque
from datetime import date
from typing import Any, Callable

import requests

from . import runtime
from .runtime import API


# --------------------------------------------------------------------------
# Rate-limited Tradier client
# --------------------------------------------------------------------------

class RateLimited(RuntimeError):
    pass


class SamplingDeadline(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    """The sweep budget ended before this request could be dispatched."""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Tradier:
    """Market-data client paced against a local cap and Tradier's headers."""

    def __init__(self, token: str, max_rpm: int = 100, reserve: int = 10,
                 session: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout: tuple[float, float] = (5.0, 20.0)) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        # Set per sweep: once passed, no request is dispatched (first attempt
        # or retry), and rate-limit/backoff waits never sleep past it.
        self.budget_deadline: float | None = None
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.max_rpm, self.reserve = max_rpm, reserve
        if not 0 <= reserve < 120 or not 1 <= max_rpm <= 120 - reserve:
            raise ValueError("max_rpm must be between 1 and 120 minus reserve")
        self.token = token
        self.effective_rpm = max_rpm
        self.next_request_at = 0.0
        self.budget_expiry = 0.0
        self.available = None
        self.inflight = 0
        self.deadline = None
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.sent: deque[float] = deque()
        self.blocked_until = 0.0
        self.stats: Counter[str] = Counter()

    def _acquire(self) -> None:
        while True:
            with self.lock:
                now = self.clock()
                if self.deadline is not None and now >= self.deadline:
                    raise SamplingDeadline("session closed before request dispatch")
                if self.budget_deadline is not None and now >= self.budget_deadline:
                    self.stats["dispatch_skipped_budget"] += 1
                    raise BudgetExhausted("sweep budget spent before request dispatch")
                while self.sent and now - self.sent[0] >= 60:
                    self.sent.popleft()
                wait = max(self.blocked_until - now, self.next_request_at - now,
                           (self.sent[0] + 60 - now) if len(self.sent) >= self.max_rpm else 0)
                if self.available is not None and now < self.budget_expiry and self.available <= self.reserve:
                    wait = max(wait, self.budget_expiry + 0.25 - now)
                if wait <= 0:
                    self.sent.append(now)
                    self.next_request_at = now + 60 / self.effective_rpm
                    if self.available is not None:
                        self.available -= 1
                    self.inflight += 1
                    return
                self.stats["rate_waits"] += 1
                sleep_seconds = min([wait, 61] + [max(0, limit - now) for limit in (self.deadline, self.budget_deadline)
                                                  if limit is not None])
                self.stats["rate_wait_seconds"] += sleep_seconds
            # Header observers and other workers must be able to take the lock.
            self.sleep(sleep_seconds)

    def _observe(self, headers: Any) -> None:
        with self.lock:
            self.inflight -= 1
        try:
            available = int(headers.get("X-Ratelimit-Available"))
            expiry = int(headers.get("X-Ratelimit-Expiry")) / 1000.0
        except (TypeError, ValueError):
            return
        with self.lock:
            try:
                allowed = int(headers.get("X-Ratelimit-Allowed"))
                self.effective_rpm = min(self.max_rpm, max(1, allowed - self.reserve))
                if self.sent:
                    self.next_request_at = max(self.next_request_at, self.sent[-1] + 60 / self.effective_rpm)
            except (TypeError, ValueError):
                pass
            # Concurrent responses can arrive out of order. Never replenish the
            # same window from an older, higher remaining-budget observation.
            if expiry > self.budget_expiry:
                self.budget_expiry, self.available = expiry, available - self.inflight
            elif expiry == self.budget_expiry:
                self.available = min(self.available if self.available is not None else available,
                                     available - self.inflight)
            if expiry >= self.budget_expiry and self.available <= self.reserve:
                self.blocked_until = max(self.blocked_until, expiry + 0.25)

    def _backoff(self, seconds: float) -> None:
        # A retry backoff never sleeps past the sweep budget; the next
        # dispatch then stops at the budget check in _acquire.
        if self.budget_deadline is not None:
            seconds = min(seconds, max(0, self.budget_deadline - self.clock()))
        if seconds > 0:
            self.sleep(seconds)

    def _may_retry(self, attempt: int, attempts: int) -> bool:
        if attempt + 1 >= attempts:
            return False
        if self.budget_deadline is not None and self.clock() >= self.budget_deadline:
            self.stats["retries_skipped_budget"] += 1
            return False
        return True

    def request(self, method: str, path: str, *, params: dict | None = None,
                data: dict | None = None, attempts: int = 3) -> Any:
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self._acquire()
            except BudgetExhausted:
                if last_exc is None:
                    raise  # nothing was requested: the caller records skipped_budget
                self.stats["retries_skipped_budget"] += 1
                break
            self.stats["requests"] += 1
            started = self.clock()
            try:
                response = self.session.request(method, f"{API}{path}", params=params,
                                                data=data, timeout=self.timeout)
            except requests.RequestException as exc:
                from market_data.diagnostics import exception_details
                with self.lock:
                    self.inflight -= 1
                    elapsed = max(0, self.clock() - started)
                    self.stats["request_seconds"] += elapsed
                    self.stats["max_request_seconds"] = max(self.stats["max_request_seconds"], elapsed)
                self.stats["transport_errors"] += 1
                last_exc = exc
                runtime.log("oa203_request_retry", path=path, attempt=attempt + 1,
                    delay_seconds=2 ** attempt, elapsed_seconds=elapsed,
                    error=exception_details(exc, (self.token,)))
                if not self._may_retry(attempt, attempts):
                    break
                self._backoff(2 ** attempt)
                continue
            elapsed = max(0, self.clock() - started)
            with self.lock:
                self.stats["request_seconds"] += elapsed
                self.stats["max_request_seconds"] = max(self.stats["max_request_seconds"], elapsed)
            self._observe(response.headers)
            if response.status_code == 429:
                self.stats["rate_limited"] += 1
                from email.utils import parsedate_to_datetime
                retry = response.headers.get("Retry-After")
                try:
                    retry_until = self.clock() + max(0, float(retry))
                except (TypeError, ValueError):
                    try:
                        retry_until = parsedate_to_datetime(retry).timestamp()
                    except (TypeError, ValueError, AttributeError):
                        retry_until = self.clock() + 60
                with self.lock:
                    self.blocked_until = max(self.blocked_until, retry_until, self.budget_expiry + 0.25)
                runtime.log("oa203_request_retry", path=path, status=429, attempt=attempt + 1,
                    delay_seconds=max(0, self.blocked_until - self.clock()),
                    available=self.available, budget_expiry=self.budget_expiry, elapsed_seconds=elapsed)
                last_exc = RateLimited(f"429 on {path}")
                if not self._may_retry(attempt, attempts):
                    break
                continue
            if response.status_code >= 500:
                self.stats["server_errors"] += 1
                last_exc = RuntimeError(f"HTTP {response.status_code} on {path}")
                runtime.log("oa203_request_retry", path=path, status=response.status_code,
                    attempt=attempt + 1, delay_seconds=2 ** attempt, elapsed_seconds=elapsed)
                if not self._may_retry(attempt, attempts):
                    break
                self._backoff(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise last_exc or RuntimeError(f"request failed: {path}")

    def expirations(self, symbol: str) -> list[str]:
        data = self.request("GET", "/markets/options/expirations",
                            params={"symbol": symbol, "includeAllRoots": "true"})
        return [str(d) for d in _as_list((data.get("expirations") or {}).get("date"))]

    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        data = self.request("GET", "/markets/options/chains",
                            params={"symbol": symbol, "expiration": expiration, "greeks": "false"})
        return [c for c in _as_list((data.get("options") or {}).get("option")) if isinstance(c, dict)]

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        data = self.request("POST", "/markets/quotes",
                            data={"symbols": ",".join(symbols), "greeks": "false"})
        quotes = _as_list((data.get("quotes") or {}).get("quote"))
        return {q["symbol"]: q for q in quotes if isinstance(q, dict) and q.get("symbol")}

    def timesales(self, symbol: str, day: date) -> list[dict[str, Any]]:
        data = self.request("GET", "/markets/timesales", params={
            "symbol": symbol, "interval": "1min", "session_filter": "open",
            "start": f"{day.isoformat()} 09:30", "end": f"{day.isoformat()} 16:00"})
        return [b for b in _as_list((data.get("series") or {}).get("data")) if isinstance(b, dict)]


def load_token() -> str:
    token = os.getenv("TRADIER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TRADIER_TOKEN is not set")
    return token
