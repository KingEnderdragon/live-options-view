"""The bounded News launcher (market_data/news/launch.py) and its config bounds.

    python -m unittest discover -s tests -p test_news_launcher.py -v

Offline: local stores, fixture feeds and fake child processes; no bucket, credential or network.
The deadline paths use a fake child that holds a real WriterLease on a local store, so the tests
observe what an interrupted or killed cycle leaves behind.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data.news import config as news_config  # noqa: E402
from market_data.news import launch  # noqa: E402
from market_data.news import publish as pub  # noqa: E402

CONFIG = ROOT / "market_data/news/deploy/news-pilot.json"
LEASE = "news/v1/_writer/lease.json"

FAKE_CHILD = textwrap.dedent("""
    import os, signal, sys, time
    sys.path.insert(0, os.environ["FAKE_ROOT"])
    from datetime import datetime, timezone
    from market_data.news import launch
    from market_data.news.archive import LocalObjectStore, WriterLease

    mode = os.environ["FAKE_MODE"]
    store = LocalObjectStore(os.environ["FAKE_STORE"])
    clock = lambda: datetime.now(timezone.utc)

    def hold(argv):
        with WriterLease(store, clock):
            open(os.environ["FAKE_READY"], "w").close()
            if os.environ.get("FAKE_BLOCK"):   # stuck in one blocking socket read, like a hung store call
                import socket
                a, b = socket.socketpair()
                a.recv(1)
            for _ in range(1200):    # busy in short steps, like a cycle between store calls
                time.sleep(0.1)
        return 0

    if mode == "interrupt":          # the real child entry, with a cycle that holds the lease
        import market_data.news.__main__ as cli
        cli.main = hold
        sys.exit(launch.run_cycle(sys.argv[1:]))
    if mode == "ignore":             # a cycle that never unwinds
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_IGN)
        hold([])
    sys.exit(int(mode))              # a cycle that finishes with this exit code
