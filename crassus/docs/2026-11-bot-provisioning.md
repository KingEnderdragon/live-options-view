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
what the Worker says about it. Exit codes: `0` ready, `2` account configuration
missing, `3` Worker rejected the key, `4` Worker unreachable. The empty-body
probe cannot create an account; a `400` means the key was accepted and body
validation rejected the request.

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
