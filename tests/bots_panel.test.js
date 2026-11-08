// B2 (readability quest): the bots panel moved out of index.html into
// site/js/bots-panel.js, loaded in the same position. The test loads the
// shipped module instead of slicing the page by banner text, pins the ?v=
// hash in the page to the module's content (like shared.js), and drives the
// module's functions with a minimal DOM/quote shim so equity, marks, leader
// selection, the strategy rollup and the stale-mark rules are verified
// without the network. (The roster fetch + polling wiring is covered by
// tests/tab_switching.test.js with a stub setBotsPolling and by the assembly
// check, which drives the real fetchBots against a stubbed /api/bots.)
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '..', 'site', 'js', 'bots-panel.js'), 'utf8');
const moduleHash = createHash('sha256').update(source).digest('hex').slice(0, 16);
assert.ok(html.includes('js/bots-panel.js?v=' + moduleHash), 'bots-panel script URL must match its content hash on every hosting path');
for (const fn of ['fetchBots', 'renderBots', 'botEquity', 'botMark', 'renderStrategyRollup',
  'setBotsPolling', 'botsStatusLine', 'formatBotLastTrade']) {
  assert.ok(!html.includes(`function ${fn}(`), `${fn} must live in js/bots-panel.js, not inline in index.html`);
}
for (const name of ['const BOTS_POLL_MS', 'const BOT_MARK_MAX_AGE_MS']) {
  assert.ok(!html.includes(name), `${name} must live in js/bots-panel.js, not inline in index.html`);
}
// The banner stays in index.html as the section's marker.
assert.ok(html.includes('// ── automated (bot) panel'), 'bots panel banner must stay in index.html as the section marker');

// ── DOM + collaborator shim ───────────────────────────────────────────────────
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, textContent: '', innerHTML: '', classList: { toggle() {} } };
  return els[id];
}
const quotes = new Map();
const liveQuotes = {
  botSymbols: null,
  get: (s) => quotes.get(s) || null,
  setBotPositions(syms) { this.botSymbols = [...syms]; },
};

// Quotes carry observedAt, as LiveQuoteService populates it. Helpers so a test
// says what it means about freshness rather than hand-rolling timestamps.
const fresh = (mid) => ({ mid, observedAt: new Date().toISOString() });
const agedBy = (mid, ms) => ({ mid, observedAt: new Date(Date.now() - ms).toISOString() });

const scope = {
  document: { getElementById: el },
  fetch: async () => { throw new Error('not used in this test'); },
  setInterval: () => 1,
  clearInterval: () => {},
  liveQuotes,
  fmtQ: (v) => (v == null ? '' : v < 10 ? v.toFixed(2) : v.toFixed(1)),
  fmtMoney: (v) => `${v < 0 ? '−' : v > 0 ? '+' : ''}$${Math.abs(v).toFixed(2)}`,
  pnlClass: (v) => (v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : ''),
  escapeHtml: (s) => String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  )),
};

const names = Object.keys(scope);
const factory = new Function(...names, `${source}\nreturn { renderBots, botEquity, renderStrategyRollup, setBotsPolling, botMark,
  get botsData(){return botsData}, set botsData(v){botsData=v},
  get botsError(){return botsError}, set botsError(v){botsError=v} };`);
const panel = factory(...names.map(n => scope[n]));

const pos = (sym, qty, avg) =>
  ({ sym, strike: 694, type: 'call', exp: '2026-07-27', qty, avg_price: avg });
const bot = (alias, username, cash, positions = [], extra = {}) =>
  ({ alias, username, balance_cash: cash, starting_balance: 10000,
     trade_count: positions.length, first_trade_ts: null, last_trade_ts: null,
     positions, created_at: null, ...extra });

