// Isolated render of the shipped Automated section and CSS, never live accounts.
// NODE_PATH=<existing playwright> TEST_CHROMIUM_PATH=<existing browser>
// O4_EVIDENCE_DIR=<fresh directory> node tests/bots_dates_browser.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
// B2 slice 13: the bots panel moved into docs/js/bots-panel.js — load the
// shipped module instead of slicing it out of the page by banner text.
const source = fs.readFileSync(path.join(__dirname, '../docs/js/bots-panel.js'), 'utf8');
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const origin = 'http://o4-fixture.invalid';
const fixtures = [
  ['Today', '2026-11-06T14:30:00Z', 'Nov 6, 2026, 9:30:00 AM EST (New York)'],
  ['Yesterday', '2026-11-05T21:15:09Z', 'Nov 5, 2026, 4:15:09 PM EST (New York)'],
  ['Older year', '2025-08-15T16:49:00Z', 'Aug 15, 2025, 12:49:00 PM EDT (New York)'],
  ['NY rollover', '2026-01-01T02:00:00Z', 'Dec 31, 2025, 9:00:00 PM EST (New York)'],
  ['DST first', '2026-11-01T01:30:00-04:00', 'Nov 1, 2026, 1:30:00 AM EDT (New York)'],
  ['DST second', '2026-11-01T01:30:00-05:00', 'Nov 1, 2026, 1:30:00 AM EST (New York)'],
  ['Missing', null, 'Not recorded'],
  ['Empty', '', 'Not recorded'],
  ['Invalid', '<img src=x onerror=alert(1)>', 'Invalid timestamp'],
];
(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.TEST_CHROMIUM_PATH ? { executablePath: process.env.TEST_CHROMIUM_PATH } : {}) });
  const evidence = process.env.O4_EVIDENCE_DIR;
  const results = [], errors = [], unexpected = [], sockets = [];
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false,
      timezoneId: 'Asia/Tokyo', locale: 'en-GB', viewport: {width:1280,height:900} });
    // Install both guards before navigation. Nothing is forwarded to a network.
    await context.route('**/*', route => {
      if (route.request().url() === origin + '/' && route.request().method() === 'GET') {
        return route.fulfill({ contentType:'text/html', body: '<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + css + '</style></head><body><h1 style="font-size:18px;margin-bottom:12px">Automated · synthetic date fixtures</h1><section id="bots-panel"><div id="bots-status"></div><div id="bots-by-strategy"></div><div id="bots-grid"></div></section></body></html>' });
      }
      unexpected.push(route.request().url()); return route.abort();
    });
    await context.routeWebSocket('**/*', ws => { sockets.push(ws.url()); ws.close(); });
    const page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    await page.clock.install({ time: new Date('2026-11-06T18:00:00Z') });
    await page.goto(origin + '/');
    await page.evaluate(({ source, fixtures }) => {
      const scope = {
        document, liveQuotes: {get(){return null},setBotPositions(){}},
        fetch: async () => {throw Error('Unexpected fixture fetch');},
        setInterval: () => {throw Error('Unexpected timer');}, clearInterval() {},
        fmtQ: v => String(v), fmtMoney: v => `${v < 0 ? '−' : v > 0 ? '+' : ''}$${Math.abs(v).toFixed(2)}`,
        pnlClass: v => v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : '',
        escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
      };
      const panel = new Function(...Object.keys(scope), source + ';return {renderBots,set data(v){botsData=v}};')(...Object.values(scope));
      const bots = fixtures.map(([alias, ts], i) => Object.freeze({alias, username:'fixture-' + i,
        balance_cash:10000,starting_balance:10000,trade_count:3,last_trade_ts:ts,
        positions:Object.freeze([]),account_closed:alias==='Older year'}));
      const data = {bots,as_of:'2026-11-06T18:00:00Z'};
      const before = JSON.stringify(data);
      panel.data = data; panel.renderBots(); panel.renderBots();
      if (JSON.stringify(data) !== before) throw Error('Fixture history mutated');
    }, {source,fixtures});
    assert.equal(await page.locator('.bot-card').count(), fixtures.length);
    assert.deepEqual(await page.locator('.bot-last-trade').allTextContents(), fixtures.map(f => 'Last trade: ' + f[2]));
    assert.equal(await page.locator('.bot-card img').count(), 0);
    assert.equal(await page.locator('.bot-equity').allTextContents().then(v => v.every(x => x==='$10000.00')), true);
    assert.ok(await page.getByText('Closed — trading stopped; history retained').isVisible());
    for (const width of [1280,390,320]) {
      await page.setViewportSize({width,height:900});
      const layout = await page.locator('.bot-last-trade').evaluateAll(labels => labels.map(label => {
        const range=document.createRange(); range.selectNodeContents(label);
        const card=label.closest('.bot-card').getBoundingClientRect();
        const rects=[...range.getClientRects()];
        return {text:label.textContent,font:getComputedStyle(label).fontSize,lines:rects.length,
          fits:rects.every(r=>r.left>=card.left && r.right<=card.right && r.top>=card.top && r.bottom<=card.bottom && r.right<=innerWidth),
          cardFits:label.closest('.bot-card').scrollWidth<=label.closest('.bot-card').clientWidth};
      }));
      assert.ok(layout.every(x=>x.fits && x.cardFits), JSON.stringify({width,layout}));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), true);
      if (evidence) {
        fs.mkdirSync(evidence,{recursive:true});
        await page.screenshot({path:path.join(evidence,`dates-${width}.png`),fullPage:true});
      }
      results.push({width,layout});
    }
    assert.deepEqual(errors,[]); assert.deepEqual(unexpected,[]); assert.deepEqual(sockets,[]);
    const report={status:'passed',browser:browser.version(),testedAt:new Date().toISOString(),
      fixtureClock:'2026-11-06T18:00:00Z',browserZone:'Asia/Tokyo',browserLocale:'en-GB',
      displayZone:'America/New_York',fixtures,results,errors,unexpected,sockets};
    if(evidence) fs.writeFileSync(path.join(evidence,'render-results.json'),JSON.stringify(report,null,2)+'\n');
    console.log('PASS shipped Automated renderer/CSS: 9 synthetic date states at 1280/390/320; zero network forwarding, sockets, page errors; unchanged inputs');
    await context.close();
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
