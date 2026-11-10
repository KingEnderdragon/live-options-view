"""Offline tests for windows_live: build + publish from local day archives.

Everything here is local and synthetic: day directories are written by
the test in the exact shape ``finalize`` writes them (summary.json,
universe.json, sweeps/manifest.jsonl, sweeps/sweep_NNNN.jsonl.gz), the
build runs the real producer, and publication runs the real publisher
against a LocalFilesystemBackend. No provider, no R2, no network.
"""

import gzip
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_data.options_scanner.windows_live as live
import market_data.options_scanner.windows_publish as pub
from market_data.options_scanner.cli import main as cli_main

DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
BOUNDS = {
    "2026-10-05": (1791207000000, 1791230400000),
    "2026-10-06": (1791293400000, 1791316800000),
    "2026-10-07": (1791379800000, 1791403200000),
    "2026-10-08": (1791466200000, 1791489600000),
    "2026-10-09": (1791552600000, 1791576000000),
}
# (symbol, underlying, type, strike, expiration, root)
CONTRACTS = [
    ("AAA261009C00100000", "AAA", "call", 100, "2026-10-09", "AAA"),
    ("BBB261009P00050000", "BBB", "put", 50, "2026-10-09", "BBB"),
    ("CCC261006C00075000", "CCC", "call", 75, "2026-10-06", "CCC"),
]
# Mid-price per contract per day: AAA rises, BBB falls, CCC lives Oct 5-6 only.
MIDS = {
    "AAA261009C00100000": [0.15, 0.48, 0.50, 0.52, 0.55],
    "BBB261009P00050000": [1.00, 0.90, 0.85, 0.80, 0.75],
    "CCC261006C00075000": [0.25, 0.35],
}
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def _write_day(root: Path, day: str, *, assessment: str = "complete") -> Path:
    """Write one synthetic finalized day directory in finalize() shape."""
    open_ms, close_ms = BOUNDS[day]
    day_dir = root / day
    (day_dir / "sweeps").mkdir(parents=True)
    alive = [c for c in CONTRACTS if DAYS.index(day) < len(MIDS[c[0]])]
    (day_dir / "summary.json").write_text(json.dumps({
        "final": True, "trade_date": day, "session_open_ms": open_ms,
        "session_close_ms": close_ms, "assessment": {"status": assessment},
    }) + "\n", encoding="utf-8")
    (day_dir / "universe.json").write_text(json.dumps({
        "trade_date": day,
        "selected": [{"underlying": c[1], "expiration": c[4], "additional_expiration": None}
                     for c in alive],
    }) + "\n", encoding="utf-8")
    manifest = []
    for sweep in (1, 2):
        # Endpoints must fall inside the 900s open/close tolerances
        # (WindowPolicy open_tolerance_s / close_tolerance_s): first
        # observation within 900s after the open, last within 900s
        # before the close, or the window has no endpoints at all.
        t = open_ms + 600000 if sweep == 1 else close_ms - 600000
        rows = []
        chains = {}
        for symbol, underlying, otype, strike, expiry, r in alive:
            mid = MIDS[symbol][DAYS.index(day)]
            rows.append({
                "underlying": underlying, "symbol": symbol, "expiration": expiry,
                "t": t, "sweep": sweep, "bid": round(mid - 0.01, 4),
                "ask": round(mid + 0.01, 4), "bid_ms": t, "ask_ms": t,
                "spot": 100.0, "bid_size": 10, "ask_size": 10,
                "volume": 5, "oi": 100, "root": r, "type": otype,
                "strike": strike, "contract_size": 100,
            })
            chains[underlying] = {"status": "ok", "fetched_ms": t, "rows": 1,
                                  "expiration": expiry}
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row) + "\n").encode())
        (day_dir / "sweeps" / f"sweep_{sweep:04d}.jsonl.gz").write_bytes(buf.getvalue())
        manifest.append({"sweep": sweep, "file": f"sweep_{sweep:04d}.jsonl.gz",
                         "started_ms": open_ms if sweep == 1 else close_ms - 7200000,
                         "ended_ms": open_ms + 7200000 if sweep == 1 else close_ms,
                         "rows": len(rows), "chains": chains, "truncated": False})
    (day_dir / "sweeps" / "manifest.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in manifest), encoding="utf-8")
    return day_dir


def _window_ids(report):
    return {(w["kind"], w["window_id"]) for w in report["windows"]}


class LiveBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.days = [_write_day(self.root / "days", d) for d in DAYS]
        self.emit = self.root / "emit"

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_all_kinds_final(self):
        report = live.build_windows(self.days, {"AAA", "BBB", "CCC"}, self.emit)
        self.assertEqual(len(report["windows"]), 14)
        kinds = {k for k, _ in _window_ids(report)}
        self.assertEqual(kinds, {"weekly_fixed", "weekly_cheapest",
                                 "weekly_cheapest_best_exit", "overnight",
                                 "sessions_2", "sessions_3"})
        by_id = {(w["kind"], w["window_id"]): w for w in report["windows"]}
        self.assertIn(("weekly_cheapest", "weekly-2026-W41-cheapest"), by_id)
        self.assertIn(("overnight", "overnight-2026-10-05_2026-10-06"), by_id)
        self.assertIn(("sessions_3", "sessions3-2026-10-05_2026-10-07"), by_id)
        for w in report["windows"]:
            self.assertIn(w["kind"], kinds)
        # Every emit has the three members and a final summary.
        for kind, window_id in _window_ids(report):
            d = self.emit / "oa203" / "windows" / "oa203-windows-v1" / kind / window_id
            self.assertTrue((d / "summary.json").is_file(), kind)
            self.assertTrue((d / "rankings.csv").is_file(), kind)
            self.assertTrue((d / "paths.csv.gz").is_file(), kind)
            summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["state"], "final", window_id)
            self.assertEqual(summary["scope"]["mode"], "live_archive")
            self.assertEqual(summary["contracts_evaluated"], 3)
        overnight = by_id[("overnight", "overnight-2026-10-05_2026-10-06")]
        self.assertEqual(overnight["qualified"]["top"][0]["symbol"], "AAA261009C00100000")

    def test_partial_day_refused_fail_closed(self):
        _write_day(self.root / "days2", "2026-10-08", assessment="partial")
        bad = [self.days[0], self.days[1], self.days[2],
               self.root / "days2" / "2026-10-08", self.days[4]]
        with self.assertRaises(RuntimeError):
            live.build_windows(bad, {"AAA", "BBB", "CCC"}, self.emit)

    def test_bounds(self):
        with self.assertRaises(ValueError):
            live.build_windows(self.days, {f"U{i:02d}" for i in range(51)}, self.emit)
        with self.assertRaises(ValueError):
            live.build_windows(self.days * 3, {"AAA"}, self.emit)

    def test_publish_all_local_then_idempotent(self):
        live.build_windows(self.days, {"AAA", "BBB", "CCC"}, self.emit)
        backend = pub.LocalFilesystemBackend(self.root / "backend")
        results = live.publish_windows(self.emit, backend, now=NOW)
        self.assertEqual(len(results), 14)
        self.assertTrue(all(r["status"] == "published" for r in results))
        for r in results:
            pointer = json.loads(
                (self.root / "backend" / r["prefix"] / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(pointer["revision"], r["revision"])
            self.assertEqual(pointer["state"], "final")
        again = live.publish_windows(self.emit, backend, now=NOW)
        self.assertTrue(all(r["status"] == "already_published" for r in again))

    def test_cli_build_and_publish(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_main(["windows-build", "--dir", str(self.days[0]), "--dir", str(self.days[1]),
                           "--dir", str(self.days[2]), "--dir", str(self.days[3]),
                           "--dir", str(self.days[4]),
                           "--underlyings", "AAA", "BBB", "CCC",
                           "--emit-root", str(self.emit)])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(len(report["windows"]), 14)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            rc2 = cli_main(["windows-publish", "--emit-root", str(self.emit),
                            "--out", str(self.root / "backend")])
        self.assertEqual(rc2, 0)
        results = json.loads(out2.getvalue())
        self.assertEqual(len(results), 14)
        self.assertTrue(all(r["status"] == "published" for r in results))


if __name__ == "__main__":
    unittest.main()
