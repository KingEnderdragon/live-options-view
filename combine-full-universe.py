#!/usr/bin/env python3
"""Combine #130 full-universe batch replays into globally ranked outputs.

Reads the 14 bounded batch replays (each <=50 underlyings, covering the
union of all selected archived underlyings for Oct 5-7, 2026), concatenates
all contract rows per window, globally re-ranks before any shortlist,
retains losses/exclusions, and asserts no duplicate/missing contracts.

Ranking matches windows.rank_window: eligible rows by qualified pct
descending, ties broken by symbol ascending; excluded rows keep rank empty
and their reasons. Every row is retained.

Usage: python3 combine-full-universe.py [--batches-dir DIR] [--out DIR]
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

WINDOWS = [
    "overnight-2026-10-05_2026-10-06",
    "overnight-2026-10-06_2026-10-07",
    "sessions2-2026-10-05_2026-10-06",
    "sessions2-2026-10-06_2026-10-07",
    "sessions3-2026-10-05_2026-10-07",
]
COLUMNS = ["rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry", "exit",
           "abs_change_per_contract", "entry_session", "exit_session", "entry_ms", "exit_ms", "flags", "status", "reasons"]


def pct_of(row):
    try:
        return float(row["pct"])
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batches-dir", type=Path, default=Path("replay-130-full"))
    ap.add_argument("--out", type=Path, default=Path("replay-130-full-combined"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    batch_dirs = sorted(d for d in a.batches_dir.iterdir() if d.is_dir() and d.name.startswith("batch-"))
    if not batch_dirs:
        print("no batch dirs found", file=sys.stderr)
        return 1
    print(f"batches: {len(batch_dirs)}", flush=True)

    # Per-batch contract counts (from each batch's summary) for the
    # no-missing assertion.
    expected_total = 0
    for d in batch_dirs:
        s = json.loads((d / "replay_summary.json").read_text())
        expected_total += s["contracts"]
    print(f"expected total contracts (sum of batch summaries): {expected_total}", flush=True)

    report = {"batches": len(batch_dirs), "expected_total_contracts": expected_total,
              "windows": {}, "files": {}}
    for win in WINDOWS:
        rows = []
        for d in batch_dirs:
            f = d / f"{win}.csv"
            with open(f, newline="") as src:
                rows.extend(csv.DictReader(src))
        # Deduplicate by OCC symbol. The same symbol can be attributed to
        # two underlyings in the source archive (a data-quality finding:
        # PSKY and SKYD share 54 SKYD-rooted symbols — the same contract
        # appears under both the PSKY chain and the SKYD chain). A contract
        # must be ranked once. Keep the row whose underlying matches the
        # symbol root (the canonical attribution); the other attribution
        # is the source-data quirk. Record how many were deduped.
        by_symbol = {}
        deduped = 0
        for r in rows:
            sym = r["symbol"]
            if sym in by_symbol:
                deduped += 1
                keep = by_symbol[sym]
                # Prefer the row whose underlying matches the symbol root.
                root = sym[:-15].strip()
                if r["underlying"] == root and keep["underlying"] != root:
                    by_symbol[sym] = r
                # Otherwise keep the first (they are the same contract).
                continue
            by_symbol[sym] = r
        rows = list(by_symbol.values())
        assert len(rows) == expected_total - deduped, (
            f"{win}: row count {len(rows)} != expected {expected_total} - {deduped} deduped")
        if deduped:
            print(f"{win}: deduped {deduped} symbols shared across underlyings (PSKY/SKYD)", flush=True)
        # Global re-rank (qualified basis): eligible by pct desc, symbol asc.
        elig = sorted((r for r in rows if r["status"] == "eligible"),
                      key=lambda r: (-pct_of(r), r["symbol"]))
        for i, r in enumerate(elig, 1):
            r["rank"] = str(i)
        for r in rows:
            if r["status"] != "eligible":
                r["rank"] = ""
        ordered = elig + [r for r in rows if r["status"] != "eligible"]
        out_path = a.out / f"{win}.csv"
        with open(out_path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
            wr.writeheader()
            for r in ordered:
                wr.writerow({c: r.get(c, "") for c in COLUMNS})
        n_elig = len(elig)
        reasons = Counter()
        for r in rows:
            if r["status"] == "excluded":
                for why in (r.get("reasons") or "").split(";"):
                    why = why.strip()
                    if why:
                        reasons[why.split(":")[0]] += 1
        report["windows"][win] = {
            "contracts": len(rows), "eligible": n_elig,
            "excluded": len(rows) - n_elig,
            "deduped_symbols": deduped,
            "top_exclusion_reasons": dict(reasons.most_common(5)),
            "top": [{"rank": r["rank"], "symbol": r["symbol"], "pct": r["pct"],
                     "entry": r["entry"], "exit": r["exit"]} for r in elig[:5]],
        }
        print(f"{win}: {len(rows)} contracts, {n_elig} eligible, {len(rows)-n_elig} excluded", flush=True)

    # Hashes for every combined output CSV. (The summary's own hash is not
    # recorded inside itself; it is self-referential. Report the summary
    # hash from the file on disk.)
    for f in sorted(a.out.glob("*.csv")):
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        report["files"][f.name] = {"sha256": h, "bytes": f.stat().st_size}
    (a.out / "full_universe_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"combined -> {a.out}", flush=True)
    print(f"full_universe_summary.json sha256: {hashlib.sha256((a.out / 'full_universe_summary.json').read_bytes()).hexdigest()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
