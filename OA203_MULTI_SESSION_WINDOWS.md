# Big Banana multi-session windows (#130): design and open decisions

Issue: [#130](https://github.com/OwenTanzer/live-options-view/issues/130).
Companion: #131 (on-demand MCP lookup), which will consume this contract.
Status: **design for review**. It contains no code, collection changes or
deployments. The owner hasn't fixed the window definitions; each choice below
is a proposal with a recommended default, not a decision.

## What the existing archive already supports

The scanner samples each selected underlying's **nearest unexpired expiry**
about every 5 minutes (`docs/plans/2026-09-options-return-scanner.md`).
A multi-session return must follow one contract (same OCC symbol: root,
expiry, type, strike), so it only exists where the same chain was selected
and sampled on every session in the window.

These numbers were measured from the published `universe.json` files for
2026-10-05..09, all five final. They are read-only public artifacts.

| Window | Chains with the same (underlying, expiry) on every session |
| --- | --- |
| Mon 10-05 → Fri 10-09 (weekly) | **310 / 500** (307 expiring Fri 10-09, 3 expiring 10-16) |
| Overnight Mon→Tue, Tue→Wed, Wed→Thu, Thu→Fri | 378, 395, 364, 390 |
| Overnight Fri 10-09 → Mon 10-12 | at most **74** (only Friday chains not expiring that day; Monday's selection may drop some) |
| Daily-expiry names (0DTE every session) | **10**: GLD, IWM, NDX, QQQ, RUT, SMH, SPX, SPY, XLF, XSP. None has any multi-session path. |

Two consequences:

1. Weekly and Mon–Thu overnight windows can be **replayed from existing
   archives** for roughly 300–400 chains with no collection change.
2. The most liquid names (the index and ETF daily expiries) and Fri→Mon holds
   are **invisible** to multi-session windows until a longer-dated expiry is
   also sampled for those names. That is a collection change, covered in
   decision D8.

The universe is reselected daily from OCC volume, so a chain can fall out
mid-window. That shows up as a coverage gap (`missing_session`), never as a
stitched path.

## Proposed window definitions

All windows use the existing chronological rule: the exit is strictly later
than the entry. A new window policy, `oa203-windows-v1`, is versioned
separately and leaves `oa203-returns-v1` and `oa203-qualified-ask-bid-v1`
unchanged.

| # | Window | Entry | Exit | Status |
| --- | --- | --- | --- | --- |
| W1 | Weekly, cheapest early-week entry → Friday | Lowest eligible entry price observed in the early-week window (D1, D2) | Last valid observation of the week's last session (D3, D4) | priority |
| W1b | Weekly, fixed endpoints | First valid observation of the week | Same Friday exit | separately named comparison |
| W1c | Weekly, cheapest entry → best later exit | As W1 | Highest strictly later observation through Friday | separately named, hindsight |
| W2 | Overnight | Last valid observation of session D, within tolerance of D's close (D7) | First valid observation of the next session, within tolerance of its open | priority |
| W3 | Two / three sessions | First valid observation of the first session (near the open) | Last valid observation of the last session (near the close) | after W1/W2 |
| W4 | Largest intraday flip | Reuse existing `trough_to_peak` per session; no new definition | | expose only |
| W5 | Monthly | No statistic yet; retain evidence (see below) | | groundwork |

Every window reports, for `mid` and `exec` (ask-entry/bid-exit), and for the
qualified variant where the endpoint checks pass:

- the percent return and the per-contract dollar change
- exact entry/exit observation times and their delay from the intended boundary
- endpoint bid/ask, sizes, spread, quote age
- underlying spot
- session list, flags and an explicit status

## Decisions for the owner

Each decision has a recommended default. Nothing is locked until confirmed.

| ID | Question | Recommended default | Alternatives |
| --- | --- | --- | --- |
| D1 | What counts as "early week"? | The week's **first trading session** (Monday, or Tuesday after a Monday holiday), whole regular session | First two sessions; first N hours of Monday |
| D2 | Price basis and eligibility for "cheapest" | Lowest **ask** among observations that pass the existing qualified endpoint checks (premium ≥ $0.10, spread ≤ 30%, ask size ≥ 1, quote age ≤ 30 min). Report the mid-basis cheapest separately. Labeled retrospective: the cheapest point is only known afterwards. | Lowest mid; lowest ask without qualification |
| D3 | Friday exit price | Last valid observation of the session (bid for `exec`, mid for `mid`), within D7 tolerance of the close | Closing-auction trade-bar price (only backfilled for top contracts); best later exit, kept as W1c |
| D4 | Friday holiday | Use the **last trading session of the calendar week** (e.g. Thursday) and say so in the window record | Skip the week; extend to the next session |
| D5 | Overnight across weekends/holidays | Close of the last session → open of the **next trading session**, with the gap length recorded | Weekday nights only |
| D6 | Two/three-session reporting | **Rolling**: one window ending on every session, so consecutive windows overlap | Calendar-anchored, non-overlapping blocks |
| D7 | Endpoint tolerance | Close endpoint within **10 min before the close**; open endpoint within **15 min after the open**. Otherwise `endpoint_missing`. | Tighter or looser; trade-bar endpoints for top contracts |
| D8 | Longer-lived expiry selection | For underlyings whose nearest expiry is ≤ 2 calendar days away, **also sample the next weekly expiry ≥ 5 calendar days out**. About 10–20 extra chains (~2–4% more requests per sweep), with storage growing proportionally. Needs approval because it changes collection. | Sample a fixed second expiry for all 500 (+100% cost); no change, accepting no multi-session coverage for daily-expiry names |
| D9 | Exiting on expiration day | Allowed (it is how weekly options are held). Flag `exit_on_expiration_day`. Exclude AM-settled roots on their settlement day (they have no PM quotes). Last-tradable time comes from the root's rules. | Exclude expiration-day exits |

## Contract continuity and integrity rules

- **No stitching.** A window path uses one OCC symbol end to end. If the
  chain wasn't sampled in a session, the result is `missing_session`. If the
  contract expires before the exit, it is `expires_before_exit`. A different
  root or deliverable (adjusted roots, e.g. `XYZ1`) is `deliverable_changed`.
- **No look-ahead.** Entry eligibility (D2) uses only that observation's own
  quote evidence. Later observations, end-of-day volume, final coverage or
  later universe membership never qualify an entry. A test proves that
  appending later observations can't change any entry decision.
- **No interpolation.** Missing, stale, crossed, locked, nonfinite or
  future-dated endpoints keep their status. A price is never filled in.
- **Full-universe ranking.** Rankings are taken over every eligible window
  path before any shortlist. Zero and negative returns are kept, and every
  excluded or unknown path keeps its reason.
- **Labels.** Quote comparisons are not fills. Spread or size qualification
  doesn't establish slippage, fees, queue position or capacity. Cheapest-entry
  and best-flip results are labeled hindsight.

## Evidence recorded per window

- policy version
- XNYS calendar version
- timezone
- intended and observed endpoints
- session list with early closes
- universe selection version and date per session
- contract identity and expiry/last-tradable time
- source artifact keys with SHA-256
- per-session coverage
- explicit state: `in_progress`, `partial` or `final`

Daily outputs and archives are never rewritten.

Proposed layout, read-only for #131, under a new prefix so the daily
contract is untouched:

```
oa203/windows/<policy>/<kind>/<window_id>/summary.json
oa203/windows/<policy>/<kind>/<window_id>/rankings.csv       # full eligible ranking, bounded shortlist
oa203/windows/<policy>/<kind>/<window_id>/paths.csv.gz       # every contract path, winners and not
```

`window_id` is deterministic, e.g. `weekly-2026-W41` or
`overnight-2026-10-08_2026-10-09`, so #131 can reference an exact window.

## Monthly groundwork (no statistic)

The daily archive already keeps every sampled contract row, universe file and
manifest, so monthly paths could be rebuilt later. That only covers contracts
whose chain was sampled every session, which for nearest-expiry sampling
is almost none over a month. A monthly statistic needs D8, or a monthly-expiry
sample, plus enough history. Until then this design only documents the
requirement and claims no monthly result.

## Proposed build order (after decisions)

1. Pure window module beside `oa203_returns.py` (calendar-aware window
   construction, continuity checks, the D1–D7 rules) with fixtures for:
   - Friday and Monday holidays, weekend gaps, early close and DST
   - expiring-before-exit and deliverable changes
   - delayed first quote and missing sessions
   - no look-ahead, and negative/zero returns
2. Offline replay of 2026-10-05..09 from the public archives (read-only) to
   produce inspectable weekly and overnight rankings with exact evidence.
   This is reported as offline validation, not deployed collection.
3. Producer publication under `oa203/windows/` (needs approval).
4. D8 expiry expansion (needs separate approval and a budget check).
5. #131 consumes the window contract.

## Not in scope

Automatic trading, predictive models, new credentials or paid data, a higher
collection rate, or any deployment by virtue of this document.
