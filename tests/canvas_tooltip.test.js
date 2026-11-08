// B2 (readability quest): the canvas hover/touch tooltip section moved out
// of index.html into docs/js/canvas-tooltip.js, loaded in the same position.
// The test pins the ?v= hash in the page to the module's content (like
// shared.js), asserts the section no longer lives inline, and drives the
// tooltip + renderers with a stub DOM + stub canvas: tooltip show/hide and
// the bind-once guard, the candle and Top-5 renderers with their hit tests,
// the intraday/daily/similar widgets with stub data functions (including the
// stale-request guard), the similar popup, and the independent degradation
// of renderHistWidgets. Values returned from the vm context are compared
// field by field, not deepEqual — deepEqual fails across the vm realm boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../docs/js/canvas-tooltip.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../docs/shared.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/canvas-tooltip.js?v=' + moduleHash), 'canvas-tooltip script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function attachCanvasTooltip'), 'attachCanvasTooltip must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function fmtBarTime'), 'fmtBarTime must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function candleTooltipHtml'), 'candleTooltipHtml must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function renderCandles'), 'renderCandles must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function renderTop5Widget'), 'renderTop5Widget must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('async function renderIntradayWidget'), 'renderIntradayWidget must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('async function renderDailyWidget'), 'renderDailyWidget must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('async function renderSimilarWidget'), 'renderSimilarWidget must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function renderHistWidgets'), 'renderHistWidgets must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('async function showSimilarPopup'), 'showSimilarPopup must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('function hideSimilarPopup'), 'hideSimilarPopup must live in js/canvas-tooltip.js, not inline in index.html');
assert.ok(!html.includes('let simPopupEl'), 'simPopupEl must live in js/canvas-tooltip.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── canvas hover/touch tooltip'), 'canvas-tooltip banner must stay in index.html as the section marker');

const BARS = [
  { t: '2026-09-23T13:30:00Z', o: 590, h: 595, l: 588, c: 593 },
  { t: '2026-09-23T13:35:00Z', o: 593, h: 594, l: 592, c: 594 },
];
const TOP5_ROWS = [
  { Strike: 590, Type: 'put', OpenInterest: 1000 },
  { Strike: 600, Type: 'call', OpenInterest: 5000 },
  { Strike: 610, Type: 'call', OpenInterest: 2000 },
];

// --- stub DOM: elements + a recording 2d context. ---
function makeCtx() {
  const texts = [];
  return new Proxy({ _texts: texts }, {
    get(t, p) {
      if (p === 'fillText') return (text) => { texts.push(String(text)); };
      if (p === '_texts') return texts;
      return () => {};
    },
    set: () => true,
  });
}
function makeEl({ width = 0, height = 0 } = {}) {
  const listeners = {};
  const classes = new Set();
  const el = {
    width, height,
    textContent: '', value: '', disabled: false, hidden: false,
    dataset: {}, style: {}, children: [], id: '',
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    _classes: classes, _listeners: listeners,
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    remove() { this._removed = true; },
    querySelector(sel) {
      const cache = (this._qs ||= {});
      // The similar popup's canvas is 180x70 in the shipped markup.
      return cache[sel] ||= makeEl(sel === 'canvas' ? { width: 180, height: 70 } : {});
    },
    querySelectorAll: () => [],
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    getBoundingClientRect: () => ({ width: el.width, height: el.height, top: 0, left: 0, right: el.width, bottom: el.height }),
    offsetWidth: 120, offsetHeight: 40,
    getContext: () => (el._ctx ||= makeCtx()),
    reset() {}, focus() {}, click() {}, scrollIntoView() {},
  };
  let _innerHTML = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => _innerHTML,
    set: v => { _innerHTML = String(v); el.children.length = 0; },
    enumerable: true, configurable: true,
  });
  return el;
}

