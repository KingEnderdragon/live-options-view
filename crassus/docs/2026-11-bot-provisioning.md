# Bot registration and credential provisioning

`BOT_REGISTRATION_KEY` exists in two independent stores: the Cloudflare Worker
secret and the Railway `crassus-runner` variable. Nothing makes writing both
atomic, so the runbook order is deliberate: pause the runner, change both
sides, restart, and verify before resuming normal operation.

## Preflight

Run from `crassus/`:

```bash
python scripts/preflight_accounts.py --probe
```

It lists every active account whose `password_env` is absent, every
`strategy_id` the runner does not register, whether the key is present, and
what the Worker says about it. Exit codes: `0` ready, `2` account or key
configuration missing, `3` Worker rejected the key, `4` the Worker was
unreachable or returned an auth, rate-limit, routing or otherwise unknown
response. The empty-body probe cannot create an account.

The only accepted proof is the current Worker's exact non-mutating validation
response: HTTP `400` with `{"error":"Username must be 3-40 characters:
letters, numbers, underscore"}`. That proves the supplied header passed the key
guard, because the Worker checks the key before username validation; the empty
body then fails validation. A different `400`, any `401`, a `403` with another
body, `404`, `429`, a `5xx`, an HTML/proxy response or an unparsable body all
fail closed and exit nonzero. A missing `BOT_REGISTRATION_KEY` is itself a
configuration failure (`2`) and no HTTP request is sent, with or without
`--probe`.

## Rotating the shared key

1. Pause the Railway runner so no account starts mid-rotation.
2. Set the new value as the Worker secret, then immediately set the same value
   on the Railway service. This is not atomic: between the writes the runner
   would receive `403 Invalid bot registration key`. The runner now raises a
   `ConfigError` that names the mismatch instead of a generic transport error.
3. Restart the runner and run `python scripts/preflight_accounts.py --probe`;
   expect exit `0` and `worker probe: accepted`.
4. Confirm `/api/bots` returns the existing roster and that settlement still
   sees every indexed bot. Only then resume.

Recovery: if the writes were interrupted, run preflight. A `rejected` probe
means the values differ; restore the previous key on both sides or complete the
new pair. No account needs to be recreated. Do not register a replacement
username to work around a key mismatch.

## Provisioning a new account

1. Add the catalog entry with `"active": false` while its credential is absent.
   Inactive entries are skipped by the runner and preflight.
2. Provision the named `CRASSUS_PW_*` variable on Railway; never commit its
   value.
3. Run preflight with all existing variables present. It reports every missing
   name at once, rather than one crash-loop restart per account.
4. Flip the account to active and restart. The first login provisions it with
   the operator key; the Worker adds it to the bot index and the runner syncs
   alias/strategy metadata on later starts.
5. Verify the account appears once in `/api/bots` with the intended strategy,
   and that a decision record exists. If registration succeeded but index
   maintenance was interrupted, log in again (the normal retry) rather than
   registering another username; the existing account is recoverable.

The runner's account loader now reports every missing credential in one error,
and a Worker `403` on registration or metadata sync is surfaced as a key
mismatch, so a missing variable and a drifted key are distinguishable from the
startup log alone.

## Pre-merge gate for newly active credentials

Preflight runs after deploy; this runs before merge, because the expensive
mistake is merging an account that becomes active while its Railway variable
exists only in someone's memory. From `crassus/`:

```bash
python scripts/provisioning_gate.py --base-ref origin/master
python scripts/provisioning_gate.py --base-ref "$BASE_SHA" --json
python scripts/provisioning_gate.py --base-ref origin/master --check-env
```

"Newly active" is narrow and mechanical: the entry is absent from the base
revision, or it flips `active: false` to active, or it stays active but changes
its `password_env` name. Each of those asks Railway for a variable that did not
have to exist before, so each needs evidence. An entry that is already active
on the same variable is grandfathered -- this gate is about the delta, not a
retroactive audit of thirty existing accounts.

Evidence lives in `crassus/provisioning_evidence.json`, which names variables
and never their values:

```json
{
  "schema": "provisioning_evidence.v1",
  "entries": {
    "CRASSUS_PW_NEWBOT": {
      "provisioned_at": "2026-11-09",
      "by": "operator initials",
      "ticket": "MOO-000",
      "note": "Railway variable set; preflight accepted"
    }
  }
}
```

Only `provisioned_at`, `by`, `ticket` and `note` are accepted, all strings,
with `note` capped at 240 characters; the loader rejects unknown fields and
over-long values rather than trusting a reviewer to notice a pasted secret. A
catalog entry carrying a literal `password` is a hard failure, and every
`password_env` must match `CRASSUS_PW_[A-Z0-9_]+`.

Exit codes: `0` pass, `1` gate findings, `2` unusable input (unreadable
catalog or evidence, unresolvable base revision -- fix the fetch rather than
silently skipping the comparison). `--check-env` additionally requires each
newly active variable to be set in the invoking environment; that is the mode
to use with the real Railway variables exported during release, and it is what
turns "a human says it is provisioned" into "this shell can see it".

CI runs the gate with `fetch-depth: 0` and the pull request's base SHA, so a
red gate blocks the merge rather than the deploy.

### Release checklist for a change that activates credentials

1. Land the catalog entry as `"active": false` in the same PR as its code; that
   path needs no evidence and cannot abort startup.
2. Create the `CRASSUS_PW_*` variable on Railway. Never commit the value.
3. Add the redacted evidence entry for that variable name in the same PR.
4. Confirm `python scripts/provisioning_gate.py --base-ref origin/master
   --check-env` exits `0` with the Railway variables exported.
5. Deploy, then run `python scripts/preflight_accounts.py --probe` against the
   deployed Worker; expect exit `0` and `worker probe: accepted`.
6. Flip the entry to active in a follow-up PR (or an `accounts.json` override
   for a private account), restart, and confirm one roster card and a decision
   record as in "Provisioning a new account" above.
7. Rollback: revert to `active: false` and restart. The variable is harmless
   while unused; the account keeps its identity and is not deleted.

## Periodic registration-key health

A key that was correct at startup can drift afterwards -- the Worker secret is
rotated and Railway is not, or a backup restores an older value -- and every
account then fails registration with the same generic transport error. The
runner now probes its own key on a bounded cadence, using the same
non-mutating empty-registration request as preflight (`crassus/preflight.py`),
and reports state changes through the normal structured log as
`key_health_ok`, `key_health_rejected`, `key_health_unreachable` and
`key_health_recovered`.

Defaults: a check every 900 seconds with a 15 second probe timeout, so the
worst-case delay between a real key change and detection is **915 seconds**.
The event carries `max_detection_delay_s` so nobody has to derive it. Repeated
identical states are not re-logged every cycle: an event fires on a state
change, on recovery, or on a four-hour heartbeat, and the emitted state is
persisted in `state/key_health.json` so a restart does not re-announce a known
condition.

Configuration, all environment-only so rollout and rollback are settings
changes rather than code changes:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CRASSUS_KEY_HEALTH_INTERVAL_S` | `900` | Seconds between probes; `0` disables the periodic check |
| `CRASSUS_KEY_HEALTH_TIMEOUT_S` | `15` | Probe timeout; added to the interval for the detection bound |
| `CRASSUS_KEY_HEALTH_HEARTBEAT_S` | `14400` | Re-announce an unchanged state at most this often |

A malformed or negative interval falls back to the default rather than
disabling the check -- the one thing a typo must not do is silently remove
monitoring. A failed probe never stops trading: it is classified and logged,
and per-account registration errors remain authoritative.

To verify a deployment, run the same code path by hand:

```bash
python -m crassus.key_health --once --json
```

Exit `0` means accepted, `3` means the Worker rejected the key, `4` means the
Worker was unreachable or gave an unaccepted response, `2` means the key is not
configured. Rollback is `CRASSUS_KEY_HEALTH_INTERVAL_S=0` and a restart; no
state needs cleaning, and the state file is inert without the runner.

