# Removing Cloudflare KV from bot operations (issue #208)

Workers KV was the wrong store for account and session state: no conditional
write, no transaction, no uniqueness constraint, and a daily *write* quota that
the production runner exhausted (`KV put() limit exceeded for the day`). This
document records what replaces it, why, and how the cutover is executed and
rolled back. The runtime target is **zero KV operations**; the legacy
namespaces stay in place, untouched, as the rollback source.

## Inventory: every runtime KV dependency

| KV key / namespace | Used by | What it holds | Replacement |
| --- | --- | --- | --- |
| `USERS` → `user:<name>` | `auth.js` (register, login), `common.js` (`requireSession`, `withUserRecord`), `bots.js` (markers, reconciliation, metadata), `settlement.js` (settle, delete) | The account record: salt, PBKDF2 hash, iterations, `balance_cash`, trades, bot identity, margin fields | `users` table, record JSON preserved verbatim |
| `USERS` → `bot:<name>` | `bots.js` (`repairLegacyBotMarker`), `settlement.js` (delete) | Legacy membership marker written by pre-#102 code | `bot_markers` table (R2 index remains authoritative) |
| `USERS.list(prefix)` | `bots.js` reconciliation/migration | Enumerating accounts and markers | `users.username` primary key scan, paginated |
| `SESSIONS` → `sess:<token>` | `auth.js` (`startSession`, logout), `common.js` (`requireSession`), `settlement.js` | `{username, createdAt}` keyed by the raw bearer token | `sessions` table keyed by `SHA-256(token)` |
| `PAPER_TRADES` (R2) | `bots.js` bot index, archives | Public roster index and artifacts | **unchanged** — R2 stays |

There is no other KV access in the Worker. The Python and Node test harnesses
keep their KV fakes only as a *source* for migration tests.

## D1 versus SQLite-backed Durable Objects

Both are free-plan-capable transactional stores; the choice is about shape, not
price. Figures below are from Cloudflare's published limits at the time of
writing and **must be re-verified against the dashboard at cutover** — this
environment has no network access, so they are stated for review rather than
asserted as current:

| | D1 | Durable Objects (SQLite storage) |
| --- | --- | --- |
| Model | One logical SQLite database, centrally placed, SQL over HTTP | One SQLite database per object, single-threaded per object |
| Consistency | Serialized single-writer SQLite; `batch()` is transactional | Strong per object; cross-object coordination is your problem |
| Uniqueness | Native `PRIMARY KEY` / `UNIQUE` | Native within one object only |
| Free-tier shape | 5 GB storage, ~5 M rows read/day, ~100 k rows written/day | 5 GB storage, row read/write allowances per day, plus request/duration billing |
| Latency | One network hop per query; batch to amortize | Co-located with the request that routes to it |
| Operational cost | A binding, a schema file, `wrangler d1 execute` | A class, a namespace binding, routing/coordination code, alarms for expiry |

