# Active Big Banana lookup (#131): design and open decisions

Issue: [#131](https://github.com/OwenTanzer/live-options-view/issues/131).
Companion: #130 (multi-session windows, draft PR #134), whose window contract
this lookup also serves.
Status: **design for review**. It contains no code, collection changes or
deployments.

Goal: Chej asks during the trading day "what leads Big Banana so far today?"
and gets an honestly labeled, as-of leaderboard built only from evidence
collected up to that time. It can then follow one result to its exact
evidence.

## Verified baseline

This comes from source on `master` and the public 2026-10-09 artifacts.

- **Evidence is already public during the session.** After every sweep,
  `run_day` pushes `sweeps/sweep_NNNN.jsonl.gz` and the appended
  `sweeps/manifest.jsonl` to R2. `universe.json` is pushed before the
  open. Rankings (`summary.json`, `leaderboard.csv`,
  `qualified_ask_bid_v1.csv`) are built only at finalization after the
  close.
- **Cadence and size.** On 2026-10-09 there were 77 sweeps with a median
  duration of 306 s. One sweep is about 1.5 MB gzip and about **20 MB
  expanded**.
- **The MCP's existing bounds** (`market_mcp/README.md`) are 12 source
  requests, 64 MiB of total query bytes, 24 MiB per expanded artifact, a
  15 s deadline and 256 KiB of output.
- **#131 forbids duplicating producer calculations in the consumer.** The
  ranking must come from `oa203_returns.py`'s own build.

## Options

| | Option | Feasibility | Verdict |
| --- | --- | --- | --- |
| A | MCP consumer rebuilds rankings on demand from raw sweeps | A mid-session build needs every sweep so far (≈40 × 20 MB), against a 64 MiB / 12-request / 15 s budget. It would also re-implement the producer calculation in JS. | **Not viable** |
| B | **Producer publishes immutable in-progress snapshots** using the same build code, and the MCP reads a pointer plus one snapshot | About 3–4 small requests per query and no consumer recalculation. The cost moves to the scanner (below). | **Recommended** |
| C | A separate on-demand builder service the MCP calls | New hosting, credentials and grants | Out of scope without separate approval |

## Proposed design (option B)

### Producer: in-progress snapshots

Every K completed sweeps (decision A1), the scanner builds the existing
outputs over observations **up to a cutoff**:

```
oa203/scanner/<date>/inprogress/sweep_<NNNN>/summary.json
oa203/scanner/<date>/inprogress/sweep_<NNNN>/leaderboard.csv
oa203/scanner/<date>/inprogress/sweep_<NNNN>/qualified_ask_bid_v1.csv
oa203/scanner/<date>/inprogress/latest.json      # pointer: sweep, cutoff_ms, digests, state
```

- **Same code, cut off at the boundary.** The snapshot calls
  `build_contract_rows(day_dir, policy, start_ms=open, end_ms=cutoff_ms)`,
  where `cutoff_ms` is the end time of sweep NNNN. **The manifest must be
  truncated to sweeps ≤ NNNN as well.** Today `chain_ok_sweeps` counts
  successes over the whole day's manifest. That is fine after the close, but
  it would leak later coverage into the `low_coverage` flag and the clean
  view of an earlier snapshot. This one-line truncation is a required change
  and gets a no-look-ahead test.
- **Immutable snapshots, moving pointer.** A snapshot directory is never
  rewritten. `latest.json` names the newest one and carries the SHA-256 of
  each member, the same pattern as the squeeze scanner's
  `latest.json` → `runs/<id>`. A reference returned to Chej is pinned to a
  snapshot, so later sweeps or finalization can't change its explanation.
- **State.** Each snapshot is `in_progress`, with the as-of cutoff and the
  sweeps included. The post-close `summary.json` stays the only `final`
  product. Snapshots never overwrite daily outputs.
- **It must never slow collection.** The build runs between sweeps under a
  hard time budget (decision A2). If the budget is exceeded, the snapshot is
  skipped and logged, and sampling continues. If the build is slow, the
  cadence drops; the sweep rate doesn't.

### Consumer: MCP tool

There are two ways to expose it (decision A3):

1. **Extend `return_rankings`** with `session: "current"` (resolved
   through the XNYS calendar, returning the actual session) and
   `snapshot: "final" | "latest" | <sweep>`. Existing calls stay the same
   because the default is `final`.
2. **A new `active_rankings` tool** with the same views and filters, so
   `return_rankings` stays strictly the finalized-session contract.

Either way, every response:

- uses only the pinned snapshot, with no rebuild or re-scoring
- reports the effective cutoff, the sweeps included, the snapshot age, the
  expected cadence, and any newer failed snapshot attempt
- reports `before_open`, `in_progress`, `closed_pending_final`, `final`,
  `holiday`, `missing` or `failed` explicitly. It **never falls back to
  yesterday** when today's snapshot is absent.
- states that the sweeps are not simultaneous, that results are sampled and
  that values are quote comparisons, not fills
- keeps the existing all/clean/qualified views, producer ranks and reasons,
  and bounds

Detail follows the existing `result_detail` path, reading only that
snapshot's sweeps up to its cutoff and the selected contract. It pages at
most 6 sweeps per request, as today.

**#130 windows:** once #130's producer publishes `oa203/windows/…`, a weekly
or multi-session window with a later exit session than today is returned as
`in_progress` (entry chosen, no final exit). The tool never invents a final
endpoint.

## Decisions for the owner

| ID | Question | Recommended default | Alternatives |
| --- | --- | --- | --- |
| A1 | Snapshot cadence | Every **6 sweeps** (about 30 min), plus one at the close before finalization | Every 3 sweeps (about 15 min); hourly |
| A2 | Build time budget between sweeps | Skip the snapshot if the build would exceed **60 s**. Measure the actual build time on a full day first. | A separate worker process with its own budget |
| A3 | MCP surface | **New `active_rankings` tool** (keeps `return_rankings` final-only) | Extend `return_rankings` with `session: "current"` and `snapshot` |
| A4 | Which views get snapshots | all, clean and qualified (the same three as final) | Qualified only, to cut build cost |
| A5 | Snapshot retention | Keep the day's snapshots. Optionally prune them after the session is final. | Keep only the latest |

## Required tests (beyond the issue's checklist)

- A snapshot at sweep N is identical whether it is built at N or rebuilt
  later from the archive with the cutoff at N (no future rows, no future
  manifest entries).
- The `low_coverage` and clean views at cutoff N don't change when sweeps
  after N are appended.
- Pointer-advance races: a reference pinned to snapshot N survives
  `latest.json` moving to N+6, and a mismatched digest returns
  `source_changed`.
- A slow build is skipped without delaying the next sweep's start.

## Not in scope

Continuous estimates, streaming, alerts, a higher sampling rate, automatic
trading, or any deployment by virtue of this document.
