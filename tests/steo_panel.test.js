// Exercise the shipped optional STEO fetch/render path with fixture responses.
// No live EIA/R2 request, API key, or published forecast is created here.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { fmtSteoDelta, findRevision } = require('../docs/shared.js');

// B2 slice 7: the STEO fetch moved out of docs/index.html into
// docs/js/steo-fetch.js — load the shipped module, and pin the ?v= hash so
// the page cannot silently serve a different copy.
const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
const moduleSrc = fs.readFileSync(path.join(__dirname, '../docs/js/steo-fetch.js'), 'utf8');
const hash = crypto.createHash('sha256').update(moduleSrc).digest('hex').slice(0, 16);
assert.equal(html.match(/<script src="js\/steo-fetch\.js\?v=([0-9a-f]{16})"><\/script>/)?.[1], hash, 'page pins the shipped steo-fetch.js');
assert.ok(!html.includes('async function fetchSteoCalibration()'), 'fetchSteoCalibration no longer lives inline');

const elements = new Map();
const el = id => {
  if (!elements.has(id)) {
    const classes = new Set(id === 'steo-panel' ? ['hidden'] : []);
    elements.set(id, {
      textContent: '', className: '',
      classList: {
        add: value => classes.add(value),
        remove: value => classes.delete(value),
        contains: value => classes.has(value),
      },
    });
  }
  return elements.get(id);
};
class FrozenDate extends Date {
  constructor(...args) { super(...(args.length ? args : ['2026-10-09T12:00:00Z'])); }
  static now() { return Date.parse('2026-10-09T12:00:00Z'); }
}
const calls = [], warnings = [];
let nextResponse;
const scope = {
  document: { getElementById: el },
  BROWSER_DATA: '/browser-data',
  Date: FrozenDate,
  fmtSteoDelta, findRevision,
  console: { warn: (...args) => warnings.push(args) },
  fetch: async (url, options) => {
    calls.push({ url, options });
    if (nextResponse instanceof Error) throw nextResponse;
    return nextResponse;
  },
};
const fetchPanel = new Function(...Object.keys(scope),
  moduleSrc + '; return fetchSteoCalibration;'
)(...Object.values(scope));
const response = (data, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => data,
});
const fixture = {
  timestamp: '2026-10-09T10:00:00Z',
  current_release: '2026-10', prior_release: '2026-09',
  points: [
    { period: '2026-09', brent: 70, wti: 65, balance: 1 },
    { period: '2026-10', brent: 72, wti: 67, balance: -0.5 },
    { period: '2026-11', brent: 73, wti: 68, balance: -0.7 },
  ],
  revisions: [{ period: '2026-10', brent_delta: 2, wti_delta: -1, balance_delta: -0.5 }],
  ovx: { value: 40, regime: 'elevated' },
};
const hidden = () => el('steo-panel').classList.contains('hidden');
async function showValid(data = fixture) {
  nextResponse = response(data);
  await fetchPanel();
  assert.equal(hidden(), false, 'real-format fixture displays after an unavailable state');
}

(async () => {
  // An absent optional publication is expected, and its error body is never parsed.
  nextResponse = { ok: false, status: 404, json() { throw new Error('must not parse a 404'); } };
  await fetchPanel();
  assert.equal(hidden(), true);
  assert.equal(warnings.length, 0);
  assert.equal(calls[0].url, '/browser-data/macro/eia_steo.json?_=1791547200000');
  assert.equal(calls[0].options, undefined, 'no credentials or custom request headers added');

  await showValid();
  assert.equal(el('steo-release').textContent, '2026-10 (vs 2026-09)');
  assert.equal(el('steo-period').textContent, '2026-10');
  assert.equal(el('steo-brent').textContent, '$72.00');
  assert.equal(el('steo-wti').textContent, '$67.00');
  assert.equal(el('steo-balance').textContent, '-0.50 Mbbl/d');
  assert.equal(el('steo-brent-delta').textContent, '+2.00');
  assert.equal(el('steo-brent-delta').className, 'steo-delta up');
  assert.equal(el('steo-wti-delta').textContent, '-1.00');
  assert.equal(el('steo-ovx').textContent, '40.00');
  assert.equal(el('steo-ovx-regime').textContent, 'elevated');

  nextResponse = response(null, 404);
  await fetchPanel();
  assert.equal(hidden(), true, 'absence after success hides previous values');
  assert.equal(warnings.length, 0);
  await showValid();

  // Empty publication remains hidden; it is not rendered as zero forecasts.
  nextResponse = response({ points: [] });
  await fetchPanel();
  assert.equal(hidden(), true);
  assert.equal(warnings.length, 0);
  await showValid();

  // Real failures remain distinct from 404 and may recover on the next poll.
  for (const status of [403, 429, 500, 502, 503]) {
    const before = warnings.length;
    nextResponse = response(null, status);
    await fetchPanel();
    assert.equal(hidden(), true, 'HTTP failure hides previous values');
    assert.equal(warnings.length, before + 1);
    assert.match(warnings.at(-1)[1].message, new RegExp('HTTP ' + status));
    await showValid();
    assert.equal(warnings.length, before + 1, 'recovery does not add an error');
  }
  for (const failure of [
    new TypeError('Failed to fetch'),
    { ok: true, status: 200, json: async () => { throw new SyntaxError('Invalid JSON'); } },
    response(null),
    response({ ...fixture, current_release: undefined }),
    response({ ...fixture, current_release: '2026-13' }),
    response({ ...fixture, points: [{period:'invalid',brent:72}] }),
    response({ points: { length: 1 } }),
    response({ ...fixture, points: [{ period: '2026-10', brent: 'invalid' }] }),
  ]) {
    const before = warnings.length;
    nextResponse = failure;
    await fetchPanel();
    assert.equal(hidden(), true, 'invalid/unreadable publication is hidden');
    assert.equal(warnings.length, before + 1, 'invalid/unreadable publication remains a failure');
    await showValid();
  }

  await showValid({
    current_release: '2026-10', prior_release: null,
    points: [{ period: '2026-10', brent: null, wti: null, balance: null }],
    revisions: [], ovx: { value: null, regime: 'no_data' },
  });
  assert.equal(el('steo-release').textContent, '2026-10');
  assert.equal(el('steo-brent').textContent, '—');
  assert.equal(el('steo-wti').textContent, '—');
  assert.equal(el('steo-balance').textContent, '—');
  assert.equal(el('steo-brent-delta').textContent, '');
  assert.equal(el('steo-ovx').textContent, '—');
  assert.equal(el('steo-ovx-regime').textContent, 'no_data');

  assert.ok(calls.every(({ url }) => url.startsWith('/browser-data/macro/eia_steo.json?_=')));
  // B2 slice 16 (Nov 2026): boot moved out of index.html into docs/js/boot.js,
  // so the recovery cadence now lives there, not in the page — read it from
  // the shipped module (boot.test.js also pins the full interval list).
  const bootSrc = fs.readFileSync(path.join(__dirname, '../docs/js/boot.js'), 'utf8');
  assert.match(bootSrc, /setInterval\(fetchSteoCalibration, 60_000\)/, 'existing recovery cadence is unchanged');
  console.log('PASS optional STEO panel: 404, empty, real-format render, upstream/transport/schema failures, missing values and recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
