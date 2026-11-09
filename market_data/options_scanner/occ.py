"""OCC daily contract volume: fetch, parse, rank the day's underlyings.

The scanner's universe starts from where the volume actually was, not from a
fixed list, so this is the one external (non-Tradier) data source it reads.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date

import requests

from . import runtime
from .runtime import OCC_VOLUME_URL


# --------------------------------------------------------------------------
# OCC volume ranking
# --------------------------------------------------------------------------

def fetch_occ_volume(report_date: date, session: requests.Session | None = None) -> bytes:
    params = {
        "reportDate": report_date.strftime("%Y%m%d"), "format": "csv",
        "volumeQueryType": "O", "symbolType": "ALL", "reportType": "D",
        "accountType": "ALL", "productKind": "ALL", "porc": "BOTH",
    }
    http = session or requests.Session()
    response = http.get(OCC_VOLUME_URL, params=params, timeout=120,
                        headers={"User-Agent": "Mozilla/5.0 (OA-203 research scanner)"})
    response.raise_for_status()
    return response.content


def parse_occ_volume(body: bytes, report_date: date) -> dict[str, dict[str, int]]:
    """Per-underlying contracts from OCC's volume-by-account-type CSV.

    Raises if the file has no rows for ``report_date`` (not yet published,
    or an error page), rather than ranking on stale or empty data.
    """
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    expected = {"quantity", "underlying", "actype", "porc", "actdate"}
    if not reader.fieldnames or not expected <= {f.strip() for f in reader.fieldnames}:
        raise RuntimeError(f"OCC volume file has unexpected header: {text[:200]!r}")
    want = report_date.strftime("%m/%d/%Y")
    sides: dict[str, Counter] = defaultdict(Counter)
    seen = 0
    for row in reader:
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if row["actdate"] != want or not row["underlying"]:
            continue
        try:
            quantity = int(row["quantity"])
        except ValueError:
            continue
        seen += 1
        key = "calls" if row["porc"] == "C" else "puts" if row["porc"] == "P" else "other"
        sides[row["underlying"]][key] += quantity
    if not seen:
        raise RuntimeError(f"OCC volume file has no rows for {want}")
    # Each cleared contract is reported once per side (buyer and seller).
    return {u: {"contracts": sum(c.values()) // 2, "calls": c["calls"] // 2, "puts": c["puts"] // 2}
            for u, c in sides.items()}


def rank_underlyings(volume: dict[str, dict[str, int]]) -> list[tuple[str, dict[str, int]]]:
    return sorted(volume.items(), key=lambda kv: (-kv[1]["contracts"], kv[0]))


# --------------------------------------------------------------------------
