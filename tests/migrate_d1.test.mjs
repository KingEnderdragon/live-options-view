// tests/migrate_d1.test.mjs -- the KV -> D1 copy that issue #208's cutover runs.
//
// Real SQL (SQLite via node:sqlite), a fake legacy namespace with the same
// list/get/put shape as Workers KV. What is exercised: phase ordering,
// version-aware convergence, idempotent re-runs, resume after an interrupted
// page, session hashing that keeps existing cookies working, redacted
// verification counts, finalize refusing an incomplete copy, and the reverse
// path that preserves post-cutover writes for rollback.

import test from 'node:test';
import assert from 'node:assert/strict';
import { makeStorage, FakeKV } from './helpers/d1_shim.mjs';
import { handleMigration } from '../backend/migrate.js';
import { createStorage, hashSessionToken } from '../backend/storage.js';
import { SESSION_TTL_SECONDS } from '../backend/common.js';

const TOKEN = 'migration-token';

function legacyRecord(username, version, balance) {
  return {
    username,
    salt: 'c2FsdA==',
    hash: 'aGFzaA==',
    iterations: 100000,
    balance_cash: balance,
    trades: [{ execution_request_id: `req-${username}-${version}`, sym: 'QQQ', side: 'buy', qty: 1, price: 1 }],
    createdAt: '2026-11-01T00:00:00.000Z',
    version,
    is_bot: true,
    alias: username,
    strategy_id: 'reddit_sentiment_qqq',
  };
}

function harness() {
  const d1 = makeStorage();
  const kv = new FakeKV();
  const env = { DB: d1, USERS: kv, SESSIONS: kv, MIGRATION_TOKEN: TOKEN };
  const storage = createStorage(env);
  const call = async (phase, extra = {}) => {
    const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
      method: 'POST',
      headers: { 'X-Migration-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phase, ...extra }),
    }), env, storage);
    return { status: response.status, body: await response.json() };
  };
  return { d1, kv, env, storage, call };
}

test('the route is invisible without the token', async () => {
  const { env, storage } = harness();
  const response = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
    method: 'POST', body: JSON.stringify({ phase: 'users' }),
  }), env, storage);
  assert.equal(response.status, 404);

  const wrong = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
    method: 'POST',
    headers: { 'X-Migration-Token': 'nope' },
    body: JSON.stringify({ phase: 'users' }),
  }), env, storage);
  assert.equal(wrong.status, 404);

  const unconfigured = await handleMigration(new Request('https://worker.test/api/admin/migrate', {
    method: 'POST',
    headers: { 'X-Migration-Token': TOKEN },
    body: JSON.stringify({ phase: 'users' }),
  }), { ...env, MIGRATION_TOKEN: undefined }, storage);
  assert.equal(unconfigured.status, 404);
});

test('users copy over and keep their hashes, balances, trades and bot fields', async () => {
  const { kv, storage, call } = harness();
  await kv.put('user:crassus_luigi', JSON.stringify(legacyRecord('crassus_luigi', 3, 9740)));
  await kv.put('user:crassus_jesus', JSON.stringify(legacyRecord('crassus_jesus', 1, 10000)));

  const result = await call('users');
  assert.equal(result.status, 200);
  assert.equal(result.body.written, 2);
  assert.equal(result.body.done, true);

  const luigi = await storage.users.get('crassus_luigi');
  assert.deepEqual(luigi, legacyRecord('crassus_luigi', 3, 9740));
  assert.equal(luigi.balance_cash, 9740);
  assert.equal(luigi.hash, 'aGFzaA==');
});

test('a cutover write is never downgraded by a stale legacy copy', async () => {
  const { kv, storage, call } = harness();
  // KV still holds version 3; the cutover window already wrote version 5 to D1.
  await kv.put('user:crassus_luigi', JSON.stringify(legacyRecord('crassus_luigi', 3, 100)));
  await storage.users.insert('crassus_luigi', legacyRecord('crassus_luigi', 5, 500));

  const result = await call('users');
  assert.equal(result.body.skipped, 1);
  const record = await storage.users.get('crassus_luigi');
  assert.equal(record.version, 5);
  assert.equal(record.balance_cash, 500);
});

test('a newer legacy record is copied over an older D1 row', async () => {
  const { kv, storage, call } = harness();
  await storage.users.insert('crassus_luigi', legacyRecord('crassus_luigi', 2, 200));
  await kv.put('user:crassus_luigi', JSON.stringify(legacyRecord('crassus_luigi', 4, 400)));

  const result = await call('users');
  assert.equal(result.body.written, 1);
  assert.equal((await storage.users.get('crassus_luigi')).version, 4);
  assert.equal((await storage.users.get('crassus_luigi')).balance_cash, 400);
});

