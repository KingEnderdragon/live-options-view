"""Offline tests for local-output retention / disk headroom.

The scanner's persistent /data volume is 5000 MB with 4190 MB used
(~810 MB free, Chej's evidence, not verified here). The hook reads up
to ten finalized sessions and writes one per-cycle emit root per
finalized day, with no bounded emit cleanup. These tests prove the two
pieces that make ongoing publication reliable: the headroom pre-check
fails closed before a build when the emit volume is too full, and the
prune deletes ONLY verified per-cycle emit output -- never the
finalized day archives, never source history, never live/published
data. Nothing here touches a provider, R2, or a collector.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows_live as live  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from test_oa203_windows_live import DAYS, _write_day  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
NOW_PLUS_5 = datetime(2026, 10, 9, 21, 5, tzinfo=timezone.utc)
NOW_PLUS_10 = datetime(2026, 10, 9, 21, 10, tzinfo=timezone.utc)


class HeadroomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in DAYS:
            _write_day(self.spool, day)
        self.universe = {"selected": [{"underlying": u, "expiration": "2026-10-09"}
                                       for u in ("AAA", "BBB", "CCC")]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_refuses_when_emit_volume_too_full(self):
        # Fail closed BEFORE building: no emit dir is created at all.
        emit = self.root / "emit"
        with self.assertRaises(ValueError) as ctx:
            live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                               emit, full_universe=True, min_free_bytes=10 ** 18)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertFalse(emit.exists())

    def test_build_succeeds_with_zero_threshold(self):
        report = live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                                    self.root / "emit", full_universe=True, min_free_bytes=0)
        self.assertEqual(len(report["windows"]), 14)

    def test_run_after_finalize_headroom_fail_closed(self):
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(
                self.spool, "2026-10-09", self.universe,
                backend=pub.LocalFilesystemBackend(self.root / "backend"),
                emit_root=self.root / "emit", now=NOW, min_free_bytes=10 ** 18)
        self.assertIn("emit headroom", str(ctx.exception))
        self.assertFalse((self.root / "emit").exists())

    def test_empty_and_missing_spool_fail_closed(self):
        empty = self.root / "empty-spool"
        empty.mkdir()
        with self.assertRaises(ValueError) as ctx:
            live.run_after_finalize(empty, "2026-10-09", self.universe,
                                    backend=pub.LocalFilesystemBackend(self.root / "backend"),
                                    emit_root=self.root / "emit", now=NOW)
        self.assertIn("no finalized day archives", str(ctx.exception))
        with self.assertRaises(FileNotFoundError):
            live.run_after_finalize(self.root / "no-such-spool", "2026-10-09", self.universe,
                                    backend=pub.LocalFilesystemBackend(self.root / "backend"),
                                    emit_root=self.root / "emit", now=NOW)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "days"
        self.spool.mkdir(parents=True)
        for day in DAYS:
            _write_day(self.spool, day)
        self.universe = {"selected": [{"underlying": u, "expiration": "2026-10-09"}
                                       for u in ("AAA", "BBB", "CCC")]}
        self.backend_root = self.root / "backend"
        self.emit_root = self.root / "emit"
        # Three real cycles (Oct 7, 8, 9), each publishing to the same
        # backend at an advancing wall clock, so the Oct 7 cycle's windows
        # are superseded by the Oct 9 publications.
        self.backend = pub.LocalFilesystemBackend(self.backend_root)
        for day, now in (("2026-10-07", NOW), ("2026-10-08", NOW_PLUS_5),
                         ("2026-10-09", NOW_PLUS_10)):
            live.run_after_finalize(self.spool, day, self.universe,
                                    backend=self.backend, emit_root=self.emit_root, now=now)

    def tearDown(self):
        self.tmp.cleanup()

    def _cycle(self, day):
        return self.emit_root / day

    def test_prune_dry_run_lists_but_deletes_nothing(self):
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=2, max_emit_bytes=0, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["pruned"], ["2026-10-07"])
        self.assertEqual(report["kept"], ["2026-10-08", "2026-10-09"])
        self.assertTrue(self._cycle("2026-10-07").exists())
        for day in DAYS:
            self.assertTrue((self.spool / day).is_dir(), day)

    def test_prune_deletes_only_verified_old_cycle(self):
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=2, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], ["2026-10-07"])
        self.assertFalse(self._cycle("2026-10-07").exists())
        self.assertTrue(self._cycle("2026-10-08").exists())
        self.assertTrue(self._cycle("2026-10-09").exists())
        # Day archives untouched; published pointers untouched.
        for day in DAYS:
            self.assertTrue((self.spool / day / "summary.json").is_file(), day)
        pointer = json.loads((self.backend_root / "oa203" / "windows" / "oa203-windows-v1"
                              / "overnight" / "overnight-2026-10-08_2026-10-09"
                              / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(pointer["revision"].startswith("rev_"))

    def test_prune_respects_keep_cycles_floor(self):
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=3, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], [])
        self.assertEqual(report["kept"], ["2026-10-07", "2026-10-08", "2026-10-09"])
        self.assertTrue(report["bytes_after"] > report["max_emit_bytes"])
        for day in ("2026-10-07", "2026-10-08", "2026-10-09"):
            self.assertTrue(self._cycle(day).exists())

    def test_prune_skips_unpublished_cycle(self):
        # A cycle built but never published: its emit is the only copy.
        # Verified against a FRESH backend (no pointers at all), so every
        # window in it is unpublished.
        empty_backend = pub.LocalFilesystemBackend(self.root / "backend-empty")
        unpublished = self.root / "emit-unpub" / "2026-10-09"
        live.build_windows([self.spool / d for d in DAYS], {"AAA", "BBB", "CCC"},
                           self.root / "emit-unpub" / "2026-10-09",
                           full_universe=True, min_free_bytes=0)
        # build_windows writes the oa203 tree directly under the emit root,
        # so the cycle dir needs its cycle.json manifest for the prune to see it.
        (unpublished / "cycle.json").write_text(json.dumps({
            "cycle": "2026-10-09", "day": "2026-10-09", "days": DAYS,
            "underlyings": ["AAA", "BBB", "CCC"], "universe_scope": "full_universe",
            "generated_at": NOW.isoformat(),
            "windows": [["overnight", "overnight-2026-10-05_2026-10-06"]],
        }) + "\n", encoding="utf-8")
        report = live.prune_windows_emit(self.root / "emit-unpub", empty_backend,
                                         keep_cycles=0, max_emit_bytes=0, dry_run=False)
        self.assertEqual(report["pruned"], [])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("not published", report["skipped"][0]["reason"])
        self.assertTrue(unpublished.exists())

    def test_prune_never_touches_non_cycle_entries(self):
        stray_dir = self.emit_root / "notes"
        stray_dir.mkdir()
        (stray_dir / "cycle.json").write_text("{}\n", encoding="utf-8")
        (self.emit_root / "stray.txt").write_text("keep me\n", encoding="utf-8")
        report = live.prune_windows_emit(self.emit_root, self.backend,
                                         keep_cycles=0, max_emit_bytes=0, dry_run=False)
        self.assertTrue(stray_dir.exists())
        self.assertTrue((self.emit_root / "stray.txt").is_file())
        self.assertNotIn("notes", report["pruned"])
        self.assertNotIn("stray.txt", report["pruned"])


if __name__ == "__main__":
    unittest.main()