// ── botEquity ─────────────────────────────────────────────────────────────────
{
  quotes.clear();
  quotes.set('X', fresh(4.0));

  // Flat account: equity is just cash.
  assert.deepEqual(panel.botEquity(bot('A', 'a', 10000)),
    { complete: true, unmarked: 0, openValue: 0, equity: 10000 });

  // Long 2 @ mark 4.00 -> 2 * 4.00 * 100 = 800 on top of cash.
  const long = panel.botEquity(bot('B', 'b', 9000, [pos('X', 2, 3.0)]));
  assert.equal(long.equity, 9800);
  assert.equal(long.unmarked, 0);

  // Short positions reduce equity by the cost to buy them back.
  const short = panel.botEquity(bot('C', 'c', 11000, [pos('X', -3, 5.0)]));
  assert.equal(short.equity, 11000 - 1200);

  // An unquoted contract makes the whole account incomplete. Returning the sum
  // of the marked legs would omit an unmarked short's liability entirely.
  const partial = panel.botEquity(bot('D', 'd', 9000, [pos('X', 1, 3.0), pos('NOQUOTE', 5, 1.0)]));
  assert.equal(partial.unmarked, 1, 'a contract with no quote is reported unmarked');
  assert.equal(partial.complete, false, 'any unmarked leg makes the account incomplete');
  assert.equal(partial.equity, null, 'no equity is reported when it cannot be fully computed');
  assert.equal(partial.openValue, null);
}

// ── a stale mark is not a mark ────────────────────────────────────────────────
// LiveQuoteService keeps the last quote it received until the symbol leaves
// the interest set, so an upstream outage leaves a frozen price in place. Left
// unchecked that keeps accounts "complete" and keeps ranking them on prices
// from minutes ago.
{
  quotes.clear();
  quotes.set('FRESH', fresh(4.0));
  quotes.set('STALE', agedBy(4.0, 5 * 60_000));    // 5 minutes old
  quotes.set('EDGE',  agedBy(4.0, 30_000));        // within the 60s bound
  quotes.set('SKEWED', { mid: 4.0, observedAt: new Date(Date.now() + 120_000).toISOString() });
  quotes.set('NOTS',  { mid: 4.0 });               // no timestamp at all

  assert.equal(panel.botEquity(bot('A', 'a', 9000, [pos('FRESH', 1, 1.0)])).complete, true);
  assert.equal(panel.botEquity(bot('B', 'b', 9000, [pos('EDGE', 1, 1.0)])).complete, true,
    'a mark inside the freshness bound still counts');
  assert.equal(panel.botEquity(bot('C', 'c', 9000, [pos('STALE', 1, 1.0)])).complete, false,
    'a frozen mark is treated as no mark at all');
  assert.equal(panel.botEquity(bot('D', 'd', 9000, [pos('SKEWED', 1, 1.0)])).complete, false,
    'a wildly future timestamp is not evidence of freshness');
  assert.equal(panel.botEquity(bot('E', 'e', 9000, [pos('NOTS', 1, 1.0)])).complete, false,
    'a quote with no observedAt cannot be shown to be fresh');

  // Partial refresh: one leg updates, the other stays frozen. The account is
  // incomplete even though it holds a perfectly current quote.
  const mixed = panel.botEquity(bot('F', 'f', 9000, [pos('FRESH', 1, 1.0), pos('STALE', 1, 1.0)]));
  assert.equal(mixed.complete, false, 'a partial refresh leaves the account unranked');
  assert.equal(mixed.unmarked, 1);
  assert.equal(mixed.equity, null);

  // A full outage: everything freezes, so nothing is ranked and no leader is
  // crowned off prices that stopped moving.
  quotes.clear();
  quotes.set('A1', agedBy(9.0, 10 * 60_000));
  quotes.set('A2', agedBy(1.0, 10 * 60_000));
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      { ...bot('Frozen1', 'f1', 10000, [pos('A1', 5, 1.0)]), strategy_id: 's' },
      { ...bot('Frozen2', 'f2', 10000, [pos('A2', 5, 1.0)]), strategy_id: 's' },
    ],
  };
  panel.renderBots();
  const out = els['bots-grid'].innerHTML;
  assert.equal((out.match(/bot-card leader/g) || []).length, 0,
    'no leader is crowned during a quote outage');
  assert.equal((out.match(/pending/g) || []).length >= 2, true,
    'every account reports pending while its marks are frozen');
}

