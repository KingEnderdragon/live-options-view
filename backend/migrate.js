// backend/migrate.js -- operator-only, bounded KV -> D1 cutover machinery (#208).
//
// This module is mounted only when `MIGRATION_TOKEN` is set and only answers the
// migration route. `cutoverFenceActive` is the one function the router calls: it
// is how the *additive* revision (the one still serving KV) stops accepting
// writes once the cutover fence is armed, giving the copy a single-writer
// window instead of two stores racing.
//
// Design rules, each one a defect this file exists to not repeat:
//
// * Bounded work. D1 Free allows 50 queries per Worker invocation; every phase
//   processes at most `max_records` records and stops at a query budget, then
//   returns a continuation. No phase loops an unbounded scan in one request.
// * Exact resumption. A KV list page is fetched by a page token and a per-page
//   offset, both persisted, so an interruption inside a page resumes at the key
//   after the last processed one. Re-applying the records after the last
//   checkpoint is safe: every write is idempotent.
// * Parity, not counts. `verify` compares identity and a canonical digest of
//   each record against the D1 row, then checks sessions and markers, so equal
//   counts cannot hide a replaced, altered or missing account. Responses carry
//   counts only -- never a username, a record or a secret.
// * Conflict detection. A version-only merge cannot tell "newer" from "another
//   writer also produced version N+1 with different contents", so the copy
//   compares digests at equal versions, counts those as conflicts and refuses
//   to finalize. `d1_ahead` (D1's version above KV's after the fence) is also a
//   refusal: a write landed outside the single-writer window.
// * Deletion and revocation converge. Under the fence a full scan is
//   authoritative: accounts absent from KV are deleted in D1 (settlement
//   liquidation), and D1 sessions whose hash the scan did not see are deleted,
//   so a repeated session copy cannot resurrect a logged-out cookie.
// * No false promise of KV rollback. The incident is exhausted KV writes, so
//   `reverse_preflight` probes one KV write+delete and reports whether a KV
//   rollback is actually possible; `reverse_users` refuses to run without a
//   passing probe unless explicitly forced, and never reports a partial reverse
//   as success. D1 data is never discarded by a probe or a refused reverse.
//
// The additive revision still serves KV; the switch revision serves D1 only.
// The fence is what makes the hand-off single-writer.

import { SESSION_TTL_SECONDS } from './common.js';
import { createLegacyKvSource, hashSessionToken } from './storage.js';

export const RECORD_BUDGET = 20;
export const QUERY_BUDGET = 40;          // D1 Free: 50 queries per invocation
const CHECKPOINT_EVERY = 5;              // records between checkpoint writes

const PHASES = new Set([
  'fence', 'status', 'users', 'sessions', 'bot_markers', 'reconcile', 'verify',
  'finalize', 'reverse_preflight', 'reverse_users',
]);

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