// --- harness: the page's shared scope, as the module sees it. ---
// `fmtOI` comes from shared.js; `histRequest` from the historical panel;
// `loadIntradayPrices`, `loadDailyWindow`, `queryTopSimilar` and
// `nextDayMove` from the sidebar-widgets module. The sandbox provides them
// here, with stub data functions the caller can steer. Ad-hoc values are
// passed into the vm context through `h.context[name] = value`.
function harness(stubs = {}) {
  const els = {
    'widget-top5': makeEl({ width: 240, height: 110 }),
    'widget-intraday': makeEl({ width: 240, height: 90 }),
    'widget-daily': makeEl({ width: 240, height: 90 }),
    'widget-intraday-label': makeEl(),
    'similar-list': makeEl(),
  };
  const bodyChildren = [];
  const body = makeEl();
  body.appendChild = c => { bodyChildren.push(c); return c; };
  const created = [];
  const sandbox = {
    console,
    window: { scrollX: 0, scrollY: 0 },
    document: {
      getElementById: id => els[id] || (els[id] = makeEl()),
      createElement: () => { const el = makeEl(); created.push(el); return el; },
      querySelector: () => makeEl(),
      querySelectorAll: () => [],
      addEventListener() {}, removeEventListener() {},
      body,
      documentElement: { clientWidth: 1280 },
    },
    histRequest: 0,
    loadIntradayPrices: stubs.loadIntradayPrices || (async () => BARS),
    loadDailyWindow: stubs.loadDailyWindow || (async () => ({ candles: BARS, centerIndex: 1 })),
    queryTopSimilar: stubs.queryTopSimilar || (async () => [
      { date: '2026-09-21', score: 0.95 },
      { date: '2026-09-22', score: 0.5 },
    ]),
    nextDayMove: stubs.nextDayMove || (async dateStr => (dateStr === '2026-09-21' ? 1.23 : null)),
  };
  const context = vm.createContext(sandbox);
  // The real shared.js + shared/heatmap.js: `fmtOI` is a browser global in
  // production (not in shared.js's module.exports), so load the whole files
  // into the context, in page order — B3 slice 2 (Nov 2026): fmtOI moved out
  // of docs/shared.js into docs/shared/heatmap.js.
  vm.runInContext(sharedSource, context, { filename: 'docs/shared.js' });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../docs/shared/heatmap.js'), 'utf8'), context, { filename: 'docs/shared/heatmap.js' });
  vm.runInContext(moduleSource, context, { filename: 'docs/js/canvas-tooltip.js' });
  return { context, els, bodyChildren, created };
}
const call = (h, expr) => vm.runInContext(expr, h.context);
const tick = () => new Promise(r => setTimeout(r, 0));

