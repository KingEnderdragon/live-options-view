'use strict';
// Producer-publisher-consumer parity bridge (F2).
//
// The Python parity test (tests/test_oa203_windows_parity.py) builds every
// supported window kind with the REAL producer, publishes each with the REAL
// publisher to a LocalFilesystemBackend tree, and writes a manifest of the
// producer's own rows/counts/detail. This test reads that manifest and the
// same publisher-written trees through the REAL consumer over a local
// Source directory, asserting the consumer serves exactly what the producer
// wrote: same rows, ranks, pcts, entry/exit and pinned detail, for every
// kind x basis x state. It is skipped unless PARITY_MANIFEST is set, so the
// ordinary `node --test market_mcp/*.test.js` run stays hermetic.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { Consumer } = require('./consumer');
const { Source } = require('./source');

const manifestPath = process.env.PARITY_MANIFEST;
const BASES = ['mid', 'exec', 'qualified'];
const ROW_FIELDS = ['symbol', 'rank', 'pct', 'entry', 'exit', 'underlying', 'option_type',
  'strike', 'expiration', 'entry_session', 'exit_session'];
const DETAIL_FIELDS = ['pct', 'entry', 'exit', 'entry_ms', 'exit_ms', 'entry_session', 'exit_session'];
const NOW = Date.parse('2026-10-10T12:00:00Z');

test('parity: producer-publisher-consumer for every kind, basis and state',
  { skip: !manifestPath }, async () => {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    assert.equal(manifest.version, 1);
    assert.ok(Array.isArray(manifest.variants) && manifest.variants.length > 0);
    const seen = new Set();
    for (const v of manifest.variants) {
      const id = `${v.kind}/${v.window_id}/${v.state}`;
      assert.ok(!seen.has(id), `duplicate manifest variant ${id}`);
      seen.add(id);
      const source = new Source({ directory: v.root });
      const consumer = new Consumer(source, { now: () => NOW });
      for (const basis of BASES) {
        const out = await consumer.call('window_rankings',
          { window_id: v.window_id, kind: v.kind, basis, limit: 50 });
        assert.equal(out.status, v.state === 'final' ? 'available' : 'partial',
          `${id} ${basis}: status`);
        assert.deepEqual(out.window_state, { state: v.state, reasons: v.state_reasons },
          `${id} ${basis}: window_state`);
        assert.deepEqual(out.coverage, v.counts, `${id} ${basis}: coverage`);
        assert.equal(out.summary.state, v.state, `${id} ${basis}: summary state`);
        const got = out.rows.map(r => Object.fromEntries(ROW_FIELDS.map(f => [f, r[f]])));
        assert.deepEqual(got, v.rows[basis], `${id} ${basis}: rows`);
        const expectedDetail = v.detail[basis];
        if (expectedDetail) {
          assert.ok(out.rows.length > 0, `${id} ${basis}: expected a rank-1 row`);
          assert.equal(out.rows[0].symbol, expectedDetail.symbol, `${id} ${basis}: rank 1`);
          const detail = await consumer.call('result_detail',
            { reference: out.rows[0].detail_reference });
          assert.equal(detail.status, 'available', `${id} ${basis}: detail status`);
          assert.equal(detail.row.symbol, expectedDetail.symbol, `${id} ${basis}: detail row`);
          assert.equal(detail.path.symbol, expectedDetail.symbol, `${id} ${basis}: detail path`);
          for (const f of DETAIL_FIELDS) {
            assert.equal(String(detail.path[`${basis}_${f}`]), String(expectedDetail[f]),
              `${id} ${basis}: detail ${f}`);
          }
          // The parity trees are publisher-written, so they carry latest.json;
          // the detail path must be digest-pinned to that pointer, not merely
          // consistency-checked against the ranking row.
          assert.equal(detail.paths.verified, 'matches_published_pointer',
            `${id} ${basis}: detail paths pin`);
        } else {
          assert.equal(out.rows.length, 0, `${id} ${basis}: expected no rows`);
        }
      }
    }
  });
