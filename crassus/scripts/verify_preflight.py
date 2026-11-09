#!/usr/bin/env python3
"""Verify the bot provisioning preflight and its diagnostics.

Covers aggregated missing credentials, inactive-account exclusion, key
acceptance/rejection classification and the runner's actionable ConfigError
on a Worker key mismatch. No network and no real secret.

    python scripts/verify_preflight.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
sys.path.insert(0, str(ROOT / "crassus" / "scripts"))

from crassus.client import AccountSession, ConfigError  # noqa: E402
from crassus.config import account_gaps, load_accounts  # noqa: E402
from preflight_accounts import probe_registration_key  # noqa: E402


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _catalog(tmp: Path, entries: list[dict]) -> Path:
    path = tmp / "accounts.json"
    path.write_text(json.dumps({"accounts": entries}))
    return path


class PreflightTests(unittest.TestCase):
    def test_reports_every_missing_env_and_skips_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _catalog(Path(tmp), [
                {"alias": "A", "username": "a", "password_env": "PW_A", "strategy_id": "x"},
                {"alias": "B", "username": "b", "password_env": "PW_B", "strategy_id": "x"},
                {"alias": "C", "username": "c", "password_env": "PW_C", "strategy_id": "x", "active": False},
            ])
            gaps = account_gaps(path, environ={})
            self.assertEqual([gap["missing"] for gap in gaps], ["PW_A", "PW_B"])
            with self.assertRaises(ValueError) as caught:
                load_accounts(path)
            message = str(caught.exception)
            self.assertIn("A→$PW_A", message)
            self.assertIn("B→$PW_B", message)
            self.assertNotIn("PW_C", message)

    def test_load_with_provisioned_values_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _catalog(Path(tmp), [
                {"alias": "A", "username": "a", "password_env": "PW_A", "strategy_id": "x"},
            ])
            with mock.patch.dict(os.environ, {"PW_A": "s3cret"}, clear=False):
                accounts = load_accounts(path)
            self.assertEqual([a.alias for a in accounts], ["A"])
            self.assertNotIn("s3cret", repr(accounts[0]))

    def test_probe_separates_accepted_rejected_and_unreachable(self):
        accepted = probe_registration_key("https://w", "k", request=lambda *a, **k: _Response(400, '{"error":"bad body"}'))
        self.assertEqual(accepted["status"], "accepted")
        rejected = probe_registration_key("https://w", "k", request=lambda *a, **k: _Response(403, '{"error":"Invalid bot registration key"}'))
        self.assertEqual(rejected["status"], "rejected")

        def offline(*args, **kwargs):
            raise requests.ConnectionError("offline")

        self.assertEqual(probe_registration_key("https://w", "k", request=offline)["status"], "unreachable")

    def test_key_mismatch_is_a_config_error_not_a_transport_error(self):
        account = mock.Mock(alias="A", username="a")
        session = AccountSession(account, base_url="https://w", bot_registration_key="wrong")
        session.login = lambda: _Response(401, "bad password")
        session.register = lambda: _Response(403, '{"error":"Invalid bot registration key"}')
        with self.assertRaises(ConfigError) as caught:
            session.ensure_session()
        self.assertIn("does not match the Worker secret", str(caught.exception))
        self.assertIn("preflight_accounts.py", str(caught.exception))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PreflightTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
