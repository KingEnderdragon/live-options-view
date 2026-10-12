import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import oa203_windows as w  # noqa: E402
import oa203_windows_outputs as outputs  # noqa: E402
from test_oa203_windows import SESSIONS, WEEK, info, ob, week_path  # noqa: E402

FINAL = {d: {"final": True, "assessment": "complete"} for d in WEEK}


def rows_for(window):
    up = w.evaluate(window, info(symbol="AAA261009C00100000"), week_path(), WEEK)
    down = w.evaluate(window, info(symbol="BBB261009C00100000"),
                      week_path()[:-1] + [ob("2026-10-09", 15, 55, 0.9, 1.0)], WEEK)
    gone = w.evaluate(window, info(symbol="CCC261006C00100000", expiration="2026-10-06"), week_path(), WEEK)
    return [gone, down, up]


class Outputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.window = w.weekly_windows(SESSIONS)[0][0]

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, status=FINAL, sources=(), shortlist=200):
        return outputs.write_window(self.root, self.window, rows_for(self.window), w.WindowPolicy(), status,
                                    list(sources), "2026-10-12T07:00:00+00:00", {"mode": "test"}, shortlist)

    def test_layout_and_summary(self):
        out = self.write(sources=[{"key": "oa203/scanner/2026-10-05/summary.json", "sha256": "ab", "bytes": 1}])
        self.assertEqual(out.relative_to(self.root).as_posix(),
                         "oa203/windows/oa203-windows-v1/weekly_cheapest/weekly-2026-W41-cheapest")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["paths.csv.gz", "rankings.csv", "summary.json"])
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((s["state"], s["state_reasons"]), ("final", []))
        self.assertEqual(s["policy"]["decisions_status"], "provisional_recommended_defaults")
        self.assertEqual(s["counts"]["exec"]["eligible"], 2)
        self.assertEqual(s["counts"]["exec"]["negative"], 1)
        self.assertEqual(s["counts"]["exec"]["exclusion_reasons"], {"expires_before_exit": 1})
        self.assertEqual(s["sources"][0]["sha256"], "ab")
        self.assertTrue(any("hindsight" in label for label in s["labels"]))
        self.assertFalse(list(out.glob("*.tmp")))

    def test_every_path_written_including_non_winners(self):
        out = self.write()
        rows = list(csv.DictReader(io.StringIO(gzip.decompress((out / "paths.csv.gz").read_bytes()).decode())))
        self.assertEqual([r["symbol"] for r in rows], ["AAA261009C00100000", "BBB261009C00100000", "CCC261006C00100000"])
        self.assertEqual(rows[2]["exec_status"], "excluded")
        self.assertIn("expires_before_exit", rows[2]["exec_reasons"])

    def test_shortlist_is_cut_after_full_ranking(self):
        out = self.write(shortlist=1)
        ranked = list(csv.DictReader((out / "rankings.csv").open(encoding="utf-8")))
        self.assertEqual([(r["basis"], r["rank"], r["symbol"]) for r in ranked],
                         [(b, "1", "AAA261009C00100000") for b in w.BASES])
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((s["counts"]["exec"]["eligible"], s["counts"]["exec"]["shortlist"]), (2, 1))

    def test_states(self):
        partial = dict(FINAL, **{"2026-10-08": {"final": True, "assessment": "partial"}})
        self.assertEqual(outputs.window_state(self.window, partial), ("partial", ["session_partial:2026-10-08"]))
        pending = dict(FINAL, **{"2026-10-09": {"final": False, "assessment": None}})
        self.assertEqual(outputs.window_state(self.window, pending), ("in_progress", ["session_not_final:2026-10-09"]))
        missing = {k: v for k, v in FINAL.items() if k != "2026-10-07"}
        self.assertEqual(outputs.window_state(self.window, missing)[0], "in_progress")
        self.assertEqual(outputs.window_state(self.window, FINAL, ["oa203/scanner/2026-10-07/sweeps/sweep_0003.jsonl.gz"]),
                         ("partial", ["source_missing:oa203/scanner/2026-10-07/sweeps/sweep_0003.jsonl.gz"]))

    def test_deterministic_content(self):
        a = self.write()
        first = {p.name: p.read_bytes() for p in a.iterdir()}
        b = self.write()
        self.assertEqual(first, {p.name: p.read_bytes() for p in b.iterdir()})


if __name__ == "__main__":
    unittest.main()
