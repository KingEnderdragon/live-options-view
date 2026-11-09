// DOM and refresh-controller tests for the adaptive quote-card panel (#179).
// A small deterministic DOM drives the shipped modules; no browser needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const NOW = Date.parse('2026-11-09T15:00:00Z');

class ClassList {
  constructor() { this.set = new Set(); }
  add(...names) { names.forEach(name => this.set.add(name)); }
  remove(...names) { names.forEach(name => this.set.delete(name)); }
  contains(name) { return this.set.has(name); }
}

class El {
  constructor(tag, doc) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = doc;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this._text = '';
    this.className = '';
    this.classList = new ClassList();
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.listeners = {};
    this.title = '';
    this.focused = false;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = { name, value: String(value) }; }
  getAttribute(name) { return this.attributes[name]?.value ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler);
  }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach(fn => fn({ preventDefault() {}, stopPropagation() {}, ...event })); }
  focus() { this.focused = true; }
  get textContent() {
    if (this.children.length) return this.children.map(child => child.textContent).join('');
    return this._text;
  }
  set textContent(value) { this._text = value === null || value === undefined ? '' : String(value); this.children = []; }
  querySelector(selector) {
    const className = selector.replace(/^\./, '');
    const walk = node => {
      for (const child of node.children) {
        if (child.classList?.contains(className)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}

class Doc {
  constructor(ids) {
    this.elements = new Map();
    this.ids = ids;
    this.visibilityState = 'visible';
    this.listeners = {};
    for (const id of ids) this.elements.set(id, new El('div', this));
  }
  getElementById(id) { return this.elements.get(id) || null; }
  createElement(tag) { return new El(tag, this); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler);
  }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach(fn => fn({ preventDefault() {}, ...event })); }
}

const IDS = ['qp-session', 'qp-updated', 'qp-core', 'qp-pins', 'qp-pins-empty', 'qp-pin-count',
  'qp-pin-scope', 'qp-pin-add', 'qp-pin-dialog', 'qp-pin-search', 'qp-pin-list',
  'qp-pin-message', 'qp-pin-close', 'qp-movers-scope', 'qp-movers-up', 'qp-movers-down',
  'qp-movers-note', 'qp-announce'];

function loadPanel({ documentRef, store, pins, nowFn = () => NOW }) {
  const module = { exports: {} };
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    console, module, document: documentRef, Intl, Date, JSON, Math, Number, Object, Array, Set, Map, String,
    localStorage: null,
    isTradeableShareSymbol: () => false,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms, cleared: false }); return id; },
    clearTimeout: id => { const timer = timers.get(id); if (timer) timer.cleared = true; },
  });
  vm.runInContext(read('site/shared/instruments.js'), context, { filename: 'instruments.js' });
  vm.runInContext(read('site/shared/quote-panel.js'), context, { filename: 'quote-panel.shared.js' });
  const model = module.exports;
  module.exports = {};
  vm.runInContext(read('site/js/quote-panel.js'), context, { filename: 'quote-panel.js' });
  const dom = module.exports;
  const panel = dom.initQuotePanel({ documentRef, store, pins });
  return { context, model, dom, panel, timers, store, pins };
}

function row(label, overrides = {}) {
  return {
    price: 100, prev_close: 99, chg_pct: 1.01, source: 'dxlink',
    observed_at: new Date(NOW - 10_000).toISOString(),
    collector_received_at: new Date(NOW - 5_000).toISOString(),
    quote_ts: new Date(NOW - 5_000).toISOString(),
    observation_granularity: 'event', observation_timezone: 'UTC',
    session: 'regular', market_session: 'regular', adjustment_basis: 'unknown',
    reference: { value: 99, source: 'dxlink', price_basis: 'previous_close' },
    ...overrides,
  };
}

function payload(rows, { lane = { mode: 'equities', eligible: true, sessions: { equities: { status: 'premarket' } } } } = {}) {
  return { schema_version: 2, timestamp: new Date(NOW).toISOString(), feed_stale: false, prices: rows, price_lane: lane };
}

function findCard(container, label) {
  return container.children.find(child => child.getAttribute('data-symbol') === label)?._quoteCard || null;
}

function makeStorage(initial = null) {
  const data = new Map(initial === null ? [] : [['optionsView.quotePins.v1', initial]]);
  return { getItem: key => data.has(key) ? data.get(key) : null, setItem: (key, value) => data.set(key, value) };
}

