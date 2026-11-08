import {
  ISO_DATE, MAX_BODY_BYTES, R2_ORIGIN,
  checkOrigin, computeBookFromTrades, jsonResponse, readLimited,
  requireSession, withUserRecord,
} from './worker/common.js';
import { BROWSER_DATA_PREFIX, fetchLiveQuotes, handleBrowserData, handleLiveQuotes } from './worker/browser-data.js';
import { handleSettle, settleAllBots, settlementPriceServer, validateSettleRequest } from './worker/settlement.js';
import { handleBotIndexReconcile, handleBotMetadata, handleBots } from './worker/bots.js';
import { handleLogin, handleLogout, handleMe, handleRegister } from './worker/auth.js';

// Re-exported from worker/common.js so the Worker's public API (the 32
// exports pinned by tests/worker_logic.test.js) stays on worker.js.
export { BOT_USERNAME_RE, STARTING_BALANCE, STRATEGY_ID_RE, computeBookFromTrades, parseCookies } from './worker/common.js';
export { settleAllBots, settlementPriceServer, validateSettleRequest } from './worker/settlement.js';
export { BOT_INDEX_KEY, handleBotIndexReconcile, handleBotMetadata, handleBots, netPositions, repairLegacyBotMarker, updateBotIndex } from './worker/bots.js';
export { MAX_PASSWORD_LEN, MIN_PASSWORD_LEN, USERNAME_RE, constantTimeEqual, derivePasswordHash, randomSaltBase64 } from './worker/auth.js';

const MAX_QUOTE_AGE_MS = 15 * 1000;
const MAX_QUOTE_FUTURE_MS = 30 * 1000;
const TRADEABLE_SHARE_SYMBOLS = new Set([
  'QQQ', 'USO', 'SMH', 'IGV', 'META', 'GOOGL', 'AMZN', 'TSLA', 'MU', 'SPCX', 'AAPL',
]);