// ── an unmarked short must not be crowned leader ──────────────────────────────
// The failure this guards: skipping the unmarked leg and returning the rest
// omits the short's liability, inflating equity above every honest account.
{
  quotes.clear();
  quotes.set('X', fresh(1.0));

  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      // Huge cash, but short 50 contracts nobody can mark. Naively summing the
      // marked legs would show $50,000 and win outright.
      { ...bot('Illusory', 'illusory', 50000, [pos('NOQUOTE', -50, 2.0)]), strategy_id: 's' },
      { ...bot('Honest', 'honest', 10500, [pos('X', 1, 1.0)]), strategy_id: 's' },
    ],
  };
  panel.renderBots();

  const out = els['bots-grid'].innerHTML;
  // The incomplete account sorts last, so its card runs from its alias to the end.
  const illusory = out.slice(out.indexOf('Illusory'));
  assert.equal(illusory.includes('leader'), false, 'an account with an unmarked short cannot be leader');
  // Cash is a known fact and is still shown; it is *equity* that is withheld.
  const equityField = illusory.slice(illusory.indexOf('bot-equity'), illusory.indexOf('bot-meta'));
  assert.match(equityField, /pending/, 'equity is withheld, not estimated');
  assert.equal(/\$[\d,]+\.\d\d/.test(equityField), false,
    'no equity figure is rendered for an account that could not be fully marked');
  assert.match(illusory, /awaiting marks/, 'PnL is withheld too');

  // With only one rankable account there is no contest, so nobody is crowned.
  assert.equal((out.match(/bot-card leader/g) || []).length, 0,
    'a single rankable account is not crowned leader over an unranked one');

  // The incomplete account is ordered after the complete one.
  assert.ok(out.indexOf('Honest') < out.indexOf('Illusory'),
    'unranked accounts sort after ranked ones regardless of nominal cash');
}

// ── ordering is by descending equity ──────────────────────────────────────────
{
  quotes.clear();
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      bot('Alpha', 'alpha', 9000),    // alphabetically first, worst equity
      bot('Zulu', 'zulu', 12000),     // alphabetically last, best equity
      bot('Mike', 'mike', 10500),
    ],
  };
  panel.renderBots();
  const out = els['bots-grid'].innerHTML;
  const order = ['Zulu', 'Mike', 'Alpha'].map(n => out.indexOf(n));
  assert.deepEqual(order, [...order].sort((a, b) => a - b),
    'cards render in descending-equity order, not the alphabetical order the API returns');
}

// ── renderBots ────────────────────────────────────────────────────────────────
{
  quotes.clear();
  quotes.set('X', fresh(4.0));

  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      bot('Winner', 'crassus_win', 12000, [pos('X', 1, 1.0)]),
      bot('Loser', 'crassus_lose', 8000),
    ],
  };
  panel.renderBots();

  const out = els['bots-grid'].innerHTML;
  assert.match(out, /Winner/);
  assert.match(out, /Loser/);
  assert.match(els['bots-status'].textContent, /2 accounts/);

  // The leader border marks exactly the top account by equity.
  assert.equal((out.match(/bot-card leader/g) || []).length, 1, 'exactly one leader is highlighted');
  const winnerCard = out.slice(Math.max(0, out.indexOf('Winner') - 200), out.indexOf('Loser'));
  assert.match(winnerCard, /leader/, 'the highest-equity account is the one marked leader');

  // A flat account renders the explicit flat state, not an empty table.
  assert.match(out, /flat — no open positions/);

  // Bot contracts are registered for quoting.
  assert.deepEqual(liveQuotes.botSymbols, ['X']);
}

