#!/usr/bin/env python3
"""Fetch the sampled intraday bodies that back the qualification evidence.

The only recovered intraday key list is the prior sampled listing cached at
`research/synthetic_days/tests/fixtures/r2_cache/intraday/_listing.json`
(30 snapshots on each of six days). This reads it, downloads each object from
the public read-only origin, verifies its MD5 against the response ETag, and
writes the bodies under `research/archive_evidence/intraday_bodies/` so
`archive_qualification.py --dir ... --bodies ...` can be re-run.

Read-only. No credentials. No object is written back.

    python research/archive_sample_fetch.py
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LISTING = ROOT / "research" / "synthetic_days" / "tests" / "fixtures" / "r2_cache" / "intraday" / "_listing.json"
DEST = ROOT / "research" / "archive_evidence" / "intraday_bodies"
ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev"


def fetch(key: str) -> tuple[str, str, str]:
    target = DEST / key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return key, hashlib.md5(target.read_bytes()).hexdigest(), "cached"
    request = urllib.request.Request(f"{ORIGIN}/{key}", headers={"User-Agent": "brian-archive-sample/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        etag = (response.headers.get("ETag") or "").strip('"')
    target.write_bytes(body)
    digest = hashlib.md5(body).hexdigest()
    return key, etag, digest


def main() -> int:
    keys = json.loads(LISTING.read_text())
    mismatches = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, expected, actual in pool.map(fetch, keys):
            if expected in ("cached", actual) or expected == actual:
                continue
            mismatches += 1
            print(f"ETag mismatch: {key} expected {expected} got {actual}")
    print(f"{len(keys)} sampled bodies available under {DEST}; {mismatches} ETag mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
