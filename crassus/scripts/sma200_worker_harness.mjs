// Local, in-memory Worker for the #97 integrated lifecycle test.
// It runs the real backend/index.js fetch handler over Node HTTP; no deployed
// Worker, account or external provider is touched. The live-quote origin is a
// second local server returning a deterministic SPY quote.
import http from 'node:http';
import worker from '../../backend/index.js';

const WORKER_PORT = 8899;
const QUOTE_PORT = 8898;
const BOT_KEY = 'sma-integration-key';

const users = new Map();
const sessions = new Map();
const objects = new Map();
let revision = 0;

function kv(store) {
  return {
    async get(key) { return store.get(key) ?? null; },
    async put(key, value) { store.set(key, String(value)); },
    async delete(key) { store.delete(key); },
    async list({ prefix = '', limit = 1000 } = {}) {
      return { keys: [...store.keys()].filter(k => k.startsWith(prefix)).slice(0, limit).map(name => ({ name })), list_complete: true };
    },
  };
}

const r2 = {
  async get(key) {
    const item = objects.get(key);
    return item ? { etag: item.etag, json: async () => JSON.parse(item.value) } : null;
  },
  async put(key, value, options = {}) {
    const current = objects.get(key);
    if (options.onlyIf?.etagDoesNotMatch === '*' && current) return null;
    if (options.onlyIf?.etagMatches && current?.etag !== options.onlyIf.etagMatches) return null;
    const etag = `e${++revision}`;
    objects.set(key, { value: String(value), etag });
    return { etag };
  },
  async delete(key) { objects.delete(key); },
  async list({ prefix = '', limit = 1000 } = {}) {
    return { objects: [...objects.keys()].filter(k => k.startsWith(prefix)).slice(0, limit).map(key => ({ key })) };
  },
};

const env = {
  USERS: kv(users), SESSIONS: kv(sessions), PAPER_TRADES: r2,
  BOT_REGISTRATION_KEY: BOT_KEY,
  LIVE_QUOTE_ORIGIN: `http://127.0.0.1:${QUOTE_PORT}`,
  LIVE_QUOTE_KEY: 'local-quote-key',
  BOT_INDEX_READS: 'r2',
  ASSETS: { async fetch() { return new Response('not found', { status: 404 }); } },
};

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

const quoteServer = http.createServer((req, res) => {
  const now = new Date().toISOString();
  const body = JSON.stringify({
    server_ts: now,
    quotes: [{
      symbol: 'SPY', instrument_class: 'equity', bid: 499.9, ask: 500.1,
      quote_ts: now, bid_ts: now, ask_ts: now,
    }],
  });
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(body);
});

const workerServer = http.createServer(async (req, res) => {
  try {
    const body = await readBody(req);
    const request = new Request(`http://127.0.0.1:${WORKER_PORT}${req.url}`, {
      method: req.method,
      headers: req.headers,
      body: body.length ? body : undefined,
    });
    const response = await worker.fetch(request, env);
    res.writeHead(response.status, Object.fromEntries(response.headers.entries()));
    const buffer = Buffer.from(await response.arrayBuffer());
    res.end(buffer);
  } catch (error) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: String(error) }));
  }
});

await new Promise(resolve => quoteServer.listen(QUOTE_PORT, '127.0.0.1', resolve));
await new Promise(resolve => workerServer.listen(WORKER_PORT, '127.0.0.1', resolve));
console.log(JSON.stringify({ ready: true, worker: WORKER_PORT, quotes: QUOTE_PORT, bot_key: BOT_KEY }));

const shutdown = () => {
  workerServer.close();
  quoteServer.close();
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
