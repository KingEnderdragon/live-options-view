// P1: site/js/news-research.js rendering the research projections built by
// tests/news_research_scenarios.py (prepared records A-F and the SYNTHETIC timeline) in a minimal fake DOM
// whose innerHTML throws, so any markup path fails. Checks each stage's required and forbidden text,
// failure states for malformed views, and that only http(s) links become anchors.
// Run: node tests/news_research_view.test.js
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const research = require(path.join(__dirname, '../site/js/news-research.js'));

class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.className = ''; this._text = ''; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set innerHTML(v) { throw new Error('innerHTML must not be used: ' + v); }
  get innerHTML() { throw new Error('innerHTML must not be read'); }
  all(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.all(pred, out)); return out; }
}
function makeDoc() {
  const head = new Node('head');
  return { head, createElement: t => new Node(t), getElementById: id => head.all(n => n.id === id)[0] || null };
}

const out = fs.mkdtempSync(path.join(os.tmpdir(), 'news-research-'));
execFileSync(process.env.PYTHON || 'python', [path.join(__dirname, 'news_research_scenarios.py'), out],
  { stdio: ['ignore', 'pipe', 'inherit'] });
const stages = JSON.parse(fs.readFileSync(path.join(out, 'stages.json'), 'utf8'));
const load = name => JSON.parse(fs.readFileSync(path.join(out, name), 'utf8'));

let n = 0;
function test(name, fn) { fn(); n++; console.log('ok - ' + name); }

for (const s of stages) {
  test(`stage ${s.stage}: ${s.mode} @ ${s.cutoff}`, () => {
    const doc = makeDoc(); const root = new Node('div');
    assert.equal(research.render(doc, root, load(s.file)), 'ready');
    const text = root.textContent;
    for (const m of s.must) assert.ok(text.includes(m), `${s.stage}: missing "${m}"`);
    for (const m of s.must_not) assert.ok(!text.includes(m), `${s.stage}: must not show "${m}"`);
    assert.equal(root.getAttribute('data-items'), String(s.items));
    for (const a of root.all(x => x.tagName === 'A')) {
      assert.ok(/^https?:\/\//.test(a.href), `${s.stage}: anchor ${a.href}`);
      assert.equal(a.rel, 'noopener noreferrer');
    }
    assert.equal(doc.head.all(x => x.id === 'news-research-style').length, 1, 'one style block');
  });
}

test('every time line names its kind: publication / EDGAR acceptance / access / capture commit / annotation commit', () => {
  const root = new Node('div');
  research.render(makeDoc(), root, load('synthetic-edgar.json'));
  const box = root.all(x => x.getAttribute && x.getAttribute('data-kind') === 'research_capture')
    .find(b => b.textContent.includes('13F'));
  const t = box.textContent;
  for (const m of ['EDGAR acceptance 2026-11-16T21:05:12Z', 'accessed 2026-11-17T14:00:00Z', 'capture committed 2026-11-17T14:30:05Z',
    'no collector known-at', 'made 2026-11-17T14:30:00Z', 'committed 2026-11-17T14:30:05Z', 'measured 2026-09-30',
    'filing_due 2026-11-16 (date precision) (not a measurement time)']) {
    assert.ok(t.includes(m), m);
  }
});

test('prepared records keep evidence, status, context and unread hops distinct', () => {
  const root = new Node('div');
  research.render(makeDoc(), root, load('prepared-research.json'));
  const t = root.textContent;
  for (const m of ['bot-blocked 2026-11-12T16:59:58Z', 'HTTP 401 (login)', 'login required', 'not attempted', 'not available to us',
    'redemption', 'unverified', 'offered', 'Context (stated figures, not pressure quantities)',
    'flagship fund AUM (context; never multiplied or added)', 'measured unknown', 'not captured',
    'upper bound: write time of the design note', 'published 2026-11-06 (date precision)',
    'published 2026-11-09T10:30-05:00 (minute precision)', 'PREPARED']) {
    assert.ok(t.includes(m), m);
  }
  const e = root.all(x => x.getAttribute && x.getAttribute('data-lineage') === 'E')[0].textContent;
  assert.ok(e.includes('RETROSPECTIVE hypothesis') && e.includes('thesis assertion'));
  const d = root.all(x => x.getAttribute && x.getAttribute('data-lineage') === 'D')[0].textContent;
  assert.ok(d.includes('CONTEMPORANEOUS hypothesis') && d.includes('bid_deadline 2026-11-19'));
});

test('malformed views are failures, not empty pages', () => {
  const good = load('prepared-research.json');
  const bad = [null, {}, { ...good, schema_version: 'x' }, { ...good, mode: 'live' }, { ...good, items: null },
    { ...good, items: [{ source: {}, annotations: [{}] }] }];
  for (const v of bad) {
    const root = new Node('div');
    assert.equal(research.render(makeDoc(), root, v), 'failed');
    assert.equal(root.getAttribute('data-state'), 'failed');
    assert.ok(root.textContent.includes('Could not show research records'));
  }
});

test('only http(s) URLs become links', () => {
  assert.equal(research.safeUrl('javascript:alert(1)'), null);
  assert.equal(research.safeUrl('data:text/html,x'), null);
  assert.equal(research.safeUrl('//evil.example'), null);
  assert.equal(research.safeUrl('https://example.test/a'), 'https://example.test/a');
  const v = load('synthetic-capture-only.json');
  v.items[0].source.url = 'javascript:alert(1)';
  const root = new Node('div');
  research.render(makeDoc(), root, v);
  assert.equal(root.all(x => x.tagName === 'A').length, 0);
  assert.ok(root.textContent.includes('SYNTHETIC: fund reported cutting gross exposure'));
});

console.log(`\n${n} news research view tests passed`);