export const EXECUTION_RESERVATION_LEASE_MS = 30 * 1000;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/browser-data' || url.pathname.startsWith(BROWSER_DATA_PREFIX)) {
      return handleBrowserData(request, url);
    }

    // Same-origin passthrough to the R2 bucket, fetched edge-to-edge by
    // Cloudflare rather than over the client's own network. Exists so
    // diag.html can compare "direct to the r2.dev subdomain" against "same
    // origin as the page" — if a client's network/DNS/carrier treats the two
    // hostnames differently, this tells them apart instead of leaving it a
    // guess. Not used by the main app yet.
    if (url.pathname.startsWith("/r2-proxy/")) {
      const key = url.pathname.slice("/r2-proxy/".length);
      const upstream = await fetch(`${R2_ORIGIN}/${key}${url.search}`, { cf: { cacheTtl: 0 } });
      const proxied = new Response(upstream.body, upstream);
      proxied.headers.set("Cache-Control", "no-store");
      proxied.headers.set("Access-Control-Allow-Origin", "*");
      return proxied;
    }

    if (request.method === 'POST' && url.pathname === '/api/register') {
      return handleRegister(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/login') {
      return handleLogin(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/logout') {
      return handleLogout(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/bots') {
      return handleBots(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-metadata') {
      return handleBotMetadata(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-index/reconcile') {
      return handleBotIndexReconcile(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/me') {
      return handleMe(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/paper-trade') {
      return handlePaperTrade(request, env);
    }

    if (request.method === 'POST' && url.pathname === '/api/settle') {
      return handleSettle(request, env);
    }

    if (request.method === 'GET' && url.pathname === '/api/live-quotes') {
      return handleLiveQuotes(url, env);
    }

    const response = await env.ASSETS.fetch(request);

    // HTML documents (the single-file app + diagnostic page) must never be
    // cached — a stale cached copy silently keeps serving old JS against a
    // live backend indefinitely, with no user-visible signal that anything
    // is wrong. Everything else (none currently — this is a single-file
    // static site) can use normal caching.
    if (url.pathname === "/" || url.pathname.endsWith(".html")) {
      const noCache = new Response(response.body, response);
      noCache.headers.set("Cache-Control", "no-store");
      return noCache;
    }

    return response;
  },

  // Cloudflare Cron Trigger (see wrangler.toml [triggers]). Settlement used to
  // be entirely client-triggered via /api/settle, called opportunistically
  // from the dashboard's own render loop -- fine for a human's own account,
  // but a bot nobody is watching never opens a dashboard, so its expired
  // positions just piled up unsettled (issue #42). This sweeps every bot
  // account server-side on a schedule instead of relying on a viewer.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(settleAllBots(env));
  },
}





// ---------------------------------------------------------------------------
// Auth: username/password accounts backed by the USERS KV namespace, opaque
// session tokens backed by SESSIONS. Real auth (unlike the old shared-token
// deterrent below) because a user's balance now needs to follow them across
// devices rather than living in one browser's localStorage.
// ---------------------------------------------------------------------------










// ---------------------------------------------------------------------------
// Trade execution
// ---------------------------------------------------------------------------

// Serializes each client-generated request id with a leased R2 `pending`
// reservation. The owner refreshes the reservation immediately before account
// mutation; that conditional ETag write fences off stale owners after a retry
// takes over an abandoned lease. Rejections finalize the same reservation for
// idempotent replay.
//
// Auth is now a real session (see requireSession above), not the shared
// static token this endpoint used to check. The Origin allowlist, a
// Cloudflare rate limiting rule (configured at the zone level, not in this
// file), and the body-size cap remain as defense in depth.
async function handlePaperTrade(request, env) {
  const session = await requireSession(request, env);
  if (!session) return new Response('Forbidden', { status: 401 });

  const originError = checkOrigin(request);
  if (originError) return originError;

  const contentLength = Number(request.headers.get('Content-Length') || 0);
  if (contentLength > MAX_BODY_BYTES) {
    return new Response('Payload too large', { status: 413 });
  }

  let raw;
  try {
    raw = await readLimited(request, MAX_BODY_BYTES);
  } catch (e) {
    return new Response('Payload too large', { status: 413 });
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    return new Response('Invalid JSON', { status: 400 });
  }
  const validationError = validateTradeIntent(body);
  if (validationError) {
    return new Response(validationError, { status: 400 });
  }

  const executionId = body.execution_request_id;
  const key = `paper-trades/requests/${executionId}.json`;
  let reserved;

  const responseForStoredOutcome = async (storedOutcome, etag) => {
    if (storedOutcome.username !== session.username || !executionIntentMatches(storedOutcome, body)) {
      return jsonResponse({ error: 'execution_request_id conflicts with a different trade intent' }, 409);
    }
    if (storedOutcome.status === 'pending') {
      // Closure and its exact rejected intent are retained together in KV.
      // Recover a crash after closure but before R2 outcome finalization,
      // without obtaining a new quote or attempting another account mutation.
      const closure = session.record.closure_execution;
      if (session.record.account_closed && closure?.execution_request_id === executionId) {
        try {
          const recovered = await finalizeExecutionRequest(env.PAPER_TRADES, key, closure, etag);
          return existingExecutionResponse(recovered.outcome, body);
        } catch (error) {
          return jsonResponse({ error: 'Closed-account rejection could not be recovered' }, 503);
        }
      }
      const appliedTrade = session.record.trades.find(t => t.execution_request_id === executionId);
      if (!appliedTrade) {
        if (session.record.account_closed) {
          try {
            const recovered = await finalizeExecutionRequest(env.PAPER_TRADES, key,
              { ...storedOutcome, status: 'rejected', http_status: 403,
                error: 'Account closed', account_closed: true }, etag);
            return existingExecutionResponse(recovered.outcome, body);
          } catch (error) {
            return jsonResponse({ error: 'Closed-account pending intent could not be recovered' }, 503);
          }
        }
        return jsonResponse({
          error: 'Execution is already pending',
          order_id: executionId,
          status: 'pending',
        }, 409);
      }
      try {
        const recovered = await finalizeExecutionRequest(env.PAPER_TRADES, key, appliedTrade, etag);
        storedOutcome = recovered.outcome;
      } catch (error) {
        return jsonResponse({ error: 'Pending execution could not be recovered' }, 503);
      }
    }
    if (storedOutcome.status === 'rejected') return existingExecutionResponse(storedOutcome, body);
    if (storedOutcome.status !== 'filled' || !Number.isFinite(storedOutcome.price)) {
      return jsonResponse({ error: 'Execution state is invalid' }, 503);
    }
    return jsonResponse({ ...storedOutcome, balance_cash: session.record.balance_cash }, 200);
  };

  try {
    const existing = await env.PAPER_TRADES.get(key);
    if (existing) {
      const storedOutcome = await existing.json();
      const appliedTrade = session.record.trades.find(t => t.execution_request_id === executionId);
      if (
        storedOutcome.username !== session.username ||
        !executionIntentMatches(storedOutcome, body) ||
        storedOutcome.status !== 'pending' ||
        session.record.account_closed ||
        appliedTrade ||
        !executionReservationExpired(storedOutcome)
      ) {
        return responseForStoredOutcome(storedOutcome, existing.etag);
      }

      const renewed = await renewExecutionReservation(
        env.PAPER_TRADES,
        key,
        storedOutcome,
        existing.etag,
        new Date().toISOString(),
      );
      if (!renewed.updated) return responseForStoredOutcome(renewed.outcome, renewed.etag);
      reserved = renewed;
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution state unavailable' }, 503);
  }

  if (session.record.account_closed) {
    return jsonResponse({ error: 'Account closed', account_closed: true,
      closure_reason: session.record.closure_reason }, 403);
  }

  let quotePayload;
  try {
    const quoteResponse = await fetchLiveQuotes([body.sym], env);
    if (!quoteResponse.ok) return quoteProviderErrorResponse(quoteResponse);
    quotePayload = await quoteResponse.json();
  } catch (error) {
    return jsonResponse({ error: 'Fresh quote unavailable' }, 503);
  }
  const quoteReceivedAt = new Date();

  const quoteResult = exactExecutionQuote(quotePayload, body, quoteReceivedAt.getTime());
  if (quoteResult.error) {
    return jsonResponse({ error: quoteResult.error }, quoteResult.status);
  }

  const executedAt = new Date();
  const priceResult = executionPriceForOrder(quoteResult, body);
  const common = {
    execution_id: executionId,
    execution_request_id: executionId,
    ts: executedAt.toISOString(),
    quote_received_ts: quoteReceivedAt.toISOString(),
    quote_ts: quoteResult.quoteTs,
    bid_ts: quoteResult.bidTs,
    ask_ts: quoteResult.askTs,
    sym: body.sym,
    instrument_type: body.instrument_type ?? 'option',
    multiplier: quoteResult.multiplier,
    strike: quoteResult.strike,
    type: quoteResult.type,
    exp: quoteResult.exp,
    side: body.side,
    qty: body.qty,
    order_id: executionId,
    order_type: body.order_type ?? 'market',
    limit_price: body.order_type === 'limit' ? body.limit_price : null,
    bid: quoteResult.bid,
    ask: quoteResult.ask,
    username: session.username,
  };
  const candidate = Object.freeze(priceResult.error
    ? { ...common, error: priceResult.error, status: 'rejected', http_status: 409 }
    : { ...common, status: 'filled', price: priceResult.price });
  if (!reserved) {
    const reservation = Object.freeze({
      execution_request_id: executionId,
      sym: body.sym,
      instrument_type: body.instrument_type ?? 'option',
      side: body.side,
      qty: body.qty,
      order_type: body.order_type ?? 'market',
      limit_price: body.order_type === 'limit' ? body.limit_price : null,
      username: session.username,
      status: 'pending',
      ts: executedAt.toISOString(),
    });
    try {
      reserved = await reserveExecutionRequest(env.PAPER_TRADES, key, reservation);
    } catch (error) {
      return jsonResponse({ error: 'Execution could not be reserved' }, 503);
    }
    if (!reserved.created) return responseForStoredOutcome(reserved.outcome, reserved.etag);
  }

  if (candidate.status === 'rejected') {
    try {
      const finalized = await finalizeExecutionRequest(env.PAPER_TRADES, key, candidate, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Execution rejection could not be recorded' }, 503);
    }
  }

  const trade = candidate;
  // Buys debit cash and are capped by balance below. Option sells retain the
  // existing uncovered-short behavior; share sells are constrained to the
  // held long quantity inside the account mutation below.
  const multiplier = trade.multiplier ?? 100;
  const cashDelta = trade.side === 'buy'
    ? -(trade.price * trade.qty * multiplier)
    : (trade.price * trade.qty * multiplier);

  try {
    const fenced = await renewExecutionReservation(
      env.PAPER_TRADES,
      key,
      reserved.outcome,
      reserved.etag,
      new Date().toISOString(),
    );
    if (!fenced.updated) return responseForStoredOutcome(fenced.outcome, fenced.etag);
    reserved = fenced;
  } catch (error) {
    return jsonResponse({ error: 'Execution lease could not be refreshed' }, 503);
  }

  const kvOutcome = await withUserRecord(env, session.username, (record) => {
    const existingTrade = record.trades.find(t => t.execution_request_id === executionId);
    if (existingTrade) {
      return { result: { trade: existingTrade, balance_cash: record.balance_cash } };
    }
    if (record.account_closed) return { error: 'account_closed' };
    if (trade.side === 'buy' && -cashDelta > record.balance_cash) {
      if (record.is_bot) {
        const rejection = { ...common, error: 'Insufficient balance', status: 'rejected',
          http_status: 400, account_closed: true };
        return { record: { ...record, account_closed: true, closed_at: executedAt.toISOString(),
          closure_reason: 'insufficient_balance', closure_execution: rejection },
          result: { balance_rejection: rejection } };
      }
      return { error: 'insufficient_balance' };
    }
    if ((trade.instrument_type ?? 'option') === 'share' && trade.side === 'sell') {
      const held = computeBookFromTrades(record.trades)[trade.sym]?.pos ?? 0;
      if (held < trade.qty) return { error: 'insufficient_position' };
    }
    const balance_cash = record.balance_cash + cashDelta;
    return {
      record: { ...record, balance_cash, trades: [...record.trades, trade] },
      result: { trade, balance_cash },
    };
  });

  if (kvOutcome.error === 'insufficient_balance' || kvOutcome.result?.balance_rejection) {
    const rejection = Object.freeze(kvOutcome.result?.balance_rejection ?? {
      ...common,
      error: 'Insufficient balance',
      status: 'rejected',
      http_status: 400,
    });
    try {
      const finalized = await finalizeExecutionRequest(env.PAPER_TRADES, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Balance rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error === 'account_closed') {
    const rejection = { ...common, error: 'Account closed', account_closed: true,
      status: 'rejected', http_status: 403 };
    try {
      const finalized = await finalizeExecutionRequest(env.PAPER_TRADES, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Account closure rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error === 'insufficient_position') {
    const rejection = Object.freeze({
      ...common,
      error: 'Cannot sell more shares than are held',
      status: 'rejected',
      http_status: 400,
    });
    try {
      const finalized = await finalizeExecutionRequest(env.PAPER_TRADES, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Position rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error) {
    return jsonResponse({ error: 'Balance could not be updated, try again' }, 503);
  }

  try {
    const finalized = await finalizeExecutionRequest(env.PAPER_TRADES, key, trade, reserved.etag);
    if (finalized.outcome.status !== 'filled') {
      return jsonResponse({ error: 'Execution finalization conflicted' }, 503);
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution could not be finalized' }, 503);
  }

  return jsonResponse({ ...trade, balance_cash: kvOutcome.result.balance_cash }, 201);
}







const MAX_STRING_LEN = 64;

// Schema check for a trade record before it's written to R2. This log is
// meant to become cross-strategy analysis data, so loose validation here
// (accepting non-numeric prices, negative quantities, arbitrary side/type
// strings, unbounded text fields) is how the dataset silently rots — a
// malformed record parses fine as JSON but breaks downstream analysis with
// no visible failure in the UI. Returns an error string, or null if valid.
export function validateTradeIntent(body) {
  if (!body || typeof body !== 'object') return 'Body must be a JSON object';

  if (typeof body.execution_request_id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.execution_request_id)) {
    return 'execution_request_id must be a UUID';
  }
  if (typeof body.sym !== 'string' || !body.sym.trim() || body.sym.length > MAX_STRING_LEN) {
    return 'sym must be a non-empty string';
  }
  const instrumentType = body.instrument_type ?? 'option';
  if (instrumentType !== 'option' && instrumentType !== 'share') {
    return 'instrument_type must be "option" or "share"';
  }
  if (instrumentType === 'share' && !TRADEABLE_SHARE_SYMBOLS.has(body.sym)) {
    return 'share symbol is not in the tradeable equity list';
  }
  if (body.side !== 'buy' && body.side !== 'sell') {
    return 'side must be "buy" or "sell"';
  }
  if (!Number.isInteger(body.qty) || body.qty <= 0 || body.qty > 100_000) {
    return 'qty must be a positive integer';
  }

  const orderType = body.order_type ?? 'market';
  if (orderType !== 'market' && orderType !== 'limit') {
    return 'order_type must be "market" or "limit"';
  }
  if (orderType === 'market' && body.limit_price !== undefined) {
    return 'limit_price is only allowed for limit orders';
  }
  if (orderType === 'limit') {
    if (typeof body.limit_price !== 'number' || !Number.isFinite(body.limit_price) ||
        body.limit_price <= 0 || body.limit_price > 1_000_000) {
      return 'limit_price must be a positive finite number';
    }
    if (Math.abs(body.limit_price * 100 - Math.round(body.limit_price * 100)) > 1e-7) {
      return 'limit_price must use increments of 0.01';
    }
  }

  return null;
}

export function executionIntentMatches(trade, intent) {
  return trade?.execution_request_id === intent.execution_request_id &&
    trade?.sym === intent.sym &&
    (trade?.instrument_type ?? 'option') === (intent.instrument_type ?? 'option') &&
    trade?.side === intent.side &&
    trade?.qty === intent.qty &&
    (trade?.order_type ?? 'market') === (intent.order_type ?? 'market') &&
    (trade?.limit_price ?? null) === (intent.limit_price ?? null);
}

export function executionPriceForOrder(quote, intent) {
  const price = intent.side === 'buy' ? quote.ask : quote.bid;
  if ((intent.order_type ?? 'market') !== 'limit') return { price };

  if (intent.side === 'buy' && price > intent.limit_price) {
    return { error: `Buy limit $${intent.limit_price.toFixed(2)} is below current ask $${price.toFixed(2)}` };
  }
  if (intent.side === 'sell' && price < intent.limit_price) {
    return { error: `Sell limit $${intent.limit_price.toFixed(2)} is above current bid $${price.toFixed(2)}` };
  }
  return { price };
}

export function existingExecutionResponse(outcome, intent) {
  if (!executionIntentMatches(outcome, intent)) {
    return jsonResponse({ error: 'execution_request_id conflicts with a different trade intent' }, 409);
  }
  return jsonResponse(outcome, outcome.status === 'rejected' ? (outcome.http_status ?? 409) : 200);
}

export async function reserveExecutionRequest(bucket, key, reservation) {
  const stored = await bucket.put(key, JSON.stringify(reservation), {
    onlyIf: { etagDoesNotMatch: '*' },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: { executionId: reservation.execution_request_id, reservedAt: reservation.ts },
  });
  if (stored) return { outcome: reservation, created: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution reservation missing after conditional write');
  return { outcome: await existing.json(), created: false, etag: existing.etag };
}

export function executionReservationExpired(
  reservation,
  nowMs = Date.now(),
  leaseMs = EXECUTION_RESERVATION_LEASE_MS,
) {
  if (reservation?.status !== 'pending') return false;
  const reservedAt = Date.parse(reservation.ts);
  return !Number.isFinite(reservedAt) || nowMs - reservedAt >= leaseMs;
}

export async function renewExecutionReservation(bucket, key, reservation, reservationEtag, renewedAt) {
  const renewed = Object.freeze({ ...reservation, status: 'pending', ts: renewedAt });
  const stored = await bucket.put(key, JSON.stringify(renewed), {
    onlyIf: { etagMatches: reservationEtag },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: {
      executionId: renewed.execution_request_id,
      reservedAt: renewed.ts,
    },
  });
  if (stored) return { outcome: renewed, updated: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution reservation missing after conditional renewal');
  return { outcome: await existing.json(), updated: false, etag: existing.etag };
}

export async function finalizeExecutionRequest(bucket, key, terminalOutcome, reservationEtag) {
  const stored = await bucket.put(key, JSON.stringify(terminalOutcome), {
    onlyIf: { etagMatches: reservationEtag },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: {
      executionId: terminalOutcome.execution_request_id,
      executedAt: terminalOutcome.ts,
    },
  });
  if (stored) return { outcome: terminalOutcome, updated: true, etag: stored.etag };

  const existing = await bucket.get(key);
  if (!existing) throw new Error('Execution outcome missing after conditional write');
  return { outcome: await existing.json(), updated: false, etag: existing.etag };
}

async function quoteProviderErrorResponse(response) {
  const status = response.status >= 400 && response.status <= 599 ? response.status : 503;
  let upstream = {};
  try {
    upstream = await response.json();
  } catch (error) {
    // Keep the public error stable even if the provider returns non-JSON.
  }
  const headers = {};
  const retryAfter = response.headers.get('Retry-After');
  if (retryAfter) headers['Retry-After'] = retryAfter;
  return jsonResponse(
    { error: upstream.error || 'Fresh quote unavailable', provider_status: status },
    status,
    headers,
  );
}

function exactExecutionQuote(payload, intent, nowMs) {
  if ((intent.instrument_type ?? 'option') === 'share') {
    return exactShareQuote(payload, intent.sym, intent.side, nowMs);
  }
  return exactContractQuote(payload, intent.sym, intent.side, nowMs);
}

export function exactShareQuote(payload, symbol, side, nowMs) {
  const row = Array.isArray(payload?.quotes)
    ? payload.quotes.find(candidate => candidate.symbol === symbol)
    : null;
  if (!row) return { error: 'Exact share quote not found', status: 409 };
  if (row.instrument_class !== 'equity') {
    return { error: 'Symbol is not a tradeable equity', status: 409 };
  }
  const bidMs = Date.parse(row.bid_ts);
  const askMs = Date.parse(row.ask_ts);
  const sideMs = side === 'buy' ? askMs : bidMs;
  if (!Number.isFinite(sideMs)) return { error: `Share quote has no valid ${side} timestamp`, status: 503 };
  const age = nowMs - sideMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: 'Share quote is stale', status: 409 };
  }
  const bid = row.bid;
  const ask = row.ask;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask <= 0 || bid > ask) {
    return { error: 'Exact share quote is invalid', status: 409 };
  }
  return {
    bid, ask, strike: null, type: null, exp: null,
    multiplier: 1,
    bidTs: Number.isFinite(bidMs) ? new Date(bidMs).toISOString() : null,
    askTs: Number.isFinite(askMs) ? new Date(askMs).toISOString() : null,
    quoteTs: new Date(sideMs).toISOString(),
  };
}

function exactContractQuote(payload, symbol, side, nowMs) {
  const row = Array.isArray(payload?.quotes)
    ? payload.quotes.find(candidate => candidate.symbol === symbol)
    : null;
  if (!row) return { error: 'Exact contract quote not found', status: 409 };
  const bidMs = Date.parse(row.bid_ts);
  const askMs = Date.parse(row.ask_ts);
  const sideMs = side === 'buy' ? askMs : bidMs;
  if (!Number.isFinite(sideMs)) return { error: `Quote has no valid ${side} timestamp`, status: 503 };
  const age = nowMs - sideMs;
  if (age > MAX_QUOTE_AGE_MS || age < -MAX_QUOTE_FUTURE_MS) {
    return { error: `${side === 'buy' ? 'Ask' : 'Bid'} quote is stale`, status: 409 };
  }
  const bid = row.bid;
  const ask = row.ask;
  if (typeof bid !== 'number' || typeof ask !== 'number' ||
      !Number.isFinite(bid) || !Number.isFinite(ask) || bid < 0 || ask < 0 || bid > ask) {
    return { error: 'Exact contract quote is invalid', status: 409 };
  }
  const strike = Number(row.strike);
  if (!Number.isFinite(strike) || strike <= 0 || !['call', 'put'].includes(row.type) ||
      typeof row.exp !== 'string' || !ISO_DATE.test(row.exp)) {
    return { error: 'Exact contract metadata is invalid', status: 409 };
  }
  return {
    bid, ask, strike, type: row.type, exp: row.exp,
    multiplier: 100,
    bidTs: Number.isFinite(bidMs) ? new Date(bidMs).toISOString() : null,
    askTs: Number.isFinite(askMs) ? new Date(askMs).toISOString() : null,
    quoteTs: new Date(sideMs).toISOString(),
  };
}


