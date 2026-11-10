#!/usr/bin/env python3
"""Producer -> publisher -> consumer parity for every #130 window kind.

For every supported window kind (weekly_fixed, weekly_cheapest,
weekly_cheapest_best_exit, overnight, sessions_2, sessions_3) x final and
genuine partial state, this test:

1. builds the window with the REAL producer (windows.evaluate +
   windows_outputs.write_window) from synthetic observations;
2. publishes it with the REAL publisher (windows_publish.publish_window)
   to a LocalFilesystemBackend tree;
3. asserts the published bytes are exactly the producer's bytes;
4. writes a manifest of the producer's own rows/counts/detail and runs
   the REAL Node consumer over the same publisher-written trees
   (market_mcp/parity.test.js, via a local Source directory), asserting
   the consumer serves exactly the producer's rows, ranks, pcts,
   entry/exit and pinned detail for all three bases.

Partial states are producer-emitted: every session final, the last
session assessed partial, with the producer's own state_reasons
(e.g. session_partial:<day>). in_progress windows are NOT published
(the publisher refuses them entirely), so they have no parity row here;
the consumer's in_progress handling is covered by market_mcp/window.test.js.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data.options_scanner.windows as w  # noqa: E402
import market_data.options_scanner.windows_outputs as outputs  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from market_data.options_scanner.returns import ContractInfo  # noqa: E402
from test_oa203_windows import SESSIONS, info, ob  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
BASES = ("mid", "exec", "qualified")
ROW_FIELDS = ["symbol", "rank", "pct", "entry", "exit", "underlying", "option_type",
              "strike", "expiration", "entry_session", "exit_session"]
DETAIL_FIELDS = ["pct", "entry", "exit", "entry_ms", "exit_ms", "entry_session", "exit_session"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_node_parity(manifest_path: Path) -> subprocess.CompletedProcess:
    """Run market_mcp/parity.test.js against a manifest; shared with the
    concurrency tests, which build mixed-revision manifests the same way."""
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PARITY_MANIFEST=str(manifest_path))
    return subprocess.run(["node", "--test", "market_mcp/parity.test.js"],
                          cwd=str(repo), env=env, capture_output=True, text=True, timeout=300)


def check_node_ok(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        tail = (result.stdout or "")[-4000:] + "\n--- stderr ---\n" + (result.stderr or "")[-2000:]
        raise AssertionError(f"node parity run failed for {what} (exit {result.returncode}):\n{tail}")


def contract_specs():
    """(ContractInfo, entry bid, entry ask, exit bid, exit ask).

    XLU reproduces the real Oct 5-6 replay's qualified rank 1 (entry 0.15,
    exit 0.48, pct 2.2). ABC rises, BBB falls (negative but eligible), CCC
    expires before every exit day and is excluded on every basis.
    """
    return [
        (ContractInfo(symbol="XLU261009C00040500", underlying="XLU", option_type="call",
                      strike=40.5, expiration="2026-10-09", root="XLU"), 0.14, 0.15, 0.48, 0.49),
        (info(), 1.7, 1.8, 2.4, 2.5),
        (ContractInfo(symbol="BBB261009C00100000", underlying="BBB", option_type="call",
                      strike=100.0, expiration="2026-10-09", root="BBB"), 1.9, 2.0, 1.0, 1.1),
        (ContractInfo(symbol="CCC261005C00100000", underlying="CCC", option_type="call",
                      strike=100.0, expiration="2026-10-05", root="CCC"), 1.7, 1.8, 2.4, 2.5),
    ]


def contract_path(days, entry_bid, entry_ask, exit_bid, exit_ask):
    """Observations on every session day: first-day 9:35/11:00/15:55 at the
    entry prices, last-day 9:35/15:55 at the exit prices, mid-days at the
    midpoint. Each kind picks its own endpoints from these (open/close,
    cheapest, best-exit); the producer decides, the test only compares."""
    obs = [ob(days[0], 9, 35, entry_bid, entry_ask),
           ob(days[0], 11, 0, entry_bid, entry_ask),
           ob(days[0], 15, 55, entry_bid, entry_ask)]
    for day in days[1:-1]:
        mid_bid = round((entry_bid + exit_bid) / 2, 2)
        mid_ask = round((entry_ask + exit_ask) / 2, 2)
        obs.append(ob(day, 12, 0, mid_bid, mid_ask))
    if len(days) > 1:
        obs += [ob(days[-1], 9, 35, exit_bid, exit_ask),
                ob(days[-1], 15, 55, exit_bid, exit_ask)]
    return obs


def build_rows(win):
    days = [s.day for s in win.sessions]
    rows = []
    for ci, eb, ea, xb, xa in contract_specs():
        rows.append(w.evaluate(win, ci, contract_path(days, eb, ea, xb, xa), days))
    return rows


def write_variant(root: Path, win, state: str) -> Path:
    """Write one producer window; `state` is final or (genuine) partial."""
    days = [s.day for s in win.sessions]
    if state == "final":
        status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in days}
    elif state == "partial":
        status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in days}
        status[days[-1]] = {"final": True, "assessment": "partial", "sweeps": 6}
    else:
        raise ValueError(f"parity covers final/partial only, not {state}")
    sources = [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "a" * 64, "bytes": 123}
               for d in days]
    return outputs.write_window(root, win, build_rows(win), w.WindowPolicy(), status, sources,
                                f"{days[-1]}T20:00:00Z",
                                {"mode": "test", "underlyings": ["XLU", "ABC", "BBB", "CCC"]})


def manifest_rows(window_dir: Path) -> dict:
    """Producer truth for one window dir: rows/counts/detail as plain strings."""
    summary = json.loads((window_dir / "summary.json").read_text(encoding="utf-8"))
    with open(window_dir / "rankings.csv", encoding="utf-8", newline="") as f:
        ranked = list(csv.DictReader(f))
    rows = {b: [{k: r[k] for k in ROW_FIELDS} for r in ranked if r["basis"] == b]
            for b in BASES}
    with gzip.open(window_dir / "paths.csv.gz", "rt", encoding="utf-8", newline="") as f:
        paths = {r["symbol"]: r for r in csv.DictReader(f)}
    detail = {}
    for basis in BASES:
        top = rows[basis][0] if rows[basis] else None
        if top is None:
            detail[basis] = None
            continue
        prow = paths[top["symbol"]]
        detail[basis] = {"symbol": top["symbol"],
                         **{f: prow[f"{basis}_{f}"] for f in DETAIL_FIELDS}}
    return {"state": summary["state"], "state_reasons": summary["state_reasons"],
            "counts": summary["counts"], "rows": rows, "detail": detail}


def all_windows():
    weekly, _ = w.weekly_windows(SESSIONS)
    by_id = {x.window_id: x for x in weekly}
    by_id.update({x.window_id: x for x in w.overnight_windows(SESSIONS)})
    by_id.update({x.window_id: x for x in w.multi_session_windows(SESSIONS)})
    return by_id


# (kind, window_id, state) — every supported kind, final + genuine partial.
VARIANTS = [
    ("weekly_cheapest", "weekly-2026-W41-cheapest", "final"),
    ("weekly_cheapest", "weekly-2026-W41-cheapest", "partial"),
    ("weekly_fixed", "weekly-2026-W41-fixed", "final"),
    ("weekly_fixed", "weekly-2026-W41-fixed", "partial"),
    ("weekly_cheapest_best_exit", "weekly-2026-W41-cheapest_best_exit", "final"),
    ("weekly_cheapest_best_exit", "weekly-2026-W41-cheapest_best_exit", "partial"),
    ("overnight", "overnight-2026-10-05_2026-10-06", "final"),
    ("overnight", "overnight-2026-10-07_2026-10-08", "partial"),
    ("sessions_2", "sessions2-2026-10-05_2026-10-06", "final"),
    ("sessions_2", "sessions2-2026-10-07_2026-10-08", "partial"),
    ("sessions_3", "sessions3-2026-10-05_2026-10-07", "final"),
    ("sessions_3", "sessions3-2026-10-07_2026-10-09", "partial"),
]


class ParityBridge(unittest.TestCase):
    def test_all_kinds_bases_states_match_consumer(self):
        by_id = all_windows()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            manifest = {"version": 1, "variants": []}
            for i, (kind, window_id, state) in enumerate(VARIANTS):
                win = by_id[window_id]
                self.assertEqual(win.kind, kind)
                emit = tmp / f"emit-{i}"
                window_dir = write_variant(emit, win, state)
                summary = json.loads((window_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["state"], state, f"{kind}/{window_id}")
                if state == "partial":
                    self.assertTrue(summary["state_reasons"], f"{kind}/{window_id}")
                store = tmp / f"store-{i}"
                backend = pub.LocalFilesystemBackend(store)
                result = pub.publish_window(emit, kind, window_id, backend, now=NOW)
                self.assertEqual(result["status"], "published", f"{kind}/{window_id}")
                self.assertEqual(result["state"], state)
                prefix = f"oa203/windows/oa203-windows-v1/{kind}/{window_id}"
                # Producer -> publisher equality, member by member.
                for name in pub.WINDOW_MEMBERS:
                    self.assertEqual((store / prefix / name).read_bytes(),
                                     (window_dir / name).read_bytes(), f"{kind}/{window_id}/{name}")
                pointer = json.loads((store / prefix / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(pointer["state"], state)
                self.assertEqual(pointer["members"],
                                 {n: sha((window_dir / n).read_bytes()) for n in pub.WINDOW_MEMBERS})
                entry = {"kind": kind, "window_id": window_id, "root": str(store),
                         **manifest_rows(window_dir)}
                manifest["variants"].append(entry)
            kinds = {(v["kind"], v["state"]) for v in manifest["variants"]}
            self.assertEqual(len(manifest["variants"]), 12)
            self.assertEqual(len(kinds), 12)
            for v in manifest["variants"]:
                for basis in BASES:
                    self.assertTrue(v["rows"][basis], f"{v['kind']} {basis} has no rows")
                    self.assertIsNotNone(v["detail"][basis], f"{v['kind']} {basis} has no detail")
            manifest_path = tmp / "parity-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            check_node_ok(run_node_parity(manifest_path), "all-kinds parity")


if __name__ == "__main__":
    unittest.main()
