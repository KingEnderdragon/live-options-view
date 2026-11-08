import sys
import unittest
import gzip
import json
from collections import defaultdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import oa203_windows_replay as replay  # noqa: E402
from test_oa203_windows import SESSIONS, ms, sess  # noqa: E402


class ReplayIntegrity(unittest.TestCase):
    def replay_day(self, extra_status="ok", extra_rows=1, include_extra_row=True,
                   extra_role="additional_expiry", nearest_status="ok", top_rows=2,
                   nearest_t=1, extra_t=1, sweep_start=0, sweep_end=2,
                   session_close=2, fetched_ms=None, nearest_fetched_ms=None):
        day = "2026-10-16"
        base = ms(day, 9, 30)
        at = lambda offset: base + offset if offset is not None else None
        prefix = f"oa203/scanner/{day}/"
        nearest = {"underlying": "SPY", "symbol": "SPY261016C00100000",
                   "expiration": day, "sweep": 1, "t": at(nearest_t)}
        extra = {**nearest, "symbol": "SPY261023C00100000", "expiration": "2026-10-23",
                 "collection_role": extra_role, "t": at(extra_t)}
        manifest = [{"sweep": 1, "file": "sweep_0001.jsonl.gz", "truncated": False, "rows": top_rows,
                     "started_ms": at(sweep_start), "ended_ms": at(sweep_end),
                     "chains": {"SPY": {"status": nearest_status, "rows": 1,
                                        "fetched_ms": at(nearest_t if nearest_fetched_ms is None else nearest_fetched_ms)}},
                     "additional_chains": {"SPY": {"2026-10-23": {
                         "status": extra_status, "expiration": "2026-10-23", "rows": extra_rows,
                         "fetched_ms": at(extra_t)}}}}]
        if fetched_ms is not None:
            manifest[0]["additional_chains"]["SPY"]["2026-10-23"]["fetched_ms"] = at(fetched_ms)
        rows = [nearest] + ([extra] if include_extra_row else [])
        artifacts = {
            prefix + "summary.json": json.dumps({"trade_date": day, "final": True,
                                                   "session_open_ms": base, "session_close_ms": at(session_close),
                                                   "assessment": {"status": "complete"}}).encode(),
            prefix + "universe.json": json.dumps({"trade_date": day, "selected": [{
                "underlying": "SPY", "expiration": day,
                "additional_expiration": "2026-10-23"}]}).encode(),
            prefix + "sweeps/manifest.jsonl": (json.dumps(manifest[0]) + "\n").encode(),
            prefix + "sweeps/sweep_0001.jsonl.gz": gzip.compress(
                "".join(json.dumps(r) + "\n" for r in rows).encode()),
        }
        symbols, sampled = defaultdict(list), defaultdict(set)
        with mock.patch.object(replay, "fetch", side_effect=lambda key: artifacts[key]):
            replay.load_session(day, {"SPY"}, symbols, sampled)
        return symbols, sampled

    def test_extra_rows_require_successful_manifest_and_exact_count(self):
        symbols, sampled = self.replay_day()
        self.assertEqual(len(symbols), 2)
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-23")])
        for settings in ({"extra_status": "error"}, {"extra_rows": 2},
                         {"include_extra_row": False}, {"extra_role": "nearest"},
                         {"nearest_status": "error"}, {"top_rows": 3},
                         {"nearest_t": 5000, "nearest_fetched_ms": 1000, "sweep_end": 1000},
                         {"extra_t": None}, {"sweep_start": 3, "sweep_end": 2},
                         {"fetched_ms": 3}, {"fetched_ms": 0}):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                self.replay_day(**settings)

    def test_sweep_bounds_inclusive_session_close_exclusive(self):
        _, sampled = self.replay_day(nearest_t=0, extra_t=2, session_close=3)
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-16")])
        self.assertIn("2026-10-16", sampled[("SPY", "2026-10-23")])
        symbols, sampled = self.replay_day(nearest_t=0, extra_t=2, session_close=2)
        self.assertEqual(len(symbols), 1)
        self.assertNotIn("2026-10-16", sampled[("SPY", "2026-10-23")])

    def test_row_after_matched_sweep_end_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "row disagrees with sweep manifest"):
            self.replay_day(nearest_t=5000, nearest_fetched_ms=1000, sweep_end=1000)
        with self.assertRaisesRegex(RuntimeError, "row lacks a successful matching chain fetch"):
            self.replay_day(nearest_t=0, nearest_fetched_ms=1)

    def test_occ_identity_rejects_stitched_metadata(self):
        row = {"expiration": "2026-10-09", "type": "call", "strike": 100.0, "root": "ABC"}
        replay.validate_contract_identity("ABC261009C00100000", row)
        with self.assertRaises(RuntimeError):
            replay.validate_contract_identity("ABC261016C00100000", row)
        with self.assertRaises(RuntimeError):
            replay.validate_contract_identity("ABC261009P00100000", row)

    def test_manifest_cutoff_requires_complete_contiguous_sweeps(self):
        good = [{"sweep": 1, "file": "sweep_0001.jsonl.gz", "chains": {}, "truncated": False,
                 "started_ms": 100, "ended_ms": 1000}]
        summary = {"assessment": {"status": "complete"}}
        replay.validate_manifest(good, summary, "2026-10-05")
        for bad in ([dict(good[0], sweep=2)], [dict(good[0], truncated=True)],
                    [dict(good[0], started_ms=None)], [dict(good[0], ended_ms=99)],
                    good + [dict(good[0], sweep=2, file="sweep_0002.jsonl.gz", started_ms=999)]):
            with self.assertRaises(RuntimeError):
                replay.validate_manifest(bad, summary, "2026-10-05")
        with self.assertRaises(RuntimeError):
            replay.validate_manifest(good, {"assessment": {"status": "partial"}}, "2026-10-05")

    def test_calendar_rejects_missing_session_and_wrong_bounds(self):
        self.assertIn((2026, 41), replay.validate_calendar(SESSIONS))
        with self.assertRaises(RuntimeError):
            replay.validate_calendar(SESSIONS[:2] + SESSIONS[3:])
        with self.assertRaises(RuntimeError):
            replay.validate_calendar([sess("2026-10-05", close=(13, 0))] + SESSIONS[1:])
        self.assertNotIn((2026, 41), replay.validate_calendar(SESSIONS[:3]))


if __name__ == "__main__":
    unittest.main()