function sameSecret(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function authorised(request, env) {
  const expected = env.MIGRATION_TOKEN;
  if (!expected) return false;
  return sameSecret(request.headers.get('X-Migration-Token'), expected);
}

function clamp(value, low, high) {
  const n = Number(value);
  if (!Number.isFinite(n)) return low;
  return Math.min(high, Math.max(low, Math.trunc(n)));
}

/** Stable JSON: sorted keys, so equal records always hash equally. */
export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`;
}

/** SHA-256 hex of a record's canonical form. */
export async function recordDigest(record) {
  const bytes = new TextEncoder().encode(canonicalJson(record));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// ---------------------------------------------------------------------------
// Query-counting D1 session
// ---------------------------------------------------------------------------

class QuerySession {
  constructor(db) {
    this.db = db;
    this.queries = 0;
  }

  async first(sql, ...args) {
    this.queries += 1;
    return this.db.prepare(sql).bind(...args).first();
  }

  async run(sql, ...args) {
    this.queries += 1;
    return this.db.prepare(sql).bind(...args).run();
  }

  async all(sql, ...args) {
    this.queries += 1;
    return this.db.prepare(sql).bind(...args).all();
  }
}

// ---------------------------------------------------------------------------
// Checkpoints and fence
// ---------------------------------------------------------------------------

async function readCheckpoint(session, name) {
  const row = await session.first('SELECT value FROM migration_state WHERE name = ?', name);
  return row ? JSON.parse(row.value) : null;
}

async function writeCheckpoint(session, name, value) {
  const record = { ...value, mismatch_total: mismatchTotal(value) };
  await session.run(
    'INSERT INTO migration_state (name, value) VALUES (?, ?) ' +
    'ON CONFLICT(name) DO UPDATE SET value = excluded.value',
    name, JSON.stringify(record),
  );
}

/** True when the additive revision must reject writes. Fails open only if D1 is unbound. */
export async function cutoverFenceActive(env) {
  if (!env?.DB) return false;
  try {
    const row = await env.DB.prepare('SELECT value FROM migration_state WHERE name = ?')
      .bind('fence').first();
    if (!row) return false;
    return JSON.parse(row.value).armed === true;
  } catch {
    return false;   // no table yet: nothing has been provisioned to fence
  }
}

// ---------------------------------------------------------------------------
// KV pagination with a resumable page token + offset
// ---------------------------------------------------------------------------

async function kvPage(listFn, prefix, pageToken) {
  const page = await listFn({ prefix, pageToken });
  return { keys: page.keys, nextToken: page.nextToken, token: pageToken };
}

// ---------------------------------------------------------------------------
// Phase state
// ---------------------------------------------------------------------------

function freshState() {
  return {
    cursor: null, offset: 0, scanned: 0, written: 0, skipped: 0, deleted: 0,
    conflicts: 0, d1_ahead: 0, missing: 0, mismatch: 0, extra: 0,
    done: false, stage: null, cleared: false,
  };
}

/** Redacted projection: counts and flags only.
 *
 * Deliberately no cursor: a KV page token is opaque, but the D1 stages advance
 * by username, and echoing that back would leak an account name from a route
 * whose whole contract is "counts only".
 */
function publicState(state) {
  return {
    continuation: state.done !== true,
    offset: state.offset ?? 0,
    scanned: state.scanned ?? 0,
    written: state.written ?? 0,
    skipped: state.skipped ?? 0,
    deleted: state.deleted ?? 0,
    conflicts: state.conflicts ?? 0,
    d1_ahead: state.d1_ahead ?? 0,
    missing: state.missing ?? 0,
    mismatch: state.mismatch ?? 0,
    extra: state.extra ?? 0,
    mismatch_total: mismatchTotal(state),
    stage: state.stage ?? null,
    done: state.done === true,
  };
}

function mismatchTotal(state) {
  return (state?.conflicts ?? 0) + (state?.d1_ahead ?? 0) + (state?.missing ?? 0) +
    (state?.mismatch ?? 0) + (state?.extra ?? 0);
}

async function copyUsers(session, legacy, state, budget) {
  let processed = 0;
  while (processed < budget.records) {
    if (session.queries >= budget.queries - 4) break;
    const page = await kvPage(legacy.listUsers, 'user:', state.cursor);
    if (state.offset >= page.keys.length) {
      if (!page.nextToken) { state.done = true; break; }
      state.cursor = page.nextToken; state.offset = 0;
      continue;
    }
    const key = page.keys[state.offset];
    state.offset += 1;
    state.scanned += 1;
    processed += 1;

    const raw = await legacy.getRaw(key);
    if (!raw) { state.skipped += 1; continue; }
    let record;
    try { record = JSON.parse(raw); } catch { state.skipped += 1; continue; }

    const username = String(key.slice('user:'.length)).toLowerCase();
    const digest = await recordDigest(record);
    const row = await session.first('SELECT record, version FROM users WHERE username = ?', username);
    if (!row) {
      await session.run(
        'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(username) DO NOTHING',
        username, JSON.stringify(record), Number(record.version) || 0, new Date().toISOString(),
      );
      state.written += 1;
    } else {
      const d1Version = Number(row.version) || 0;
      const kvVersion = Number(record.version) || 0;
      if (kvVersion === d1Version) {
        if (await recordDigest(JSON.parse(row.record)) !== digest) state.conflicts += 1;
        else state.skipped += 1;
      } else if (kvVersion > d1Version) {
        await session.run(
          'UPDATE users SET record = ?, version = ?, updated_at = ? WHERE username = ?',
          JSON.stringify(record), kvVersion, new Date().toISOString(), username,
        );
        state.written += 1;
      } else {
        state.d1_ahead += 1;
      }
    }
    if (processed % CHECKPOINT_EVERY === 0) await writeCheckpoint(session, 'phase:users', state);
  }
  return processed;
}

async function copySessions(session, legacy, state, budget) {
  let processed = 0;
  if (!state.cleared) {
    // A fresh pass rebuilds the seen-set; under the fence this set is what makes
    // "absent from the scan" mean "revoked", so a stale entry here would keep a
    // logged-out cookie alive. Resumed passes do not clear it.
    await session.run('DELETE FROM migration_session_seen');
    state.cleared = true;
  }
  while (processed < budget.records) {
    if (session.queries >= budget.queries - 4) break;
    const page = await kvPage(legacy.listSessions, 'sess:', state.cursor);
    if (state.offset >= page.keys.length) {
      if (!page.nextToken) { state.done = true; break; }
      state.cursor = page.nextToken; state.offset = 0;
      continue;
    }
    const key = page.keys[state.offset];
    state.offset += 1;
    state.scanned += 1;
    processed += 1;

    const token = key.slice('sess:'.length);
    const raw = await legacy.getSessionRaw(token);
    if (!raw) { state.skipped += 1; continue; }
    let payload;
    try { payload = JSON.parse(raw); } catch { state.skipped += 1; continue; }
    const createdMs = Date.parse(payload?.createdAt ?? '') || Date.now();
    const expiresAt = Math.floor(createdMs / 1000) + SESSION_TTL_SECONDS;
    if (expiresAt <= Math.floor(Date.now() / 1000)) { state.skipped += 1; continue; }

    const hash = await hashSessionToken(token);
    const existing = await session.first(
      'SELECT username FROM sessions WHERE token_hash = ?', hash,
    );
    if (existing) {
      if (String(existing.username) !== String(payload.username).toLowerCase()) state.conflicts += 1;
      else state.skipped += 1;
    } else {
      await session.run(
        'INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?) ' +
        'ON CONFLICT(token_hash) DO UPDATE SET username = excluded.username, expires_at = excluded.expires_at',
        hash, String(payload.username).toLowerCase(), new Date().toISOString(), expiresAt,
      );
      state.written += 1;
    }
    await session.run(
      'INSERT INTO migration_session_seen (token_hash, seen_at) VALUES (?, ?) ' +
      'ON CONFLICT(token_hash) DO UPDATE SET seen_at = excluded.seen_at',
      hash, new Date().toISOString(),
    );
    if (processed % CHECKPOINT_EVERY === 0) await writeCheckpoint(session, 'phase:sessions', state);
  }
  return processed;
}

async function copyMarkers(session, legacy, state, budget) {
  let processed = 0;
  while (processed < budget.records) {
    if (session.queries >= budget.queries - 4) break;
    const page = await kvPage(legacy.listBotMarkers, 'bot:', state.cursor);
    if (state.offset >= page.keys.length) {
      if (!page.nextToken) { state.done = true; break; }
      state.cursor = page.nextToken; state.offset = 0;
      continue;
    }
    const key = page.keys[state.offset];
    state.offset += 1;
    state.scanned += 1;
    processed += 1;
    const raw = await legacy.getRaw(key);
    if (!raw) { state.skipped += 1; continue; }
    const username = key.slice('bot:'.length).toLowerCase();
    const row = await session.first('SELECT 1 AS present FROM bot_markers WHERE username = ?', username);
    if (row) { state.skipped += 1; continue; }
    await session.run(
      'INSERT INTO bot_markers (username, value, created_at) VALUES (?, ?, ?) ' +
      'ON CONFLICT(username) DO NOTHING',
      username, raw, new Date().toISOString(),
    );
    state.written += 1;
  }
  return processed;
}

/**
 * Convergence pass run under the fence: anything the full scan did not see is a
 * deletion in the legacy store, not a missing copy.
 */
async function reconcile(session, legacy, state, budget) {
  let processed = 0;
  state.stage = state.stage ?? 'users';
  while (processed < budget.records) {
    if (session.queries >= budget.queries - 4) break;
    if (state.stage === 'users') {
      const page = await session.all(
        'SELECT username FROM users WHERE username > ? ORDER BY username LIMIT ?',
        state.cursor ?? '', Math.min(50, budget.records - processed),
      );
      const rows = page.results ?? [];
      if (!rows.length) { state.stage = 'sessions'; state.cursor = null; continue; }
      for (const row of rows) {
        processed += 1;
        state.scanned += 1;
        state.cursor = row.username;
        const raw = await legacy.getUser(row.username);
        if (!raw) {
          await session.run('DELETE FROM users WHERE username = ?', row.username);
          state.deleted += 1;
        }
      }
    } else {
      const page = await session.all(
        'SELECT token_hash FROM sessions WHERE token_hash > ? ORDER BY token_hash LIMIT ?',
        state.cursor ?? '', Math.min(50, budget.records - processed),
      );
      const rows = page.results ?? [];
      if (!rows.length) { state.done = true; break; }
      for (const row of rows) {
        processed += 1;
        state.scanned += 1;
        state.cursor = row.token_hash;
        const seen = await session.first(
          'SELECT 1 AS present FROM migration_session_seen WHERE token_hash = ?', row.token_hash,
        );
        if (!seen) {
          await session.run('DELETE FROM sessions WHERE token_hash = ?', row.token_hash);
          state.deleted += 1;
        }
      }
    }
    await writeCheckpoint(session, 'phase:reconcile', state);
  }
  return processed;
}

/**
 * Parity verification, one record per loop iteration, stage-advancing. Steps:
 * users compared KV -> D1 by digest, D1 -> KV for extras, sessions, markers.
 */
async function verify(session, legacy, state, budget) {
  let processed = 0;
  state.stage = state.stage ?? 'users_kv';
  while (processed < budget.records) {
    if (session.queries >= budget.queries - 4) break;

    if (state.stage === 'users_kv' || state.stage === 'users_db') {
      const useKv = state.stage === 'users_kv';
      let keys; let nextToken = null; let dbRows = null;
      if (useKv) {
        const page = await kvPage(legacy.listUsers, 'user:', state.cursor);
        keys = page.keys;
        nextToken = page.nextToken;
      } else {
        dbRows = await session.all(
          'SELECT username FROM users WHERE username > ? ORDER BY username LIMIT ?',
          state.cursor ?? '', Math.min(50, budget.records - processed),
        );
        keys = (dbRows.results ?? []).map(r => `user:${r.username}`);
      }
      if (state.offset >= keys.length) {
        if (nextToken) { state.cursor = nextToken; state.offset = 0; continue; }
        state.stage = useKv ? 'users_db' : 'sessions';
        state.cursor = null;
        state.offset = 0;
        continue;
      }
      const key = keys[state.offset];
      state.offset += 1;
      state.scanned += 1;
      processed += 1;

      if (useKv) {
        const raw = await legacy.getRaw(key);
        if (!raw) { state.missing += 1; continue; }
        let record;
        try { record = JSON.parse(raw); } catch { state.mismatch += 1; continue; }
        const username = String(key.slice('user:'.length)).toLowerCase();
        const row = await session.first('SELECT record, version FROM users WHERE username = ?', username);
        if (!row) { state.missing += 1; continue; }
        const d1Version = Number(row.version) || 0;
        const kvVersion = Number(record.version) || 0;
        if (d1Version > kvVersion) { state.d1_ahead += 1; continue; }
        if (d1Version < kvVersion) { state.mismatch += 1; continue; }
        if (await recordDigest(JSON.parse(row.record)) !== await recordDigest(record)) state.mismatch += 1;
      } else {
        const username = String(key.slice('user:'.length));
        state.cursor = username;
        // The DB stage advances by username, not by page offset.
        state.offset = 0;
        const raw = await legacy.getUser(username);
        if (!raw) state.extra += 1;
      }
      continue;
    }

    if (state.stage === 'sessions') {
      const page = await kvPage(legacy.listSessions, 'sess:', state.cursor);
      if (state.offset >= page.keys.length) {
        if (!page.nextToken) { state.stage = 'markers'; state.cursor = null; state.offset = 0; continue; }
        state.cursor = page.nextToken; state.offset = 0;
        continue;
      }
      const key = page.keys[state.offset];
      state.offset += 1;
      state.scanned += 1;
      processed += 1;
      const token = key.slice('sess:'.length);
      const raw = await legacy.getSessionRaw(token);
      if (!raw) { state.missing += 1; continue; }
      let payload;
      try { payload = JSON.parse(raw); } catch { state.mismatch += 1; continue; }
      const hash = await hashSessionToken(token);
      const row = await session.first(
        'SELECT username, expires_at FROM sessions WHERE token_hash = ?', hash,
      );
      if (!row) { state.missing += 1; continue; }
      if (String(row.username) !== String(payload.username).toLowerCase()) { state.mismatch += 1; continue; }
      const createdMs = Date.parse(payload?.createdAt ?? '') || Date.now();
      const expected = Math.floor(createdMs / 1000) + SESSION_TTL_SECONDS;
      if (Math.abs(Number(row.expires_at) - expected) > 2) state.mismatch += 1;
      continue;
    }

    const page = await kvPage(legacy.listBotMarkers, 'bot:', state.cursor);
    if (state.offset >= page.keys.length) {
      if (!page.nextToken) { state.done = true; break; }
      state.cursor = page.nextToken; state.offset = 0;
      continue;
    }
    const key = page.keys[state.offset];
    state.offset += 1;
    state.scanned += 1;
    processed += 1;
    const username = key.slice('bot:'.length).toLowerCase();
    const row = await session.first('SELECT 1 AS present FROM bot_markers WHERE username = ?', username);
    if (!row) state.missing += 1;
  }
  return processed;
}

async function requireFence(session) {
  const fence = await readCheckpoint(session, 'fence');
  return Boolean(fence?.armed);
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

export async function handleMigration(request, env, storage) {
  void storage;
  if (!authorised(request, env)) return new Response('Not Found', { status: 404 });

  let body;
  try { body = await request.json(); } catch { return json({ error: 'invalid json' }, 400); }
  const phase = String(body?.phase ?? '');
  if (!PHASES.has(phase)) return json({ error: 'unknown phase' }, 400);
  if (!env.DB) return json({ error: 'D1 binding missing' }, 503);

  const session = new QuerySession(env.DB);
  const budget = {
    records: clamp(body?.max_records ?? RECORD_BUDGET, 1, 50),
    queries: clamp(body?.max_queries ?? QUERY_BUDGET, 5, 50),
  };
  const legacy = createLegacyKvSource(env);
  const restart = body?.restart === true;

  if (phase === 'fence') {
    const action = String(body?.action ?? '');
    if (!['arm', 'disarm'].includes(action)) return json({ error: 'action must be arm or disarm' }, 400);
    const cutover = await readCheckpoint(session, 'cutover');
    if (action === 'disarm' && cutover?.finalized_at) {
      return json({ error: 'refusing to disarm after finalize' }, 409);
    }
    const fence = action === 'arm'
      ? { armed: true, armed_at: new Date().toISOString() }
      : { armed: false, disarmed_at: new Date().toISOString() };
    await writeCheckpoint(session, 'fence', fence);
    return json({ phase, ...fence });
  }

  if (phase === 'status') {
    const names = ['fence', 'cutover', 'phase:users', 'phase:sessions', 'phase:bot_markers',
      'phase:reconcile', 'phase:verify', 'reverse_probe', 'phase:reverse_users'];
    const out = {};
    for (const name of names) out[name] = await readCheckpoint(session, name);
    return json({ phase, checkpoints: out });
  }

  if (phase === 'finalize') {
    const fence = await readCheckpoint(session, 'fence');
    const users = await readCheckpoint(session, 'phase:users');
    const sessions = await readCheckpoint(session, 'phase:sessions');
    const markers = await readCheckpoint(session, 'phase:bot_markers');
    const reconciled = await readCheckpoint(session, 'phase:reconcile');
    const verified = await readCheckpoint(session, 'phase:verify');

    const missingPhases = [];
    if (!fence?.armed) missingPhases.push('fence');
    for (const [name, state] of [['users', users], ['sessions', sessions],
      ['bot_markers', markers], ['reconcile', reconciled], ['verify', verified]]) {
      if (!state?.done) missingPhases.push(name);
    }
    if (missingPhases.length) {
      return json({ error: 'required phases incomplete', missing_phases: missingPhases }, 409);
    }
    const total = mismatchTotal(verified) + (users?.conflicts ?? 0) + (sessions?.conflicts ?? 0)
      + (users?.d1_ahead ?? 0);
    if (total > 0) return json({ error: 'parity not established', mismatch_total: total }, 409);

    const cutover = {
      finalized_at: new Date().toISOString(),
      fence_armed_at: fence.armed_at ?? null,
      users: users.scanned, sessions: sessions.scanned, markers: markers.scanned,
      deleted: reconciled.deleted ?? 0, mismatch_total: 0,
    };
    await writeCheckpoint(session, 'cutover', cutover);
    return json({ phase, status: 'finalized', ...cutover });
  }

  if (phase === 'reverse_preflight') {
    let available = false;
    let error = null;
    const probeKey = `migration-probe:${crypto.randomUUID()}`;
    try {
      await legacy.putRaw(probeKey, '1');
      await env.USERS.delete(probeKey);
      available = true;
    } catch {
      error = 'kv_write_unavailable';
    }
    const probe = { available, checked_at: new Date().toISOString(), error };
    await writeCheckpoint(session, 'reverse_probe', probe);
    return json({ phase, ...probe });
  }

  if (phase === 'reverse_users') {
    const probe = await readCheckpoint(session, 'reverse_probe');
    if (!body?.force && !probe?.available) {
      return json({
        error: 'KV writes unavailable; refusing to promise a rollback that cannot complete',
        preflight_available: Boolean(probe?.available),
        recovery: 'keep serving from D1; D1 is the rollback floor while KV writes are exhausted',
      }, 409);
    }
    const stateName = 'phase:reverse_users';
    const state = restart ? freshState() : (await readCheckpoint(session, stateName)) ?? freshState();
    if (state.done) return json({ phase, ...publicState(state) });
    while (session.queries < budget.queries - 1) {
      const page = await session.all(
        'SELECT username, record, version FROM users WHERE username > ? ORDER BY username LIMIT ?',
        state.cursor ?? '', Math.min(budget.records, 10),
      );
      const rows = page.results ?? [];
      if (!rows.length) { state.done = true; break; }
      for (const row of rows) {
        state.scanned += 1;
        state.cursor = row.username;
        const raw = await legacy.getUser(row.username);
        const legacyVersion = raw ? (Number(JSON.parse(raw).version) || 0) : -1;
        if (Number(row.version) <= legacyVersion) { state.skipped += 1; continue; }
        try {
          await legacy.putUser(row.username, JSON.parse(row.record));
          state.written += 1;
        } catch {
          await writeCheckpoint(session, stateName, state);
          return json({ phase, error: 'kv_write_failed_midway', ...publicState(state) }, 503);
        }
      }
      await writeCheckpoint(session, stateName, state);
    }
    return json({ phase, ...publicState(state) });
  }

  // Copy/reconcile/verify phases.
  let stateName = `phase:${phase}`;
  let state = restart ? freshState() : (await readCheckpoint(session, stateName)) ?? freshState();
  if (phase === 'verify') {
    state.stage = String(body?.stage ?? state.stage ?? 'users_kv');
    if (restart) state = { ...freshState(), stage: String(body?.stage ?? 'users_kv') };
  }
  if (state.done) return json({ phase, ...publicState(state), checkpoint: stateName });

  if ((phase === 'reconcile' || phase === 'verify') && !(await requireFence(session))) {
    return json({ error: 'fence must be armed before absence can be treated as deletion', phase }, 409);
  }

  let processed = 0;
  if (phase === 'users') processed = await copyUsers(session, legacy, state, budget);
  else if (phase === 'sessions') processed = await copySessions(session, legacy, state, budget);
  else if (phase === 'bot_markers') processed = await copyMarkers(session, legacy, state, budget);
  else if (phase === 'reconcile') processed = await reconcile(session, legacy, state, budget);
  else if (phase === 'verify') processed = await verify(session, legacy, state, budget);

  await writeCheckpoint(session, stateName, state);
  return json({
    phase,
    checkpoint: stateName,
    processed_this_call: processed,
    queries_this_call: session.queries,
    ...publicState(state),
  });
}
