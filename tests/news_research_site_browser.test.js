// P1 wiring: the News tab of the real site/index.html with the research view mounted under the news views.
//   NODE_PATH=<dir containing playwright or playwright-core> node tests/news_research_site_browser.test.js
//   NEWS_RESEARCH_SITE_EVIDENCE=<dir>  also saves screenshots there.
// A disposable public store is built locally: two pilot cycles through the real CLI with the shipped inactive
// config (news views, prices unavailable), plus one sanitized research projection per case written by
// tests/news_research_scenarios.py at the one public research key, news/v2/research/view.json. Each case is
// served on both data bases:
//   proxy  the production origin: /browser-data/ runs through the real Worker (backend/index.js), its
//          upstream answered from the store;
//   r2     another origin, reading the public R2 origin answered from the same store.
// Checks: nothing is requested before the News tab opens; the news views and the research view both mount
// (or the research view shows absent/failed while the news views still work); research requests are
// exactly the one key with ?_=, never the private ledger; the proxy marks the research view no-store and
// refuses ledger keys; the research view hides with the tab and is not refetched; timing/provenance labels
// and "prices unavailable" are visible; no page errors, foreign requests or horizontal scroll.
// Local only: fixture feeds, temporary directories, nothing reaches a provider or the network.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let pw;
try { pw = require('playwright'); } catch (e) { pw = require('playwright-core'); }

const REPO = path.resolve(__dirname, '..');
const SITE = path.join(REPO, 'site');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const OTHER = 'http://news-site.test';
const R2 = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const EVIDENCE = process.env.NEWS_RESEARCH_SITE_EVIDENCE || null;
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, narrow: { width: 390, height: 844 } };
const RESEARCH_KEY = 'news/v2/research/view.json';
const INDEX_KEY = 'news/v2/views/index.json';
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
const PY = process.env.PYTHON || 'python';

