"""Replay-series reader over archived snapshot sidecars (#107)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

import market_readings_series as series  # noqa: E402
from market_data.overview.readings import readings_digest  # noqa: E402


def reading(reading_id, values, parameters=None, status="ok"):
    return {
        "reading_id": reading_id, "schema_version": "market_reading.v1",
        "observed_at": None, "calculated_at": "2026-11-09T15:00:00+00:00",
        "published_at": "2026-11-09T15:00:01+00:00",
        "status": {"code": status, "reason": None}, "freshness": {"status": "live"},
        "calculation": {"version": f"{reading_id}.v1", "parameters": parameters or {}},
        "values": values, "units": {k: "dimensionless" for k in values},
        "caveats": ["oi_zero_or_missing_indistinguishable"], "coverage": {"partial": False},
    }


def sidecar(key, readings, digest=None, market=None):
    body = {
        "schema_version": "snapshot_provenance.v1", "snapshot_key": key,
        "symbol": "QQQ", "constructed_at": "2026-11-09T15:00:00+00:00",
        "publication": {"status": "published"},
        "latest_published_at": "2026-11-09T15:00:01+00:00",
    }
    if readings is not None:
        body["market_readings"] = readings
        body["market_readings_digest"] = digest if digest is not None else readings_digest(readings)
    if market is not None:
        body["market"] = market
    return body


class SeriesReader(unittest.TestCase):
    def test_current_and_legacy_sidecars_both_replay(self):
        readings = [reading("reference_pcr", {"put_call_oi_ratio": 1.2}),
                    reading("reference_vwap", {"vwap": 599.9, "spot": 600.1})]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.provenance.json").write_text(json.dumps(
                sidecar("intraday/20261109/snapshot_1.csv", readings)))
            (root / "b.provenance.json").write_text(json.dumps(sidecar(
                "intraday/20261109/snapshot_2.csv", None,
                market={"spot": 600.1, "spot_ts": "2026-11-09T14:59:59+00:00",
                        "vwap": 599.9, "rvol": {"status": "ok"},
                        "momentum": {"status": "warming_up"},
                        "vwap_partial_session": True, "freshness": "live"})))
            rows = series.collect(series.from_directory(root))
        self.assertEqual(len(rows), 3)
        by_key = {}
        for row in rows:
            by_key.setdefault(row["snapshot_key"], []).append(row)
        current = by_key["intraday/20261109/snapshot_1.csv"]
        self.assertEqual([r["reading_id"] for r in current], ["reference_pcr", "reference_vwap"])
        self.assertTrue(all(r["digest_status"] == "ok" for r in current))
        legacy = by_key["intraday/20261109/snapshot_2.csv"][0]
        self.assertEqual(legacy["reading_id"], "legacy_underlying_market")
        self.assertEqual(legacy["status"], "live")
        self.assertTrue(legacy["coverage"]["partial"])
        self.assertIn("legacy_market_block_only", legacy["caveats"])

    def test_digest_mismatch_is_reported_not_repaired(self):
        readings = [reading("reference_pcr", {"put_call_oi_ratio": 1.2})]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.provenance.json"
            path.write_text(json.dumps(sidecar(
                "intraday/20261109/snapshot_1.csv", readings, digest="0" * 64)))
            rows = series.collect(series.from_directory(Path(tmp)))
        self.assertEqual(rows[0]["digest_status"], "digest_mismatch")
        self.assertEqual(rows[0]["values"], {"put_call_oi_ratio": 1.2})

    def test_missing_and_zero_values_survive_the_replay(self):
        readings = [reading("reference_oi_skew", {
            "near_money_call_oi": 0, "imbalance_ratio": None, "band_strikes": 0})]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.provenance.json").write_text(json.dumps(
                sidecar("intraday/20261109/snapshot_1.csv", readings)))
            rows = series.collect(series.from_directory(Path(tmp)))
        self.assertEqual(rows[0]["values"]["near_money_call_oi"], 0)
        self.assertIsNone(rows[0]["values"]["imbalance_ratio"])
        self.assertEqual(rows[0]["parameters"], {})

    def test_series_replay_needs_no_strategy_runtime(self):
        code = (
            "import sys; sys.path.insert(0, 'research'); import market_readings_series; "
            "print('crassus' in sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
