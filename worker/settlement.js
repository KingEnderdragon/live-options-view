// worker/settlement.js (B4) -- settlement: the pure pricing/validation
// functions (this slice), plus the settlement machinery and the /api/settle
// route (slice e). Moved verbatim from worker.js; the entry re-exports the
// public names. computeBookFromTrades lives in worker/common.js, not here:
// the bot roster, paper trading, and settlement all need it (see the B4 run
// log for the cycle it avoids).

import { ISO_DATE } from './common.js';

// Longs settle worthless -- this book has no exercise capacity, so an ITM
// long must be closed before expiration to realize value. Shorts don't get
// that same free pass: a real counterparty will exercise an ITM option
// against a short seller regardless of what this account can cover, so
// shorts settle at intrinsic value using the caller-supplied spot for that
// expiration.
export function settlementPriceServer(b, spotMarks) {
  if (b.pos > 0) return 0;
  const spot = spotMarks[b.exp];
  if (spot == null) return 0;
  return b.type === 'call' ? Math.max(spot - b.strike, 0) : Math.max(b.strike - spot, 0);
}

export function validateSettleRequest(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';
  if (typeof body.as_of !== 'string' || !ISO_DATE.test(body.as_of)) return 'as_of must be an ISO date string';
  if (!body.spot_marks || typeof body.spot_marks !== 'object' || Array.isArray(body.spot_marks)) {
    return 'spot_marks must be an object';
  }
  for (const [exp, spot] of Object.entries(body.spot_marks)) {
    if (!ISO_DATE.test(exp)) return 'spot_marks keys must be ISO dates';
    if (typeof spot !== 'number' || !Number.isFinite(spot) || spot < 0) return 'spot_marks values must be non-negative numbers';
  }
  return null;
}
