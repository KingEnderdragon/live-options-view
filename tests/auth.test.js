// B2 (readability quest): the auth section moved out of index.html into
// site/js/auth.js, loaded in the same position. The test pins the ?v= hash
// in the page to the module's content (like shared.js), asserts the section
// no longer lives inline, and drives the gate / margin-call screen /
// submitAuth / refreshSessionFromServer with a stub DOM + stub fetch:
// the login/register gate text, the session-gated panel swap, the
// margin-call clear-out, the /api/login vs /api/register routing, and the
// /api/me refresh paths (200, non-ok, thrown fetch).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../site/js/auth.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/auth.js?v=' + moduleHash), 'auth script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function renderAuthGate'), 'renderAuthGate must live in js/auth.js, not inline in index.html');
assert.ok(!html.includes('function showMarginCallScreen'), 'showMarginCallScreen must live in js/auth.js, not inline in index.html');
assert.ok(!html.includes('function hideMarginCallScreen'), 'hideMarginCallScreen must live in js/auth.js, not inline in index.html');
assert.ok(!html.includes('async function submitAuth'), 'submitAuth must live in js/auth.js, not inline in index.html');
assert.ok(!html.includes('async function refreshSessionFromServer'), 'refreshSessionFromServer must live in js/auth.js, not inline in index.html');
assert.ok(!html.includes("let authMode = 'login'"), 'authMode must live in js/auth.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── auth'), 'auth banner must stay in index.html as the section marker');

// --- stub DOM: panels + gate controls with a recording classList. ---
function makeEl() {
  const classes = new Set();
  return {
    textContent: '', value: '', disabled: false, autocomplete: '',
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    _classes: classes,
  };
}
const els = {};
for (const id of ['auth-panel', 'paper-panel', 'auth-heading', 'auth-submit',
                  'auth-toggle', 'auth-warning', 'auth-password', 'auth-error',
                  'margin-call-reason', 'margin-call-screen']) {
  els[id] = makeEl();
}
// The shipped markup starts logged out: the auth panel is visible.
els['paper-panel']._classes.add('hidden');
els['auth-warning']._classes.add('hidden');
els['margin-call-screen']._classes.add('hidden');

// --- harness: the page's shared scope, as the module sees it. ---
// `session`, `SESSION_CACHE_KEY` and `cacheSessionDisplay()` stay declared
// in the paper-trading section (they are the page's shared scope); the
// module reads and assigns them, so the sandbox provides them here.
let fetchCalls = [];
let nextResponse = null; // a response object, or an Error to throw
let cacheCalls = 0;
let removedKeys = [];
const sandbox = {
  console,
  document: { getElementById: id => els[id] },
  localStorage: {
    getItem: () => null,
    setItem() {},
    removeItem: key => { removedKeys.push(key); },
  },
  SESSION_CACHE_KEY: 'paperbook_session_cache',
  session: null,
  cacheSessionDisplay: () => { cacheCalls++; },
  fetch: async (url, options) => {
    fetchCalls.push({ url, options });
    if (nextResponse instanceof Error) throw nextResponse;
    return nextResponse;
  },
};
const context = vm.createContext(sandbox);
vm.runInContext(moduleSource, context, { filename: 'site/js/auth.js' });
const get = name => vm.runInContext(name, context);
const set = (name, value) => vm.runInContext(`${name} = (${JSON.stringify(value)})`, context);

assert.equal(typeof get('renderAuthGate'), 'function', 'module must define renderAuthGate');
assert.equal(typeof get('showMarginCallScreen'), 'function', 'module must define showMarginCallScreen');
assert.equal(typeof get('hideMarginCallScreen'), 'function', 'module must define hideMarginCallScreen');
assert.equal(typeof get('submitAuth'), 'function', 'module must define submitAuth');
assert.equal(typeof get('refreshSessionFromServer'), 'function', 'module must define refreshSessionFromServer');
assert.equal(get('authMode'), 'login', 'authMode starts in login mode');

const hidden = id => els[id]._classes.has('hidden');

// --- renderAuthGate: logged out, login mode (the shipped default). ---
get('renderAuthGate')();
assert.equal(els['auth-heading'].textContent, 'Log In');
assert.equal(els['auth-submit'].textContent, 'Log In');
assert.equal(els['auth-toggle'].textContent, 'Need an account? Register');
assert.equal(hidden('auth-warning'), true, 'no-recovery warning is register-only');
assert.equal(els['auth-password'].autocomplete, 'current-password');
assert.equal(els['auth-error'].textContent, '');
assert.equal(hidden('auth-panel'), false, 'gate shown while logged out');
assert.equal(hidden('paper-panel'), true, 'paper panel hidden while logged out');

// --- renderAuthGate: logged out, register mode. ---
vm.runInContext("authMode = 'register'", context);
get('renderAuthGate')();
assert.equal(els['auth-heading'].textContent, 'Register');
assert.equal(els['auth-submit'].textContent, 'Create Account');
assert.equal(els['auth-toggle'].textContent, 'Have an account? Log in');
assert.equal(hidden('auth-warning'), false, 'register mode shows the no-recovery warning');
assert.equal(els['auth-password'].autocomplete, 'new-password');
assert.equal(hidden('auth-panel'), false);
assert.equal(hidden('paper-panel'), true);
vm.runInContext("authMode = 'login'", context);

