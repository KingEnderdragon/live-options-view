// The browser instrument projection must be a deterministic view of the one
// accepted exact-100 registry. Regenerating it must not change the committed
// file, and every core/default pin must resolve.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const root = path.join(__dirname, '..');
  const generator = await import(pathToFileURL(path.join(root, 'scripts', 'generate-quote-instruments.mjs')));
  const committed = fs.readFileSync(path.join(root, 'site', 'shared', 'instruments.js'), 'utf8');
  assert.equal(generator.renderInstruments(), committed, 'site/shared/instruments.js must match the registry projection');

  const instruments = require(path.join(root, 'site', 'shared', 'instruments.js'));
  const universe = JSON.parse(fs.readFileSync(path.join(root, 'market_data', 'overview', 'universe.json'), 'utf8'));
  assert.equal(Object.keys(instruments.QUOTE_INSTRUMENTS).length, 100);
  assert.deepEqual(Object.keys(instruments.QUOTE_INSTRUMENTS), universe.instruments.map(row => row.label));
  for (const label of instruments.QUOTE_CORE_LABELS) {
    assert.ok(instruments.QUOTE_INSTRUMENTS[label], `core label ${label} must exist`);
  }
  for (const label of instruments.QUOTE_DEFAULT_PINS) {
    assert.ok(instruments.QUOTE_INSTRUMENTS[label], `default pin ${label} must exist`);
    assert.ok(!instruments.QUOTE_CORE_LABELS.includes(label), `default pin ${label} must not duplicate core`);
  }
  console.log('quote instruments projection: ok');
})().catch(error => { console.error(error); process.exit(1); });
