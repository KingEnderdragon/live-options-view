// B2 (readability quest): the trade-ticket popover section moved out of
// index.html into docs/js/trade-ticket.js, loaded in the same position. The
// test pins the ?v= hash in the page to the module's content (like
// shared.js), asserts the section no longer lives inline, and drives
// openTicket / openShareTicket / closeTicket with a stub DOM + the page's
// shared-scope stubs: the option ticket's bid×ask head and buy/sell
// enablement, the share ticket's held-position sell gating and limit toggle,
// the order-normalization error path, and the click-outside close.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../docs/js/trade-ticket.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/trade-ticket.js?v=' + moduleHash), 'trade-ticket script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function openTicket'), 'openTicket must live in js/trade-ticket.js, not inline in index.html');
assert.ok(!html.includes('function openShareTicket'), 'openShareTicket must live in js/trade-ticket.js, not inline in index.html');
assert.ok(!html.includes('function closeTicket'), 'closeTicket must live in js/trade-ticket.js, not inline in index.html');
// No test slices up to this banner; it stays in index.html as the section's marker.
assert.ok(html.includes('// ── trade ticket popover'), 'trade ticket banner must stay in index.html as the section marker');

// --- stub DOM: elements with per-selector children + a recording classList. ---
function makeChild() {
  const classes = new Set();
  return {
    textContent: '', value: '', disabled: false,
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    _classes: classes,
    onclick: null, onchange: null,
  };
}
function makeTicketEl() {
  const children = {};
  const el = {
    id: '', innerHTML: '', style: {}, offsetWidth: 300,
    querySelector: sel => (children[sel] ||= makeChild()),
    querySelectorAll: () => [],
    appendChild() {}, remove() { el._removed = true; },
    contains(target) { return target === el || Object.values(children).includes(target); },
    getBoundingClientRect: () => ({ left: 100, right: 200, top: 30, bottom: 50 }),
    _children: children, _removed: false,
  };
  return el;
}
const appended = [];
const listeners = {};
const documentStub = {
  createElement: () => makeTicketEl(),
  body: { appendChild: el => { appended.push(el); } },
  documentElement: { clientWidth: 1280 },
  addEventListener: (type, fn) => { listeners[type] = fn; },
  removeEventListener() {},
};

// --- harness: the page's shared scope, as the module sees it. ---
// `ticketEl` stays declared in the paper-trading section (the page's shared
// scope); the module reads and assigns it, so the sandbox provides it here.
const executeCalls = [];
const pendingCalls = [];
const announceCalls = [];
let book = { QQQ: { pos: 5 } };
let normalizeResult = { value: { qty: 2, order_type: 'market', limit_price: null } };
const sandbox = {
  console,
  document: documentStub,
  window: { scrollX: 0, scrollY: 0 },
  ticketEl: null,
  liveQuotes: new Map([['QQQ260918C600000', { bid: 1.1, ask: 1.2, mid: 1.15 }]]),
  tickerState: new Map([['QQQ', { bid: 600.0, ask: 600.2 }]]),
  instrumentLabel: info => `${info.sym} ${info.strike} ${info.type}`,
  fmtQ: x => '$' + Number(x).toFixed(2),
  executeTrade: (...args) => { executeCalls.push(args); },
  freshShareQuote: q => q,
  computeBook: () => book,
  normalizeShareOrder: () => normalizeResult,
  setTradePending: (...args) => { pendingCalls.push(args); },
  announceOrderStatus: (...args) => { announceCalls.push(args); },
};
const context = vm.createContext(sandbox);
vm.runInContext(moduleSource, context, { filename: 'docs/js/trade-ticket.js' });
const get = name => vm.runInContext(name, context);

assert.equal(typeof get('openTicket'), 'function', 'module must define openTicket');
assert.equal(typeof get('openShareTicket'), 'function', 'module must define openShareTicket');
assert.equal(typeof get('closeTicket'), 'function', 'module must define closeTicket');
assert.equal(typeof listeners.click, 'function', 'module must register the click-outside listener at load');

const fakeEvent = () => ({
  stopPropagation() {},
  currentTarget: { getBoundingClientRect: () => ({ left: 100, right: 200, top: 30, bottom: 50 }) },
});
const optionRow = { OptionSymbol: 'QQQ260918C600000', Strike: 600, Type: 'call', Expiration: '2026-09-18' };

