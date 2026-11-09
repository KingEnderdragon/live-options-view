# Archive qualification: bounded public reads, listing gap, and the three parity dates

Issue #146 asks for a reproducible coverage and schema qualification of the
retained archives, and specifically for the three reported option/underlying
discrepancies. This note separates what was measured here from prior reports
and from what remains unretrievable without a listing.

## Access: bodies yes, listing no

The public read-only origin `https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev`
serves object GET and byte ranges (verified `206 Partial Content` with
`Content-Range`, `ETag`, `Last-Modified`). It does **not** serve a listing:
`/?list-type=2&max-keys=1` returns `404`, and the anonymous S3 endpoint
(`https://<account>.r2.cloudflarestorage.com/<bucket>?list-type=2`) fails the
TLS handshake. No credentials exist in the launch environment. The precise
remaining gap is therefore a `ListObjectsV2` route (read-only token or a
manifest dump); absence of a listing is not evidence that bodies are missing.

Bounded reads performed (all cached under `research/archive_evidence/`, which
is git-ignored except for the two summary JSONs):

| Key | HTTP | Bytes | Evidence |
|---|---|---|---|
| `manifest.json` | 200 | 23,700 | 176 dates, 2025-12-29 → 2026-09-18 |
| `raw/qqq_chain_20251229.csv` | 200 | 73,609 | ETag `2036dd…022f`, written 2026-06-26 (re-backfill) |
| `raw/qqq_chain_20260916.csv` | 200 | 154,246 | ETag `f20df8…f78a`, written 12:01 GMT on the trade date |
| `raw/qqq_chain_20260918.csv` | 200 | 128,889 | ETag `ff07b9…3c22`, written 12:02 GMT on the trade date |
| `prices/qqq_intraday_20260915.csv` | 200 | 7,918 | supporting prior-session bars |
| `prices/qqq_intraday_20260917.csv` | 200 | 8,028 | supporting prior-session bars |
| `prices/qqq_intraday_20251229.csv` | 404 | — | pre-pipeline date; no bars retained |
| `raw/qqq_chain_20260903.csv` | 404 | — | not in the manifest either, so not an inconsistency |

The manifest's date index is not proof an object exists, but the sampled keys
below all resolved. The manifest gap Aug 28–Sep 7 is real in the index; 2026-09-03
is absent from it.

## Sampled intraday qualification (not full-day coverage)

The only recovered key list is a prior sampled read of 180 intraday keys
(`research/synthetic_days/tests/fixtures/r2_cache/intraday/_listing.json`), 30
snapshots on each of six days (2026-08-03…2026-08-10). All 180 bodies were
downloaded and their MD5 matched their `ETag`; no body was missing.

Running `research/archive_qualification.py` over them (report:
`research/archive_evidence/intraday_sample_qualification.json`) gives one schema
signature on every day — 35 columns, including the macro strip (`QQQ`, `VIX`,
`10Y`, …) but **no** quote receipt, provider observation, price-basis or
staleness column. No `UnderlyingPrice` or `Bid` values were missing in the
sample, and no checksum failed.

These coverage numbers are sample artifacts and must not be read as session
coverage: 30 snapshots per day cannot fill 390 regular-session slots. The
report says exactly that. It supports the D1 provenance gap and the schema
question for these six days only; it does not reproduce Jayden's 33,171-body
RTH count, which remains a prior finding to check against a full listing.

## The three parity dates: a within-close level offset, not a strike error

Method (reproduced by `research/historical_archive_parity.py --self-test` and
`--cache research/archive_evidence`, output
`research/archive_evidence/three_date_parity.json`): European put-call parity at
zero rate and zero time-to-expiry, same strike and expiry, no dividend
adjustment — `S_implied = mid_call − mid_put + strike` at the strike nearest the
recorded `UnderlyingPrice`, plus the same calculation across ±3 strikes.

**Producer convention first.** The chain for trade date D is written before D's
open (12:0x GMT = 08:0x ET on D) and carries the **prior session's** settled
data. Stored `UnderlyingPrice` matches the D−1 15:55 ET bar close within 0.03–0.05
on four independently checked dates (2026-08-20, 2026-09-10, 2026-09-16,
2026-09-18). Same-day close comparisons are 5–8 points away and are not the
matched value. 2025-12-29 was re-backfilled on 2026-06-26 and has no retained
bars, so its write-time convention is unverified.

With that correction:

| Date | Stored | ATM | Implied (mid) | Offset | Offset spread over ±3 strikes | Prior close | Stored − prior close |
|---|---|---|---|---|---|---|---|
| 2025-12-29 | 622.4203 | 622 | 620.89 | −1.5303 | 0.075 | unknown (no bars) | unknown |
| 2026-09-16 | 704.54 | 705 | 705.545 | +1.0050 | 0.025 | 704.59 | +0.05 |
| 2026-09-18 | 716.92 | 717 | 716.545 | −0.3750 | 0.020 | 716.87 | −0.05 |

Bid/ask midpoints and `Last` give the same offset to within 0.01–0.02, so the
result is not a `Mid`-column artifact. The offset is nearly constant across
strikes, so it is a level difference between the recorded underlying and the
option mids within one close snapshot, not a bad ATM selection or a rounding
error.

**What the offset is consistent with, and what is not proved.** The chain has no
option-quote timestamps. The recorded underlying matches the D−1 15:55 bar, while
the option mids imply a level that on those two dates sits at several different
bars within 0.05 (7–8 bars each), so bar matching cannot assign a capture time;
the 5-minute bar file also stops at 15:55 and does not contain the closing print.
A non-simultaneous option/underlying capture at or after the close is a
consistent explanation, especially on 2026-09-16 where the implied level is 1
point above the 15:55 bar. It is not proved, and the 2025-12-29 case cannot even
be checked for bars. This supersedes the earlier same-day-bar reading, which
compared a pre-open chain to the wrong session.

The displayed ATM star follows the stored `UnderlyingPrice`; because the stored
level and the option-implied level straddle a strike boundary on 2025-12-29
(622 vs 620.89), the star can sit one strike from the option-implied level. That
is a display consequence of the offset, not a separate calculation error.

## Qualification rules and permitted use

- Calendar membership is not usable coverage; a folder must show slot coverage
  and content. A missing folder is a coverage gap, not a diagnosed cause.
- An unchanged underlying value is not proof of a stale price; a changed value
  is not proof of freshness. The historical chain lacks the timestamps needed to
  decide, so timing stays `unknown` except where bars constrain it.
- A bar derived from sampled collector prices is a derived/sampled bar, never the
  original market OHLCV bar.
- No backfill may invent historical timestamps or freshness. The 2025-12-29
  discrepancy is reported as a level offset with unknown timing, not repaired.
- Raw downloaded objects stay out of version control; the two summary JSONs and
  the scripts are the committed evidence.

## Remaining #146 archive work

1. Full `ListObjectsV2` (or a producer-dumped manifest of keys) for
   `intraday/`, `intraday/spy/`, `raw/`, `prices/` to reproduce total folder and
   slot coverage and to test the 33,171-body claim.
2. Object-level schema census across all folders, especially the reported
   reduced first-of-day schema and the June 23 development day.
3. A retained option-quote timestamp would be required to prove which leg of the
   three parity cases is stale; the current archive cannot.
