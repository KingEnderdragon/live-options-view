// backend/browser-data.js (B4) -- public read-only data: the /browser-data
// allowlist proxy and the /api/live-quotes passthrough, moved verbatim from
// worker.js. The router in worker.js still owns the route table.
// fetchLiveQuotes is exported for internal use by paper-trade/execution; it
// is not one of the Worker's public exports.

import { R2_ORIGIN, jsonResponse } from './common.js';

// Only these public artifacts are exposed to the browser through this route.
// Never use the private bucket binding or a caller-provided destination.
const BROWSER_DATA_ORIGIN = 'https://live-options-view.otmooper12.workers.dev';
export const BROWSER_DATA_PREFIX = '/browser-data/';
const BROWSER_DATA_KEYS = new Set([
  'intraday/latest.json',
  'intraday/spy/latest.json',
  'intraday/ibit/latest.json',
  'intraday/prices.json',
  'intraday/health.json',
  'derived/OIranges.csv',
  'macro/eia_steo.json',
  'manifest.json',
  'squeeze-scanner/v1/scheduled/latest.json',
  'squeeze-scanner/v1/scheduled/latest-attempt.json',
  'squeeze-scanner/v1/scheduled/latest-schedule.json',
]);
// Only the three QQQ archive filename families consumed by Historical.
// Validate calendar dates, not just eight arbitrary digits. No prefix proxying.
function isPublicArchiveKey(key) {
  const match = /^(?:raw\/(?:opex\/)?qqq_chain_|prices\/qqq_intraday_)([0-9]{4})([0-9]{2})([0-9]{2})\.csv$/.exec(key);
  if (!match) return false;
  const date = `${match[1]}-${match[2]}-${match[3]}`;
  const parsed = new Date(`${date}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date;
}
const MAX_LIVE_QUOTE_SYMBOLS = 100;
export async function handleBrowserData(request, url) {
  const origin = request.headers.get('Origin');
  if (url.origin !== BROWSER_DATA_ORIGIN ||
      (origin !== null && origin !== BROWSER_DATA_ORIGIN)) {
    return new Response('Forbidden', { status: 403 });
  }
  if (request.method !== 'GET') {
    return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET' } });
  }
  const key = url.pathname.slice(BROWSER_DATA_PREFIX.length);
  if (!BROWSER_DATA_KEYS.has(key) && !isPublicArchiveKey(key)) return new Response('Not found', { status: 404 });
  // Accept only the existing numeric cache-buster, never arbitrary query data.
  if (url.search && !/^\?_=[0-9]{1,20}$/.test(url.search)) {
    return new Response('Invalid query', { status: 400 });
  }
  try {
    const upstream = await fetch(`${R2_ORIGIN}/${key}${url.search}`, {
      method: 'GET',
      headers: {},
      credentials: 'omit',
      redirect: 'manual',
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    // Preserve status and stream bytes without parsing. Limit response headers
    // so upstream cookies/auth cannot affect this origin, and Location cannot
    // make the browser follow a redirect that the Worker refused to follow.
    const headers = new Headers();
    for (const name of ['Content-Type', 'Content-Encoding', 'Content-Length',
      'Cache-Control', 'ETag', 'Last-Modified', 'Retry-After']) {
      const value = upstream.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    return new Response(upstream.body, {
      status: upstream.status, statusText: upstream.statusText, headers,
    });
  } catch {
    return new Response('Public data upstream unavailable', { status: 502 });
  }
}
export async function handleLiveQuotes(url, env) {
  const symbols = parseRequestedSymbols(url.searchParams.get('symbols'));
  if (!symbols) {
    return jsonResponse({ error: `Request 1-${MAX_LIVE_QUOTE_SYMBOLS} valid symbols` }, 400);
  }
  try {
    const upstream = await fetchLiveQuotes(symbols, env);
    const response = new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'no-store',
      },
    });
    const retryAfter = upstream.headers.get('Retry-After');
    if (retryAfter) response.headers.set('Retry-After', retryAfter);
    return response;
  } catch (error) {
    return jsonResponse({ error: 'Live quote service unavailable' }, 503);
  }
}
export function fetchLiveQuotes(symbols, env) {
  if (!env.LIVE_QUOTE_ORIGIN || !env.LIVE_QUOTE_KEY) {
    throw new Error('Live quote service is not configured');
  }
  const upstreamUrl = new URL('/live-quotes', env.LIVE_QUOTE_ORIGIN);
  upstreamUrl.searchParams.set('symbols', symbols.join(','));
  // Live quote data must never be served from an intermediate cache: the
  // upstream URL is the cache key, and a stale cached 503 (pre-#222, no
  // session field) kept the option lane backing off after the producer was
  // redeployed, while new symbol lists returned fresh bodies. A per-request
  // nonce forces a fresh origin read every time. The collector ignores
  // unknown query params (it reads only `symbols`).
  upstreamUrl.searchParams.set('_', `${Date.now()}-${Math.random().toString(36).slice(2)}`);
  return fetch(upstreamUrl, {
    headers: { 'X-Live-Quote-Key': env.LIVE_QUOTE_KEY, 'Cache-Control': 'no-cache' },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
}
function parseRequestedSymbols(value) {
  if (!value) return null;
  const symbols = [...new Set(value.split(',').map(symbol => symbol.trim().replace(/\s/g, '')).filter(Boolean))];
  if (!symbols.length || symbols.length > MAX_LIVE_QUOTE_SYMBOLS) return null;
  if (symbols.some(symbol => symbol.length > 64 || !/^[A-Za-z0-9._/:+-]+$/.test(symbol))) return null;
  return symbols;
}