// ── per-strategy rollup ───────────────────────────────────────────────────────
{
  quotes.clear();
  quotes.set('X', fresh(4.0));

  const withStrat = (alias, username, cash, strategy_id, positions = []) =>
    ({ ...bot(alias, username, cash, positions), strategy_id });

  // Two strategies: smoke down 1000 across two accounts, sentiment up 2000
  // across one. The rollup must aggregate by strategy, not by account.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      withStrat('A', 'a', 9500, 'smoke_atm_roundtrip'),
      withStrat('B', 'b', 9500, 'smoke_atm_roundtrip'),
      withStrat('C', 'c', 12000, 'reddit_sentiment_qqq'),
    ],
  };
  panel.renderBots();

  const roll = els['bots-by-strategy'].innerHTML;
  assert.match(roll, /reddit_sentiment_qqq/);
  assert.match(roll, /smoke_atm_roundtrip/);
  assert.match(roll, /2 accounts/, 'accounts are counted per strategy');
  assert.match(roll, /\+\$2000\.00/, 'the winning strategy aggregates its PnL');
  assert.match(roll, /−\$1000\.00/, 'losses aggregate across a strategy\'s accounts');
  assert.ok(roll.indexOf('reddit_sentiment_qqq') < roll.indexOf('smoke_atm_roundtrip'),
    'strategies are ordered best PnL first');

  // Cards name their strategy so a reader can tie a result to a strategy.
  assert.match(els['bots-grid'].innerHTML, /bot-strategy">reddit_sentiment_qqq/);
  // Future Crassus strategies are data, not front-end releases. Any valid
  // strategy id returned by the roster receives both a card and a rollup tile.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [withStrat('Future', 'future', 10000, 'future_pr_strategy'),
      withStrat('A', 'a', 10000, 'smoke_atm_roundtrip')],
  };
  panel.renderBots();
  assert.match(els['bots-grid'].innerHTML, /bot-strategy">future_pr_strategy/);
  assert.match(els['bots-by-strategy'].innerHTML, /future_pr_strategy/);

  // A single strategy across the whole book makes the rollup a restatement of
  // the cards, so it is suppressed rather than shown as one meaningless tile.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [withStrat('A', 'a', 9500, 'smoke_atm_roundtrip')],
  };
  panel.renderBots();
  assert.equal(els['bots-by-strategy'].innerHTML, '', 'a one-strategy book shows no rollup');

  // An incomplete account is excluded from its strategy's aggregate and
  // reported as pending, rather than folded in on a partial number.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      withStrat('A', 'a', 9500, 'smoke_atm_roundtrip', [pos('NOQUOTE', 1, 1.0)]),
      withStrat('C', 'c', 12000, 'reddit_sentiment_qqq'),
    ],
  };
  panel.renderBots();
  {
    const roll = els['bots-by-strategy'].innerHTML;
    // smoke's only account is incomplete, so it has nothing marked at all.
    const smoke = roll.slice(roll.indexOf('smoke_atm_roundtrip'));
    assert.match(smoke, /0\/1 marked — unranked/, 'coverage is stated explicitly');
    assert.equal(smoke.includes('−$500.00'), false, 'the excluded account does not contribute PnL');
    assert.ok(roll.indexOf('reddit_sentiment_qqq') < roll.indexOf('smoke_atm_roundtrip'),
      'a strategy with nothing rankable sorts after a fully-marked one');
  }

  // A *partially* marked strategy must not be ranked against fully-marked
  // ones on its subtotal: a 2-of-3 figure and a 3-of-3 figure cover different
  // fractions of their books and are not comparable.
  quotes.clear();
  quotes.set('X', fresh(4.0));
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      // smoke: two accounts up big, one unmarked -> huge subtotal, but partial.
      withStrat('S1', 's1', 20000, 'smoke_atm_roundtrip'),
      withStrat('S2', 's2', 20000, 'smoke_atm_roundtrip'),
      withStrat('S3', 's3', 10000, 'smoke_atm_roundtrip', [pos('NOQUOTE', 1, 1.0)]),
      // sentiment: fully marked, but a modest gain.
      withStrat('R1', 'r1', 10100, 'reddit_sentiment_qqq'),
    ],
  };
  panel.renderBots();
  {
    const roll = els['bots-by-strategy'].innerHTML;
    assert.ok(roll.indexOf('reddit_sentiment_qqq') < roll.indexOf('smoke_atm_roundtrip'),
      'a fully-marked strategy outranks a partially-marked one with a larger subtotal');
    const smoke = roll.slice(roll.indexOf('smoke_atm_roundtrip'));
    assert.match(smoke, /2\/3 marked — unranked/, 'partial coverage is stated as N/M');
    assert.match(smoke, /subtotal/, 'the subtotal is shown but labelled as such');
    assert.match(smoke, /\+\$20000\.00/, 'the subtotal itself is still visible');
  }

  // A bot with no recorded strategy is grouped, not dropped.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      withStrat('A', 'a', 9500, 'smoke_atm_roundtrip'),
      { ...bot('Z', 'z', 10000), strategy_id: null },
    ],
  };
  panel.renderBots();
  assert.match(els['bots-by-strategy'].innerHTML, /unassigned/);
  assert.match(els['bots-grid'].innerHTML, /no strategy recorded/);
}