**Chosen: D1.** The workload is small and relational: ~31 accounts, one record
per account, a session per login, trades written a few times per account per
day. At a 300-second cycle the dominant operation is a per-cycle `GET /api/me`
read — roughly 31 × 288 ≈ 9 k record reads/day — and writes are tens to low
hundreds per day (sessions, trades, settlements), far inside D1's row budget.
What this data needs is exactly what D1 gives cheaply: a uniqueness constraint
for registration, a conditional `UPDATE ... WHERE version = ?` for balance and
position mutation, and a transaction for "trade + balance + intent" — none of
which need per-object placement. A Durable Object would add a routing layer and
a cross-object uniqueness problem (one object per user means "is this username
taken?" needs a second coordinator) to buy latency this cadence does not care
about. D1 also keeps the Worker stateless, which is how it is deployed today.

If the workload ever becomes latency-bound per account (many strategies per
account, sub-second decisions), a per-account SQLite DO is the natural
evolution — and the storage module is the seam that would make that swap
possible without touching route code.

## Source of truth and write ordering

* **Runtime:** D1 is the only source of truth. `createStorage` throws if `DB`
  is unbound; there is deliberately no KV fallback that could hide a failure.
* **Registration:** a single `INSERT ... ON CONFLICT DO NOTHING`; the UNIQUE
  primary key decides the winner, so two simultaneous registrations cannot both
  succeed. `changes === 1` means "created", otherwise "exists" (HTTP 409).
* **Account mutation (trades, settlement):** read record + version, apply the
  in-memory mutation, then one `UPDATE ... SET record, version = version + 1
  WHERE username = ? AND version = ?`. `changes === 1` is the commit; otherwise
  re-read and retry, bounded. This is a real compare-and-swap, unlike the old
  confirming re-read, which the code itself described as "best effort".
* **Sessions:** `sessions.token_hash` is `SHA-256(raw token)`. The cookie still
  carries the raw token; the database never holds a bearer credential. Expiry is
  enforced on read and the row is deleted lazily. Logout and settlement
  revocation delete by token hash, and `deleteForUser` revokes every session for
  an account.
* **Bot roster:** R2 stays authoritative (#102). `bot_markers` exists only so
  the legacy markers are visible and can be retired deliberately.

## Migration, cutover and rollback

The copy runs through an operator route that is invisible unless
`MIGRATION_TOKEN` is set; it needs no redeploy per phase and every phase is
idempotent and resumable through `migration_state`.

1. **Schema** (once):
   `wrangler d1 create live-options` then
   `wrangler d1 execute live-options --remote --file backend/migrations/0001_accounts_sessions.sql`
2. **Deploy the additive revision** (this patch): it adds the route and the
   schema, and does not yet change how logins read. `MIGRATION_TOKEN` is a
   separate secret, not `BOT_REGISTRATION_KEY`.
3. **Copy**, phases in this order, re-runnable at any point:
   `curl -sS -X POST https://options.moopertonic.net/api/admin/migrate -H "X-Migration-Token: $MIGRATION_TOKEN" -H 'Content-Type: application/json' -d '{"phase":"users"}'`
   then `sessions`, then `bot_markers`.
4. **Verify:** `{"phase":"verify"}` returns counts only (`kv_users`,
   `db_users`, `kv_sessions`, `db_sessions`) — no usernames or records.
5. **Finalize:** `{"phase":"finalize"}` refuses with 409 if D1 holds fewer
   users than KV.
6. **Switch reads:** deploy the storage revision that routes auth/account
   handlers through `backend/storage.js`. From that moment the runtime performs
   no KV operations. `MIGRATION_TOKEN` can then be unset.

**Rollback.** The legacy namespaces are never deleted. Before the switch,
rolling back is just redeploying the previous revision. After the switch, run
`{"phase":"reverse_users"}` first: it writes each D1 record back to KV only when
the D1 `version` is higher than the KV copy, so post-cutover trades and balances
survive. Sessions cannot be reversed, because D1 stores only hashes; a rollback
invalidates sessions created after the cutover and those clients log in again —
accounts, positions and balances are preserved. Then redeploy the previous
revision.

## Why the copy is version-aware

Both sides carry `version`. The copy is therefore an upsert that writes only
when the incoming version is higher, which converges no matter which side wrote
last: KV during the pre-copy, D1 during the cutover window. A plain
"copy everything" would silently downgrade an account that traded mid-cutover,
and a plain "only fill gaps" would miss a trade that landed in KV before the
final page was read.

## Evidence

* `tests/storage_d1.test.mjs` (12 checks) — real SQL against Node's SQLite:
  unique registration under concurrent inserts, byte-exact record round-trip,
  no lost update under two concurrent mutations, stale-version rejection,
  session hashing/expiry/revocation, migration insert semantics, no KV fallback,
  idempotent schema.
* `tests/migrate_d1.test.mjs` (11 checks) — phase authorization and 404
  invisibility, users copy preserving hashes/balances/trades/bot fields,
  newer-wins convergence in both directions, idempotent re-run, resume after an
  interrupted list page, session hashing that keeps existing cookies valid,
  expired-session skip, marker copy, redacted verify counts, finalize refusing
  an incomplete copy, reverse migration preserving post-cutover writes.

## Not done yet, and not claimed

The route handlers still read KV through the old helpers; wiring
`auth.js`, `common.js`, `bots.js`, `paper-trade.js` and `settlement.js` to
`backend/storage.js`, updating the Node/Python harnesses, and removing the KV
bindings from the deployed configuration are the next phase on the same branch.
Until that lands, the runtime is **not** KV-free and this patch must not be
described as the completed #208 migration. The legacy namespaces stay untouched
throughout, and no production record is mutated by this work.
