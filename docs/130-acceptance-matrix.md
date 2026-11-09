# #130 offline acceptance matrix (A1/A2 end state)

Branch `nova/130-multi-session`, base master `96d59b48`. Inert, non-deploying.
Local head `b1bb6a5`; MSI single commit `19c05ddd` (to be updated with this
round's commits). No push, no PR, no deployment, no collector runs.

Chej engineering review (2026-11-09) confirms the v1 definitions below for
inert/offline use only. They are NOT Owen release approval. D1-D5, D7 and
bounded D8 remain owner-approved. Cheapest entry stays separately labeled as
retrospective hindsight. The 30-minute quote-side freshness limit is unchanged.

## Chej engineering-reviewed v1 definitions (offline/inert only)

- **D6 — rolling two/three-session windows (v1).** One window ending on every
  session; consecutive windows overlap and are labeled by their session span.
  `WindowPolicy.multi_session_mode` is `"rolling"` and `__post_init__`
  rejects anything else.
- **D9 — expiration-day exit allowed (v1).** A contract may exit on its
  expiration day, flagged `exit_on_expiration_day`. AM-settled roots (SPX,
  NDX, RUT, VIX) are excluded on their settlement day (no PM quotes).
- **Opening tolerance — 900 s (v1).** Open endpoint is the first valid
  observation within 15 minutes after the open, matching the owner-approved
  900 s close tolerance (D7).
- **Continuity algorithm (v1).** One OCC symbol end to end;
  `missing_session` / `expires_before_exit` / `am_settled_on_exit_day`
  exclusions; adjusted roots flagged. **Adjusted/unknown deliverables fail
  closed**: `adjusted_deliverable_unknown` excludes the contract (raw
  observations preserved in the row with the reason). `contract_size` is
  compared but does not establish the economic deliverable.

## Acceptance checkboxes → exact tests (all passing)

Suite: OA-203, **156 tests, OK** (local; MSI to be re-verified this round).
Run: `~/workspace/.tools/182-venv/bin/python -m unittest
tests.test_oa203_windows tests.test_oa203_windows_outputs
tests.test_oa203_windows_replay tests.test_oa203_return_scanner
tests.test_oa203_active tests.test_oa203_active_budget`
from the worktree root. Chej independently ran windows+replay: 33 passed /
15 subtests (prior round).

### Box 1 — Weekly and overnight fixtures reproduce the approved policy
(D1/D2/D3/D5, separate opening comparison, last-trading-day holiday rule,
weekend gaps, exact contract paths, endpoint arithmetic)
- `test_weekly_windows_normal_week` — D1/D2/D3 weekly construction.
- `test_friday_and_monday_holidays` — last-trading-day holiday rule.
- `test_short_week_is_skipped_not_shortened` — a short week is skipped,
  never shortened.
- `test_overnight_includes_weekend_gap_by_policy` — weekend gaps included
  by policy, excludable by flag.
- `test_cheapest_entry_to_friday_close` — D1 cheapest qualifying sampled
  ask as weekly entry.
- `test_fixed_and_best_exit_variants_stay_separate` — opening comparison
  (`weekly_fixed`) and hindsight best-exit (`weekly_cheapest_best_exit`)
  remain distinctly labeled; cheapest entry is retrospective hindsight.
- `test_close_endpoint_tolerance_and_no_interpolation` — endpoint
  arithmetic; a crossed quote is skipped, never blended.
- `test_15_minute_close_latest_qualifying_and_quote_side_age` — D7
  900 s close tolerance; 30-min (1800 s) quote-side freshness.

### Box 2 — Calendar tests (Friday/Monday holidays, weekend overnight gaps,
early close, DST, unavailable calendar coverage)
- `test_friday_and_monday_holidays`
- `test_overnight_includes_weekend_gap_by_policy`
- `test_early_close_endpoint`
- `test_dst_boundary_uses_each_sessions_own_open`
- `test_short_week_is_skipped_not_shortened`
- `test_calendar_rejects_missing_session_and_wrong_bounds`
  (replay) — a missing session or wrong session bounds raises; an
  incomplete week does not claim to be a full week. Unavailable calendar
  coverage fails closed, never fabricates a week.

### Box 3 — Expiring-before-exit, differing roots/deliverables, universe
changes, missing session/endpoint and delayed first quote cannot fabricate
a continuous hold
- `test_expires_before_exit_and_missing_session` — expires-before-exit and
  missing-session exclusions; `pct` is None when excluded.
- `test_expiration_day_rules` — AM-settled exclusion on exit day; PM-settled
  weekly roots allowed; expiration-day exit disallowed by policy flag.
- `test_adjusted_deliverable_fails_closed` — **new this round.** An
  adjusted root (e.g. XYZ1) is EXCLUDED (`adjusted_deliverable_unknown`),
  not merely flagged; raw observations preserved in the row. A standard
  root with the same shape is unaffected (eligible).
- `test_missing_observation_time_cannot_form_an_endpoint`
- `test_delayed_first_quote_makes_fixed_entry_missing` — delayed first
  quote makes the fixed entry missing, never interpolated.
- `test_future_or_missing_quote_times_never_enter_any_basis`
- `test_stale_is_distinct_from_impossible_future_evidence`
- Universe changes are the `missing_session` mechanism: a contract whose
  chain was not sampled on a session (it left the universe that day) is
  excluded with `missing_session:<day>`, never fabricated from partial data
  (`test_expires_before_exit_and_missing_session`).

### Box 4 — No-look-ahead (adding later observations cannot alter entry-time
qualification; fixed endpoint and hindsight best-flip distinctly labeled)
- `test_no_look_ahead_on_entry`
- `test_fixed_and_best_exit_variants_stay_separate`
- `test_overnight_rejects_future_entry_and_exit_sides`
- `test_future_or_missing_quote_times_never_enter_any_basis`
- `test_close_endpoint_tolerance_and_no_interpolation` — a later crossed
  quote never changes the exit.

### Box 5 — Quality/unknown/negative-return cases preserve the qualified
policy; full-universe ranking and legacy-output regressions pass
- `test_qualified_entry_skips_wide_spread` — qualified entry skips a wide
  spread.
- `test_full_universe_keeps_negative_and_excluded` — ranking keeps the
  full universe, including zero/negative returns and excluded contracts;
  deterministic tie-break.
- `test_shortlist_is_cut_after_full_ranking` (outputs) — the shortlist is
  cut only after the full ranking.
- `test_every_path_written_including_non_winners` (outputs) — every path
  is written, including non-winners; losses and exclusions are retained.
- `test_layout_and_summary`, `test_states`, `test_deterministic_content`
  (outputs) — legacy-output regression: layout, summary, states and
  deterministic content unchanged.
- `test_stale_is_distinct_from_impossible_future_evidence` — unknown vs
  stale are distinct; neither fabricates a result.

### Box 6 — A documented replay produces inspectable rankings with exact
evidence, coverage and calculation versions
- Real-data replay, Oct 5–7 2026, **top-50 underlyings by OCC rank** (the
  tool's `MAX_UNDERLYINGS=50` bound; see the limitation below). Output in
  `/tmp/replay-130-c` (this round). Prior 3-underlying sample in
  `/tmp/replay-130-b` (5 windows over 2,280 contracts, exit 0).
- **Top-50 results (15,536 contracts per window; losses and exclusions
  retained in every CSV):**
  - overnight Oct 5→6: 2,019 eligible / 13,517 excluded.
  - overnight Oct 6→7: 3,327 eligible / 12,209 excluded.
  - sessions_2 Oct 5→6: 2,058 eligible / 13,478 excluded.
  - sessions_2 Oct 6→7: 3,162 eligible / 12,374 excluded.
  - sessions_3 Oct 5→7: 1,934 eligible / 13,602 excluded.
  - Weekly W41 correctly skipped (incomplete week). Dominant exclusions:
    `missing_session` (universe reselected daily) and
    `expires_before_exit`. Excluded final sweeps recorded per day
    (Oct 5: 5,580 rows/50 chains; Oct 6: 16,682/150; Oct 7: 1,856/50).
  - Rows kept: Oct 5: 621,072; Oct 6: 632,168; Oct 7: 634,752.
  - Policy: `oa203-windows-v1`,
    `D1-D5_D7_D8_approved; D6_D9_opening_tolerance_continuity_chej_engineering_reviewed_offline_only`.
  - Calendar: NYSE, pandas_market_calendars 5.6.0.
- **Hashes (sha256, `/tmp/replay-130-c`):**
  - `replay_summary.json`: `58bd0f1d316c9af45dc5e119e72ce04cbd1e2a88b27511d270bc45830e63e79a`
  - `source_lock.json`: `72eeba5047ea6de307bf03228bb784a83da50b4e3d3eff5fd021a84a699f9846`
  - `overnight-2026-10-05_2026-10-06.csv`: `428aaa8a1111c40f94602e35386d8a0a785e7c1a8a5d36ea64d9ed349340b0ee`
  - `overnight-2026-10-06_2026-10-07.csv`: `edc65e7fde02150e74cc561c3e54d1e8e2e55f9f23cc234532536b7099c6c5f3`
  - `sessions2-2026-10-05_2026-10-06.csv`: `0905f0c0dd505f387eab7fdbaad446e574a744f06fedb92302e65d9973c71eb0`
  - `sessions2-2026-10-06_2026-10-07.csv`: `88ecd43ed1478cac7c9f22661258412da9518a86fe01a54ac3c527c02cfe40b4`
  - `sessions3-2026-10-05_2026-10-07.csv`: `6b75546f2e3a0f248e231c83b6f3414326869d0eea9b4febd9a05ce9ae053b2e`
- Replay tests: `test_extra_rows_require_successful_manifest_and_exact_count`,
  `test_sweep_bounds_inclusive_session_close_exclusive`,
  `test_row_after_matched_sweep_end_is_rejected`,
  `test_occ_identity_rejects_stitched_metadata`,
  `test_manifest_cutoff_requires_complete_contiguous_sweeps`,
  `test_truncated_final_sweep_rows_excluded`,
  `test_calendar_rejects_missing_session_and_wrong_bounds`.
- Each replay report records `policy` (version `oa203-windows-v1` and
  `decisions_status`), `window` (id, kind, sessions), `sources`
  (R2 prefix, manifest sha), and `excluded_final_sweep` rows/chains.
- **MSI replay limitation (environment, not code):** The top-50 replay command
  is documented above and is correct, but on the MSI the replay's HTTPS
  fetch fails with `SSL: CERTIFICATE_VERIFY_FAILED: certificate has
  expired` from the MSI's Python 3.14 `urllib`. The R2 certificate is
  valid: PowerShell `Invoke-WebRequest` on the same MSI returns HTTP 200,
  and the local Python replay succeeds. The MSI's Python CA bundle is
  stale/mismatched (certifi 2026.05.20 is installed; upgrading it did not
  resolve it). SSL verification is NOT bypassed. The MSI test suite
  (156 tests) passes, validating the code on the MSI; the replay evidence
  is the local run above, with hashes. Fixing the MSI's Python CA store
  is an environment task, not a #130 code change.

### Box 7 — Monthly evidence requirements and current historical limitations
documented
- **Monthly evidence requirements** (from the design doc, unchanged):
  re-run the replay for the new complete calendar week; confirm the D7
  close-tolerance behaviour on the new week's data; re-check the D8
  second-expiry selection for SPY/QQQ; re-run the OA-203 suite; record the
  new replay's source lock and report hash alongside the prior month's.
- **Current historical limitations (precise):**
  - No complete calendar week exists in the archive for a full weekly
    replay: Oct 5–7 are complete, Oct 8 is `assessment: partial`
    (71 sweeps), Oct 9 is not in the archive set checked. The weekly
    window is therefore validated only on deterministic fixtures, not on a
    real complete week. **The incomplete Oct 8 week is not invented.**
  - Deterministic full-week/holiday fixtures (Box 1/2 tests above) cover
    the weekly construction, holiday rule, weekend gaps, early close and
    DST. A real complete-week replay is a held check pending an archive
    week with all five sessions `assessment: complete`.
  - The replay's `MAX_UNDERLYINGS=50` bound (Box 6) limits a single run
    to the top 50 underlyings by OCC rank.

### Box 8 — D8 fixtures (checked, prior round)
- `test_d8_friday_to_monday_same_contract_continuity` — the Oct 23
  contract sampled as additional on Fri Oct 16 is the same contract
  sampled as nearest on Mon Oct 19; missing Monday sampling excludes,
  never fabricates. Within the approved two-chain limit.

### Box 9 — Post-merge live D7 check — HELD, deployment-dependent
- Prepared as an explicit held check, not a pass. Requires a deployed
  collector run; no deployment is authorized on this branch.

## Stable window/run/contract contract (for #131)

The on-demand lookup (#131) consumes the same contract the replay writes.
The contract is stable across the offline replay and the future lookup:

- **Window:** `window_id` (`<kind>-<entry_day>_<exit_day>`), `kind`
  (`weekly_cheapest`, `weekly_fixed`, `weekly_cheapest_best_exit`,
  `overnight`, `sessions_2`, `sessions_3`), `sessions` (ordered list of
  `{day, open_ms, close_ms, early_close}`), `policy` (version
  `oa203-windows-v1`, `decisions_status`, `close_tolerance_s=900`,
  `qualified_endpoint_policy.max_quote_age_s=1800`).
- **Run:** `generated` (ISO timestamp), `sources` (R2 prefix + manifest
  sha per day), `excluded_final_sweep` (rows/chains per day), `window`
  (as above), `ranked` rows.
- **Contract (per row):** `symbol`, `underlying`, `root`, `expiration`,
  `strike`, `type`, `contract_size`, `status` (`eligible`/`excluded`),
  `reasons` (exclusion reasons), `flags`, `pct`, `entry`, `exit`,
  `entry_ms`, `exit_ms`, `entry_session`, `exit_session`,
  `contract_exclusions`, `qualification_scope`.
- A row is `eligible` only if every basis passes; an `excluded` row keeps
  its raw observations and its reasons. No row is deleted.
- The lookup (#131) reads the same `window_id`/`symbol`/`expiration`/
  `strike` identity and the same policy version; it does not re-derive
  windows.

## Legacy-output regression and no-look-ahead evidence

- Legacy output: `test_layout_and_summary`,
  `test_every_path_written_including_non_winners`,
  `test_shortlist_is_cut_after_full_ranking`, `test_states`,
  `test_deterministic_content` (all in `test_oa203_windows_outputs.py`).
  The writer's layout, summary, states and deterministic content are
  unchanged by the windows v1 work.
- No-look-ahead: `test_no_look_ahead_on_entry`,
  `test_fixed_and_best_exit_variants_stay_separate`,
  `test_overnight_rejects_future_entry_and_exit_sides`,
  `test_future_or_missing_quote_times_never_enter_any_basis`.

## Monthly limits and held D7 release protocol

- **Monthly limits:** the offline replay is bounded to 10 sessions and
  50 underlyings per run; the collector is not run; no production config
  or account changes; no deployment, publication or activation. The
  monthly evidence run is the replay + suite + source-lock/hash record.
- **Held D7 release protocol:** the post-merge live D7 check (10- vs
  15-minute close coverage) is HELD. It requires a deployed collector run
  and is prepared as an explicit held check, not a pass. No merge to
  master/main, no deployment, no publication, no activation on this
  branch. When a release is authorized separately, the D7 check runs on
  the deployed collector and its result is recorded before any release
  claim.

## Evidence paths (this round)

- Worktree: `~/workspace/goals/make-live-options-view-readable/work-130`
  (local) and the MSI worktree `live-options-view-130`.
- Gap report: `docs/130-g0-gap-report.md`.
- This matrix: `docs/130-acceptance-matrix.md`.
- Replay output: `/tmp/replay-130-c` (top-50, Oct 5–7); prior sample
  `/tmp/replay-130-b` (3 underlyings, Oct 5–7).
- Source lock / report / output hashes and the exact MSI replay commands
  are recorded in the replay report and in the return message.

## Exact replay command (MSI, for independent replay)

Run from the MSI worktree
`C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130`:

```powershell
C:\Python314\python.exe -m unittest tests.test_oa203_windows tests.test_oa203_windows_outputs tests.test_oa203_windows_replay tests.test_oa203_return_scanner tests.test_oa203_active tests.test_oa203_active_budget
```

Top-50 replay (Oct 5–7, 50 underlyings by OCC rank):

```powershell
$env:PYTHONPATH = '.'
C:\Python314\python.exe market_data/options_scanner/windows_replay.py --days 2026-10-05 2026-10-06 2026-10-07 --underlyings SPY QQQ SPX NVDA TSLA IWM SPCX TLT AAPL EWZ MU VIX INTC MSTR IBIT AMZN AMD NKE HYG GOOGL META AVGO HOOD MSFT TQQQ ORCL SOXL SLV PLTR NFLX SMH SMCI GME XLU ETHA MARA SOFI GLD USO CRWV IREN XSP COIN SNDK GOOG TSLL WULF RKLB NBIS DRAM --out C:\Users\Owen\Documents\Codex\2026-11-08\nova-owen-authorizes-starting-your-multi\work\live-options-view-130\replay-130-c
```

The replay fetches from the R2 origin
`https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev` with User-Agent
`oa203-windows-replay/1`. It is read-only: no collector runs, no
production config or account changes.
