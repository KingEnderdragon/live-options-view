"""Reader for the intraday cross-ticker board (10Y yield, USO crude proxy, VIX, ...).

Companion to `market.py`'s `SnapshotReader`: the same durable-R2-board
polling shape -- fetch, cache, and respect the collector's own ~60s
republish cadence rather than hammering it every runner cycle -- pointed at
a different artifact (`intraday/prices.json`, see `config.TICKERS_URL`)
instead of the option-chain snapshot (`intraday/latest.json`). Kept as its
own module rather than folded into `market.py` because it is a different
payload shape (`{"prices": {ticker: {"price": ...}}}`, a flat multi-ticker
board, not a single-underlying option chain with rows/greeks) consumed by a
different class of strategy -- `strategies/macro_cross_market.py`, not
anything that reads `MarketSnapshot`.

No secrets, no auth: this is the same free, public, unauthenticated R2
bucket every other strategy in this repo already reads off of.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import TICKERS_URL


@dataclass(frozen=True)
class TickersSnapshot:
    """One immutable read of the durable multi-ticker board."""

    url: str
    timestamp: str
    snapshot_time: str
    feed_stale: bool
    prices: dict[str, float | None]

    @classmethod
    def from_payload(cls, url: str, payload: dict[str, Any]) -> "TickersSnapshot":
        raw_prices = payload.get("prices") or {}
        prices: dict[str, float | None] = {}
        for ticker, entry in raw_prices.items():
            price = entry.get("price") if isinstance(entry, dict) else None
            prices[ticker] = price
        return cls(
            url=url,
            timestamp=payload.get("timestamp", ""),
            snapshot_time=payload.get("snapshot_time", ""),
            feed_stale=bool(payload.get("feed_stale", False)),
            prices=prices,
        )

    def price(self, ticker: str) -> float | None:
        """The ticker's price, or `None` if it's absent or explicitly null."""
        return self.prices.get(ticker)


class TickersReader:
    """Polls the tickers board, respecting its native ~60s cadence.

    Mirrors `market.SnapshotReader` exactly (same caching rationale): a read
    inside `min_interval_s` of the last fetch returns the cached snapshot
    rather than re-fetching bytes that haven't changed on the collector's
    side yet.
    """

    def __init__(self, url: str = TICKERS_URL, min_interval_s: float = 60.0, timeout_s: float = 15.0):
        self.url = url
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._cached: TickersSnapshot | None = None
        self._cached_at: float = 0.0

    def read(self, force: bool = False) -> TickersSnapshot:
        age = time.monotonic() - self._cached_at
        if self._cached and not force and age < self.min_interval_s:
            return self._cached

        resp = requests.get(self.url, timeout=self.timeout_s)
        resp.raise_for_status()
        snapshot = TickersSnapshot.from_payload(self.url, json.loads(resp.content))
        self._cached, self._cached_at = snapshot, time.monotonic()
        return snapshot
