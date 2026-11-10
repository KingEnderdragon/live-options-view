import { R2_ORIGIN } from './common.js';
import { BROWSER_DATA_PREFIX, handleBrowserData, handleLiveQuotes } from './browser-data.js';
import { handleSettle, settleAllBots, settlementPriceServer, validateSettleRequest } from './settlement.js';
import { handleBotIndexReconcile, handleBotMetadata, handleBots } from './bots.js';
import { handleLogin, handleLogout, handleMe, handleRegister } from './auth.js';
import { handlePaperTrade } from './paper-trade.js';
import { handleMigration } from './migrate.js';
import { createStorage } from './storage.js';

// Re-exported from backend/common.js so the Worker's public API (the 31
// exports pinned by tests/worker_logic.test.js) stays on backend/index.js.
export { BOT_USERNAME_RE, STARTING_BALANCE, STRATEGY_ID_RE, computeBookFromTrades, parseCookies } from './common.js';
export { settleAllBots, settlementPriceServer, validateSettleRequest } from './settlement.js';
export { EXECUTION_RESERVATION_LEASE_MS, exactShareQuote, executionIntentMatches, executionPriceForOrder, executionReservationExpired, existingExecutionResponse, finalizeExecutionRequest, renewExecutionReservation, reserveExecutionRequest, validateTradeIntent } from './execution.js';
export { BOT_INDEX_KEY, handleBotIndexReconcile, handleBotMetadata, handleBots, netPositions, repairLegacyBotMarker, updateBotIndex } from './bots.js';
export { MAX_PASSWORD_LEN, MIN_PASSWORD_LEN, USERNAME_RE, constantTimeEqual, derivePasswordHash, randomSaltBase64 } from './auth.js';

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

    // Operator-only, issue #208 cutover route. Present only when
    // MIGRATION_TOKEN is configured; handleMigration answers 404 otherwise, so
    // an unconfigured deployment does not advertise it.
    if (request.method === 'POST' && url.pathname === '/api/admin/migrate') {
      return handleMigration(request, env, createStorage(env));
    }

    if (request.method === 'POST' && url.pathname === '/api/bot-index/reconcile') {
      return handleBotIndexReconcile(request, env);    }

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
