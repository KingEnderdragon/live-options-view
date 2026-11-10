"""Offline tests for the live window trigger (Task 1).

The hook is disabled by default: flag off means finalize runs exactly
as before. Flag on (in tests, with a fake hook / local backend) means
build + publish runs for the finalized day. Nothing here touches a
provider, R2, or a collector.
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.session as session  # noqa: E402
import market_data.options_scanner.windows_live as live  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from market_data.options_scanner.archive import DayArchive  # noqa: E402
from market_data.options_scanner.runtime import Config  # noqa: E402
from test_oa203_windows_live import DAYS, _write_day  # noqa: E402

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)


class _FakeArchive:
    def __init__(self, spool, day):
        self.dir = spool / day
        self.day = date.fromisoformat(day)
        self.written = {}

    def write_json(self, name, data):
        self.written[name] = data
        return self.dir / name

    def push(self, name):
        return True


class TriggerGateTests(unittest.TestCase):
    def test_flag_off_is_noop_and_calls_nothing(self):
        calls = []
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(session.WINDOWS_LIVE_ENV, None)
            result = session.maybe_run_windows_after_finalize(
                _FakeArchive(Path("/tmp"), "2026-10-09"),
                {"selected": []}, None, hook=lambda *a: calls.append(a))
        self.assertEqual(result, {"enabled": False})
        self.assertEqual(calls, [])

    def test_flag_values_other_than_1_are_off(self):
        for value in ("0", "true", "yes", ""):
            with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: value}):
                self.assertFalse(session.windows_live_enabled(), value)
                self.assertFalse(live.windows_live_enabled(), value)

    def test_flag_on_calls_hook_and_records_result(self):
        calls = []
        archive = _FakeArchive(Path("/tmp"), "2026-10-09")

        def hook(spool_dir, day, universe):
            calls.append((str(spool_dir), day.isoformat(), universe))
            return {"enabled": True, "day": day.isoformat(), "ok": True}

        with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: "1"}):
            result = session.maybe_run_windows_after_finalize(
                archive, {"selected": [{"underlying": "AAA"}]}, None, hook=hook)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["ok"])
        self.assertEqual(archive.written["windows_live.json"], result)

    def test_hook_failure_never_unfinalizes(self):
        def boom(*a):
            raise RuntimeError("boom")

        with mock.patch.dict(os.environ, {session.WINDOWS_LIVE_ENV: "1"}):
            result = session.maybe_run_windows_after_finalize(
                _FakeArchive(Path("/tmp"), "2026-10-09"),
                {"selected": []}, None, hook=boom)
        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("RuntimeError", result["error"])


class FinalizeHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _finalize(self, *, hook=None, env_on=False):
        day = date(2026, 10, 5)
        archive = DayArchive(self.root, day, "oa203/scanner", upload=False)
        archive.dir.mkdir(parents=True, exist_ok=True)
        (archive.dir / "universe.json").write_text(
            '{"trade_date": "2026-10-05", "selected": []}', encoding="utf-8")
        client = SimpleNamespace(stats={})
        cfg = Config.from_env()
        env = {session.WINDOWS_LIVE_ENV: "1"} if env_on else {}
        with mock.patch.dict(os.environ, env):
            if not env_on:
                os.environ.pop(session.WINDOWS_LIVE_ENV, None)
            return session.finalize(archive, client, cfg, {"selected": []},
                                    None, None, backfill_enabled=False,
                                    windows_hook=hook)

    def test_finalize_flag_off_unchanged(self):
        with mock.patch.object(session, "maybe_run_windows_after_finalize",
                               return_value={"enabled": False}) as spy:
            summary = self._finalize()
        self.assertTrue(summary["final"])
        self.assertEqual(summary["windows_live"], {"enabled": False})
        spy.assert_called_once()

    def test_finalize_flag_on_records_hook_result(self):
        calls = []

        def hook(spool_dir, day, universe):
            calls.append(day.isoformat())
            return {"enabled": True, "day": day.isoformat()}

        summary = self._finalize(hook=hook, env_on=True)
        self.assertTrue(summary["final"])
        self.assertEqual(summary["windows_live"], {"enabled": True, "day": "2026-10-05"})
        self.assertEqual(calls, ["2026-10-05"])


class RunAfterFinalizeTests(unittest.TestCase):
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

    def test_end_to_end_build_and_publish_local(self):
        backend_root = self.root / "backend"
        report = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["day"], "2026-10-09")
        self.assertEqual(report["days"], DAYS)
        self.assertEqual(report["build"]["universe_scope"], "full_universe")
        self.assertEqual(len(report["build"]["windows"]), 14)
        self.assertTrue(all(r["status"] == "published" for r in report["publish"]))
        self.assertEqual(len(report["publish"]), 14)
        first_revisions = {r["window_id"]: r["revision"] for r in report["publish"]}
        # A second run rebuilds (generated_at changes), so it publishes a
        # NEW revision rather than silently overwriting: the publisher
        # never mutates a revision in place. Publish-layer idempotency
        # (same emit root twice -> already_published) is proven by the
        # live tests; the hook level is re-runnable, not a no-op.
        again = live.run_after_finalize(
            self.spool, "2026-10-09", self.universe,
            backend=pub.LocalFilesystemBackend(backend_root),
            emit_root=self.root / "emit", now=NOW)
        self.assertTrue(all(r["status"] == "published" for r in again["publish"]))
        self.assertEqual(len(again["publish"]), 14)
        self.assertTrue(all(r["revision"] != first_revisions[r["window_id"]]
                            for r in again["publish"]))

    def test_finalized_day_dirs_includes_partial_day(self):
        import shutil
        shutil.rmtree(self.spool / "2026-10-08")
        _write_day(self.spool, "2026-10-08", assessment="partial")
        dirs = live.finalized_day_dirs(self.spool, through_day=date(2026, 10, 9))
        self.assertEqual([d.name for d in dirs], DAYS)
        # A non-final day is skipped, never silently built.
        (self.spool / "2026-10-11").mkdir()
        (self.spool / "2026-10-11" / "summary.json").write_text(
            '{"final": false}', encoding="utf-8")
        dirs = live.finalized_day_dirs(self.spool, through_day=date(2026, 10, 11))
        self.assertNotIn("2026-10-11", [d.name for d in dirs])


if __name__ == "__main__":
    unittest.main()
