import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_cross_market_symbols as probe  # noqa: E402

SOURCE = (ROOT / "scripts" / "probe_cross_market_symbols.py").read_text(encoding="utf-8")


class ProbeSafety(unittest.TestCase):
    def test_no_order_account_or_write_paths(self):
        for forbidden in ("/orders", "/accounts", "/sessions", "requests.post", "requests.put",
                          "requests.delete", "requests.patch", "put_object", "R2_"):
            self.assertNotIn(forbidden, SOURCE, forbidden)
        self.assertEqual(re.findall(r"requests\.(\w+)\(", SOURCE), ["get"])

    def test_requires_explicit_read_only_flag(self):
        with mock.patch.object(sys, "argv", ["probe", "--out", "x.json"]), \
                mock.patch.object(probe.collector, "tasty_auth") as auth:
            with self.assertRaises(SystemExit):
                probe.main()
            auth.assert_not_called()

    def test_listen_window_is_bounded(self):
        with mock.patch.object(sys, "argv", ["probe", "--confirm-read-only", "--seconds", "600", "--out", "x.json"]), \
                mock.patch.object(probe.collector, "tasty_auth") as auth:
            with self.assertRaises(SystemExit):
                probe.main()
            auth.assert_not_called()


class ProbePlan(unittest.TestCase):
    RESOLVED = {"futures": {"CL": {"contracts": [{"streamer_symbol": "/CLX26:XNYM"}, {"streamer_symbol": "/CLZ26:XNYM"},
                                                {"streamer_symbol": "/CLF27:XNYM"}]},
                            "6J": {"contracts": [{"streamer_symbol": "/6JZ26:XCME"}]}},
                "crypto": {"pairs": [{"symbol": "BTC/USD", "streamer_symbol": "BTC/USD:CXTALP"}]}}

    def test_resolved_symbols_are_added_once_and_bounded(self):
        plan = probe.symbol_plan(self.RESOLVED)
        self.assertEqual(plan["fut.cl"], ["/CL:XNYM", "/CLX26:XNYM", "/CLZ26:XNYM"])
        self.assertIn("/6JZ26:XCME", plan["control.6j"])
        self.assertEqual(plan["crypto.btcusd"].count("BTC/USD:CXTALP"), 1)
        self.assertLessEqual(sum(map(len, plan.values())), probe.MAX_SYMBOLS)

    def test_summary_verdicts(self):
        plan = {"idx.spx": ["SPX", "$SPX.X"], "fut.cl": ["/CLX26:XNYM"]}
        state = {"$SPX.X": {"last": 6650.0, "prev_close": 6600.0}}
        r = probe.summarize(plan, state)
        self.assertEqual(r["idx.spx"]["verdict"], "served")
        self.assertEqual(r["idx.spx"]["working_symbols"], ["$SPX.X"])
        self.assertTrue(r["idx.spx"]["tried"][1]["prev_close"])
        self.assertEqual(r["fut.cl"]["verdict"], "no_data")


if __name__ == "__main__":
    unittest.main()