// --- openTicket: with a live quote, both sides enabled, buy executes. ---
get('openTicket')(fakeEvent(), optionRow);
let ticket = appended.at(-1);
assert.equal(ticket.id, 'trade-ticket');
assert.ok(ticket.innerHTML.includes('QQQ260918C600000 600 call'), 'head shows the instrument label');
assert.ok(ticket.innerHTML.includes('$1.10 × $1.20'), 'head shows bid × ask');
assert.ok(!ticket.innerHTML.includes('disabled'), 'both sides enabled with a live quote');
assert.equal(ticket.style.left, '100px', 'ticket positioned by the click target');
assert.equal(ticket.style.top, '54px');
assert.equal(get('ticketEl'), ticket, 'openTicket records the ticket in the shared scope');
const qtyEl = ticket.querySelector('#t-qty');
const orderTypeEl = ticket.querySelector('#t-order-type');
const limitEl = ticket.querySelector('#t-limit');
qtyEl.value = '3'; orderTypeEl.value = 'market';
ticket.querySelector('.btn-buy').onclick();
assert.equal(executeCalls.length, 1, 'buy click executes a trade');
assert.equal(executeCalls[0][1], 'buy');
assert.equal(executeCalls[0][2], '3', 'quantity comes from the ticket input');
assert.equal(executeCalls[0][3], 'market');
assert.equal(executeCalls[0][0].sym, 'QQQ260918C600000');

// --- closeTicket: removes the ticket and clears the shared scope. ---
get('closeTicket')();
assert.equal(ticket._removed, true, 'closeTicket removes the ticket element');
assert.equal(get('ticketEl'), null, 'closeTicket clears the shared scope');
get('closeTicket')(); // closing with nothing open is a no-op, not a throw

// --- openTicket: with no live quote, both sides disabled in the markup. ---
sandbox.liveQuotes = new Map();
get('openTicket')(fakeEvent(), optionRow);
ticket = appended.at(-1);
assert.ok(ticket.innerHTML.includes('— × —'), 'head shows dashes with no quote');
assert.ok(ticket.innerHTML.includes('disabled'), 'both sides disabled with no quote');
get('closeTicket')();
sandbox.liveQuotes = new Map([['QQQ260918C600000', { bid: 1.1, ask: 1.2, mid: 1.15 }]]);

// --- openShareTicket: held shares, buy executes through normalization. ---
get('openShareTicket')(fakeEvent(), 'QQQ');
ticket = appended.at(-1);
assert.equal(ticket.id, 'trade-ticket');
assert.ok(ticket.innerHTML.includes('QQQ shares'), 'share head shows the symbol');
assert.ok(ticket.innerHTML.includes('$600.00 × $600.20'), 'share head shows bid × ask');
assert.ok(!ticket.innerHTML.includes('disabled'), 'sell enabled while holding shares');
const shareQty = ticket.querySelector('#t-qty');
const shareOrderType = ticket.querySelector('#t-order-type');
const shareLimit = ticket.querySelector('#t-limit');
const shareBuy = ticket.querySelector('.btn-buy');
const shareSell = ticket.querySelector('.btn-sell');
shareQty.value = '2'; shareOrderType.value = 'market';
shareBuy.onclick();
assert.equal(executeCalls.length, 2, 'share buy executes a trade');
assert.equal(executeCalls[1][0].instrument_type, 'share');
assert.equal(executeCalls[1][1], 'buy');
assert.equal(executeCalls[1][2], 2, 'quantity comes from the normalized order');
assert.equal(executeCalls[1][3], 'market');

// --- openShareTicket: limit toggle renames the buttons and clears the price. ---
shareOrderType.value = 'limit';
shareOrderType.onchange();
assert.equal(shareBuy.textContent, 'Buy limit');
assert.equal(shareSell.textContent, 'Sell limit');
shareLimit.value = '601';
shareOrderType.value = 'market';
shareOrderType.onchange();
assert.equal(shareBuy.textContent, 'Buy market');
assert.equal(shareLimit.value, '', 'switching back to market clears the limit price');

// --- openShareTicket: a normalization error reports, never executes. ---
normalizeResult = { error: 'Quantity must be at least 1' };
const execBefore = executeCalls.length;
shareBuy.onclick();
assert.equal(executeCalls.length, execBefore, 'a rejected order never executes');
assert.deepEqual(pendingCalls.at(-1), [false, 'Quantity must be at least 1']);
assert.deepEqual(announceCalls.at(-1), ['Quantity must be at least 1', 'error']);
normalizeResult = { value: { qty: 2, order_type: 'market', limit_price: null } };
get('closeTicket')();

// --- openShareTicket: nothing held, the sell side is disabled. ---
book = {};
get('openShareTicket')(fakeEvent(), 'QQQ');
ticket = appended.at(-1);
assert.ok(ticket.innerHTML.includes('disabled'), 'sell disabled with nothing held');
get('closeTicket')();
book = { QQQ: { pos: 5 } };

// --- click-outside listener: closes on an outside click, ignores an inside one. ---
get('openTicket')(fakeEvent(), optionRow);
ticket = appended.at(-1);
const inside = ticket.querySelector('#t-qty');
listeners.click({ target: inside });
assert.equal(get('ticketEl'), ticket, 'a click inside the ticket leaves it open');
listeners.click({ target: {} });
assert.equal(get('ticketEl'), null, 'a click outside the ticket closes it');
assert.equal(ticket._removed, true);
