// B3 slice 2 (Nov 2026): DISPLAY / TIER_COLORS, the heatmap cell formatters
// (fmtOI / fmtVol / fmtQ / quoteHtml / flowLevel / flowHtml),
// computeAtmWindow and buildHeatmapRows moved verbatim out of docs/shared.js
// into docs/shared/heatmap.js. Pins the module's ?v= hash in the page,
// asserts the ten names no longer live in shared.js, pins the entry's export
// surface (computeAtmWindow is still re-exported — it was always one of the
// 26 names — while the other nine stay browser globals by design), and drives
// the moved code directly: the formatters and the ATM window under Node, and
// buildHeatmapRows in a vm context with stubbed DOM, loaded in page order
// (csv-ranges.js first, so getThresh / oiBucket are the globals it reads).
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'docs', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'docs', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'docs', 'shared', 'heatmap.js'), 'utf8');
const heatmap = require('../docs/shared/heatmap.js');
const entry = require('../docs/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/heatmap.js?v=' + moduleHash),
  'heatmap script URL must match its content hash on every hosting path');

for (const name of ['fmtOI', 'fmtVol', 'fmtQ', 'quoteHtml', 'flowLevel', 'flowHtml', 'computeAtmWindow', 'buildHeatmapRows']) {
  assert.ok(!shared.includes(`function ${name}(`), `${name} must no longer live in shared.js`);
  assert.equal(typeof heatmap[name], 'function', `heatmap.js must export ${name}`);
}
for (const decl of ['const DISPLAY = 20;', 'const TIER_COLORS = {']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
}
assert.equal(heatmap.DISPLAY, 20, 'DISPLAY is the ±20 strike window');
assert.deepEqual(heatmap.TIER_COLORS, { '0DTE_Regular': '#4a90d9', '0DTE_Weekly': '#e8c84b', '0DTE_Monthly': '#e8604b' });

// The entry's export surface is unchanged by the split: exactly the same 26
// names, with computeAtmWindow re-exported from the new home — the same
// function object, not a copy — and the other nine names still browser-only.
assert.equal(typeof entry.computeAtmWindow, 'function', 'the entry must still export computeAtmWindow');
assert.equal(entry.computeAtmWindow, heatmap.computeAtmWindow, 'the entry re-exports the moved function itself');
for (const name of ['DISPLAY', 'TIER_COLORS', 'fmtOI', 'fmtVol', 'fmtQ', 'quoteHtml', 'flowLevel', 'flowHtml', 'buildHeatmapRows']) {
  assert.equal(entry[name], undefined, `${name} stays a browser global — the entry must not re-export it`);
}

// --- cell formatters: the same values the live table renders. ---
assert.equal(heatmap.fmtOI(0), '');
assert.equal(heatmap.fmtOI(999), '999');
assert.equal(heatmap.fmtOI(5432), '5.4K');
assert.equal(heatmap.fmtOI(12345), '12K');
assert.equal(heatmap.fmtVol(0), '');
assert.equal(heatmap.fmtVol(999), '999');
assert.equal(heatmap.fmtVol(5432), '5.4k');
assert.equal(heatmap.fmtVol(12345), '12k');
assert.equal(heatmap.fmtQ(null), '');
assert.equal(heatmap.fmtQ(1.234), '1.23');
assert.equal(heatmap.fmtQ(12.34), '12.3');
assert.equal(heatmap.quoteHtml(null, null), '');
assert.equal(heatmap.quoteHtml(1.1, 1.2), '<span class="quote">1.10 × 1.20</span>');
assert.equal(heatmap.flowLevel(0), 0);
assert.equal(heatmap.flowLevel(19), 1);
assert.equal(heatmap.flowLevel(99), 2);
assert.equal(heatmap.flowLevel(499), 3);
assert.equal(heatmap.flowLevel(500), 4);
assert.equal(heatmap.flowHtml(0), '');
assert.ok(heatmap.flowHtml(10).includes('flow-1'), 'a small delta is a level-1 flow chip');
assert.ok(heatmap.flowHtml(50).includes('flow-2'), 'a mid delta is a level-2 flow chip');
assert.ok(heatmap.flowHtml(2500).includes('+2.5k'), 'a large delta is abbreviated');

