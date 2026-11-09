# Archive evidence (committed summaries and local cache)

This directory holds the committed evidence for the #146 archive work. The
objects themselves are not versioned: they are re-fetchable from the public
read-only origin and are ignored by `.gitignore` here.

Committed:

- `three_date_parity.json` - `research/historical_archive_parity.py` output for
  2025-12-29, 2026-09-16 and 2026-09-18: stored underlying, ATM strike,
  parity-implied spot, offset and its spread across nearby strikes, prior and
  same-day bar evidence, write times and ETags.
- `intraday_sample_qualification.json` - `research/archive_qualification.py`
  output over the 180-key sampled intraday listing (30 snapshots/day on six
  days). Its `sample_note` says explicitly that this is not full-day coverage.

Re-fetch the cached inputs:

```sh
python research/archive_sample_fetch.py          # 180 sampled intraday bodies
python research/historical_archive_parity.py --cache research/archive_evidence
python research/archive_qualification.py --dir research/archive_evidence/intraday_bodies/intraday \
    --bodies research/archive_evidence/intraday_bodies --json-out /tmp/report.json
```

The public origin serves GET and byte ranges but not `ListObjectsV2`; that is
the remaining listing gap, not a body-access failure.
