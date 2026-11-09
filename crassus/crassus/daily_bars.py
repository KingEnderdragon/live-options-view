"""Daily close history for the SMA(200) paper strategy.

Only completed daily bars are exposed. The provider is deliberately separate
from the intraday snapshot: the strategy must never turn a live quote or an
unfinished daily bar into a closing price. Yahoo's chart endpoint supplies
``adjclose`` (split/dividend-adjusted) and raw ``close``; the strategy uses the
adjusted series and documents that convention in its decision metadata.

No credential or new package is required: `requests` is already a Crassus
dependency. Network access is injectable so the strategy can be tested against
a fixed bar fixture without touching a provider.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
DEFAULT_RANGE = "20y"
DEFAULT_CACHE_HOURS = 6.0
_USER_AGENT = "Mozilla/5.0 (compatible; CrassusSma200/1.0)"


class DailyBarError(RuntimeError):
    """The provider returned no usable completed daily series."""


@dataclass(frozen=True)
class DailyBar:
    date: date
    close: float          # adjusted close used by the signal
    raw_close: float | None = None
    source: str = "yahoo_chart_adjclose"
    open: float | None = None          # raw session open, for replay execution
    adjusted_open: float | None = None  # open scaled by adjclose/raw close

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "close": self.close,
            "raw_close": self.raw_close,
            "source": self.source,
            "open": self.open,
            "adjusted_open": self.adjusted_open,
        }


def _date_et(timestamp: int | float) -> date:
    return datetime.fromtimestamp(float(timestamp), ET).date()


def parse_yahoo_chart(payload: dict[str, Any]) -> list[DailyBar]:
    """Parse a Yahoo chart payload into sorted, de-duplicated daily bars.

    A missing adjusted value falls back to raw close only as a last resort and
    is labelled with that source; it is never silently treated as adjusted.
    """
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        raw_closes = quote.get("close") or []
        raw_opens = quote.get("open") or []
        adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise DailyBarError("Yahoo chart payload has no usable result") from exc

    by_date: dict[date, DailyBar] = {}
    for index, timestamp in enumerate(timestamps):
        raw = raw_closes[index] if index < len(raw_closes) else None
        raw_open = raw_opens[index] if index < len(raw_opens) else None
        adjusted = adj[index] if index < len(adj) else None
        value = adjusted if adjusted is not None else raw
        if value is None:
            continue
        try:
            numeric = float(value)
            raw_numeric = float(raw) if raw is not None else None
            open_numeric = float(raw_open) if raw_open is not None else None
        except (TypeError, ValueError):
            continue
        if numeric <= 0:
            continue
        source = "yahoo_chart_adjclose" if adjusted is not None else "yahoo_chart_raw_close"
        adjustment = numeric / raw_numeric if raw_numeric and raw_numeric > 0 else None
        adjusted_open = (open_numeric * adjustment) if open_numeric is not None and adjustment else open_numeric
        day = _date_et(timestamp)
        # A later row for the same date wins; Yahoo occasionally repeats the
        # current session in a range response.
        by_date[day] = DailyBar(day, numeric, raw_numeric, source, open_numeric, adjusted_open)
    bars = sorted(by_date.values(), key=lambda bar: bar.date)
    if not bars:
        raise DailyBarError("Yahoo chart payload contains no positive daily closes")
    return bars


def fetch_yahoo_daily_bars(
    symbol: str,
    *,
    range_: str = DEFAULT_RANGE,
    timeout_s: float = 20.0,
    http_get=requests.get,
) -> list[DailyBar]:
    """Fetch adjusted daily bars. Caller must filter to completed sessions."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
    response = http_get(
        url,
        params={"interval": "1d", "range": range_, "includeAdjustedClose": "true"},
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout_s,
    )
    response.raise_for_status()
    return parse_yahoo_chart(response.json())


def completed_bars(bars: Iterable[DailyBar], now_et: datetime) -> list[DailyBar]:
    """Bars whose session date is strictly before today's ET date.

    This deliberately excludes the current session even after the bell: an
    EOD feed can still revise the day's bar, and the issue requires completed
    daily bars. The resulting signal is available for the next regular
    session, never retrospectively at the signal close.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be timezone-aware")
    today = now_et.astimezone(ET).date()
    return [bar for bar in bars if bar.date < today]


def cache_path(root: Path, symbol: str) -> Path:
    return Path(root) / "sma200" / f"{symbol.upper()}_daily.json"


def load_cached_bars(root: Path, symbol: str, *, max_age_hours: float = DEFAULT_CACHE_HOURS,
                     now: datetime | None = None) -> list[DailyBar]:
    path = cache_path(root, symbol)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if now is not None and abs((now - fetched_at).total_seconds()) > max_age_hours * 3600:
            return []
        return [DailyBar(date.fromisoformat(item["date"]), float(item["close"]),
                         None if item.get("raw_close") is None else float(item["raw_close"]),
                         item.get("source", "yahoo_chart_adjclose"),
                         None if item.get("open") is None else float(item["open"]),
                         None if item.get("adjusted_open") is None else float(item["adjusted_open"]))
                for item in payload["bars"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_cached_bars(root: Path, symbol: str, bars: list[DailyBar], now: datetime) -> None:
    path = cache_path(root, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "symbol": symbol.upper(),
        "fetched_at": now.isoformat(),
        "bar_source": "yahoo_chart",
        "adjustment_basis": "adjusted_close",
        "bars": [bar.as_dict() for bar in bars],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, path)


def get_daily_bars(
    symbol: str,
    *,
    state_root: Path | None = None,
    now: datetime,
    fetcher=fetch_yahoo_daily_bars,
    range_: str = DEFAULT_RANGE,
    cache_hours: float = DEFAULT_CACHE_HOURS,
) -> tuple[list[DailyBar], str]:
    """Return bars plus a cache status (``fresh``/``cached``/``fetched``).

    A failed refresh falls back to a cached series only while that series is
    still within its freshness window; otherwise the strategy sees an explicit
    error and waits rather than trading on an old or invented bar.
    """
    root = Path(state_root or os.environ.get("CRASSUS_DATA_ROOT", "."))
    cached = load_cached_bars(root, symbol, max_age_hours=cache_hours, now=now)
    if cached:
        return cached, "cached"
    try:
        bars = fetcher(symbol, range_=range_)
        save_cached_bars(root, symbol, bars, now)
        return bars, "fetched"
    except Exception as exc:
        raise DailyBarError(f"daily bars unavailable for {symbol}: {exc}") from exc


def latest_completed_date(bars: Iterable[DailyBar], now_et: datetime) -> date | None:
    completed = completed_bars(bars, now_et)
    return completed[-1].date if completed else None


def trading_days_between(start: date, end: date) -> int:
    """Calendar days; used only for financing accrual, never for bar selection."""
    return max(0, (end - start).days)


def next_session_hint(now_et: datetime) -> datetime:
    """A small, explicit next-regular-session hint for operator messages."""
    day = now_et.astimezone(ET)
    candidate = day.replace(hour=9, minute=30, second=0, microsecond=0)
    if day >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate
