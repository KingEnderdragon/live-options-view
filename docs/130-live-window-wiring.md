# Live window producer / publication wiring — #130 / F2 inventory

Status: INVENTORY + DORMANT WIRING. Nothing in this document activates,
deploys, or publishes anything. The first bounded batch publication
remains an acceptance probe (docs/130-first-window-publication-plan.md);
this doc answers what has to exist for window products to be produced
and published continuously, not just once.

Live facts used (established 2026-11-09, not re-asserted from stale
deployment-plan files): Railway `market-mcp-read-only` healthz 200,
`/mcp` 401 `missing_token`; R2 active pointer sweep 72 published
20:41:38Z, cadence on_time; final daily summary 2026-11-09 complete
(76 sweeps); R2 `oa203/windows/` overnight Oct 5–6 summary 404 — no
published windows found.

## The live chain, piece by piece

| # | Piece | Location | Status |
|---|---|---|---|
| 1 | Session collection: one weekday scanner job | `market_data/options_scanner/session.py:run_day` (line ~214); `cli.py` `run`; Railway service `oa203-banana-scanner-AWsT`, `python -u -m market_data.options_scanner run`, cron `45 12 * * 1-5` UTC, restart `NEVER` (`market_data/options_scanner/README.md`) | PRESENT, live |
| 2 | Intraday active snapshots (~30-min cadence) | `session.py:299` flag `OA203_ACTIVE_SNAPSHOTS_ENABLED == "1"` → subprocess `snapshot-supervise` → `active.py:build_active_snapshot` | PRESENT, live (sweep 72, 2026-11-09) |
| 3 | Final daily summary + R2 upload | `session.py:finalize` (build_contract_rows → backfill → write_outputs → summary.json → `DayArchive.push`) | PRESENT, live |
| 4 | Window evaluation (all 6 kinds × 3 bases) | `market_data/options_scanner/windows.py:weekly_windows`, `overnight_windows`, `multi_session_windows`, `evaluate` (line 408) | PRESENT as code, offline only |
| 5 | Emit writer (summary/rankings/paths) | `market_data/options_scanner/windows_outputs.py:write_window` (line 114); state via `window_state` (line 62) | PRESENT as code, offline only |
| 6 | Local-day loader (finalized archives → observations) | `market_data/options_scanner/windows_live.py:load_local_session` (new in this commit) | IMPLEMENTED, offline |
| 7 | Build driver (all kinds → local emit root) | `windows_live.py:build_windows`; CLI `windows-build` (`cli.py`) | IMPLEMENTED, offline |
| 8 | Publisher (validate → budget → readback → pointer) | `market_data/options_scanner/windows_publish.py:publish_window` (line 912); `WindowBudget` (line 734: 90 days, 8 GiB, 20 MiB reserve, 3 attempts, 16 revisions); `R2Backend` + `r2_backend()` (line 714, reuses `market_data.tradier.probe` `r2_client`) | PRESENT as code, offline only |
| 9 | Publish-all driver (emit root → backend) | `windows_live.py:publish_windows`; CLI `windows-publish` (local backend only) | IMPLEMENTED, offline |
| 10 | Consumer (window_rankings + pointer check) | `market_mcp/consumer.js:window()`, `server.js:window_rankings`; image pinned to exact `c8fcf529` in `.github/workflows/publish-market-mcp-renaissance.yml` | PRESENT as code; deployment is Chej's release step |
| 11 | Live trigger (build + publish after finalize / on cadence) | — no caller — | MISSING, out of scope (see below) |
| 12 | Partial-day admission (final but assessment ≠ complete) | `windows_replay.validate_manifest` refuses any non-complete day; `windows_live` inherits the refusal | MISSING, out of scope (see below) |
| 13 | Full-universe combine-emit (14 batches → one emit) | — no such step — (plan doc records the same gap) | MISSING, out of scope (see below) |
| 14 | Retention janitor (standalone expiry deletion) | `WindowBudget` retention is enforced only inside `clean_and_reserve` during a publish; no independent expiry runner exists | PARTIAL — runs only when a publish happens |
| 15 | Intraday `in_progress` windows | Publisher refuses `in_progress` outright; consumer serves summary-only | NOT SUPPORTED BY DESIGN (scope disposition, not a gap) |

## What the offline chain proves

`windows_live` closes pieces 6–9 with the same evidence standard as
the parity work: the real producer (`windows.evaluate` +
`windows_outputs.write_window`) over local finalized archives, the
real publisher over a local backend, all 6 kinds × 3 bases, final
state preserved (`live_archive` scope — unlike replay emits, which
`write_window` forces to `partial`/`bounded_universe_replay`). Tests:
`tests/test_oa203_windows_live.py` (build all kinds final; partial-day
refusal; bounds; publish-all + idempotent re-publish; CLI build +
publish). The chain is callable today, by hand, offline:

```sh
python -m market_data.options_scanner windows-build \
  --dir <day-2026-10-05> --dir <day-2026-10-06> --dir <day-2026-10-07> \
  --dir <day-2026-10-08> --dir <day-2026-10-09> \
  --underlyings AAA BBB CCC --emit-root <emit-root>
python -m market_data.options_scanner windows-publish \
  --emit-root <emit-root> --out <local-backend-root>
```

## Remaining implementation (not in this commit)

1. **Live trigger — owner Chej (release) / Brian (scanner lane),
   gate: Owen/Chej activation approval.** What is needed: a call to
   `build_windows` + `publish_windows` (with `r2_backend()`) after
   `finalize` in `run_day`, or a second scheduled step on the scanner
   service, gated by a new env flag in the shape of
   `OA203_ACTIVE_SNAPSHOTS_ENABLED` (`session.py:299`). This was NOT
   implemented here because it needs a new flag and a live activation,
   which are outside the approved scope for this commit. Until it
   exists, windows are produced only by the manual commands above or
   by the one-off batch in the first-window plan.
2. **Partial-day admission — owner Chej/Brian, gate: design decision.**
   Today a day whose summary is `final` but whose assessment is not
   `complete` (e.g. real Oct 8, 2026: partial) is refused outright by
   `validate_manifest`, so no window covering that day can be built at
   all — fail-closed, but no windows for that week. The decision
   needed: is a final-but-partially-assessed day trustworthy evidence,
   with `session_partial:<day>` reasons propagated (the parity work
   already proves the consumer serves such partial windows correctly)?
   Until decided, live weeks containing a partial day produce nothing.
3. **Full-universe combine-emit — owner Chej/Brian.** `build_windows`
   is bounded to 50 underlyings / 10 sessions (mirroring the replay
   bound); the preserved #130 full universe is 661 underlyings in 14
   batches. A combine step merging batch emits with the same conflict
   semantics as `combine-full-universe.py` does not exist yet.
4. **Retention janitor — owner Chej.** Expired revisions are deleted
   only when a later publish runs `clean_and_reserve`. If publications
   stop, expired data is not deleted until the next publish. A
   standalone expiry run (or an explicit decision that publish-time
   cleanup is sufficient) is still owed.
5. **Live R2 publication + attached-client verification — owner Chej.**
   Covered by the first-window plan's pre-checks (bounded builder,
   conditional reservation/readback/delete of a trial window, verified
   deployed revision, saved activation flag) and by the pinned MCP
   image workflow. No step here approves or performs them.
