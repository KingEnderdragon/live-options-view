// Tests-only helper (G3; NOT RUN when it was written). The committed fixture views
// (tests/fixtures/news/views/) are builder output with unqualified keys. Since G3 the panel, the proxy
// and the gateway accept only the published form: the fixed index plus immutable generation pages
// news/v1/views/g/<gen>/<mode>/<stream>/<date>/page-NNNN.json. This mirrors
// market_data/news/views.qualify_generation:
//   gen   = <build_as_of as YYYYMMDDTHHMMSSZ>-<first 16 hex of files_digest(unqualified fixture bytes)>
//           (files_digest: SHA-256 over sorted "len(key):key:len(body):" + body; identical to Python's,
//           so the gen equals what the publisher would derive for these bytes);
//   pages get "gen" and same-generation prev_page/next_page; the index gets "gen", rewritten
//   first_page values and "pages_sha256" (here over this helper's JSON text, not Python's encode():
//   the panel only checks its format).
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const VIEWS = path.join(__dirname, '../fixtures/news/views');
const INDEX_KEY = 'news/v1/views/index.json';
const UNQ = /^news\/v1\/views\/(collector|archive|publication)\/(all|ai_buildout|oil)\/([0-9]{4}-[0-9]{2}-[0-9]{2})\/page-([0-9]{4})\.json$/;

function listKeys(dir = VIEWS) {
  const out = [];
  (function walk(d) {
    for (const name of fs.readdirSync(d)) {
      const full = path.join(d, name);
      if (fs.statSync(full).isDirectory()) walk(full);
      else out.push(path.relative(dir, full).split(path.sep).join('/'));
    }
  })(dir);
  return out.sort();
}

function filesDigest(files) {
  const h = createHash('sha256');
  for (const key of Object.keys(files).sort()) {
    const body = Buffer.isBuffer(files[key]) ? files[key] : Buffer.from(files[key], 'utf8');
    h.update(`${key.length}:${key}:${body.length}:`, 'utf8');
    h.update(body);
  }
  return h.digest('hex');
}

function stamp(iso) {
  // "2026-11-09T11:15:00Z" -> "20261109T111500Z" (seconds precision, like strftime('%Y%m%dT%H%M%SZ')).
  return new Date(iso).toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z';
}

function qualifyKey(gen, key) {
  const m = UNQ.exec(key);
  if (!m) throw new Error('not an unqualified page key: ' + key);
  return `news/v1/views/g/${gen}/${m[1]}/${m[2]}/${m[3]}/page-${m[4]}.json`;
}

// files: {unqualified key: Buffer|string}. Returns {gen, files: {published key: object}, q(key)}.
function qualifyFiles(files) {
  const index = JSON.parse(String(files[INDEX_KEY]));
  const gen = stamp(index.build_as_of) + '-' + filesDigest(files).slice(0, 16);
  const q = key => (key === INDEX_KEY ? key : qualifyKey(gen, key));
  const out = {};
  for (const key of Object.keys(files)) {
    if (key === INDEX_KEY) continue;
    const page = JSON.parse(String(files[key]));
    page.gen = gen;
    for (const field of ['prev_page', 'next_page']) {
      if (typeof page[field] === 'string') page[field] = q(page[field]);
    }
    out[q(key)] = page;
  }
  const pagesDigest = filesDigest(Object.fromEntries(Object.entries(out).map(([k, v]) => [k, JSON.stringify(v)])));
  out[INDEX_KEY] = Object.assign({}, index, {
    gen, pages_sha256: pagesDigest,
    views: index.views.map(v => Object.assign({}, v, { first_page: q(v.first_page) })),
  });
  return { gen, files: out, q };
}

// The committed fixture, published form. byKey accepts an unqualified fixture key or a published one.
function loadQualified(dir = VIEWS) {
  const raw = {};
  for (const key of listKeys(dir)) raw[key] = fs.readFileSync(path.join(dir, key));
  const { gen, files, q } = qualifyFiles(raw);
  const byKey = key => files[key in files ? key : q(key)];
  return {
    gen, files, q, byKey,
    index: files[INDEX_KEY],
    text: key => (key in files ? JSON.stringify(files[key]) : null),
  };
}

module.exports = { VIEWS, INDEX_KEY, listKeys, filesDigest, qualifyKey, qualifyFiles, loadQualified };
