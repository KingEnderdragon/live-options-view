# PR134 review and live acceptance record

This draft implements #130's offline windows and a disabled-by-default,
two-chain SPY/QQQ collection option. It does not publish windows or change the
running collector. Daily Banana/QQQ/SPY output names and the existing nearest
chain remain intact. The second chain makes at most two additional chain
requests per sweep, only when SPY/QQQ are already in the selected universe and
a later listed Friday exists. At 500 nearest chains this is at most 0.4% more
chain requests; row/storage growth depends on actual contract counts and must
be measured before activation. The rate limiter remains the existing one.

## Decision status

- D1/D2: first trading session's lowest qualifying sampled ask. This is
  hindsight, not an entry-time signal. `weekly_fixed` is the separately named
  first qualifying near-open ask comparison.
- D3/D4: latest qualifying sampled bid in the last trading session's close
  window, including Thursday in a Friday holiday week.
- D5: consecutive trading sessions include weekend and holiday overnight gaps.
- D7: 900-second pre-close collection tolerance approved. Selection uses the
  latest qualifying observation, never an after-close one. The independent
  1800-second limit on each bid and ask quote timestamp is unchanged. The
  900-second post-open collection tolerance remains provisional.
- D8: one extra expiry for SPY and QQQ only. The opt-in rule chooses the first
  listed Friday **after the nearest expiry**. On Friday 2026-10-16 it selects
  2026-10-23; on Monday 2026-10-19 it still selects 2026-10-23. If a Friday
  series is not listed, it records the resulting coverage gap rather than
  adding a third chain or changing symbols. The flag is
  `OA203_SPY_QQQ_SECOND_EXPIRY=true`, default false pending deployment review.
- D6 rolling two/three-session grouping, D9 expiration-day PM treatment,
  and the opening tolerance remain technical-review gates. The current rolling
  calculation uses first-session near-open to last-session near-close; it is
  never labeled owner-approved. AM-settled roots are excluded on expiry day.
  Confirm last-tradable and settlement treatment per series before release.

## Evidence limits and reproduction

`scripts/oa203_windows_replay.py` is offline and read-only. It accepts only
final, complete daily summaries, contiguous non-truncated manifest sweeps and
exact NYSE calendar bounds. It rejects skipped sessions and suppresses weekly
results unless the full calendar week is supplied. It writes SHA-256 hashes of
every fetched artifact to `source_lock.json`; rerun with `--source-lock` to
detect amended manifests or sweeps. `--emit-root` is local-only; bounded
underlying replays are labeled `partial`, because they do not rank the entire
daily universe. The summary and path files include exact endpoints and
bid/ask-side ages. Do not use these files as a live deployment check.

For the optional extra expiry, each expected per-sweep chain status contributes
to the session's success denominator even when omitted. Failed or missing
statuses and zero-row successes make the session partial, with separate extra
chain success and expiry coverage fields. Replay cross-checks each archived
row's selected expiry, collection role, successful manifest status and exact
per-chain row count; a stray row cannot supply continuity evidence.

All window bases reject a two-sided quote with a missing, malformed or
future-dated bid/ask timestamp. The `mid` and `exec` descriptive comparisons
may still include an old quote; the separately named qualified basis applies
the unchanged 1800-second age limit to each side. Replay requires monotone
millisecond sweep bounds, the row time equal to its matching chain fetch time,
and that time inside the sweep's inclusive start/end interval. Only rows in
the market session's half-open `[open, close)` interval enter window evidence;
an acquisition at the close is counted for archive reconciliation but cannot
become a closing endpoint or certify a sampled session.

Example, using only public archives and a local output directory:

```sh
python scripts/oa203_windows_replay.py --days 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09 --underlyings SPY QQQ PLTR --out replay-a
python scripts/oa203_windows_replay.py --days 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09 --underlyings SPY QQQ PLTR --out replay-b --source-lock replay-a/source_lock.json
```

## Post-merge live checklist (requires separate merge/deployment approval)

1. Finish #132 MCP rollout and live acceptance first. Pin merged commit,
   collector configuration, calendar package/version, finalized trading dates,
   universe and summary files, manifest cutoff, and SHA-256 of every sweep.
2. Verify only SPY and QQQ gained one additional chain each, that the nearest
   chain and ordinary daily outputs remain present, and measure actual chain
   requests, sweep duration, rate-limit headroom and storage growth.
3. Trace the same SPY/QQQ OCC symbols and expiries from Friday through Monday
   and across a holiday. Record any absent chain, failed sweep or dropped
   expiry explicitly; never stitch across series.
4. On identical frozen contract inputs, run the window evaluator twice with
   `WindowPolicy(close_tolerance_s=600)` and `900`; keep every other parameter
   fixed. Count eligible contracts in each, newly gained contracts and percent
   gained, and remaining exclusions by reason.
5. For gained contracts, publish intended and observed endpoint times,
   collection lead before close, bid and ask quote timestamps and their
   separate ages, selected prices and return changes. Verify each side is at
   most 1800 seconds old and no observation is after close. Previously eligible
   contracts must retain the same selected prices and returns; their ranks may
   move as coverage expands.
6. Preserve complete and partial outputs, non-winners, source lock, policy
   values, code commit and exclusion lists. Review the 10-versus-15-minute
   result with the owner before deciding whether to retain the extension.

No live collection, publication or provider probe is part of PR review.