// --- computeAtmWindow: the ATM strike + the ±DISPLAY window. ---
{
  const rows = [{ Strike: 598 }, { Strike: 600 }, { Strike: 602 }];
  const w = heatmap.computeAtmWindow(rows, 600.5);
  assert.equal(w.atm, 600);
  assert.equal(w.offsets.length, 41, '±20 strikes either side of the ATM strike');
  assert.ok(w.strikes.has(580) && w.strikes.has(620), 'the window spans the full ±20 range');
  assert.deepEqual(heatmap.computeAtmWindow(rows, null), { atm: null, offsets: [], strikes: new Set() });
}

// --- buildHeatmapRows: renders the same 41 rows through the real parsers. ---
function makeEl() {
  const el = {
    textContent: '', style: {}, dataset: {}, children: [],
    className: '', title: '', onclick: null,
    classList: { added: [], removed: [],
      add(c) { this.added.push(c); }, remove(c) { this.removed.push(c); } },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
  };
  let _innerHTML = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => _innerHTML,
    set: v => { _innerHTML = String(v); el.children.length = 0; },
    enumerable: true, configurable: true,
  });
  return el;
}
const rangesCsv = (() => {
  const lines = ['Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90'];
  for (let o = -20; o <= 20; o++) {
    const base = 1000 + Math.abs(o) * 10;
    lines.push(`0DTE_Regular,${o},${base * 0.4},${base * 0.6},${base * 0.8},${base},${base * 0.4},${base * 0.6},${base * 0.8},${base}`);
  }
  return lines.join('\n');
})();
const ctx = vm.createContext({
  console,
  document: { createElement: () => makeEl() },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'docs', 'shared', 'csv-ranges.js'), 'utf8'), ctx, { filename: 'docs/shared/csv-ranges.js' });
vm.runInContext(moduleSource, ctx, { filename: 'docs/shared/heatmap.js' });
const get = name => vm.runInContext(name, ctx);
assert.equal(get('typeof buildHeatmapRows'), 'function', 'the module must define buildHeatmapRows in the page scope');
assert.equal(get('typeof getThresh'), 'function', 'csv-ranges.js must load first so the module reads its globals');
vm.runInContext(`ranges = parseRangesCsv(${JSON.stringify(rangesCsv)});`, ctx);
const rows = [];
for (let s = 580; s <= 620; s++) {
  rows.push({ Strike: s, Type: 'call', OpenInterest: 1000, Volume: 50, VolDelta: 10, Bid: 1.1, Ask: 1.2, OptionSymbol: `QQQ261006C00${s}000` });
  rows.push({ Strike: s, Type: 'put', OpenInterest: 800, Volume: 40, VolDelta: 5, Bid: 1.0, Ask: 1.1, OptionSymbol: `QQQ261006P00${s}000` });
}
vm.runInContext('tbody = document.createElement("tbody");', ctx);
const ok = vm.runInContext(`buildHeatmapRows(tbody, ${JSON.stringify({ rows, underlying_price: 600, tier: '0DTE_Regular' })}, ranges)`, ctx);
assert.equal(ok, true, 'a usable spot + rows renders');
assert.equal(vm.runInContext('tbody.children.length', ctx), 41, 'one row per strike in the ±20 window');
assert.ok(vm.runInContext('tbody.children[20].classList.added.includes("atm-row")', ctx), 'the middle row is the ATM row');
assert.ok(vm.runInContext('tbody.children[20].children[1].textContent', ctx).includes('600 ★'), 'the ATM strike cell is marked');
assert.equal(vm.runInContext('buildHeatmapRows(tbody, { rows: [], underlying_price: 600 }, ranges)', ctx), false, 'no rows renders nothing');
assert.equal(vm.runInContext(`buildHeatmapRows(tbody, ${JSON.stringify({ rows, underlying_price: null })}, ranges)`, ctx), false, 'no spot renders nothing');
