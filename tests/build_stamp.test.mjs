import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { stampHtml } from '../scripts/stamp-build.mjs';

const source = readFileSync(new URL('../docs/index.html', import.meta.url), 'utf8');
const revision = '73db30a01092a1fce45b8f0b0a88186db5ecb2ea';
const time = new Date('2026-11-02T23:36:14Z');

test('packages the real source, changes only the declaration, and emits a byte receipt', () => {
  const { html, receipt } = stampHtml(source, revision, time);
  assert.equal(receipt.build_id, '73db30a-20261102233614');
  assert.equal(html.replace(receipt.build_id, '__BUILD_ID__'), source);
  assert.equal(receipt.bytes, Buffer.byteLength(html));
  assert.equal(receipt.sha256, createHash('sha256').update(html).digest('hex'));
  assert.equal(receipt.commit, revision);
  assert.equal(receipt.asset, 'docs/index.html');
  // The stale-page detector's existing extractor sees the exact stamped ID.
  assert.equal(html.match(/const BUILD_ID = "([^"]+)"/)[1], receipt.build_id);
});

test('different build times and revisions yield different IDs and asset hashes', () => {
  const a = stampHtml(source, revision, time);
  const b = stampHtml(source, revision, new Date('2026-11-07T11:00:00Z'));
  const c = stampHtml(source, 'a'.repeat(40), time);
  assert.notEqual(a.receipt.build_id, b.receipt.build_id);
  assert.notEqual(a.receipt.sha256, b.receipt.sha256);
  assert.notEqual(a.receipt.build_id, c.receipt.build_id);
});

for (const [name, transform] of [
  ['already stamped input', s => s.replace('__BUILD_ID__', 'old-build')],
  ['missing declaration', s => s.replace('const BUILD_ID = "__BUILD_ID__";', '')],
  ['duplicate declaration', s => s + '\nconst BUILD_ID = "__BUILD_ID__";\n'],
  ['extra unresolved placeholder', s => s + '\n__BUILD_ID__\n'],
  ['unexpected declaration format', s => s.replace('const BUILD_ID =', 'let BUILD_ID =')],
]) {
  test(`rejects ${name} before producing a packaged asset`, () => {
    assert.throws(() => stampHtml(transform(source), revision, time), /exactly one unstamped/);
  });
}

test('rejects unverified revision and invalid clock', () => {
  assert.throws(() => stampHtml(source, '73db30a', time), /full Git commit/);
  assert.throws(() => stampHtml(source, revision, new Date('invalid')), /Invalid build time/);
});

test('preserves CRLF line endings and non-declaration content', () => {
  const crlf = source.replace(/\r?\n/g, '\r\n');
  const result = stampHtml(crlf, revision, time);
  assert.equal(result.html.replace(result.receipt.build_id, '__BUILD_ID__'), crlf);
});