""")


def bounds(deadline=2, grace=5, kill_wait=5):
    return {"launcher": {"deadline_s": deadline, "interrupt_grace_s": grace, "kill_wait_s": kill_wait}}


class ConfigBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raw = json.loads(CONFIG.read_bytes())

    def load(self, raw):
        path = self.tmp / "cfg.json"
        path.write_bytes(json.dumps(raw).encode())
        return pub.load_pilot_config(path)

    def test_shipped_config_runs_the_bounded_launcher(self):
        cfg = pub.load_pilot_config(CONFIG)
        self.assertFalse(cfg.active)
        self.assertEqual(cfg["schedule"]["command"],
                         "python -m market_data.news.launch --config market_data/news/deploy/news-pilot.json --live")
        b = cfg["launcher"]
        total = b["deadline_s"] + b["interrupt_grace_s"] + b["kill_wait_s"]
        self.assertGreater(b["deadline_s"], cfg["cycle_budget_s"])
        self.assertLess(total, cfg["schedule"]["interval_s"], "a launcher ends before the next tick")
        self.assertLessEqual(total, news_config.LEASE_TTL_S, "a live cycle never outlives its lease")
        worst_fetch = news_config.REQUEST_TIMEOUT_S * (news_config.MAX_RETRIES + 1) + sum(news_config.RETRY_BACKOFF_S)
        self.assertGreaterEqual(b["interrupt_grace_s"], worst_fetch)

    def test_unbounded_or_overlapping_configs_are_refused(self):
        cases = {
            "direct pilot-cycle command": lambda c: c["schedule"].update(
                command="python -m market_data.news pilot-cycle --config x --live"),
            "no launcher block": lambda c: c.pop("launcher"),
            "deadline inside the collection budget": lambda c: c["launcher"].update(deadline_s=180),
            "grace shorter than one fetch": lambda c: c["launcher"].update(interrupt_grace_s=30),
            "runs into the next tick": lambda c: c["launcher"].update(deadline_s=800),
            "exactly the interval": lambda c: c["launcher"].update(deadline_s=800, interrupt_grace_s=90, kill_wait_s=10),
            "zero kill wait": lambda c: c["launcher"].update(kill_wait_s=0),
            "bool deadline": lambda c: c["launcher"].update(deadline_s=True),
            "text deadline": lambda c: c["launcher"].update(deadline_s="600"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                raw = copy.deepcopy(self.raw)
                change(raw)
                with self.assertRaises(pub.PublishError):
                    self.load(raw)

    def test_largest_bound_below_the_interval_is_accepted(self):
        raw = copy.deepcopy(self.raw)
        raw["launcher"].update(deadline_s=799, interrupt_grace_s=90, kill_wait_s=10)   # 899 < 900
        self.assertEqual(self.load(raw)["launcher"]["deadline_s"], 799)


class RealCycle(unittest.TestCase):
    def test_a_fixture_cycle_finishes_inside_the_deadline(self):
        tmp = Path(tempfile.mkdtemp())
        args = ["--private", str(tmp / "p"), "--public", str(tmp / "q"), "--spool", str(tmp / "s"),
                "--clock", "2026-11-16T12:00:00Z"]
        record = launch.launch(str(CONFIG), args)
        self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]), ("ok", 0, 0), record)
        self.assertEqual(record["schema"], "news-launch/v1")
        self.assertEqual(record["deadline_s"], 600)
        self.assertLess(record["elapsed_s"], 600)
        self.assertTrue((tmp / "q" / "news/v2/views/index.json").is_file(), "the cycle published locally")
        self.assertFalse((tmp / "p" / LEASE).exists(), "its writer lease was released")

    def test_live_mode_stays_refused_through_the_launcher(self):
        tmp = Path(tempfile.mkdtemp())
        record = launch.launch(str(CONFIG), ["--live", "--private", str(tmp / "p"), "--public", str(tmp / "q")])
        self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]), ("cycle_failed", 2, 2))
        self.assertFalse((tmp / "p").exists())

    def test_invalid_config_starts_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        bad = tmp / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        with mock.patch.object(launch, "_spawn", side_effect=AssertionError("spawned")):
            record = launch.launch(str(bad), [])
        self.assertEqual((record["outcome"], record["exit_code"]), ("config_invalid", 2))
        self.assertIn("pilot config invalid", record["error"])

    def test_cli_prints_one_terminal_record(self):
        import contextlib
        import io
        out = io.StringIO()
        tmp = Path(tempfile.mkdtemp())
        bad = tmp / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        with contextlib.redirect_stdout(out):
            code = launch.main(["--config", str(bad)])
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["exit_code"], code)
        self.assertEqual(code, 2)


class Deadline(unittest.TestCase):
    """A fake child holding a real WriterLease, run under small bounds."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "fake_child.py"
        self.script.write_text(FAKE_CHILD, encoding="utf-8")
        self.store = self.tmp / "store"
        self.ready = self.tmp / "ready"

    def run_launch(self, mode, b, signal_after=None, block=False):
        env = {"FAKE_ROOT": str(ROOT), "FAKE_MODE": mode, "FAKE_STORE": str(self.store), "FAKE_READY": str(self.ready),
               "FAKE_BLOCK": "1" if block else ""}
        timer = None
        if signal_after is not None:
            timer = threading.Timer(signal_after, os.kill, (os.getpid(), signal.SIGTERM))
            timer.start()
        try:
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(pub, "load_pilot_config", return_value=b):
                return launch.launch("unused.json", [], cmd_prefix=[sys.executable, str(self.script)])
        finally:
            if timer is not None:
                timer.cancel()

    def test_child_exit_codes_pass_through(self):
        for code, outcome, exit_code in ((0, "ok", 0), (1, "cycle_failed", 1), (2, "cycle_failed", 2),
                                         (9, "cycle_failed", 1)):
            with self.subTest(code=code):
                record = self.run_launch(str(code), bounds())
                self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]),
                                 (outcome, exit_code, code))

    def test_deadline_interrupts_and_the_cycle_releases_its_lease(self):
        started = time.monotonic()
        record = self.run_launch("interrupt", bounds(deadline=3, grace=20, kill_wait=5))
        self.assertTrue(self.ready.exists(), "the cycle had taken its lease")
        self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]),
                         ("deadline_interrupted", 124, launch.CHILD_INTERRUPTED), record)
        self.assertFalse((self.store / LEASE).exists(), "the interrupted cycle released its writer lease")
        self.assertLess(time.monotonic() - started, 3 + 20, "ended well inside deadline + grace")
        self.assertGreaterEqual(record["elapsed_s"], 3)
        self.assertIn("interrupted_at", record)

    @unittest.skipIf(os.name == "nt", "a console break does not interrupt a blocking read on Windows; the "
                                      "scheduler host is Linux")
    def test_a_cycle_stuck_in_a_blocking_read_is_interrupted_and_releases_its_lease(self):
        record = self.run_launch("interrupt", bounds(deadline=3, grace=20, kill_wait=5), block=True)
        self.assertTrue(self.ready.exists())
        self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]),
                         ("deadline_interrupted", 124, launch.CHILD_INTERRUPTED), record)
        self.assertFalse((self.store / LEASE).exists())
        self.assertLess(record["elapsed_s"], 3 + 20)

    def test_a_cycle_that_will_not_unwind_is_killed_and_the_record_says_so(self):
        started = time.monotonic()
        record = self.run_launch("ignore", bounds(deadline=3, grace=2, kill_wait=5))
        elapsed = time.monotonic() - started
        self.assertTrue(self.ready.exists())
        self.assertEqual((record["outcome"], record["exit_code"], record["cause"]), ("killed", 137, "deadline"), record)
        self.assertTrue((self.store / LEASE).exists(), "a killed cycle cannot release its lease")
        self.assertIn("--break-stale-lease", record["lease_note"])
        self.assertLess(elapsed, 3 + 2 + 5 + 2, "bounded by deadline + grace + kill wait")

    @unittest.skipIf(os.name == "nt", "SIGTERM to the launcher's own process is POSIX-only")
    def test_a_stop_signal_interrupts_the_cycle(self):
        record = self.run_launch("interrupt", bounds(deadline=60, grace=20, kill_wait=5), signal_after=2)
        self.assertEqual((record["outcome"], record["exit_code"], record["child_exit"]),
                         ("stopped_interrupted", 143, launch.CHILD_INTERRUPTED), record)
        self.assertEqual(record["signal"], signal.SIGTERM)
        self.assertFalse((self.store / LEASE).exists())
        self.assertLess(record["elapsed_s"], 20)

    def test_launcher_restores_its_signal_handlers(self):
        before = signal.getsignal(signal.SIGTERM)
        self.run_launch("0", bounds())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
