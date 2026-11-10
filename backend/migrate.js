// backend/migrate.js -- bounded, restartable KV -> D1 copy for issue #208.
//
// This is an operator route, not part of normal request handling: it is only
// mounted when `MIGRATION_TOKEN` is set, it authenticates on a separate header,
// and every phase is idempotent and resumable. The runtime itself never calls
// KV; this module exists so the cutover can read the legacy store, and so a
// rollback can write it back.
//
// Ordering and conflict rule:
//
// * Users carry a `version` on both sides, so the copy is a *version-aware
//   upsert*: a row is written only when the incoming record's version is
//   higher. That converges whichever side wrote last -- KV during the pre-copy,
//   D1 during the cutover window -- without a third "which is newer" decision.
// * Sessions cannot be versioned (KV keyed them by the raw token, D1 stores a
//   hash), so they are insert-if-absent. A session created after cutover is
//   never clobbered; a legacy session is copied once and keeps working because
//   the hash of its token is what the new code looks up.
// * Bot markers are copied as-is; the authoritative roster stays in R2.
//
// Every phase stores its cursor and counters in `migration_state`, so an
// interrupted run resumes where it stopped instead of starting over, and
// re-running a completed phase is harmless.

import { SESSION_TTL_SECONDS } from './common.js';
import { createLegacyKvSource, hashSessionToken } from './storage.js';

const PHASES = new Set(['users', 'sessions', 'bot_markers', 'verify', 'finalize', 'reverse_users']);

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

function authorised(request, env) {
  const expected = env.MIGRATION_TOKEN;
  if (!expected) return false;
  const presented = request.headers.get('X-Migration-Token');
  return sameSecret(presented, expected);
}

