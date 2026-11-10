# #130 window publisher (F2) — offline implementation

Status: implemented and tested offline (fake + local-filesystem backends
only). No push, no PR, no deployment, no live R2 publication, no activation.
Chej owns review/release.

## What was built

- `market_data/options_scanner/windows_publish.py`: validates one local
  `oa203/windows/` window against the consumer's exact contract
  (`market_mcp/consumer.js` `windowSummary` + rankings + paths-detail
  checks) and publishes it to a pluggable storage backend.
- `tests/test_oa203_windows_publish.py`: 20 tests over a fake in-memory
  backend and a local-filesystem backend. No network, no credentials, no
  live R2 in any test.
- The real R2 backend reuses the scanner's existing upload client
  (`market_data.tradier.probe` via `market_data.options_scanner._probe`:
  `r2_client()` + `put_bytes_verified()`); no new credentials, host, or
  secrets. It is constructed only by `r2_backend()` and is never used in
  tests.

## Exact R2 layout

```
oa203/windows/oa203-windows-v1/<kind>/<window_id>/
  summary.json            # flat consumer keys (paths, rankings, summary last)
  rankings.csv
  paths.csv.gz
  rev_<sha256(summary.json)>/summary.json
  rev_<sha256(summary.json)>/rankings.csv
  rev_<sha256(summary.json)>/paths.csv.gz
  latest.json             # pointer to the current revision (operators/audit)
  latest-attempt.json     # outcome of the last reserved attempt
  budget/<rev>-<attempt>.json   # conditional reservation tokens (1..3)
```

The consumer reads only the three flat keys
(`market_mcp/source.js` allowlist); `rev_*/`, `latest.json`,
`latest-attempt.json` and `budget/` are never queried by the consumer.
The revision copy is immutable: a corrected republication writes a new
`rev_<sha>/` and moves the pointer, so the old revision stays pinned and
auditable. The flat keys are what the consumer reads, so they are
overwritten on republication; the consumer's before/after digest check
detects that rebuild and asks for a fresh query.

Upload order: revision members → flat members (paths, rankings, summary
last, so an interruption leaves either the old complete window or a new
one the consumer rejects on its digest check, never a mixed ranking) →
`latest.json` pointer last → `latest-attempt.json` ledger. Every upload
is verified by a full readback (GET + SHA-256), not just the uploader's
HEAD length check. A readback mismatch fails closed with no pointer
written; already-uploaded revision members are immutable and harmless.

## States (decision, recorded explicitly)

- `final` and `partial` windows are published with their true state. A
  partial window is never marked final.
- `in_progress` windows are **refused entirely**: no member is published,
  not even a summary-only one. A half-populated window directory is a
  footgun (the consumer serves in-progress as summary-only with no rows,
  and a later final publish must overwrite it); an in-progress window
  becomes publishable when its sessions finalize and it is re-emitted.
- A window is published only with all three members present and valid.
  A missing `paths.csv.gz` is a refusal, not a rankings-only partial
  publish: the consumer tolerates a missing paths file at read time, but
  the publisher never creates that state.

## Validation (mirrors the consumer, plus full-universe proofs)

The publisher holds the full universe in `paths.csv.gz`, so it proves
things the consumer cannot:

- Policy fixed to `oa203-windows-v1`; `close_tolerance_s == 900`,
  `open_tolerance_s == 900`, `qualified_endpoint_policy.max_quote_age_s
  == 1800`. Window-id shape per kind, and id/session agreement (weekly
  id names its ISO week; span ids name their first/last session days).
- Per-basis counts: `eligible + excluded == contracts_evaluated`,
  `shortlist == min(200, eligible)`, exclusion-reason sums match
  `excluded`. Hindsight label required for `weekly_cheapest` /
  `weekly_cheapest_best_exit`.
- Rankings: exact header, grouped by basis in producer order, per basis
  exactly ranks `1..shortlist`, pct-descending with symbol tiebreak,
  negative-count rule, OCC identity cross-check, pct arithmetic within
  `2e-6`.
