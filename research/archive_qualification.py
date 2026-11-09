#!/usr/bin/env python3
"""Qualify the retained intraday snapshot archive: coverage, schema, provenance.

Issue #146's archive audit needs a reproducible inventory rather than another
prose reading of a sample. This script turns an object listing (S3/R2
ListObjectsV2 JSON, or a local directory laid out as `intraday/YYYYMMDD/`) into
a per-session qualification table:

- session vs non-session date folders, using the NYSE calendar;
- snapshots per folder and regular-session slot coverage (empty and
  underfilled slots, with early-close awareness);
- schema signatures, the day's first snapshot schema, and reduced/full
  switches;
- whether any archived column preserves quote/observation/basis provenance;
- UnderlyingPrice and bid coverage when object bodies are available;
- ETag/MD5 integrity for single-part objects.

Keys and ETags alone can answer the coverage questions; content questions are
reported as "not inspected" unless `--bodies` supplies the objects, so the
output never claims a check it did not perform.

    python research/archive_qualification.py --listing listing.json --bodies ./
    python research/archive_qualification.py --dir ./intraday
    python research/archive_qualification.py --self-test

Raw R2 access is the missing input in the current environment; see
`research/archive_qualification.md` for the exact access needed and the rules
this script encodes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "crassus"))

try:
    from crassus.exchange_calendar import is_holiday, session_close
except Exception:  # pragma: no cover - calendar module degrades independently
    def is_holiday(_d: date) -> bool:  # type: ignore[no-redef]
        return False

    def session_close(_d: date) -> time:  # type: ignore[no-redef]
        return time(16, 0)


SESSION_OPEN = time(9, 30)
PROVENANCE_MARKERS = ("quote_ts", "observed_at", "price_basis", "collector_received_at", "source")


def parse_key(key: str) -> tuple[str, str, date, str] | None:
    """(symbol, kind, date, slot) for an intraday snapshot key, else None."""
    parts = key.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "intraday":
        return None
    symbol = "QQQ"
    if parts[1] == "spy":
        symbol = "SPY"
        parts = [parts[0]] + parts[2:]
    if len(parts) < 3:
        return None
    folder, name = parts[1], parts[-1]
    if not (folder.isdigit() and len(folder) == 8):
        return None
    if not name.endswith(".csv") or not name.startswith("snapshot_"):
        return None
    try:
        day = datetime.strptime(folder, "%Y%m%d").date()
    except ValueError:
        return None
    slot = name[len("snapshot_"):][:4]
    return symbol, "snapshot", day, slot


def expected_slots(day: date) -> list[str]:
    close = session_close(day)
    end = datetime.combine(day, close)
    cursor = datetime.combine(day, SESSION_OPEN)
    slots = []
    while cursor < end:
        slots.append(cursor.strftime("%H%M"))
        cursor += timedelta(minutes=1)
    return slots


def _normalize_etag(value) -> str:
    return str(value or "").strip('"')


def qualify(entries: list[dict], bodies: dict[str, bytes] | None = None) -> dict:
    """Build the per-day qualification records from listing entries."""
    bodies = bodies or {}
    days: dict[tuple[str, date], dict] = defaultdict(lambda: {
        "snapshots": [], "etag_failures": [], "etag_checked": 0,
    })
    ignored = 0
    for entry in entries:
        key = entry.get("Key") or entry.get("key")
        if not key:
            continue
        parsed = parse_key(key)
        if parsed is None:
            ignored += 1
            continue
        symbol, _kind, day, slot = parsed
        record = days[(symbol, day)]
        record["snapshots"].append({"key": key, "slot": slot, "size": entry.get("Size"),
                                    "etag": _normalize_etag(entry.get("ETag") or entry.get("etag"))})

    records = []
    for (symbol, day), data in sorted(days.items()):
        snapshots = sorted(data["snapshots"], key=lambda s: s["key"])
        headers = []
        underlying_missing = 0
        bid_missing = 0
        content_inspected = 0
        for snap in snapshots:
            body = bodies.get(snap["key"])
            if body is None:
                continue
            content_inspected += 1
            if snap["etag"]:
                digest = hashlib.md5(body).hexdigest()
                data["etag_checked"] += 1
                if digest != snap["etag"]:
                    data["etag_failures"].append(snap["key"])
            text = body.decode("utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            fieldnames = tuple(reader.fieldnames or ())
            headers.append(fieldnames)
            first = next(reader, None)
            if first is not None:
                if not first.get("UnderlyingPrice"):
                    underlying_missing += 1
                if not first.get("Bid"):
                    bid_missing += 1
        session = day.weekday() < 5 and not is_holiday(day)
        expected = expected_slots(day) if session else []
        present = {s["slot"] for s in snapshots}
        covered = [slot for slot in expected if slot in present]
        missing_slots = [slot for slot in expected if slot not in present]
        counts = Counter(s["slot"] for s in snapshots if s["slot"] in expected)
        underfilled = [slot for slot, count in counts.items() if count <= 4]
        signatures = Counter(headers)
        modal = signatures.most_common(1)[0][0] if signatures else None
        first_schema = headers[0] if headers else None
        record = {
            "symbol": symbol,
            "date": day.isoformat(),
            "session": session,
            "snapshots": len(snapshots),
            "expected_slots": len(expected),
            "covered_slots": len(covered),
            "missing_slots": missing_slots,
            "underfilled_slots": sorted(underfilled),
            "schema_signatures": [{"columns": list(sig), "count": n}
                                  for sig, n in signatures.items()],
            "first_snapshot_schema_is_reduced": bool(
                modal and first_schema and len(first_schema) < len(modal)),
            "schema_switches": len(signatures) > 1,
            "provenance_columns_present": bool(modal and any(
                marker in column for column in modal for marker in PROVENANCE_MARKERS)),
            "content_inspected": content_inspected,
            "underlying_price_missing_rows": underlying_missing,
            "bid_missing_rows": bid_missing,
            "etag_checked": data["etag_checked"],
            "etag_failures": data["etag_failures"],
        }
        records.append(record)

    session_records = [r for r in records if r["session"]]
    return {
        "folders": len(records),
        "session_folders": len(session_records),
        "non_session_folders": len(records) - len(session_records),
        "missing_session_dates": _missing_dates(session_records),
        "content_inspected_days": sum(1 for r in records if r["content_inspected"]),
        "etag_failures": sum(len(r["etag_failures"]) for r in records),
        "ignored_entries": ignored,
        "records": records,
    }


def _missing_dates(records: list[dict]) -> list[str]:
    if not records:
        return []
    dates = sorted(date.fromisoformat(r["date"]) for r in records)
    missing = []
    cursor = dates[0]
    present = set(dates)
    while cursor <= dates[-1]:
        if cursor.weekday() < 5 and not is_holiday(cursor) and cursor not in present:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return missing


def _markdown(report: dict) -> str:
    lines = [
        "| date | symbol | session | snapshots | slots | missing slots | schema sigs | first reduced | provenance cols | content checked | ETag failures |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["records"]:
        lines.append(
            f"| {r['date']} | {r['symbol']} | {'yes' if r['session'] else 'no'} | "
            f"{r['snapshots']} | {r['covered_slots']}/{r['expected_slots']} | "
            f"{len(r['missing_slots'])} | {len(r['schema_signatures'])} | "
            f"{'yes' if r['first_snapshot_schema_is_reduced'] else 'no'} | "
            f"{'yes' if r['provenance_columns_present'] else 'no'} | "
            f"{r['content_inspected']} | {len(r['etag_failures'])} |")
    return "\n".join(lines)


def _self_test() -> int:
    """Build a tiny archive and assert the classifications that matter."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        day = date(2026, 7, 17)  # Friday
        saturday = date(2026, 7, 18)
        entries = []
        bodies = {}
        full_header = "TradeDate,Expiration,Strike,Type,OptionSymbol,OpenInterest,Bid,Ask,UnderlyingPrice,Volume,VolDelta"
        reduced_header = "TradeDate,Expiration,Strike,Type"

        def add(day_value: date, slot: str, header: str, body: str = "x"):
            key = f"intraday/{day_value.strftime('%Y%m%d')}/snapshot_{slot}000000.csv"
            raw = (header + "\n" + body + "\n").encode()
            entries.append({"Key": key, "Size": len(raw), "ETag": hashlib.md5(raw).hexdigest()})
            bodies[key] = raw

        add(day, "0930", reduced_header)
        add(day, "0931", full_header, "d,2026-07-17,600,Call,QQQx,10,1.0,1.2,600.1,5,5")
        add(day, "0932", full_header, "d,2026-07-17,600,Call,QQQx,10,1.0,1.2,600.1,5,5")
        add(saturday, "1200", full_header)
        report = qualify(entries, bodies)
        assert report["session_folders"] == 1, report
        assert report["non_session_folders"] == 1, report
        session_day = next(r for r in report["records"] if r["session"])
        assert session_day["first_snapshot_schema_is_reduced"], session_day
        assert session_day["schema_switches"], session_day
        assert "0933" in session_day["missing_slots"], session_day["missing_slots"]
        assert not session_day["provenance_columns_present"], session_day
        assert session_day["etag_failures"] == [], session_day
        # A corrupted body must be reported, not smoothed over.
        bad = dict(bodies)
        bad[next(iter(bad))] = b"tampered\n"
        assert qualify(entries, bad)["etag_failures"] == 1
        # Key-only mode claims no content checks.
        bare = qualify(entries)
        assert all(r["content_inspected"] == 0 for r in bare["records"]), bare
        assert "| 2026-07-17 |" in _markdown(report)
    print("archive qualification self-test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listing", help="S3/R2 ListObjectsV2 JSON (Contents list) or a bare list of objects")
    ap.add_argument("--dir", help="local directory holding intraday/YYYYMMDD/*.csv")
    ap.add_argument("--bodies", help="directory of downloaded objects mirroring the keys")
    ap.add_argument("--json-out", help="write the full report here")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    entries: list[dict] = []
    if args.listing:
        payload = json.loads(Path(args.listing).read_text(encoding="utf-8"))
        entries = payload.get("Contents", payload) if isinstance(payload, dict) else payload
    elif args.dir:
        base = Path(args.dir)
        for path in base.rglob("*.csv"):
            key = "intraday/" + path.relative_to(base).as_posix()
            entries.append({"Key": key, "Size": path.stat().st_size,
                            "ETag": hashlib.md5(path.read_bytes()).hexdigest()})
            if args.bodies:
                entries[-1]["_local"] = str(path)
    else:
        ap.error("one of --listing, --dir or --self-test is required")

    bodies: dict[str, bytes] = {}
    if args.bodies:
        body_root = Path(args.bodies)
        for entry in entries:
            key = entry["Key"]
            candidate = Path(entry["_local"]) if entry.get("_local") else body_root / key
            if candidate.exists():
                bodies[key] = candidate.read_bytes()

    report = qualify(entries, bodies)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))
    print(_markdown(report))
    print(
        "\nLimits: keys/ETags prove folder and slot coverage only. Without --bodies, "
        "schema, UnderlyingPrice/bid coverage and checksum claims are explicitly "
        "not inspected. Missing historical quote/basis fields cannot be recovered "
        "from these CSVs; report that as unknown, never backfilled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