function sameSecret(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function upsertIfNewer(db, username, record) {
  const id = String(username).toLowerCase();
  const version = Number(record.version) || 0;
  const result = await db.prepare(
    'INSERT INTO users (username, record, version, updated_at) VALUES (?, ?, ?, ?) ' +
    'ON CONFLICT(username) DO UPDATE SET record = excluded.record, ' +
    'version = excluded.version, updated_at = excluded.updated_at ' +
    'WHERE excluded.version > users.version',
  ).bind(id, JSON.stringify(record), version, new Date().toISOString()).run();
  return result.meta.changes === 1 ? 'written' : 'skipped';
}

export async function handleMigration(request, env, storage) {
  if (!authorised(request, env)) {
    // 404 rather than 403: an unauthenticated caller should not learn that a
    // migration route exists.
    return new Response('Not Found', { status: 404 });
  }
  let body;
  try {
    body = await request.json();
  } catch (error) {
    return json({ error: 'invalid json' }, 400);
  }
  const phase = String(body?.phase ?? '');
  if (!PHASES.has(phase)) return json({ error: 'unknown phase' }, 400);

  const legacy = createLegacyKvSource(env);
  const checkpointName = `phase:${phase}`;
  const saved = await storage.migration.checkpoint(checkpointName);
  let state = {
    cursor: body?.restart === true ? null : (body?.cursor ?? saved?.cursor ?? null),
    scanned: body?.restart === true ? 0 : (saved?.scanned ?? 0),
    written: body?.restart === true ? 0 : (saved?.written ?? 0),
    skipped: body?.restart === true ? 0 : (saved?.skipped ?? 0),
    done: false,
  };

  if (phase === 'users') {
    for (;;) {
      const page = await legacy.listUsers(state.cursor);
      for (const key of page.keys) {
        const raw = await legacy.getRaw(key);
        if (!raw) { state.scanned += 1; state.skipped += 1; continue; }
        const record = JSON.parse(raw);
        const username = String(key.slice('user:'.length));
        const outcome = await upsertIfNewer(env.DB, username, record);
        state.scanned += 1;
        state[outcome === 'written' ? 'written' : 'skipped'] += 1;
      }
      state.cursor = page.cursor;
      await storage.migration.saveCheckpoint(checkpointName, state);
      if (!page.cursor) break;
    }
    state.done = true;
  } else if (phase === 'sessions') {
    for (;;) {
      const page = await legacy.listSessions(state.cursor);
      for (const key of page.keys) {
        const token = key.slice('sess:'.length);
        const raw = await legacy.getSessionRaw(token);
        state.scanned += 1;
        if (!raw) { state.skipped += 1; continue; }
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (error) {
          state.skipped += 1;
          continue;
        }
        const createdMs = Date.parse(payload?.createdAt ?? '') || Date.now();
        const expiresAt = Math.floor(createdMs / 1000) + SESSION_TTL_SECONDS;
        if (expiresAt <= Math.floor(Date.now() / 1000)) { state.skipped += 1; continue; }
        const hash = await hashSessionToken(token);
        const outcome = await storage.sessions.insertHashedIfAbsent(hash, payload.username, expiresAt);
        state[outcome === 'created' ? 'written' : 'skipped'] += 1;
      }
      state.cursor = page.cursor;
      await storage.migration.saveCheckpoint(checkpointName, state);
      if (!page.cursor) break;
    }
    state.done = true;
  } else if (phase === 'bot_markers') {
    for (;;) {
      const page = await legacy.listBotMarkers(state.cursor);
      for (const key of page.keys) {
        const raw = await legacy.getRaw(key);
        state.scanned += 1;
        if (!raw) { state.skipped += 1; continue; }
        const username = key.slice('bot:'.length);   // legacy key was already lowercased
        if (await storage.botMarkers.has(username)) { state.skipped += 1; continue; }
        await storage.botMarkers.put(username, JSON.parse(raw));
        state.written += 1;
      }
      state.cursor = page.cursor;
      await storage.migration.saveCheckpoint(checkpointName, state);
      if (!page.cursor) break;
    }
    state.done = true;
  } else if (phase === 'verify') {
    const db = env.DB;
    const usersRow = await db.prepare('SELECT COUNT(*) AS n FROM users').first();
    const sessionsRow = await db.prepare('SELECT COUNT(*) AS n FROM sessions').first();
    // Counts only: no usernames or records leave the database in this response.
    let kvUsers = 0;
    let cursor = null;
    do {
      const page = await legacy.listUsers(cursor);
      kvUsers += page.keys.length;
      cursor = page.cursor;
    } while (cursor);
    let kvSessions = 0;
    cursor = null;
    do {
      const page = await legacy.listSessions(cursor);
      kvSessions += page.keys.length;
      cursor = page.cursor;
    } while (cursor);
    return json({
      phase,
      kv_users: kvUsers,
      db_users: Number(usersRow?.n ?? 0),
      kv_sessions: kvSessions,
      db_sessions: Number(sessionsRow?.n ?? 0),
      complete: Number(usersRow?.n ?? 0) >= kvUsers,
    });
  } else if (phase === 'finalize') {
    const db = env.DB;
    const usersRow = await db.prepare('SELECT COUNT(*) AS n FROM users').first();
    let kvUsers = 0;
    let cursor = null;
    do {
      const page = await legacy.listUsers(cursor);
      kvUsers += page.keys.length;
      cursor = page.cursor;
    } while (cursor);
    if (Number(usersRow?.n ?? 0) < kvUsers) {
      return json({ error: 'users not fully copied', db_users: Number(usersRow?.n ?? 0), kv_users: kvUsers }, 409);
    }
    await storage.migration.saveCheckpoint('cutover', {
      completed_at: new Date().toISOString(), kv_users: kvUsers, db_users: Number(usersRow?.n ?? 0),
    });
    return json({ phase, status: 'finalized' });
  } else if (phase === 'reverse_users') {
    // Rollback path: push D1 records back to KV where D1 is newer. Sessions
    // cannot be reversed from their hashes; a rollback invalidates sessions
    // created after the cutover and those clients log in again.
    const db = env.DB;
    const rows = await db.prepare('SELECT username, record, version FROM users ORDER BY username').all();
    for (const row of rows.results ?? []) {
      state.scanned += 1;
      const legacyRaw = await legacy.getUser(row.username);
      const legacyVersion = legacyRaw ? (Number(JSON.parse(legacyRaw).version) || 0) : -1;
      if (Number(row.version) <= legacyVersion) { state.skipped += 1; continue; }
      await legacy.putUser(row.username, JSON.parse(row.record));
      state.written += 1;
    }
    await storage.migration.saveCheckpoint(checkpointName, state);
    state.done = true;
  }

  return json({ phase, ...state });
}
