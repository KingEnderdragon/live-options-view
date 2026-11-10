# #130 window consumer (#131 F2) — offline implementation

Status: implemented and tested offline (fixtures only). No push, no PR, no
deployment, no publication, no activation. Chej owns review/release.

## What was built

- New tool `window_rankings` in `market_mcp/server.js`. It does **not**
  overload `return_rankings` / `active_rankings`: a window is a different
  product (multi-session, per-basis, hindsight-labeled) and gets its own
  tool, dataset id (`options_windows`) and validators.
- Consumer `window()` in `market_mcp/consumer.js`, validators
  `windowSummary` / `windowPathsRow`, a `result_detail` branch for
  `options_windows`, and a `discover_sources` capability for windows.
- `market_mcp/source.js` KEY allowlist extended **only** for
  `oa203/windows/oa203-windows-v1/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz}`
  with strict per-kind window-id shapes (see below).
- Fixtures in `market_mcp/window_fixtures.js` built from the real Oct 5–7
  2026 replay shape (`docs/130-acceptance-matrix.md`); tests in
  `market_mcp/window.test.js`.

## Design decisions (recorded)

1. **New tool, not a parameter.** `window_rankings(window_id, kind, basis,
   underlying?, type?, limit?, offset?)`. `window_id` and `kind` are
   required and must agree; `basis` defaults to `qualified`.
2. **Policy is fixed to `oa203-windows-v1`.** The version is part of the
   key path. A summary at that path claiming another policy version is a
   typed `unsupported` answer (artifacts exist, this consumer does not
   understand them), not a failure. A missing or malformed policy is
   `incompatible_schema` → `failed`.
3. **States.** `final` serves rows (`available`); `partial` serves rows
   with its reasons (`partial`); `in_progress` returns the summary only,
   rows `[]`, status `in_progress` — the exit is not invented. An unknown
   state string is `unsupported`. A missing window is `missing`
   (`missing_publication`).
4. **Ranks cut only after full ranking.** The consumer cannot see the full
   universe (excluded rows live only in `paths.csv.gz`), so it verifies
   what the shortlist itself proves: per basis, exactly ranks
   `1..shortlist`, pct-descending with symbol tiebreak, rows grouped by
   basis in producer order, `shortlist == min(200, eligible)`, and
   `eligible + excluded == contracts_evaluated` with exclusion-reason sums
   matching `excluded` for every basis. Negative-count reconciliation is
   exact when nothing was truncated, and an upper bound when it was.
5. **No look-ahead, no stitching.** Entry qualification comes from
   entry-time evidence only: detail verifies each endpoint lies inside its
   own session, the exit follows the entry, and every quote stamp is at or
   before its observation time (never in the future). Contracts are one OCC
   symbol end to end; continuity exclusions (`missing_session`,
   `expires_before_exit`, `am_settled_on_exit_day`,
   `adjusted_deliverable_unknown`) keep rows excluded, never fabricated.
6. **Hindsight is labeled, never hidden.** `weekly_cheapest` and
   `weekly_cheapest_best_exit` require a `hindsight` label in the summary;
   the labels travel into every answer's warnings.
7. **Paths detail is consistency-verified, not digest-pinned.** The
   summary carries no member digests and no prior paths read exists, so
   detail cannot compare a paths digest to an earlier one. Instead detail
   re-reads the summary and rankings and fails closed if either digest
   changed, then verifies the paths row against the pinned ranking row
   (identity, basis status, pct/entry/exit, sessions, endpoint/session
   bounds, quote stamps, and path arithmetic). A missing paths file leaves
   the pinned ranking standing with an explicit `rankings-only` warning
   (status `partial`); a mismatching paths row fails closed
   (`source_changed`); a paths file with conflicting attributions for one
   symbol fails closed (`incompatible_schema`) — no canonical path is
   asserted. The paths row is located by a line scan without materializing
   the whole file, but the read still counts against the existing
   artifact/expanded/query byte limits: a full-universe `paths.csv.gz`
   larger than the 24 MiB expanded ceiling fails closed with
   `excessive_response` instead of returning a partial path.
8. **No window index.** There is no latest pointer or index for windows
   that the consumer reads, so `discover_sources` advertises the fixed
   contract with availability `explicit_window_required` and never claims
   live data. (The publisher's per-window `latest.json` is an
   operator/audit pointer only; the consumer never reads it.)
9. **Publisher exists offline only.** `market_data/options_scanner/windows_publish.py`
   validates and publishes windows to a pluggable backend (fake and
   local-filesystem backends tested; see `docs/130-window-publisher.md`),
   but live R2 publication is a separate, unapproved step
   (`windows_outputs.py` is local-only). This consumer reads whatever a
   future approved publisher writes; it does not publish, set any env
   flag, start a collector, or deploy.

## Supported-window matrix (implemented)

| Kind | Bases | States served | Notes |
| --- | --- | --- | --- |
| `weekly_cheapest` | mid / exec / qualified | final, partial, in_progress (summary only) | hindsight entry; label required |
| `weekly_fixed` | mid / exec / qualified | final, partial, in_progress (summary only) | fixed open endpoint |
| `weekly_cheapest_best_exit` | mid / exec / qualified | final, partial, in_progress (summary only) | hindsight both ends; label required |
| `overnight` | mid / exec / qualified | final, partial, in_progress (summary only) | gaps (weekend/holiday) included per policy |
| `sessions_2` | mid / exec / qualified | final, partial, in_progress (summary only) | rolling |
| `sessions_3` | mid / exec / qualified | final, partial, in_progress (summary only) | rolling |

Explicitly **unsupported** (typed answer, not silent): any policy other
than `oa203-windows-v1`; any state other than
`final`/`partial`/`in_progress`; any window id that does not match its
kind (`weekly-YYYY-Www-<cheapest|fixed|cheapest_best_exit>`,
`overnight-YYYY-MM-DD_YYYY-MM-DD`,
`sessions[23]-YYYY-MM-DD_YYYY-MM-DD`); monthly or other kinds (no such
producer contract exists).

## What remains for deployed retrieval (not done here)

1. **Publisher**: implemented offline in
   `market_data/options_scanner/windows_publish.py` (see
   `docs/130-window-publisher.md`); it writes
   `oa203/windows/<policy>/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz}`
   to a pluggable backend, with retention/budget policy and a
   per-window `latest.json` pointer (no index). Live R2 publication is
   still unapproved -- Chej/Owen approval plus the release gate in
   `docs/130-window-publisher.md` is required before any live write.
2. **Live R2**: verify the published keys are readable at the public origin
   and that a real complete week exists (the inspected Oct 5–9 week is not
   complete: Oct 8 is `partial`).
3. **Connected-client check**: run `window_rankings` for a real window
   (all three bases) plus pinned `result_detail` from an attached client
   once the market_mcp service is deployed (Chej-owned release).