// --- renderAuthGate: logged in swaps the panels. ---
set('session', { username: 'owen' });
get('renderAuthGate')();
assert.equal(hidden('auth-panel'), true, 'gate hidden while logged in');
assert.equal(hidden('paper-panel'), false, 'paper panel shown while logged in');
set('session', null);
get('renderAuthGate')();
assert.equal(hidden('auth-panel'), false, 'gate returns when the session clears');

// --- showMarginCallScreen: clears the session + cache, shows the reason. ---
set('session', { username: 'owen' });
get('showMarginCallScreen')('A settlement obligation exceeded the account balance');
assert.equal(get('session'), null, 'margin call clears the session');
assert.deepEqual(removedKeys, ['paperbook_session_cache'], 'margin call drops the cached session display');
assert.equal(els['margin-call-reason'].textContent, 'A settlement obligation exceeded the account balance');
assert.equal(hidden('margin-call-screen'), false, 'margin-call screen shown');
assert.equal(hidden('auth-panel'), false, 'gate re-rendered behind the margin-call screen');
assert.equal(els['auth-heading'].textContent, 'Log In');

// --- hideMarginCallScreen: hides the screen, resets to login mode. ---
vm.runInContext("authMode = 'register'", context);
get('hideMarginCallScreen')();
assert.equal(hidden('margin-call-screen'), true, 'margin-call screen hidden');
assert.equal(get('authMode'), 'login', 'dismissing a margin call resets to login mode');
assert.equal(els['auth-heading'].textContent, 'Log In');

const response = (data, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => data,
});

(async () => {
  // --- submitAuth: login posts to /api/login and returns the session. ---
  nextResponse = response({ username: 'owen', balance: 10000 });
  const loggedIn = await get('submitAuth')('owen', 'pw');
  assert.equal(loggedIn.username, 'owen');
  assert.equal(fetchCalls.at(-1).url, '/api/login');
  assert.equal(fetchCalls.at(-1).options.method, 'POST');
  assert.equal(fetchCalls.at(-1).options.credentials, 'same-origin');
  assert.equal(fetchCalls.at(-1).options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), { username: 'owen', password: 'pw' });

  // --- submitAuth: register mode posts to /api/register instead. ---
  vm.runInContext("authMode = 'register'", context);
  nextResponse = response({ username: 'owen' });
  await get('submitAuth')('owen', 'pw');
  assert.equal(fetchCalls.at(-1).url, '/api/register');
  vm.runInContext("authMode = 'login'", context);

  // --- submitAuth: a server error surfaces the server's message. ---
  nextResponse = response({ error: 'bad password' }, 401);
  await assert.rejects(get('submitAuth')('owen', 'wrong'), /bad password/);

  // --- submitAuth: without a server message, the HTTP status is the error. ---
  nextResponse = response({}, 403);
  await assert.rejects(get('submitAuth')('owen', 'pw'), /Request failed \(HTTP 403\)/);

  // --- submitAuth: an unparseable error body still reports the status. ---
  nextResponse = { ok: false, status: 500, json: async () => { throw new SyntaxError('not json'); } };
  await assert.rejects(get('submitAuth')('owen', 'pw'), /Request failed \(HTTP 500\)/);

  // --- submitAuth: an ok response with an unparseable body resolves {}. ---
  // (Compared by keys, not deepEqual: the object is built in the vm realm,
  // so its prototype differs from this realm's {} — same as the OIranges
  // loader test's cross-realm note.)
  nextResponse = { ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); } };
  assert.deepEqual(Object.keys(await get('submitAuth')('owen', 'pw')), []);

  // --- submitAuth: a thrown fetch propagates. ---
  nextResponse = new Error('network down');
  await assert.rejects(get('submitAuth')('owen', 'pw'), /network down/);

  // --- refreshSessionFromServer: 200 hydrates the session + caches it. ---
  nextResponse = response({ username: 'owen' });
  const cacheBefore = cacheCalls;
  await get('refreshSessionFromServer')();
  assert.equal(get('session').username, 'owen');
  assert.equal(cacheCalls, cacheBefore + 1, 'a fresh session is cached for display');
  assert.equal(fetchCalls.at(-1).url, '/api/me');
  assert.equal(fetchCalls.at(-1).options.credentials, 'same-origin');

  // --- refreshSessionFromServer: non-ok clears the session, no cache write. ---
  nextResponse = response(null, 401);
  await get('refreshSessionFromServer')();
  assert.equal(get('session'), null);
  assert.equal(cacheCalls, cacheBefore + 1, 'a failed refresh does not cache');

  // --- refreshSessionFromServer: a thrown fetch clears the session too. ---
  set('session', { username: 'owen' });
  nextResponse = new Error('network down');
  await get('refreshSessionFromServer')();
  assert.equal(get('session'), null);
})();