// ── rendering ────────────────────────────────────────────────────────────────
(async () => {
  const documentRef = new Doc(IDS);
  const module = { exports: {} };
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    console, module, document: documentRef, Intl, Date, JSON, Math, Number, Object, Array, Set, Map, String,
    localStorage: null, isTradeableShareSymbol: () => false,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms, cleared: false }); return id; },
    clearTimeout: id => { const timer = timers.get(id); if (timer) timer.cleared = true; },
  });
  vm.runInContext(read('site/shared/instruments.js'), context, { filename: 'instruments.js' });
  vm.runInContext(read('site/shared/quote-panel.js'), context, { filename: 'quote-panel.shared.js' });
  const model = module.exports;
  module.exports = {};
  vm.runInContext(read('site/js/quote-panel.js'), context, { filename: 'quote-panel.js' });
  const dom = module.exports;

  const store = new model.QuoteStore({ nowFn: () => NOW });
  const pins = new model.QuotePins({ storage: makeStorage('[]') });
  const panel = dom.initQuotePanel({ documentRef, store, pins });

  const rows = {
    QQQ: row('QQQ', { price: 656.2, chg_pct: 0.5 }),
    SPY: row('SPY', { price: 670.2, chg_pct: 0.3 }),
    VIX: row('VIX', { price: 18.4, chg_pct: -2 }),
    '10Y': row('10Y', { price: 4.12, chg_pct: 0.5 }),
    'EUR/USD': row('EUR/USD', { price: 1.08, chg_pct: 0.1, market_session: 'overnight' }),
    '/CL': row('/CL', { price: 70.1, chg_pct: 1, market_session: 'overnight', observation_granularity: 'day', observation_date: '2026-11-07' }),
    '/GC': row('/GC', { price: 2100, chg_pct: 0.4, market_session: 'overnight', observation_granularity: 'day', observation_date: '2026-11-07' }),
    'BTC/USD': row('BTC/USD', { price: 90000, market_session: 'continuous', session: 'continuous', rolling_change_pct: 5.88, chg_pct: 0.2, change_basis: 'rolling_24h',
      rolling_reference: { value: 85000, observed_at: new Date(NOW - 86400000).toISOString(), source: 'rolling_history', price_basis: 'rolling_24h' } }),
    AAPL: row('AAPL', { price: 210, chg_pct: 3.1 }),
    MSFT: row('MSFT', { price: 410, chg_pct: 2.2 }),
    NVDA: row('NVDA', { price: 180, chg_pct: -1.4 }),
    TSLA: row('TSLA', { price: 250, chg_pct: -3.2 }),
    SMH: row('SMH', { price: 460, chg_pct: 0.8 }),
    IGV: row('IGV', { price: 99, chg_pct: -0.2 }),
    USO: row('USO', { price: 72, chg_pct: 0.1 }),
    NFLX: row('NFLX', { price: 500, chg_pct: 0.4 }),
  };
  store.mergePayload(payload(rows));
  panel.render();
  await Promise.resolve();

  assert.equal(panel.els.session.textContent, 'Premarket · US equities');
  assert.equal(panel.els.core.children.length, 8, 'core always shows its eight anchors');
  assert.equal(findCard(panel.els.core, 'QQQ').priceEl.textContent, '656.20');
  assert.equal(findCard(panel.els.core, 'QQQ').stateEl.textContent, 'Regular');
  assert.equal(findCard(panel.els.core, 'BTC/USD').changeEl.textContent, '+5.88%');
  assert.equal(findCard(panel.els.core, '/CL').stateEl.textContent, 'daily');
  const qqqCardElement = findCard(panel.els.core, 'QQQ').el;
  store.mergePayload(payload({ ...rows, QQQ: row('QQQ', { price: 657.1, chg_pct: 0.6 }) }));
  panel.render();
  assert.equal(findCard(panel.els.core, 'QQQ').el, qqqCardElement, 'a refresh reconciles cards instead of replacing them');
  assert.equal(findCard(panel.els.core, 'QQQ').priceEl.textContent, '657.10');

  assert.equal(panel.els.pinsEmpty.hidden, false, 'empty pins show an explicit empty state');
  assert.equal(panel.pins.add('NFLX').ok, true);
  assert.equal(panel.pins.add('AAPL').ok, true);
  panel.render();
  assert.equal(panel.els.pins.children.length, 2);
  assert.ok(panel.els.pinsEmpty.hidden);
  assert.equal(findCard(panel.els.pins, 'NFLX').moveLeft.disabled, true, 'first pin cannot move left');
  assert.equal(findCard(panel.els.pins, 'AAPL').moveRight.disabled, true, 'last pin cannot move right');
  assert.equal(findCard(panel.els.pins, 'NFLX').moveRight.getAttribute('aria-label'), 'Move NFLX right');
  assert.equal(findCard(panel.els.pins, 'NFLX').unpin.getAttribute('aria-label'), 'Unpin NFLX');
  const focusedButton = findCard(panel.els.pins, 'AAPL').moveLeft;
  focusedButton.focus();
  const focusedCardElement = findCard(panel.els.pins, 'AAPL').el;
  panel.render();
  assert.equal(findCard(panel.els.pins, 'AAPL').el, focusedCardElement, 'an unchanged pin keeps its element node');
  assert.equal(focusedButton.focused, true, 'a refresh does not blur a focused pin control');
  findCard(panel.els.pins, 'AAPL').moveLeft.dispatch('click');
  assert.deepEqual(Array.from(panel.pins.list()), ['AAPL', 'NFLX'], 'move-left reorders and persists');
  assert.match(panel.els.announce.textContent, /moved left/);
  findCard(panel.els.pins, 'NFLX').unpin.dispatch('click');
  assert.deepEqual(Array.from(panel.pins.list()), ['AAPL']);
  assert.match(panel.els.announce.textContent, /Unpinned/);

  const staleClock = [NOW];
  const staleDoc = new Doc(IDS);
  const staleStore = new model.QuoteStore({ nowFn: () => staleClock[0] });
  const stalePanel = dom.initQuotePanel({
    documentRef: staleDoc, store: staleStore,
    pins: new model.QuotePins({ storage: makeStorage('[]') }),
  });
  staleStore.mergePayload(payload({
    QQQ: row('QQQ', { observed_at: new Date(NOW - 3 * 60 * 60_000).toISOString() }),
    SMH: row('SMH', { observed_at: new Date(NOW - 3 * 60 * 60_000).toISOString() }),
  }, { lane: null }));
  stalePanel.render();
  assert.equal(stalePanel.els.session.textContent, 'Session unknown',
    'stale extended-session rows cannot claim the market is open');
  assert.match(stalePanel.els.moversScope.textContent, /No active group/);
  assert.equal(stalePanel.els.moversUp.children.length, 0);

  const receiptDoc = new Doc(IDS);
  const receiptStore = new model.QuoteStore({ nowFn: () => NOW });
  const receiptPanel = dom.initQuotePanel({
    documentRef: receiptDoc, store: receiptStore,
    pins: new model.QuotePins({ storage: makeStorage('[]') }),
  });
  receiptStore.mergePayload(payload({
    SPY: row('SPY', {
      observed_at: null,
      quote_ts: new Date(NOW).toISOString(),
      collector_received_at: new Date(NOW).toISOString(),
      market_session: 'premarket', session: 'regular',
      price: 739.37, prev_close: 735.29, chg_pct: 0.55,
    }),
  }));
  receiptPanel.render();
  assert.equal(findCard(receiptPanel.els.core, 'SPY').stateEl.textContent, 'unknown',
    'a receipt-only current price is not a live observation');
  assert.equal(receiptPanel.els.moversUp.children.length, 0);
  assert.match(receiptPanel.els.moversScope.textContent, /0 eligible/);

  // Picker filters and adds; core and already-pinned instruments never appear.
  panel.openPicker();
  assert.equal(panel.els.pinDialog.hidden, false);
  assert.equal(panel.els.pinSearch.focused, true);
  panel.els.pinSearch.value = 'net';
  panel.els.pinSearch.dispatch('input');
  const candidates = panel.els.pinList.children
    .filter(item => item.className === 'pin-candidate')
    .map(item => item.children[1].textContent);
  assert.ok(candidates.includes('NFLX'));
  assert.ok(!candidates.includes('QQQ'));
  panel.els.pinSearch.value = '';
  panel.els.pinSearch.dispatch('input');
  panel.els.pinList.children[0].children[0].dispatch('click');
  assert.ok(panel.pins.list().length >= 1);
  panel.els.pinDialog.dispatch('keydown', { key: 'Escape' });
  assert.equal(panel.els.pinDialog.hidden, true);

  // Movers: separate up/down, tied to the active equity group, deterministic.
  const up = panel.els.moversUp.children.map(item => item.children[1].textContent);
  const down = panel.els.moversDown.children.map(item => item.children[1].textContent);
  assert.deepEqual(up, ['AAPL', 'MSFT', 'SMH']);
  assert.deepEqual(down, ['TSLA', 'NVDA', 'IGV']);
  assert.match(panel.els.moversScope.textContent, /US equities/);
  assert.match(panel.els.moversNote.textContent, /our tracked universe/);

  // Session switch changes only the active group, never pins.
  store.mergePayload(payload(rows, { lane: { mode: 'crypto', eligible: true } }));
  panel.render();
  assert.match(panel.els.session.textContent, /Crypto/);
  assert.deepEqual(panel.els.moversUp.children.map(item => item.children[1].textContent), ['BTC/USD']);
  assert.equal(findCard(panel.els.pins, 'AAPL').priceEl.textContent, '210.00', 'pins survive a session change');

  // ── controller ─────────────────────────────────────────────────────────────
  let calls = 0;
  let response = payload({ QQQ: row('QQQ', { price: 657 }) });
  const controller = new dom.QuotePanelController({
    refresh: async () => { calls += 1; return response; },
    store, panel, documentRef, windowRef: { addEventListener() {}, removeEventListener() {} },
    nowFn: () => NOW,
  });
  controller.start({ immediate: false });
  const scheduled = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(scheduled.ms, 15_000, 'visible tab refreshes at the bounded cadence');
  await scheduled.fn();
  assert.equal(calls, 1);
  assert.equal(store.row('QQQ').price, 657, 'one refresh feeds the shared store');
  assert.ok(!panel.els.updated.textContent.includes('failed'));

  // Failure keeps last values and backs off without duplicating timers.
  response = null;
  const next = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  await next.fn();
  assert.match(panel.els.updated.textContent, /refresh failed/);
  assert.equal(store.row('QQQ').price, 657, 'failed refresh retains the last value');
  const backoff = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(backoff.ms, 15_000);
  await backoff.fn();
  const backoff2 = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(backoff2.ms, 30_000, 'backoff doubles per failure');
  response = payload({ QQQ: row('QQQ', { price: 658 }) });
  await backoff2.fn();
  const afterRecovery = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(afterRecovery.ms, 15_000, 'success resets the cadence');
  assert.equal(panel.status, null);

  // Single-flight: two calls share one in-flight request.
  let resolvePending;
  let pendingCalls = 0;
  const pendingController = new dom.QuotePanelController({
    refresh: () => { pendingCalls += 1; return new Promise(resolve => { resolvePending = resolve; }); },
    store, panel, documentRef, nowFn: () => NOW,
  });
  pendingController.start({ immediate: true });
  const zero = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(zero.ms, 0);
  const first = zero.fn();
  const second = zero.fn();
  assert.equal(pendingCalls, 1, 'one in-flight fetch at a time');
  resolvePending(payload({ QQQ: row('QQQ', { price: 659 }) }));
  await Promise.all([first, second]);
  pendingController.stop();

  // Hidden tabs use the slower cadence; returning to the foreground refreshes now.
  documentRef.visibilityState = 'hidden';
  documentRef.dispatch('visibilitychange');
  const hiddenTimer = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(hiddenTimer.ms, 60_000);
  documentRef.visibilityState = 'visible';
  documentRef.dispatch('visibilitychange');
  const visibleTimer = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  assert.equal(visibleTimer.ms, 0);

  // An outage advances ages: the failed refresh re-decorates retained rows so
  // the pinned card becomes stale and leaves the movers instead of freezing.
  const agingClock = [NOW];
  const agingStore = new model.QuoteStore({ nowFn: () => agingClock[0] });
  const agingDoc = new Doc(IDS);
  const agingPanel = dom.initQuotePanel({
    documentRef: agingDoc,
    store: agingStore,
    pins: new model.QuotePins({ storage: makeStorage(JSON.stringify(['AAPL'])) }),
  });
  agingStore.mergePayload(payload({ AAPL: row('AAPL', { observed_at: new Date(NOW - 10_000).toISOString() }) }));
  agingPanel.render();
  assert.equal(findCard(agingPanel.els.pins, 'AAPL').stateEl.textContent, 'Regular');
  agingClock[0] += 60 * 60_000;
  let agingCalls = 0;
  const agingController = new dom.QuotePanelController({
    refresh: async () => { agingCalls += 1; return null; },
    store: agingStore, panel: agingPanel, documentRef: agingDoc,
    nowFn: () => agingClock[0], backoffMs: 1_000,
  });
  agingController.start({ immediate: true });
  const agingTimer = [...timers.values()].filter(timer => !timer.cleared).at(-1);
  await agingTimer.fn();
  assert.equal(agingCalls, 1);
  assert.equal(findCard(agingPanel.els.pins, 'AAPL').stateEl.textContent, 'stale',
    'a failed refresh ages the retained card');
  assert.equal(agingPanel.els.moversUp.children.length, 0, 'the stale row leaves movers');
  assert.match(agingPanel.els.updated.textContent, /refresh failed/);
  agingController.stop();

  controller.stop();
  assert.equal(controller.running, false);
  assert.equal([...timers.values()].filter(timer => !timer.cleared).length, 0, 'stop clears its timer');

  console.log('quote panel DOM and controller: ok');
})().catch(error => { console.error(error); process.exit(1); });