- Paths: expanded size at most 24 MiB (the consumer ceiling), exact
  header, row count equals `contracts_evaluated`, symbols unique
  (conflicting attributions for one symbol fail closed with no canonical
  path asserted), per-basis eligible/excluded counts **and**
  exclusion-reason breakdowns recomputed from the full universe match
  the summary exactly, every ranked row matches its paths row on basis
  status / pct / entry / exit / sessions, endpoints inside their own
  sessions with exit after entry, quote stamps at or before observation
  time and not before 2000-01-01, quote ages non-negative, path
  arithmetic within `2e-6` (pct) / `1e-3` (per-contract change).

Any failure raises `ValueError` before any upload; a validation refusal
uploads nothing at all, including no ledger entry.

## Idempotency and attempts

- A retry of the same window (same summary SHA-256) returns
  `already_published` with the same revision and uploads nothing, once
  the remote pointer and revision members are verified to match. A
  pointer with the same revision but different member digests fails
  closed (tampered pointer).
- Each revision gets at most 3 attempts, reserved by conditional
  `budget/<rev>-<attempt>.json` tokens (`IfNoneMatch: *`), mirroring
  `active_budget.py`. A failed attempt after reservation is recorded in
  `latest-attempt.json` (best effort); a failure to write the ledger
  after a successful publication is reported in the result, not treated
  as a failed publication.

## Retention / budget proposal (not a live approval)

Mirrors `active_budget.py`'s shape with numbers sized for windows
instead of intraday snapshots. These are proposals for Chej/Owen to
approve, not an approved budget.

| Bound | Proposed value | Reasoning |
| --- | --- | --- |
| Per-member bytes | summary ≤ 1 MiB, rankings ≤ 4 MiB, paths ≤ 4 MiB compressed / 24 MiB expanded | Consumer artifact (4 MiB) and expanded (24 MiB) ceilings |
| Reservation per revision | 20 MiB | Flat members (≤ 9 MiB) + immutable revision copy (≤ 9 MiB) + pointer/ledger |
| Global cap | 8 GiB | Matches the scanner's reviewed bound |
| Retention | 90 days | Expired revisions, flat members, pointers and ledgers deleted together; deletion re-verified by re-listing |
| Attempts | 3 per revision | Same as active snapshots |
| Revisions per window | 16 | Corrections stay bounded; beyond that, manual reconciliation |
| Inventory | 1,000 keys per kind prefix, 32,000 total | Same as active; truncated listings and unknown keys fail closed |

The byte cap binds before retention for full-universe windows near the
per-member ceilings (~400 revisions at the cap), so exhaustion refuses
new publications until cleanup or an approved raise — it never silently
deletes live data. There is no legacy data under `oa203/windows/`, so no
policy-start floor is needed (unlike the scanner prefix).

## What remains for live publication (release gate, unapproved)

1. **Chej/Owen approval** of this publisher, its state decisions, and the
   budget/retention numbers above.
2. **OA203-style pre-checks** before any live R2 write: regression tests
   green (`tests/test_oa203_windows_publish.py`,
   `tests/test_oa203_windows.py`, `tests/test_oa203_windows_replay.py`,
   `node --test market_mcp/*.test.js`); a bounded builder for the
   publish run; an R2 conditional reservation, readback verification,
   and delete of a trial window; a verified deployed revision; and a
   saved activation flag naming the first window to publish.
3. **Live R2 verification**: published keys readable at the public
   origin; `window_rankings` for a real window (all three bases) plus
   pinned `result_detail` from an attached client once market_mcp is
   deployed (Chej-owned release).
4. **Connected-client check**: same as the consumer's remaining item 3
   in `docs/131-window-consumer.md`.

Until the gate passes, `windows_publish.py` is offline-only: use the fake
or local-filesystem backend, never `r2_backend()`.
