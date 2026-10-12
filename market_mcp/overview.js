'use strict';
// Cross-market overview (#132) reader for intraday/overview.json (schema 1).
// The producer (market_overview.py) computes change and publication-time
// freshness; this module validates the contract, preserves producer values,
// recomputes only query-time age, and builds breadth over the full eligible
// curated sample before paging. It never re-derives a change.
const { DataError } = require('./source');

const KEY = 'intraday/overview.json';
const ASSET_CLASSES = ['equity', 'etf', 'index', 'commodity', 'crypto', 'rates', 'fx'];
const ORIGINS = ['baseline', 'watchlist', 'macro'];
const CHANGE = ['ok', 'partial', 'unavailable'];
const FRESHNESS = ['fresh', 'stale', 'closed', 'unknown_time', 'future_dated'];
const SORTS = ['universe', 'percent_desc', 'percent_asc'];
const ID = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$/;
const MAX_INSTRUMENTS = 600;

const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const num = v => v === null || (typeof v === 'number' && Number.isFinite(v));
const time = v => v === null || (typeof v === 'string' && Number.isFinite(Date.parse(v)));
function check(ok, message, code = 'incompatible_schema') { if (!ok) throw new DataError(code, message, KEY); }

function validate(p) {
  check(object(p) && p.schema_version === 1 && p.dataset === 'cross_market_overview', 'Unsupported overview contract.');
  check(typeof p.universe_version === 'string' && time(p.generated_at) && p.generated_at !== null, 'Overview is missing its universe version or generation time.');
  check(object(p.cycle) && ['complete', 'partial', 'failed'].includes(p.cycle.status) && time(p.cycle.newer_failed_attempt_at ?? null), 'Invalid overview cycle.');
  check(Array.isArray(p.groups) && p.groups.every(g => object(g) && ID.test(g.id)), 'Invalid overview groups.');
  check(Array.isArray(p.instruments) && p.instruments.length <= MAX_INSTRUMENTS && Array.isArray(p.gaps), 'Invalid overview instrument list.', 'excessive_or_malformed_artifact');
  const ids = new Set();
  for (const r of p.instruments) {
    check(object(r) && ID.test(r.id) && !ids.has(r.id), 'Invalid or duplicate instrument id.');
    ids.add(r.id);
    check(ASSET_CLASSES.includes(r.asset_class) && typeof r.instrument_type === 'string' && ORIGINS.includes(r.origin) && typeof r.unit === 'string', `Invalid identity for ${r.id}.`);
    check(object(r.current) && num(r.current.value) && time(r.current.observed_at) && object(r.reference) && num(r.reference.value) && time(r.reference.observed_at) && typeof r.reference.basis === 'string', `Invalid observations for ${r.id}.`);
    check(object(r.change) && CHANGE.includes(r.change.status) && Array.isArray(r.change.reasons) && ['absolute', 'percent', 'bps'].every(k => num(r.change[k])), `Invalid change for ${r.id}.`);
    check(r.change.status !== 'unavailable' || ['absolute', 'percent', 'bps'].every(k => r.change[k] === null), `Unavailable change carries values for ${r.id}.`);
    check(object(r.freshness) && FRESHNESS.includes(r.freshness.status) && typeof r.freshness.market_open === 'boolean' && typeof r.freshness.max_age_seconds === 'number' && r.freshness.max_age_seconds > 0, `Invalid freshness for ${r.id}.`);
  }
  check(p.gaps.every(g => object(g) && ID.test(g.id) && typeof g.reason === 'string' && !ids.has(g.id)), 'Invalid overview gaps.');
}

// Query-time freshness. Producer states that are not about age (closed market,
// unknown or future observation time) are kept; otherwise age is recomputed.
function rowFreshness(r, now) {
  const f = r.freshness, observed = r.current.observed_at === null ? NaN : Date.parse(r.current.observed_at);
  const age_seconds = Number.isFinite(observed) ? Math.max(0, (now - observed) / 1000) : null;
  let status = f.status;
  if (['fresh', 'stale'].includes(f.status) && age_seconds !== null) status = age_seconds > f.max_age_seconds ? 'stale' : 'fresh';
  return { status, age_seconds, market_open_at_publication: f.market_open, max_age_seconds: f.max_age_seconds,
    producer_status: f.status, warnings: f.warnings ?? [], basis: 'current.observed_at (provider observation time, never retrieval time)' };
}

function breadth(rows, { group, includeWatchlist }) {
  const eligible = rows.filter(r => r.instrument_type === 'stock' && (includeWatchlist || r.origin === 'baseline') && (!group || r.group === group));
  let advancing = 0, declining = 0, unchanged = 0, missing = 0, stale = 0; const pcts = [], bases = new Set(), sessions = new Set();
  for (const r of eligible) {
    if (r.change.percent === null) { missing++; continue; }
    if (!['fresh', 'closed'].includes(r.query_freshness.status)) { stale++; continue; }
    const p = r.change.percent; pcts.push(p); bases.add(r.reference.basis); if (r.current.session) sessions.add(r.current.session);
    if (p > 0) advancing++; else if (p < 0) declining++; else unchanged++;
  }
  const sorted = [...pcts].sort((a, b) => a - b), n = sorted.length;
  const warnings = [];
  if (bases.size > 1) warnings.push('mixed_reference_basis');
  if (sessions.size > 1) warnings.push('mixed_sessions');
  return { label: 'breadth within the curated sample', group: group ?? null, include_watchlist: includeWatchlist, weighting: 'equal (unweighted)',
    denominator: eligible.length, advancing, declining, unchanged, missing, stale_or_unknown_time: stale,
    mean_percent: n ? pcts.reduce((a, b) => a + b, 0) / n : null,
    median_percent: n ? (n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2) : null,
    reference_bases: [...bases].sort(), sessions: [...sessions].sort(), warnings };
}

const units = { price: 'instrument unit (see row.unit)', 'change.absolute': 'row.change.absolute_unit (USD only for USD-denominated rows)',
  'change.percent': 'percentage points', 'change.bps': 'basis points (yields only; no percent-of-yield)', age_seconds: 'seconds since provider observation' };
const warnings = ['An as-of collection of asynchronous observations, not a simultaneous market snapshot.',
  'Breadth covers the curated sample only, equal-weighted; it is not full-market breadth.',
  'Price moves do not establish a news cause.',
  'ETF proxies (SPY/QQQ/DIA/IWM, USO) keep their own identities; a proxy is never the index or commodity.'];

module.exports = { KEY, ASSET_CLASSES, SORTS, ID, validate, rowFreshness, breadth, units, warnings };
