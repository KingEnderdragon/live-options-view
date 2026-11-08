"""Bounded per-lane capture statistics.

The collector may run for a whole session, so nothing here grows with event
volume: per-symbol last-seen sequence numbers only, a fixed-size quote-age
reservoir, and running min/max/count/sum.
"""

from __future__ import annotations

import json
import time
from collections import Counter, deque
from datetime import datetime
from typing import Any

from ._probe import parse_epoch_ms
from .config import EXPECTED_TIMESALE_FIELDS, QUOTE_AGE_RESERVOIR_SIZE


class BoundedStats:
    """Bounded-memory event statistics for a single collector process.

    Dedup keys on (symbol -> last-seen seq) rather than a growing set of
    every event, and quote-age percentiles come from a fixed-size reservoir
    plus running lifetime aggregates, so memory is O(distinct symbols) for
    the whole session rather than O(events). These are *this process's*
    counts only -- ``reconcile_prior_records`` in the manifest covers
    events captured by an earlier owner before a restart.
    """

    def __init__(self, reservoir_size: int = QUOTE_AGE_RESERVOIR_SIZE) -> None:
        self.counts: Counter[str] = Counter()
        self.timesale_by_symbol: Counter[str] = Counter()
        self.field_key_present: Counter[str] = Counter()
        self.field_non_null: Counter[str] = Counter()
        self.field_non_empty: Counter[str] = Counter()
        self.field_total: Counter[str] = Counter()
        self.flags: Counter[str] = Counter()
        self.sessions: Counter[str] = Counter()
        self.cancel_count = 0
        self.correction_count = 0
        self.duplicate_count = 0
        self.malformed = 0
        self.last_sequence: dict[str, int] = {}
        self.sequence_discontinuities: Counter[str] = Counter()
        self.sequence_out_of_order: Counter[str] = Counter()
        self.quote_timestamps: dict[str, int] = {}
        self.preopen_quote_context: dict[str, dict[str, Any]] = {}
        self.quote_age_reservoir: deque[int] = deque(maxlen=reservoir_size)
        self.quote_age_count = 0
        self.quote_age_sum = 0
        self.quote_age_min: int | None = None
        self.quote_age_max: int | None = None
        self.last_receipt_ts: str | None = None
        self.first_receipt_ts: str | None = None
        self.excluded_timesales: Counter[str] = Counter()

    def observe_malformed(self) -> None:
        self.malformed += 1

    def observe_excluded(self, _event: dict[str, Any], reason: str) -> None:
        self.counts["excluded_timesale"] += 1
        self.excluded_timesales[reason] += 1

    def retain_quote(self, event: dict[str, Any], *, preopen: bool = False) -> None:
        """Retain quote context without adding a regular-session observation."""
        symbol = str(event.get("symbol", "unknown"))
        candidates = [value for value in (
            parse_epoch_ms(event.get("biddate")), parse_epoch_ms(event.get("askdate"))
        ) if value is not None]
        if not candidates:
            return
        timestamp = max(candidates)
        if timestamp < self.quote_timestamps.get(symbol, 0):
            return
        self.quote_timestamps[symbol] = timestamp
        if preopen:
            self.preopen_quote_context[symbol] = dict(event)
        else:
            self.preopen_quote_context.pop(symbol, None)

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "unknown"))
        self.counts[event_type] += 1
        receipt = event.get("collector_receipt_timestamp")
        if receipt:
            self.first_receipt_ts = self.first_receipt_ts or receipt
            self.last_receipt_ts = receipt
        symbol = str(event.get("symbol", "unknown"))
        if event_type == "quote":
            self.retain_quote(event)
            return event
        if event_type != "timesale":
            return event

        provider_ms = parse_epoch_ms(event.get("date"))
        if receipt and provider_ms is not None:
            try:
                lag = datetime.fromisoformat(receipt.replace("Z", "+00:00")).timestamp() * 1000 - provider_ms
                self.delivery_lag_latest_ms = round(lag, 3)
                self.delivery_lag_max_ms = max(getattr(self, "delivery_lag_max_ms", lag), lag)
                self.delivery_lag_over_5s = getattr(self, "delivery_lag_over_5s", 0) + int(lag > 5000)
                if lag > 5000 and time.monotonic() - getattr(self, "delivery_lag_warning_at", -60) >= 60:
                    self.delivery_lag_warning_at = time.monotonic()
                    print(json.dumps({"event": "stream_delivery_lag", "symbol": symbol,
                                      "receipt_timestamp": receipt, "provider_timestamp_ms": provider_ms,
                                      "lag_ms": round(lag, 3)}), flush=True)
            except (ValueError, TypeError, OverflowError):
                pass

        self.timesale_by_symbol[symbol] += 1
        for field in EXPECTED_TIMESALE_FIELDS:
            self.field_total[field] += 1
            if field in event:
                self.field_key_present[field] += 1
            if event.get(field) is not None:
                self.field_non_null[field] += 1
            if event.get(field) not in (None, ""):
                self.field_non_empty[field] += 1
        self.flags[str(event.get("flag") or "<empty>")] += 1
        self.sessions[str(event.get("session") or "<missing>")] += 1
        self.cancel_count += int(bool(event.get("cancel")))
        self.correction_count += int(bool(event.get("correction")))

        try:
            sequence = int(event["seq"])
        except (KeyError, TypeError, ValueError):
            sequence = None

        if sequence is not None:
            previous = self.last_sequence.get(symbol)
            duplicate = previous is not None and sequence == previous
            event["duplicate_in_run"] = duplicate
            self.duplicate_count += int(duplicate)
            if previous is not None:
                if sequence < previous:
                    self.sequence_out_of_order[symbol] += 1
                elif sequence > previous + 1:
                    self.sequence_discontinuities[symbol] += 1
            self.last_sequence[symbol] = max(sequence, previous if previous is not None else sequence)
        else:
            event["duplicate_in_run"] = False

        trade_ms = parse_epoch_ms(event.get("date"))
        quote_ms = self.quote_timestamps.get(symbol)
        if trade_ms is not None and quote_ms is not None and quote_ms <= trade_ms:
            age = trade_ms - quote_ms
            self.quote_age_reservoir.append(age)
            self.quote_age_count += 1
            self.quote_age_sum += age
            self.quote_age_min = age if self.quote_age_min is None else min(self.quote_age_min, age)
            self.quote_age_max = age if self.quote_age_max is None else max(self.quote_age_max, age)
            event["preceding_quote_age_ms"] = age
            if symbol in self.preopen_quote_context:
                event["preceding_quote_source"] = "preopen"
                event["preceding_quote_context"] = self.preopen_quote_context[symbol]
        return event

    def age_summary(self) -> dict[str, Any]:
        sampled = sorted(self.quote_age_reservoir)
        summary: dict[str, Any] = {
            "count": self.quote_age_count,
            "reservoir_size": len(sampled),
        }
        if sampled:
            summary.update({
                "min": self.quote_age_min,
                "max": self.quote_age_max,
                "mean": round(self.quote_age_sum / self.quote_age_count, 3),
                "sampled_median": sampled[len(sampled) // 2],
                "sampled_p95": sampled[min(len(sampled) - 1, int(0.95 * (len(sampled) - 1)))],
            })
        return summary

    def summary(self) -> dict[str, Any]:
        return {
            "trade_delivery_lag_ms": {
                "latest": getattr(self, "delivery_lag_latest_ms", None),
                "max": getattr(self, "delivery_lag_max_ms", None),
                "over_5000ms_count": getattr(self, "delivery_lag_over_5s", 0),
            },
            "event_counts": dict(self.counts),
            "timesale_counts_by_symbol": dict(self.timesale_by_symbol),
            "timesale_field_population": {
                field: {
                    "total": self.field_total[field],
                    "key_present": self.field_key_present[field],
                    "non_null": self.field_non_null[field],
                    "non_empty": self.field_non_empty[field],
                }
                for field in EXPECTED_TIMESALE_FIELDS
            },
            "flag_frequencies": dict(self.flags),
            "session_frequencies": dict(self.sessions),
            "cancel_count": self.cancel_count,
            "correction_count": self.correction_count,
            "duplicate_count": self.duplicate_count,
            "dedup_horizon": "per-symbol last-seen sequence only; resets on restart",
            "sequence_discontinuities_by_symbol": dict(self.sequence_discontinuities),
            "sequence_out_of_order_by_symbol": dict(self.sequence_out_of_order),
            "preceding_quote_age_ms": self.age_summary(),
            "malformed_payloads": self.malformed,
        }