// ── XSS: a hostile alias must not execute ─────────────────────────────────────
{
  quotes.clear();
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [bot('<img src=x onerror=alert(1)>', 'crassus_evil', 10000)],
  };
  panel.renderBots();
  const out = els['bots-grid'].innerHTML;
  assert.equal(out.includes('<img src=x'), false, 'a hostile alias must be escaped, not injected');
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
}

// ── empty roster clears both the cards and the rollup ─────────────────────────
{
  quotes.clear();
  // Populate a two-strategy rollup first, then liquidate every bot.
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [
      { ...bot('A', 'a', 9500), strategy_id: 'smoke_atm_roundtrip' },
      { ...bot('C', 'c', 12000), strategy_id: 'reddit_sentiment_qqq' },
    ],
  };
  panel.renderBots();
  assert.notEqual(els['bots-by-strategy'].innerHTML, '', 'rollup is populated before liquidation');

  panel.botsData = { as_of: '2026-07-27T15:00:00Z', bots: [] };
  panel.renderBots();
  assert.equal(els['bots-grid'].innerHTML, '');
  assert.equal(els['bots-by-strategy'].innerHTML, '',
    'stale rollup tiles must not survive liquidation of the last bots');
  assert.match(els['bots-status'].textContent, /no automated accounts registered yet/);
}

// ── a roster fetch failure survives quote-driven re-renders ───────────────────
// renderBots() is called on every quote tick. Writing the failure straight to
// the status line let the next tick paint a fresh "updated 12:34:56" over it
// while still showing the last successful roster -- real balances, silently
// out of date.
{
  quotes.clear();
  quotes.set('X', fresh(4.0));
  panel.botsError = null;
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [bot('A', 'a', 12000, [pos('X', 1, 1.0)])],
  };
  panel.renderBots();
  assert.match(els['bots-status'].textContent, /1 account · updated/);

  // The roster fetch now fails; the cards still hold the last good data.
  panel.botsError = 'HTTP 503';
  panel.renderBots();
  assert.match(els['bots-status'].textContent, /roster unavailable — HTTP 503 · showing last known/);

  // A quote tick re-renders and must not paint over the warning.
  panel.renderBots();
  assert.match(els['bots-status'].textContent, /roster unavailable/,
    'a quote-driven re-render must not overwrite the roster error');
  assert.equal(/updated \d/.test(els['bots-status'].textContent), false,
    'no fresh-looking timestamp is shown while the roster is unavailable');

  // An empty roster rendered during an outage still reports the outage.
  panel.botsData = { as_of: '2026-07-27T15:00:00Z', bots: [] };
  panel.renderBots();
  assert.match(els['bots-status'].textContent, /roster unavailable/,
    'the error outranks the empty-roster message');

  // Recovery clears it.
  panel.botsError = null;
  panel.botsData = {
    as_of: '2026-07-27T15:00:00Z',
    bots: [bot('A', 'a', 12000, [pos('X', 1, 1.0)])],
  };
  panel.renderBots();
  assert.match(els['bots-status'].textContent, /1 account · updated/,
    'a successful fetch clears the error state');
}

console.log('PASS automated tab equity, leader, escaping, and empty states');

