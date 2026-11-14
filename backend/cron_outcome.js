// backend/cron_outcome.js (E2) -- structured outcome records for the
// settlement Cron Trigger.
//
// Each scheduled invocation emits, as one-line JSON via console.log /
// console.error (Workers Logs / Tail pick these up):
//   phase "invoked"    -- the handler started (always first)
//   phase "completed"  -- the run finished: outcome "settled" or "no_op",
//                         with a no_op_reason and counts-only totals
//   phase "error"      -- the run threw; the error is re-thrown afterwards so
//                         the invocation still fails exactly as it did before
// All three share run_id, cron and scheduled_time, and carry an ISO
// timestamp, so a retained invocation can be matched to its outcome.
//
// Privacy: records hold counts, the as-of date and an error's class plus a
// bounded message. They never include usernames, balances, trades, prices,
// tokens or request bodies; the summary is whitelisted field by field.

export const CRON_EVENT = 'settlement_cron';

const SUMMARY_FIELDS = [
  'as_of', 'accounts_indexed', 'accounts_ineligible', 'accounts_with_expired',
  'expirations_needed', 'marks_resolved', 'marks_missing', 'mark_errors',
  'accounts_settled', 'positions_settled', 'accounts_unchanged',
  'accounts_liquidated', 'accounts_retry',
];
const MAX_ERROR_MESSAGE = 200;

function isoOrNull(ms) {
  return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

function newRunId() {
  return globalThis.crypto?.randomUUID?.() ?? `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createCronOutcomeLog(event, { now = () => Date.now(), log = console.log,
  logError = console.error, runId = newRunId() } = {}) {
  const base = {
    event: CRON_EVENT,
    run_id: runId,
    cron: typeof event?.cron === 'string' ? event.cron : null,
    scheduled_time: isoOrNull(event?.scheduledTime),
  };
  const startedAt = now();
  let finished = false;
  const emit = (sink, record) => sink(JSON.stringify({ ...base, ...record }));

  return {
    invoked() {
      emit(log, { phase: 'invoked', at: new Date(startedAt).toISOString() });
    },
    completed(summary) {
      if (finished) return;
      finished = true;
      const at = now();
      const counts = {};
      for (const field of SUMMARY_FIELDS) {
        if (summary && Object.hasOwn(summary, field)) counts[field] = summary[field];
      }
      const settledSomething = (summary?.accounts_settled ?? 0) > 0 || (summary?.accounts_liquidated ?? 0) > 0;
      emit(log, {
        phase: 'completed',
        outcome: settledSomething ? 'settled' : 'no_op',
        no_op_reason: settledSomething ? null : (summary?.no_op_reason ?? 'unspecified'),
        at: new Date(at).toISOString(),
        duration_ms: at - startedAt,
        ...counts,
      });
    },
    error(stage, error) {
      if (finished) return;
      finished = true;
      const at = now();
      const message = String(error?.message ?? error ?? '');
      emit(logError, {
        phase: 'error',
        stage,
        error_name: typeof error?.name === 'string' ? error.name : typeof error,
        error_message: message.length > MAX_ERROR_MESSAGE ? `${message.slice(0, MAX_ERROR_MESSAGE)}...` : message,
        at: new Date(at).toISOString(),
        duration_ms: at - startedAt,
      });
    },
  };
}
