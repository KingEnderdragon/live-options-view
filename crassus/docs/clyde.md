# Clyde: strategy catalogue and account assignment (issue #25)

Clyde is a standalone, offline command-line tool. It classifies strategy
names and assigns only `REGISTRY`-registered `strategy_id` values to
accounts in the gitignored `accounts.json`. It never fetches, clones,
vendors, or executes upstream strategy code, and it never changes
`runner.py`, `client.py`, `config.py`, the `REGISTRY`, the Worker, the
collector, or the strategy execution contract.

## Catalogue categories

- **registered** — implemented and currently in `REGISTRY`; assignable.
  Discovered from `REGISTRY` at runtime (the executable truth). Descriptive
  catalogue metadata cannot authorize assignment.
- **candidate** — fits the present single-instrument execution model but is
  not implemented/registered yet (HourBasedStrategy, Supertrend,
  UniversalMACD, TrendRiderStrategy, HLHB, PatternRecognition).
- **overlay** — a reusable position-management policy, not a complete entry
  strategy (BreakEven, FixedRiskRewardLoss, CustomStoplossWithPSAR).
  Non-assignable until strategy composition is supported.
- **research** — useful source material that does not fit the current
  runtime without materially changing the strategy. A QQQ-only descendant
  must be described as a new strategy *inspired by* the source, never as
  a faithful port.

The pasted strategy code in issue #25's comments (`hour_based_qqq`,
`fixed_risk_reward_qqq`, `breakeven_qqq`) is unverified historical
material, not shipped functionality. Those IDs are not in `REGISTRY`
and cannot be assigned.

## Invocation (from `crassus/`)

```sh
python scripts/clyde.py catalogue [--registered-only]
python scripts/clyde.py accounts [--accounts FILE]
python scripts/clyde.py assign --strategy ID --account ALIAS [--account ALIAS ...]
    [--all] [--accounts FILE] [--dry-run] [--backup FILE | --no-backup]
python scripts/clyde.py recover --backup FILE --accounts FILE
```

`--accounts` defaults to the gitignored `accounts.json`. Use an isolated
fixture file for demonstrations and tests; never point Clyde at a live
accounts file unless the assignment is intended.

## Preview, apply, and recovery

Preview first (no write). Dry-run runs the same registry/selection
validation as apply: an unassignable or unknown strategy, or an unknown
alias, fails with exit 2 and leaves the file unchanged.

```sh
python scripts/clyde.py assign --strategy momentum_qqq --account Alice --dry-run
```

`--account` (repeatable) and `--all` are mutually exclusive; giving both
is refused with exit 2 and no mutation.

Apply (writes a backup, then atomically replaces the file):

```sh
python scripts/clyde.py assign --strategy momentum_qqq --account Alice --account Bob
python scripts/clyde.py assign --strategy momentum_qqq --all
```

Only the selected accounts' `strategy_id` fields change. Aliases, ordering,
`params`, credentials (`password` / `password_env`), and all unrelated data
are preserved as values, in their original order (serialization may
reformat whitespace).

Configuration is validated before selection or any write: every account's
`alias`, `username`, and `strategy_id` must be a nonempty string, and
aliases and usernames must be unique. A malformed entry anywhere — even an
unselected account — refuses the whole operation (exit 2, file unchanged),
for both apply and dry-run, because the file is replaced as a unit. A backup is written before replacement by default
(`<accounts>.json.bak`, or `--backup FILE`); `--no-backup` skips it.

Recover from a backup (the backup is validated before replacement):

```sh
python scripts/clyde.py recover --backup accounts.json.bak --accounts accounts.json
```

## Refusals (file left unchanged)

- A strategy that is a candidate, overlay, research-only name, or unknown
  to `REGISTRY` is refused; the file is not modified.
- A missing or malformed accounts file is refused; the file is not modified.
- An unknown account alias is refused.
- A write is validated before replacement and is atomic (temp file +
  rename), so an interrupted write cannot corrupt the configuration.

## Positions before active-account use

Switching an account's `strategy_id` does not close or migrate its
existing positions. Address positions explicitly before using Clyde on an
active account: flatten/close any open position first, or confirm the new
strategy is prepared to manage the held position from `ctx.book.positions`.
Clyde changes configuration only; it does not trade, and it does not
reconcile positions.

## Automated-tab strategy picker (optional proposal, not added)

An Automated-tab picker that surfaces this catalogue in the UI remains an
optional proposal. It is not part of Clyde and is not silently added here.
If proposed later, it must present its interaction and authorization/state
model (who may change an assignment, what state is shown, and how the
change is confirmed) before implementation.

## Tests

```sh
python scripts/verify_clyde.py
```

Hermetic: no network, no real `accounts.json`. Covers the catalogue
categories, one/multiple/all selection, refused overlays/research names,
malformed input, interrupted writes, exact preservation of secret and
unrelated fields (without printing secrets), dry-run preview, and recovery.
