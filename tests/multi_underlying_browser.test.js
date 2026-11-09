// Multi-underlying browser acceptance (#182): QQQ -> SPY -> IBIT -> QQQ
// switching without cross-contamination, desktop + narrow, with screenshots.
// Optional rendered integration: NODE_PATH=<playwright installation> node tests/multi_underlying_browser.test.js
// Synthetic snapshots; no production requests, authentication or trading.
// Screenshots land in docs/evidence/multi-underlying/ (committed as evidence).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const EVIDENCE = path.join(__dirname, '../docs/evidence/multi-underlying');

function row(root, yymmdd, strike, type, exp, spot) {
  const occ = `${root}${yymmdd}${type === 'call' ? 'C' : 'P'}${String(Math.round(strike * 1000)).padStart(8, '0')}`;
  return {
    OptionSymbol: occ, Strike: strike, Type: type, Expiration: exp,
    OpenInterest: type === 'call' ? 1200 : 900, Volume: 45,
    Bid: 1.1, Mid: 1.2, Ask: 1.3, Last: 1.25, IV: 0.22,
    Delta: type === 'call' ? 0.5 : -0.5, Gamma: 0.01, Theta: -0.05, Vega: 0.1,
    UnderlyingPrice: spot,
  };
}
function snapshot(symbol, { date, expiration, spot, ts }) {
  const yymmdd = expiration.slice(2).replaceAll('-', '');
  const strikes = symbol === 'IBIT' ? [49, 50, 51] : symbol === 'SPY' ? [735, 736, 737] : [655, 656, 657];
  const rows = strikes.flatMap(strike => [row(symbol, yymmdd, strike, 'call', expiration, spot), row(symbol, yymmdd, strike, 'put', expiration, spot)]);
  return {
    symbol, timestamp: ts, snapshot_time: ts, date, expiration, tier: '0DTE_Regular',
    underlying_price: spot, snapshot_key: `intraday/${symbol.toLowerCase()}/snapshot.csv`, rows,
  };
}
const CLOCK = '2026-11-09T16:01:00Z';
// Synthetic OI calibration so the heatmap colors (and the evidence
// screenshots) are not stuck on the "calibration failed" warning. Offsets
// cover the ATM window the heatmap renders around the snapshot's strikes.
const OIRANGES_CSV = 'Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj\n' +
  Array.from({ length: 21 }, (_, i) => `0DTE_Regular,${i - 10},100,500,1000,5000,100,500,1000,5000,1.0,1.0`).join('\n');
const snapshots = {
  'intraday/latest.json': snapshot('QQQ', { date: '2026-11-09', expiration: '2026-11-09', spot: 656.51, ts: '2026-11-09T16:00:30Z' }),
  'intraday/spy/latest.json': snapshot('SPY', { date: '2026-11-09', expiration: '2026-11-09', spot: 736.0, ts: '2026-11-09T16:00:35Z' }),
  'intraday/ibit/latest.json': snapshot('IBIT', { date: '2026-11-09', expiration: '2026-11-13', spot: 50.0, ts: '2026-11-09T16:00:40Z' }),
};

(async () => {
  const { default: worker } = await import('../backend/index.js');
  const browser = await chromium.launch({ headless: true, channel: 'chromium' });
  const originalFetch = global.fetch;
  const live = { ...snapshots };
  global.fetch = async url => {
    const key = new URL(url).pathname.slice(1);
    if (key === 'derived/OIranges.csv') return new Response(OIRANGES_CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } });
    if (key in live) return new Response(JSON.stringify(live[key]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    return new Response('', { status: 404 });
  };
  fs.mkdirSync(EVIDENCE, { recursive: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.clock.setFixedTime(new Date(CLOCK));
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin === APP && url.pathname.startsWith('/browser-data/')) {
        const response = await worker.fetch(new Request(url), new Proxy({}, { get() { throw Error('binding access'); } }));
        return route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
      }
      if (url.origin === APP) {
        const filePath = 'site' + (url.pathname === '/' ? '/index.html' : url.pathname);
        if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) return route.fulfill({ path: filePath });
      }
      return route.fulfill({ status: 404, body: '{}', contentType: 'application/json' });
    });
    await page.goto(APP);
    // QQQ (default): 0DTE title, QQQ spot, QQQ rows.
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('656.51'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-qqq.png') });
    // SPY: 0DTE title, SPY spot, no QQQ rows left.
    await page.locator('#underlying-select').selectOption('SPY');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'SPY 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('736.00'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-spy.png') });
    // IBIT: nearest-expiry title (never 0DTE), IBIT spot.
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'IBIT exp 2026-11-13 Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('50.00'));
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-ibit.png') });
    // Back to QQQ: the QQQ snapshot returns, uncontaminated by SPY/IBIT.
    await page.locator('#underlying-select').selectOption('QQQ');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    await page.waitForFunction(() => document.querySelector('#price').textContent.includes('656.51'));
    // Narrow (390px): the IBIT nearest-expiry chain renders without overflow.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'IBIT exp 2026-11-13 Live OI');
    await page.waitForFunction(() => document.querySelectorAll('#heatmap-body tr').length > 0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, 'narrow IBIT chain must not overflow horizontally');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'narrow-ibit.png') });
    await page.locator('#underlying-select').selectOption('QQQ');
    await page.waitForFunction(() => document.querySelector('#title').textContent === 'QQQ 0DTE Live OI');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'narrow-qqq.png') });
    // IBIT not published (404): explicit unavailable, nothing rendered as IBIT.
    delete live['intraday/ibit/latest.json'];
    await page.reload();
    await page.locator('#underlying-select').selectOption('IBIT');
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'unavailable');
    assert.ok((await page.locator('#no-data').textContent()).length > 0, 'a missing IBIT snapshot explains itself');
    await page.screenshot({ fullPage: true, path: path.join(EVIDENCE, 'desktop-ibit-missing.png') });
    assert.deepEqual(errors, [], 'no page errors across the switching run');
    console.log('Chromium: QQQ -> SPY -> IBIT -> QQQ switching, narrow IBIT, and missing-IBIT unavailable passed (synthetic snapshots, evidence in docs/evidence/multi-underlying/)');
  } finally { global.fetch = originalFetch; await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