// Full, fixed-zone trade dates; these instants are today/yesterday relative to
// the synthetic review date 2026-11-06. No wall clock or live accounts needed.
{
  quotes.clear();
  panel.botsError = null;
  const cases = [
    ['today', '2026-11-06T14:30:00Z', 'Nov 6, 2026, 9:30:00 AM EST (New York)'],
    ['yesterday', '2026-11-05T21:15:09Z', 'Nov 5, 2026, 4:15:09 PM EST (New York)'],
    ['older year', '2025-08-15T16:49:00Z', 'Aug 15, 2025, 12:49:00 PM EDT (New York)'],
    ['NY date rollover', '2026-01-01T02:00:00Z', 'Dec 31, 2025, 9:00:00 PM EST (New York)'],
    ['DST first 1:30', '2026-11-01T01:30:00-04:00', 'Nov 1, 2026, 1:30:00 AM EDT (New York)'],
    ['DST second 1:30', '2026-11-01T01:30:00-05:00', 'Nov 1, 2026, 1:30:00 AM EST (New York)'],
    ['equivalent Z', '2026-07-01T16:49:00Z', 'Jul 1, 2026, 12:49:00 PM EDT (New York)'],
    ['equivalent offset', '2026-07-01T12:49:00-04:00', 'Jul 1, 2026, 12:49:00 PM EDT (New York)'],
    ['null', null, 'Not recorded'], ['undefined', undefined, 'Not recorded'],
    ['empty', '', 'Not recorded'], ['whitespace', '  ', 'Not recorded'],
    ['invalid', 'not-a-date', 'Invalid timestamp'],
    ['hostile', '<img src=x onerror=alert(1)>', 'Invalid timestamp'],
    ['nonfinite', Infinity, 'Invalid timestamp'], ['wrong type', true, 'Invalid timestamp'],
    ['numeric epoch', 0, 'Dec 31, 1969, 7:00:00 PM EST (New York)'],
  ];
  for (const [name, value, expected] of cases) {
    const record = bot('Fixture', 'fixture', 12000, [], {
      last_trade_ts: value, trade_count: 3, account_closed: true,
    });
    const snapshot = structuredClone(record);
    Object.freeze(record.positions); Object.freeze(record);
    panel.botsData = { as_of: '2026-11-06T18:00:00Z', bots: [record] };
    panel.renderBots();
    const output = els['bots-grid'].innerHTML;
    assert.ok(output.includes(`Last trade: ${expected}</span>`), name + ': date/zone/state');
    assert.equal(output.includes('<img'), false, name + ': no injected markup');
    assert.equal(output.includes('Invalid Date'), false, name + ': no raw parse error');
    assert.match(output, /Closed — trading stopped; history retained/);
    assert.match(output, /Cash \$12000\.00 · 3 trades/);
    assert.match(output, /class="bot-equity">\$12000\.00/);
    assert.deepEqual(record, snapshot, name + ': input history must not change');
    assert.deepEqual(panel.botEquity(record), {
      complete: true, unmarked: 0, openValue: 0, equity: 12000,
    });
  }
  // Changing only trade dates cannot change ranking, equity, PnL or rollups.
  const records = [bot('Lower', 'low', 11000, [], {strategy_id:'A'}),
    bot('Higher', 'high', 12000, [], {strategy_id:'B'}),
    bot('Unmarked', 'pending', 9000, [pos('MISSING', 1, 3)], {strategy_id:'A'})];
  const financialView = () => els['bots-grid'].innerHTML.replace(/<span class="bot-last-trade">[^<]*<\/span>/g, '');
  panel.botsData = { bots: records }; panel.renderBots();
  const before = financialView(), rollup = els['bots-by-strategy'].innerHTML;
  panel.botsData = { bots: records.map((r, i) => ({...r, last_trade_ts: cases[i][1]})) };
  const snapshot = structuredClone(panel.botsData);
  panel.renderBots();
  assert.equal(financialView(), before);
  assert.equal(els['bots-by-strategy'].innerHTML, rollup);
  assert.deepEqual(panel.botsData, snapshot);
}
console.log('PASS full last-trade dates, fixed New York zone, DST, missing/invalid, escaping and unchanged history');