(async () => {
  // --- fmtBarTime: timestamps shorten, anything else passes through. ---
  let h = harness();
  assert.equal(call(h, "fmtBarTime('2026-06-25T09:30:00')"), '9:30', 'a datetime shortens to H:MM');
  assert.equal(call(h, "fmtBarTime('13:05')"), '13:05', 'an already-short label passes through');
  assert.equal(call(h, "fmtBarTime('label')"), 'label', 'a non-timestamp passes through');
  assert.equal(call(h, 'fmtBarTime("")'), '', 'an empty time is empty');
  assert.equal(call(h, 'fmtBarTime(null)'), '', 'a null time is empty');

  // --- candleTooltipHtml: O/H/L/C with the bar label. ---
  h = harness();
  const tip = call(h, "candleTooltipHtml({t:'2026-09-23T13:30:00Z',o:590,h:595,l:588,c:593})");
  assert.ok(tip.includes('13:30'), 'the tooltip shows the bar time');
  assert.ok(tip.includes('O <b>590.00</b>'), 'the tooltip shows the open');
  assert.ok(tip.includes('H <b>595.00</b>'), 'the tooltip shows the high');
  assert.ok(tip.includes('L <b>588.00</b>'), 'the tooltip shows the low');
  assert.ok(tip.includes('C <b>593.00</b>'), 'the tooltip shows the close');
  const labelled = call(h, "candleTooltipHtml({t:'2026-09-23T13:30:00Z',label:'09/23',o:1,h:2,l:3,c:4})");
  assert.ok(labelled.includes('09/23'), 'an explicit label wins over the timestamp');

  // --- attachCanvasTooltip: bind-once, hover shows, leave hides, touch taps. ---
  h = harness();
  const canvas = makeEl({ width: 240, height: 90 });
  h.context.testCanvas = canvas;
  call(h, 'attachCanvasTooltip(testCanvas, (x, y) => x > 10 ? "<b>tip</b>" : null)');
  const move = e => canvas._listeners.pointermove[0](e);
  const down = e => canvas._listeners.pointerdown[0](e);
  move({ clientX: 50, clientY: 20, pointerType: 'mouse' });
  assert.equal(h.created.length, 1, 'the first hover creates one tooltip div');
  const tipEl = h.created[0];
  assert.equal(tipEl.className, 'canvas-tooltip', 'the tooltip div carries the class');
  assert.equal(tipEl.innerHTML, '<b>tip</b>', 'the tooltip shows the hit-test HTML');
  assert.equal(tipEl.style.display, 'block', 'the tooltip is visible on hover');
  assert.equal(tipEl.style.left, '64px', 'the tooltip sits right of the pointer');
  assert.equal(tipEl.style.top, '34px', 'the tooltip falls below near the top of the viewport');
  move({ clientX: 5, clientY: 20, pointerType: 'mouse' });
  assert.equal(tipEl.style.display, 'none', 'a miss hides the tooltip');
  move({ clientX: 50, clientY: 20, pointerType: 'mouse' });
  assert.equal(tipEl.style.display, 'block', 'hovering back shows it again');
  canvas._listeners.pointerleave[0]();
  assert.equal(tipEl.style.display, 'none', 'leaving the canvas hides the tooltip');
  let prevented = false;
  down({ clientX: 50, clientY: 20, pointerType: 'touch', preventDefault: () => { prevented = true; } });
  assert.ok(prevented, 'a touch tap prevents the default scroll/zoom');
  assert.equal(tipEl.style.display, 'block', 'a touch tap also shows the tooltip');
  prevented = false;
  down({ clientX: 50, clientY: 20, pointerType: 'mouse', preventDefault: () => { prevented = true; } });
  assert.ok(!prevented, 'a mouse press does not prevent the default');
  // A second attach swaps the hit test without adding listeners.
  call(h, 'attachCanvasTooltip(testCanvas, () => "<b>second</b>")');
  assert.equal(canvas._listeners.pointermove.length, 1, 're-attaching does not add a second pointermove listener');
  assert.equal(canvas._listeners.pointerdown.length, 1, 're-attaching does not add a second pointerdown listener');
  assert.equal(h.created.length, 1, 're-attaching does not create a second tooltip div');
  move({ clientX: 5, clientY: 20, pointerType: 'mouse' });
  assert.equal(tipEl.innerHTML, '<b>second</b>', 'the swapped hit test is the one that runs');

  // --- renderCandles: no-data, bars with a hit test, labels. ---
  h = harness();
  h.context.testCanvas = h.els['widget-intraday'];
  call(h, 'renderCandles(testCanvas, [])');
  assert.ok(h.els['widget-intraday']._ctx._texts.includes('no data'), 'an empty render writes no data');
  assert.equal(h.els['widget-intraday']._hitTest, null, 'an empty render clears the tooltip data');
  call(h, `renderCandles(testCanvas, ${JSON.stringify(BARS)})`);
  const hit = h.els['widget-intraday']._hitTest;
  assert.equal(typeof hit, 'function', 'a render with bars installs a hit test');
  assert.ok(hit(61.5, 45).includes('O <b>590.00</b>'), 'the first bar tooltips under its centre');
  assert.ok(hit(178.5, 45).includes('O <b>593.00</b>'), 'the second bar tooltips under its centre');
  assert.equal(hit(2, 45), null, 'left of the plot there is nothing to tooltip');
  assert.equal(hit(239, 45), null, 'right of the plot there is nothing to tooltip');
  const labelledBars = BARS.map((b, i) => ({ ...b, label: ['13:30', '13:35'][i] }));
  call(h, `renderCandles(testCanvas, ${JSON.stringify(labelledBars)}, { labels: true })`);
  assert.ok(h.els['widget-intraday']._ctx._texts.includes('13:30'), 'labels draw the first bar label');
  assert.ok(h.els['widget-intraday']._ctx._texts.includes('13:35'), 'labels draw the last bar label');

  // --- renderTop5Widget: no-data, bubbles with a hit test. ---
  h = harness();
  h.context.testRows = TOP5_ROWS;
  call(h, 'renderTop5Widget([], 600)');
  assert.ok(h.els['widget-top5']._ctx._texts.includes('no data'), 'no rows writes no data');
  assert.equal(h.els['widget-top5']._hitTest, null, 'no rows clears the tooltip data');
  call(h, 'renderTop5Widget(null, null)');
  assert.equal(h.els['widget-top5']._hitTest, null, 'no spot clears the tooltip data too');
  call(h, 'renderTop5Widget(testRows, 600)');
  assert.ok(h.els['widget-top5']._ctx._texts.includes('+6%'), 'the axis labels the max distance');
  assert.ok(h.els['widget-top5']._ctx._texts.includes('1'), 'the rank numbers draw');
  const topHit = h.els['widget-top5']._hitTest;
  assert.equal(typeof topHit, 'function', 'a render with rows installs a hit test');
  // Strike 600 is the top-OI bubble: pct 0 -> x 120, cy 86 (all bumps 0).
  const bubble = topHit(120, 86);
  assert.ok(bubble.includes('Strike 600'), 'the top bubble tooltips its strike');
  assert.ok(bubble.includes('CALL'), 'the top bubble tooltips its type');
  assert.ok(bubble.includes('5.0K'), 'the top bubble tooltips its OI through fmtOI');
  assert.equal(topHit(0, 0), null, 'far from every bubble there is nothing to tooltip');

  // --- renderIntradayWidget: success, error, stale request. ---
  h = harness();
  await call(h, "renderIntradayWidget('2026-09-23')");
  assert.equal(h.els['widget-intraday-label'].textContent, '2026-09-23', 'the intraday label names the date');
  assert.equal(typeof h.els['widget-intraday']._hitTest, 'function', 'the fetched bars install a hit test');
  h = harness({ loadIntradayPrices: async () => { throw new Error('offline'); } });
  await call(h, "renderIntradayWidget('2026-09-23')");
  assert.ok(h.els['widget-intraday']._ctx._texts.includes('no data'), 'a failed fetch renders no data');
  assert.equal(h.els['widget-intraday']._hitTest, null, 'a failed fetch clears the tooltip data');
  let resolveBars;
  h = harness({ loadIntradayPrices: () => new Promise(r => { resolveBars = r; }) });
  const pending = call(h, "renderIntradayWidget('2026-09-23')");
  vm.runInContext('histRequest = 99', h.context);
  resolveBars(BARS);
  await pending;
  assert.equal(h.els['widget-intraday']._hitTest, null, 'a stale request must not render its bars');

  // --- renderDailyWidget: success and error. ---
  h = harness();
  await call(h, "renderDailyWidget('2026-09-23')");
  assert.equal(typeof h.els['widget-daily']._hitTest, 'function', 'the fetched window installs a hit test');
  h = harness({ loadDailyWindow: async () => { throw new Error('offline'); } });
  await call(h, "renderDailyWidget('2026-09-23')");
  assert.equal(h.els['widget-daily']._hitTest, null, 'a failed window clears the tooltip data');

  // --- renderSimilarWidget: rows, empty, error, stale request. ---
  h = harness();
  await call(h, "renderSimilarWidget('2026-09-23')");
  await tick(); await tick();
  const rows = h.els['similar-list'].children;
  assert.equal(rows.length, 2, 'two similar sessions render');
  assert.ok(rows[0].innerHTML.includes('2026-09-21'), 'the first row names its date');
  assert.ok(rows[0].innerHTML.includes('95%'), 'the first row shows its score');
  assert.equal(rows[0].querySelector('.similar-move').textContent, '+1.23%', 'the first row shows its next-day move');
  assert.equal(rows[1].querySelector('.similar-move').textContent, '—', 'a null next-day move renders a dash');
  h = harness({ queryTopSimilar: async () => [] });
  await call(h, "renderSimilarWidget('2026-09-23')");
  assert.ok(h.els['similar-list'].innerHTML.includes('Not enough historical sessions'), 'no matches explains itself');
  h = harness({ queryTopSimilar: async () => { throw new Error('offline'); } });
  await call(h, "renderSimilarWidget('2026-09-23')");
  assert.ok(h.els['similar-list'].innerHTML.includes('Similarity unavailable'), 'a failed query explains itself');
  let resolveMatches;
  h = harness({ queryTopSimilar: () => new Promise(r => { resolveMatches = r; }) });
  const pendingSim = call(h, "renderSimilarWidget('2026-09-23')");
  vm.runInContext('histRequest = 99', h.context);
  resolveMatches([{ date: '2026-09-21', score: 0.9 }]);
  await pendingSim;
  assert.ok(h.els['similar-list'].innerHTML.includes('Loading'), 'a stale request leaves the loading state');

  // --- showSimilarPopup / hideSimilarPopup: one popup at a time. ---
  h = harness();
  const target = makeEl({ width: 100, height: 20 });
  h.context.testEvent = { currentTarget: target };
  await call(h, "showSimilarPopup(testEvent, '2026-09-21')");
  const popup = h.bodyChildren.find(c => c.id === 'similar-popup');
  assert.ok(popup, 'showing the popup appends it to the body');
  assert.equal(call(h, 'simPopupEl'), popup, 'the popup is the current one');
  const popupCanvas = popup.querySelector('canvas');
  assert.equal(typeof popupCanvas._hitTest, 'function', 'the popup renders the day chart with a hit test');
  await call(h, "showSimilarPopup(testEvent, '2026-09-22')");
  assert.equal(h.bodyChildren.filter(c => c.id === 'similar-popup').length, 2, 'the stub keeps both divs; the module replaces the current one');
  assert.equal(call(h, 'simPopupEl'), h.bodyChildren.filter(c => c.id === 'similar-popup').at(-1), 'the second popup is the current one');
  call(h, 'hideSimilarPopup()');
  assert.equal(call(h, 'simPopupEl'), null, 'hiding clears the current popup');
  call(h, 'hideSimilarPopup()');
  assert.equal(call(h, 'simPopupEl'), null, 'hiding twice is safe');

  // --- renderHistWidgets: drives all four widgets; one failing degrades alone. ---
  h = harness();
  await call(h, `renderHistWidgets(${JSON.stringify(TOP5_ROWS)}, 600, '0DTE_Regular', '2026-09-23')`);
  await tick(); await tick();
  assert.equal(typeof h.els['widget-top5']._hitTest, 'function', 'the Top-5 widget renders');
  assert.equal(h.els['widget-intraday-label'].textContent, '2026-09-23', 'the intraday widget renders');
  assert.equal(typeof h.els['widget-daily']._hitTest, 'function', 'the daily widget renders');
  assert.equal(h.els['similar-list'].children.length, 2, 'the similar widget renders');
  h = harness();
  vm.runInContext('renderTop5Widget = () => { throw new Error("boom"); }', h.context);
  await call(h, `renderHistWidgets(${JSON.stringify(TOP5_ROWS)}, 600, '0DTE_Regular', '2026-09-23')`);
  await tick(); await tick();
  assert.equal(h.els['widget-intraday-label'].textContent, '2026-09-23', 'a failing Top-5 widget does not stop the intraday widget');
  assert.equal(h.els['similar-list'].children.length, 2, 'a failing Top-5 widget does not stop the similar widget');

  console.log('canvas tooltip: hash pinned, extraction verified, tooltip show/hide, candle + Top-5 hit tests, widget fetch/error/stale paths, popup and independent degradation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
