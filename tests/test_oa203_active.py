"""Cutoff and publication invariants for provisional OA-203 rankings."""
import gzip
import csv
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import oa203_return_scanner as scanner  # noqa: E402

DAY = date(2026, 10, 2)
OPEN = 1790947800000
CLOSE = 1790971200000
SYMBOL = "QQQ261002C00600000"


class ActiveCutoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = scanner.DayArchive(Path(self.tmp.name), DAY, "oa203/scanner", False)
        self.cfg = scanner.Config(upload=False)

    def add_sweep(self, number, with_quote=True):
        t = OPEN + number * 300000
        name = f"sweep_{number:04d}.jsonl.gz"
        row = {"sweep": number, "t": t, "underlying": "QQQ", "symbol": SYMBOL,
               "type": "call", "strike": 600, "expiration": "2026-10-02",
               "bid": float(number), "ask": float(number) + .1,
               "bid_ms": t, "ask_ms": t, "bid_size": 2, "ask_size": 2,
               "volume": 5, "oi": 10, "spot": 600}
        with gzip.open(self.archive.path("sweeps/" + name), "wt") as out:
            if with_quote:
                out.write(json.dumps(row) + "\n")
        self.archive.append_manifest({"sweep": number, "file": name, "started_ms": t,
                                      "ended_ms": t + 100, "truncated": False,
                                      "rows": int(with_quote),
                                      "chains": {"QQQ": {"status": "ok", "rows": int(with_quote)}}})

    def test_future_manifest_and_sweep_cannot_change_earlier_clean_view(self):
        for i in range(1, 4):
            self.add_sweep(i)
        early = scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN,
                                            OPEN + 3 * 300000 + 100,
                                            qualified_policy=self.cfg.qualified_policy, cutoff_sweep=3)
        self.assertEqual(len(early), 1)
        self.assertTrue(early[0]["clean"])
        for i in range(4, 10):
            self.add_sweep(i, with_quote=False)
        later_same_cutoff = scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN,
                                            OPEN + 3 * 300000 + 100,
                                            qualified_policy=self.cfg.qualified_policy, cutoff_sweep=3)
        self.assertEqual(early, later_same_cutoff)
        with self.archive.path("sweeps/manifest.jsonl").open("a") as out:
            out.write('{"future_partial":')
        self.assertEqual(early, scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN,
                         OPEN + 3 * 300000 + 100, qualified_policy=self.cfg.qualified_policy,
                         cutoff_sweep=3))
        self.assertEqual(scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE)["sweep"], 3)
        with self.archive.path("sweeps/manifest.jsonl").open("rb+") as out:
            out.truncate(out.seek(0, 2) - len('{"future_partial":'))
        full = scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN,
                                           CLOSE, qualified_policy=self.cfg.qualified_policy)
        self.assertFalse(full[0]["clean"])
        self.assertIn("low_coverage", full[0]["contract_flags"])

    def test_immutable_generation_pointer_last_and_retry(self):
        for i in range(1, 4):
            self.add_sweep(i)
        first = scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE)
        self.assertEqual(first["sweep"], 3)
        self.assertEqual(len(first["members"]), 4)
        pinned = self.archive.path(first["archive"].removeprefix(self.archive.prefix + "/") + "/summary.json").read_bytes()
        for i in range(4, 7):
            self.add_sweep(i)
        second = scanner.build_active_snapshot(self.archive, self.cfg, 6, OPEN, CLOSE)
        self.assertEqual(second["sweep"], 6)
        self.assertEqual(self.archive.path(first["archive"].removeprefix(self.archive.prefix + "/") + "/summary.json").read_bytes(), pinned)
        self.assertEqual(scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE)["sweep"], 6)
        self.assertNotIn("inprogress/latest.json", self.archive.artifacts())
        self.assertFalse(self.archive.path(first["archive"].removeprefix(self.archive.prefix + "/") + "/contracts.csv.gz").exists())

    def test_partial_or_corrupt_input_never_advances_pointer(self):
        self.add_sweep(1)
        with self.assertRaises(ValueError):
            scanner.build_active_snapshot(self.archive, self.cfg, 2, OPEN, CLOSE)
        self.assertFalse(self.archive.path("inprogress/latest.json").exists())
        self.add_sweep(2)
        self.archive.path("sweeps/sweep_0002.jsonl.gz").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            scanner.build_active_snapshot(self.archive, self.cfg, 2, OPEN, CLOSE)
        self.assertFalse(self.archive.path("inprogress/latest.json").exists())

    def test_uploads_verify_sources_and_publish_pointer_last(self):
        for i in range(1, 4):
            self.add_sweep(i)
        self.archive.upload = True
        with self.assertRaisesRegex(ValueError, "unverified"):
            scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE,
                                          uploader=lambda *_: {})
        self.archive.write_json("uploads.json", {f"sweeps/sweep_{i:04d}.jsonl.gz":
            {"sha256": scanner._sha256_file(self.archive.path(f"sweeps/sweep_{i:04d}.jsonl.gz"))}
            for i in range(1, 4)})
        sent = []
        def upload(path, key, _type):
            sent.append(key)
            return {"sha256": scanner._sha256_file(path)}
        scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE, uploader=upload)
        self.assertEqual(len(sent), 6)
        self.assertEqual(sent[-1], "oa203/scanner/2026-10-02/inprogress/latest.json")
        self.assertEqual(self.archive.artifacts().count("inprogress/latest.json"), 0)
        self.assertEqual(scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE,
                         uploader=upload)["sweep"], 3)

    def test_second_local_builder_cannot_change_generation(self):
        self.add_sweep(1)
        with scanner.active_build_lock(self.archive):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                scanner.build_active_snapshot(self.archive, self.cfg, 1, OPEN, CLOSE)
        self.assertFalse(self.archive.path("inprogress/sweep_0001").exists())

    def test_snapshot_excludes_quote_after_early_close(self):
        for i in range(1, 7):
            self.add_sweep(i)
        early_close = OPEN + 6 * 300000 - 1000
        pointer = scanner.build_active_snapshot(self.archive, self.cfg, 6, OPEN, early_close)
        self.assertGreater(pointer["cutoff_ms"], early_close)
        with self.archive.path(pointer["archive"].removeprefix(self.archive.prefix + "/") + "/leaderboard.csv").open(newline="") as source:
            row = next(csv.DictReader(source))
        self.assertEqual(int(row["mid_first_to_max_exit_ms"]), OPEN + 5 * 300000)

    def test_oversized_member_is_not_pointed_to(self):
        for i in range(1, 4):
            self.add_sweep(i)
        with mock.patch.object(scanner, "ACTIVE_MAX_MEMBER_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "MCP artifact budget"):
                scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE)
        self.assertFalse(self.archive.path("inprogress/latest.json").exists())
        self.assertFalse(self.archive.path("inprogress/sweep_0003").exists())

    def test_failed_chain_row_and_additional_chain_count_fail_closed(self):
        self.add_sweep(1)
        manifest_path = self.archive.path("sweeps/manifest.jsonl")
        entry = json.loads(manifest_path.read_text())
        entry["chains"]["QQQ"] = {"status": "error"}
        manifest_path.write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(ValueError, "failed chain"):
            scanner.build_active_snapshot(self.archive, self.cfg, 1, OPEN, CLOSE)
        self.assertFalse(self.archive.path("inprogress/latest.json").exists())
        entry["chains"]["QQQ"] = {"status": "ok", "rows": 1}
        entry["chains"]["SPY"] = {"status": "ok", "rows": 1}
        entry["rows"] = 2
        manifest_path.write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(ValueError, "row count mismatch"):
            scanner.build_active_snapshot(self.archive, self.cfg, 1, OPEN, CLOSE)

    def test_matching_sweep_interval_and_quote_timestamp_units_for_all_bases(self):
        self.add_sweep(1)
        path = self.archive.path("sweeps/sweep_0001.jsonl.gz")
        with gzip.open(path, "rt") as source:
            original = json.loads(source.readline())
        def check_row(**changes):
            with gzip.open(path, "wt") as target:
                target.write(json.dumps({**original, **changes}) + "\n")
            return scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN, CLOSE,
                                               qualified_policy=self.cfg.qualified_policy, cutoff_sweep=1)
        self.assertEqual(len(check_row(t=OPEN + 300000)), 1)  # inclusive start
        self.assertEqual(len(check_row(t=OPEN + 300100, bid_ms=OPEN + 300100,
                                       ask_ms=OPEN + 300100)), 1)  # inclusive sweep end
        for bad in ({"t": OPEN + 299999}, {"t": OPEN + 300101},
                    {"bid_ms": original["t"] + 1}, {"ask_ms": original["t"] + 1},
                    {"spot_ms": original["t"] + 1}, {"trade_ms": original["t"] + 1},
                    {"bid_ms": original["t"] // 1000}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "interval|future|millisecond"):
                check_row(**bad)

    def test_additional_expiry_status_is_validated_without_changing_daily_scope(self):
        self.add_sweep(1)
        path = self.archive.path("sweeps/sweep_0001.jsonl.gz")
        with gzip.open(path, "rt") as source:
            nearest = json.loads(source.readline())
        extra = {**nearest, "symbol": "QQQ261009C00600000", "expiration": "2026-10-09",
                 "collection_role": "additional_expiry"}
        with gzip.open(path, "wt") as target:
            target.write(json.dumps(nearest) + "\n" + json.dumps(extra) + "\n")
        manifest_path = self.archive.path("sweeps/manifest.jsonl")
        entry = json.loads(manifest_path.read_text())
        entry["rows"] = 2
        entry["additional_chains"] = {"QQQ": {"2026-10-09": {
            "status": "ok", "expiration": "2026-10-09", "rows": 1}}}
        manifest_path.write_text(json.dumps(entry) + "\n")
        snapshot = scanner.build_active_snapshot(self.archive, self.cfg, 1, OPEN, CLOSE)
        summary = json.loads(self.archive.path(snapshot["archive"].removeprefix(self.archive.prefix + "/") +
                                               "/summary.json").read_text())
        self.assertEqual(summary["contracts"], 1)
        self.assertEqual(summary["assessment"]["chain_status_counts"], {"ok": 2})
        entry["additional_chains"]["QQQ"]["2026-10-09"]["status"] = "error"
        manifest_path.write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(ValueError, "failed chain"):
            scanner.build_contract_rows(self.archive.dir, self.cfg.policy, OPEN, CLOSE,
                                        qualified_policy=self.cfg.qualified_policy, cutoff_sweep=1)
        with self.assertRaisesRegex(ValueError, "source lineage changed"):
            scanner.build_active_snapshot(self.archive, self.cfg, 1, OPEN, CLOSE)

    def test_retry_after_local_loss_never_overwrites_immutable_remote_key(self):
        for i in range(1, 4):
            self.add_sweep(i)
        self.archive.upload = True
        self.archive.write_json("uploads.json", {f"sweeps/sweep_{i:04d}.jsonl.gz":
            {"sha256": scanner._sha256_file(self.archive.path(f"sweeps/sweep_{i:04d}.jsonl.gz"))}
            for i in range(1, 4)})
        remote = {}
        def upload(path, key, _type):
            digest = scanner._sha256_file(path)
            if "/rev_" in key and key in remote:
                self.assertEqual(remote[key], digest, "immutable remote key changed")
            remote[key] = digest
            return {"sha256": digest}
        first = scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE, uploader=upload)
        # Simulate a lost local generation and both local moving pointers.
        shutil.rmtree(self.archive.path("inprogress/sweep_0003"))
        self.archive.path("inprogress/sweep_0003.json").unlink()
        self.archive.path("inprogress/latest.json").unlink()
        second = scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE, uploader=upload)
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertIn(first["archive"] + "/summary.json", remote)
        self.assertIn(second["archive"] + "/summary.json", remote)

    def test_interrupted_pointer_upload_reuses_revision_on_restart(self):
        for i in range(1, 4):
            self.add_sweep(i)
        self.archive.upload = True
        self.archive.write_json("uploads.json", {f"sweeps/sweep_{i:04d}.jsonl.gz":
            {"sha256": scanner._sha256_file(self.archive.path(f"sweeps/sweep_{i:04d}.jsonl.gz"))}
            for i in range(1, 4)})
        def fail_pointer(path, key, _type):
            if key.endswith("sweep_0003.json"):
                raise RuntimeError("interrupted")
            return {"sha256": scanner._sha256_file(path)}
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            scanner.build_active_snapshot(self.archive, self.cfg, 3, OPEN, CLOSE, uploader=fail_pointer)
        self.assertFalse(self.archive.path("inprogress/latest.json").exists())
        archive = scanner.DayArchive(Path(self.tmp.name), DAY, "oa203/scanner", True)
        pointer = scanner.build_active_snapshot(archive, self.cfg, 3, OPEN, CLOSE,
            uploader=lambda path, *_: {"sha256": scanner._sha256_file(path)})
        self.assertEqual(pointer["members"]["summary.json"],
                         scanner._sha256_file(archive.path(pointer["archive"].removeprefix(archive.prefix + "/") + "/summary.json")))


if __name__ == "__main__":
    unittest.main()
