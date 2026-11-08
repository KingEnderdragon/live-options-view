// B2 (readability quest): the countdown section moved out of index.html
// into docs/js/countdown-timer.js, loaded in the same position. The test
// loads the shipped module instead of slicing the page by banner text, pins
// the ?v= hash in the page to the module's content (like shared.js), and
// drives the timer with fake setInterval/clearInterval so the 60s cycle is
// verified without waiting a minute.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../docs/index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '../docs/js/countdown-timer.js'), 'utf8');
const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/countdown-timer.js?v=' + moduleHash), 'countdown-timer script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function startCountdown'), 'startCountdown must live in js/countdown-timer.js, not inline in index.html');
// The chain_underlying test used to slice the data fetch cycle up to this
// banner; since B2 slice 5 it loads docs/js/data-fetch-cycle.js instead.
// The banner stays in index.html as the section's marker.
assert.ok(html.includes('// ── countdown timer'), 'countdown banner must stay in index.html as the section marker');

// --- fake-timer harness: the page's shared scope, as the module sees it. ---
let nextHandle = 0;
let intervals = new Map(); // handle -> { fn, ms }
let cleared = [];
let fetchCalls = 0;
const sandbox = {
  console,
  setInterval: (fn, ms) => { const h = ++nextHandle; intervals.set(h, { fn, ms }); return h; },
  clearInterval: h => { cleared.push(h); intervals.delete(h); },
  fetchLatest: () => { fetchCalls++; },
};
const context = vm.createContext(sandbox);
// countdown/timerHandle/REFRESH are declared in the page's shared scope
// (just above the data fetch cycle), before this module loads.
vm.runInContext('let countdown = 7; let timerHandle = null; const REFRESH = 60;', context);
vm.runInContext(moduleSource, context, { filename: 'docs/js/countdown-timer.js' });
const get = name => vm.runInContext(name, context);

assert.equal(typeof get('startCountdown'), 'function', 'module must define startCountdown');
assert.equal(get('countdown'), 7, 'module load alone must not reset the countdown');

// First arm: resets to REFRESH and installs a 1s interval.
vm.runInContext('startCountdown()', context);
assert.equal(get('countdown'), 60, 'startCountdown resets the countdown to REFRESH');
assert.equal(get('timerHandle'), 1, 'startCountdown installs an interval handle');
assert.equal(intervals.size, 1, 'exactly one interval installed');
const tick = intervals.get(1).fn;
assert.equal(intervals.get(1).ms, 1000, 'the tick is 1s');

// 59 ticks: no refresh yet.
for (let i = 0; i < 59; i++) tick();
assert.equal(get('countdown'), 1, '59 ticks leave one second on the countdown');
assert.equal(fetchCalls, 0, 'no refresh before the countdown reaches zero');

// 60th tick: refreshes and resets.
tick();
assert.equal(fetchCalls, 1, 'the countdown refreshes the chain at zero');
assert.equal(get('countdown'), 60, 'the countdown resets after refreshing');

// Re-arm: clears the old interval before installing a new one. (The first
// arm cleared the initial null handle — clearInterval(null) is a no-op.)
vm.runInContext('startCountdown()', context);
assert.deepEqual(cleared, [null, 1], 're-arming clears the previous interval');
assert.equal(get('timerHandle'), 2, 're-arming installs a fresh interval');
assert.equal(intervals.size, 1, 'only the new interval survives the re-arm');
assert.equal(get('countdown'), 60, 're-arming resets the countdown');
console.log('countdown timer: hash pinned, extraction verified, 60s cycle + re-arm passed');