// case: research file (from the scenario builder) or an override; expected research state; must-show text
const CASES = [
  { name: 'prepared-research', file: 'prepared-research.json', state: 'ready', both: true,
    must: ['RESEARCH', 'PREPARED', 'accessed 2026-11-12T16:59:58Z', 'capture committed 2026-11-13T18:00:05Z',
      'no collector known-at', 'RETROSPECTIVE hypothesis', 'CONTEMPORANEOUS hypothesis', 'attributed report',
      'thesis assertion', 'not evidence', 'upper bound', 'capture revision 1'] },
  { name: 'synthetic-edgar', file: 'synthetic-edgar.json', state: 'ready',
    must: ['SYNTHETIC', 'EDGAR acceptance 2026-11-16T21:05:12Z', 'accessed 2026-11-17T14:00:00Z',
      'capture committed 2026-11-17T14:30:05Z', 'annotations made on an earlier revision: S-a', 'capture revision 2'] },
  { name: 'publication-hindsight', file: 'prepared-publication.json', state: 'ready',
    must: ['RETROSPECTIVE publication-time view', 'made after cutoff'] },
  { name: 'nothing-visible', file: 'prepared-before-commit.json', state: 'ready', must: ['No research records visible'] },
  { name: 'absent', file: null, state: 'absent', both: true, must: ['No research view published'] },
  { name: 'malformed', body: '{"schema_version": "news-research-view/v1", "items": ', state: 'failed',
    must: ['Could not load research records: response is not valid JSON'] },
  { name: 'upstream-500', status: 500, state: 'failed', must: ['Could not load research records: HTTP 500'] },
  { name: 'wrong-schema', body: JSON.stringify({ schema_version: 'news-research-view/v0', mode: 'research', items: [] }),
    state: 'failed', must: ['Could not show research records'] },
];

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'news-research-site-'));
  const pub = path.join(tmp, 'public');
  const cfg = path.join(REPO, 'market_data/news/deploy/news-pilot.json');
  for (const clock of ['2026-11-09T10:05:00Z', '2026-11-09T10:20:00Z']) {
    execFileSync(PY, ['-m', 'market_data.news', 'pilot-cycle', '--config', cfg, '--private', path.join(tmp, 'private'),
      '--public', pub, '--spool', path.join(tmp, 'spool'), '--clock', clock], { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
  }
  const built = path.join(tmp, 'research');
  execFileSync(PY, [path.join(REPO, 'tests/news_research_scenarios.py'), built], { cwd: REPO, stdio: ['ignore', 'pipe', 'inherit'] });
  assert.ok(!fs.existsSync(path.join(pub, 'news/v1')), 'the public store holds no private keys');

  let current = null;   // the case being served
  const upstream = key => {
    if (key === RESEARCH_KEY) {
      if (current.status) return { status: current.status, body: 'upstream ' + current.status };
      if (current.body !== undefined) return { status: 200, body: current.body, type: 'application/json' };
      if (!current.file) return { status: 404, body: 'not found' };
      return { status: 200, body: fs.readFileSync(path.join(built, current.file)), type: 'application/json',
        cache: 'public, max-age=31536000' };   // the proxy must override this to no-store
    }
    const file = path.join(pub, ...key.split('/'));
    if (!file.startsWith(pub + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return { status: 404, body: 'not found' };
    return { status: 200, body: fs.readFileSync(file), type: 'application/json' };
  };
  const { default: worker } = await import('../backend/index.js');
  const env = new Proxy({}, { get() { throw new Error('No bindings may be accessed'); } });
  const originalFetch = global.fetch;
  const upstreamCalls = [];
  global.fetch = async (url, options) => {
    const u = new URL(url);
    assert.equal(u.origin, R2, 'the Worker only fetches the public R2 origin');
    assert.equal(options.credentials, 'omit');
    const key = u.pathname.slice(1);
    upstreamCalls.push(key);
    const r = upstream(key);
    const headers = {};
    if (r.type) headers['Content-Type'] = r.type;
    if (r.cache) headers['Cache-Control'] = r.cache;
    return new Response(r.body, { status: r.status, headers });
  };

  // The Worker itself: the research key is no-store; the private ledger and other research keys are refused.
  current = CASES[0];
  let res = await worker.fetch(new Request(APP + '/browser-data/' + RESEARCH_KEY + '?_=1', { headers: { Origin: APP } }), env);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'no-store', 'the research view is never cacheable through the proxy');
  for (const key of ['news/v1/research/runs/2026-11-13/r/annotations.jsonl.gz', 'news/v1/research/_writer/lease.json',
    'news/v2/research/', 'news/v2/research/other.json', 'news/v2/research/view.json.bak']) {
    res = await worker.fetch(new Request(APP + '/browser-data/' + key, { headers: { Origin: APP } }), env);
    assert.equal(res.status, 404, key);
  }
  const before = upstreamCalls.length;
  assert.ok(upstreamCalls.every(k => !k.startsWith('news/v1/')), 'refused keys never reach upstream');

  const browser = await pw.chromium.launch({ headless: true });
  let checks = 0;
  try {
    for (const c of CASES) {
      current = c;
      for (const [base, pageOrigin] of [['proxy', APP], ['r2', OTHER]]) {
        const views = c.both ? Object.entries(VIEWPORTS) : [['desktop', VIEWPORTS.desktop]];
        for (const [vname, viewport] of views) {
          const label = `${c.name} ${base} ${vname}`;
          const page = await browser.newPage({ viewport });
          const errors = [], foreign = [];
          let news = [];
          page.on('pageerror', e => errors.push(e.message));
          page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
          await page.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.origin === pageOrigin && url.pathname.startsWith('/browser-data/')) {
              const key = url.pathname.slice('/browser-data/'.length);
              if (key.startsWith('news/')) news.push({ base: 'proxy', key, query: url.search });
              const r = await worker.fetch(new Request(url, { headers: { Origin: APP } }), env);
              return route.fulfill({ status: r.status, body: Buffer.from(await r.arrayBuffer()), headers: Object.fromEntries(r.headers) });
            }
            if (url.origin === R2) {
              const key = url.pathname.slice(1);
              if (key.startsWith('news/')) news.push({ base: 'r2', key, query: url.search });
              const r = upstream(key);
              return route.fulfill({ status: r.status, body: r.body, contentType: r.type || 'text/plain',
                headers: { 'Access-Control-Allow-Origin': '*' } });
            }
            if (url.origin === pageOrigin) {
              const file = path.join(SITE, url.pathname === '/' ? 'index.html' : url.pathname);
              if (file.startsWith(SITE + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
                return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] || 'application/octet-stream' });
              }
              return route.fulfill({ status: 404, body: '{}', contentType: 'application/json' });
            }
            foreign.push(url.href);
            return route.abort();
          });
          await page.goto(pageOrigin + '/');
          await page.waitForSelector('#tab-news');
          await page.waitForTimeout(300);
          assert.deepEqual(news, [], `${label}: no News request before the tab opens`);
          assert.ok(await page.locator('#news-research').isHidden(), `${label}: research hidden with the News tab`);
          await page.click('#tab-news');
          const waitState = async (sel, want) => page.waitForFunction(([s, w]) => {
            const b = document.querySelector(s);
            return b && b.getAttribute('data-state') === w && !b.hasAttribute('data-pending');
          }, [sel, want], { timeout: 10000 }).catch(async () => {
            throw new Error(`${label}: ${sel} data-state ${await page.getAttribute(sel, 'data-state')} (wanted ${want})`);
          });
          await waitState('#news-panel', 'ready');
          await waitState('#news-research', c.state);
          assert.ok(await page.locator('#news-research').isVisible(), `${label}: research visible on the News tab`);
          const panelText = await page.locator('#news-panel').innerText();
          assert.ok(panelText.includes('prices unavailable'), `${label}: the news views still say prices are unavailable`);
          const text = await page.locator('#news-research').innerText();
          for (const m of c.must) assert.ok(text.includes(m), `${label}: missing "${m}"`);
          const research = news.filter(n => n.key.includes('/research/'));
          assert.equal(research.length, 1, `${label}: one research request`);
          assert.equal(research[0].key, RESEARCH_KEY);
          assert.equal(research[0].base, base);
          assert.ok(/^\?_=[0-9]{1,20}$/.test(research[0].query), `${label}: query ${research[0].query}`);
          assert.ok(news.every(n => !n.key.startsWith('news/v1/')), `${label}: no private or legacy key requested`);
          assert.ok(news.some(n => n.key === INDEX_KEY), `${label}: the news index was read`);
          const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
          assert.ok(sw <= iw, `${label}: page scrolls horizontally (${sw} > ${iw})`);
          for (const h of await page.$$eval('#news-research a', as => as.map(a => a.getAttribute('href')))) {
            assert.ok(/^https?:\/\//.test(h), `${label}: link ${h}`);
          }
          if (EVIDENCE) {
            fs.mkdirSync(EVIDENCE, { recursive: true });
            await page.screenshot({ path: path.join(EVIDENCE, `${c.name}-${base}-${viewport.width}.png`), fullPage: true });
          }
          // Leaving the tab hides the research view; reopening does not refetch.
          await page.click('#tab-live');
          assert.ok(await page.locator('#news-research').isHidden(), `${label}: research hides with the News tab`);
          await page.click('#tab-news');
          await page.waitForTimeout(200);
          assert.equal(news.filter(n => n.key.includes('/research/')).length, 1, `${label}: not refetched on reopen`);
          assert.deepEqual(foreign, [], `${label}: requests left the page and R2 origins`);
          assert.deepEqual(errors, [], `${label}: page errors`);
          checks++;
          console.log(`ok - ${label}: research ${c.state}`);
          await page.close();
        }
      }
    }
    for (const key of upstreamCalls.slice(before).filter(k => k.startsWith('news/'))) {
      assert.ok(key === RESEARCH_KEY || key.startsWith('news/v2/views/'), 'upstream ' + key);
    }
  } finally {
    global.fetch = originalFetch;
    await browser.close();
  }
  console.log(`news research site browser test passed: ${checks} rendered checks over ${CASES.length} cases` +
    (EVIDENCE ? '; screenshots in ' + EVIDENCE : ''));
})().catch(e => { console.error(e); process.exit(1); });