test('re-running a phase is idempotent', async () => {
  const { kv, storage, call } = harness();
  await kv.put('user:acct', JSON.stringify(legacyRecord('acct', 1, 10)));
  await call('users');
  const again = await call('users', { restart: true });
  assert.equal(again.body.scanned, 1);
  assert.equal(again.body.written, 0);
  assert.equal(again.body.skipped, 1);
  assert.equal((await storage.users.get('acct')).balance_cash, 10);
});

test('an interrupted page resumes from its checkpoint', async () => {
  const { kv, storage, call } = harness();
  for (let i = 0; i < 250; i++) {
    await kv.put(`user:acct_${String(i).padStart(3, '0')}`, JSON.stringify(legacyRecord(`acct_${i}`, 1, i)));
  }
  kv.failOnPage = 2;            // the second list page throws
  kv.listCalls = 0;
  await assert.rejects(() => call('users'));
  const checkpoint = await storage.migration.checkpoint('phase:users');
  assert.ok(checkpoint.cursor, 'a cursor survives the interruption');
  assert.equal(checkpoint.scanned, 100);

  kv.failOnPage = null;
  const resumed = await call('users');
  assert.equal(resumed.body.done, true);
  assert.equal((await storage.users.listUsernames()).length, 250);
});

test('sessions copy hashed so existing cookies keep working', async () => {
  const { kv, storage, call } = harness();
  const created = new Date(Date.now() - 60_000).toISOString();
  await kv.put('sess:legacy-token-1', JSON.stringify({ username: 'crassus_luigi', createdAt: created }));

  const result = await call('sessions');
  assert.equal(result.body.written, 1);
  const session = await storage.sessions.get('legacy-token-1');
  assert.equal(session.username, 'crassus_luigi');

  const rows = kv.data.size;   // legacy untouched
  assert.equal(rows, 1);
  const dbRow = (await storage.sessions.count());
  assert.equal(dbRow, 1);
  assert.notEqual(await hashSessionToken('legacy-token-1'), 'legacy-token-1');
});

test('expired legacy sessions are not copied', async () => {
  const { kv, call } = harness();
  const old = new Date(Date.now() - (SESSION_TTL_SECONDS + 3600) * 1000).toISOString();
  await kv.put('sess:stale', JSON.stringify({ username: 'acct', createdAt: old }));
  const result = await call('sessions');
  assert.equal(result.body.written, 0);
  assert.equal(result.body.skipped, 1);
});

test('bot markers copy, and verify reports counts only', async () => {
  const { kv, call } = harness();
  await kv.put('user:acct', JSON.stringify(legacyRecord('acct', 1, 10)));
  await kv.put('bot:acct', JSON.stringify({ username: 'acct' }));
  await call('users');
  const markers = await call('bot_markers');
  assert.equal(markers.body.written, 1);

  const verify = await call('verify');
  assert.equal(verify.status, 200);
  assert.equal(verify.body.kv_users, 1);
  assert.equal(verify.body.db_users, 1);
  assert.equal(verify.body.complete, true);
  // Redaction: only counts, never a username or record.
  const serialised = JSON.stringify(verify.body);
  assert.ok(!serialised.includes('acct'));
  assert.ok(!serialised.includes('hash'));
});

test('finalize refuses an incomplete copy and accepts a complete one', async () => {
  const { kv, call } = harness();
  await kv.put('user:a1', JSON.stringify(legacyRecord('a1', 1, 10)));
  await kv.put('user:a2', JSON.stringify(legacyRecord('a2', 1, 10)));

  const early = await call('finalize');
  assert.equal(early.status, 409);

  await call('users');
  const done = await call('finalize');
  assert.equal(done.status, 200);
  assert.equal(done.body.status, 'finalized');
});

test('reverse migration preserves post-cutover writes for rollback', async () => {
  const { kv, storage, call } = harness();
  // Legacy copy at version 1; after cutover the account traded twice (version 3).
  await kv.put('user:acct', JSON.stringify(legacyRecord('acct', 1, 100)));
  await storage.users.insert('acct', legacyRecord('acct', 1, 100));
  await storage.users.mutate('acct', record => ({ record: { ...record, balance_cash: 90 } }));
  await storage.users.mutate('acct', record => ({ record: { ...record, balance_cash: 80 } }));

  const result = await call('reverse_users');
  assert.equal(result.body.written, 1);

  const legacy = JSON.parse(await kv.get('user:acct'));
  assert.equal(legacy.version, 3);
  assert.equal(legacy.balance_cash, 80);

  // A second reverse run is a no-op (restart resets the counters, not the data).
  const again = await call('reverse_users', { restart: true });
  assert.equal(again.body.written, 0);
  assert.equal(again.body.skipped, 1);
});
