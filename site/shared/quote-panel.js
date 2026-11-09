// quote-panel.js — shared store, session model, movers ranking and pin state
// for the adaptive quote-card panel (#179). Plain classic script, loaded after
// shared/instruments.js and shared/quotes.js. No per-card transport lives here:
// one QuoteStore is fed by the page's single prices.json fetch and by the
// existing live-quote transport, and the DOM layer reads snapshots from it.

const QUOTE_PIN_STORAGE_KEY = 'optionsView.quotePins.v1';
const QUOTE_PIN_LIMIT = 12;
const QUOTE_STALE_MS = 5 * 60_000;
const QUOTE_MOVER_MAX_AGE_MS = 5 * 60_000;
const QUOTE_ACTIVE_SESSIONS = Object.freeze(['premarket', 'regular', 'afterhours', 'extended', 'overnight', 'continuous']);
const QUOTE_SESSION_LABELS = Object.freeze({
  premarket: 'Premarket', regular: 'Regular', afterhours: 'After hours', extended: 'Extended',
  overnight: 'Overnight', maintenance: 'Maintenance break', closed: 'Closed',
  holiday: 'Holiday', continuous: 'Crypto · 24/7', daily: 'Daily', unknown: 'Session unknown',
});
const QUOTE_MODE_LABELS = Object.freeze({
  equities: 'US equities · extended hours', futures: 'Futures · overnight',
  crypto: 'Crypto · weekend', fx: 'Foreign exchange', none: 'Markets closed',
  unknown: 'Session unknown',
});
const QUOTE_MOVER_GROUPS = Object.freeze({
  equities: { classes: ['equity', 'etf'], label: 'US equities' },
  futures: { classes: ['commodity'], label: 'Futures' },
  crypto: { classes: ['crypto'], label: 'Crypto' },
  fx: { classes: ['fx'], label: 'FX' },
});

const quoteNumber = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const quoteLabel = instrument => instrument?.label ?? null;

// One row = one instrument at one publication. Missing stays missing; a
// retained value keeps its original observation time and is flagged stale.
function normalizeQuoteRow(label, row, receivedAt, nowMs) {
  const instrument = QUOTE_INSTRUMENTS[label] || { id: null, label, asset_class: null };
  const price = quoteNumber(row?.price);
  const observedAt = typeof row?.observed_at === 'string' ? row.observed_at : null;
  const receiptAt = row?.collector_received_at || row?.quote_ts || null;
  const observedMs = observedAt ? Date.parse(observedAt) : null;
  const receiptMs = receiptAt ? Date.parse(receiptAt) : null;
  const source = row?.source ?? null;
  const stale = row?.stale === true || source === 'last-known';
  const daily = row?.observation_granularity === 'day' || row?.observation_date != null;
  const missing = price === null ||
    (price <= 0 && instrument.asset_class !== 'commodity' && instrument.asset_class !== 'rates');
  const session = row?.market_session ?? row?.session ?? null;
  const changePct = instrument.asset_class === 'crypto' && quoteNumber(row?.rolling_change_pct) !== null
    ? quoteNumber(row.rolling_change_pct)
    : quoteNumber(row?.chg_pct);
  const changeBasis = row?.change_basis
    || (instrument.asset_class === 'crypto'
      ? (row?.rolling_change_pct != null ? 'rolling_24h' : 'unavailable')
      : 'previous_close_unverified');
  return decorateQuoteRow({
    label, id: instrument.id ?? row?.instrument_id ?? null,
    name: instrument.name ?? label, asset_class: instrument.asset_class ?? null,
    unit: instrument.unit ?? null, profile: instrument.profile ?? null,
    price, prev_close: quoteNumber(row?.prev_close),
    changePct, changeBasis, session, daily, stale, missing,
    source, observedAt, observedMs, receiptAt, receiptMs,
    reference: row?.reference ?? null,
    rollingReference: row?.rolling_reference ?? null,
    resolvedContract: row?.resolved_contract ?? null,
    refreshError: row?.refresh_error ?? null,
    raw: row || {}, receivedAt,
  }, nowMs);
}

// Age and freshness are a function of the current clock, not of ingestion.
// Snapshots re-decorate retained rows so an outage ages cards and removes them
// from movers instead of freezing a one-hour-old quote as live.
function decorateQuoteRow(row, nowMs) {
  const ageMs = row.observedMs !== null ? nowMs - row.observedMs
    : row.receiptMs !== null ? nowMs - row.receiptMs : null;
  let state;
  if (row.missing) state = 'missing';
  else if (row.stale || row.refreshError) state = 'stale';
  else if (row.daily) state = 'daily';
  else if (row.session && !QUOTE_ACTIVE_SESSIONS.includes(row.session)) {
    state = row.session === 'maintenance' ? 'maintenance' : 'closed';
  } else if (!row.session) state = 'unknown';
  else if (ageMs !== null && (ageMs < -30_000 || ageMs > QUOTE_STALE_MS)) state = 'stale';
  else state = 'live';
  return { ...row, ageMs, state };
}

class QuoteStore {
  constructor({ nowFn = Date.now } = {}) {
    this.nowFn = nowFn;
    this.rows = new Map();
    this.meta = { timestamp: null, feedStale: null, lane: null };
    this.listeners = new Set();
  }

  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }

  mergePayload(payload) {
    const prices = payload && typeof payload.prices === 'object' && payload.prices ? payload.prices : null;
    if (!prices) return false;
    const receivedAt = typeof payload.timestamp === 'string' ? payload.timestamp : null;
    const nowMs = this.nowFn();
    let changed = false;
    for (const [label, row] of Object.entries(prices)) {
      if (!QUOTE_INSTRUMENTS[label]) continue;
      this.rows.set(label, normalizeQuoteRow(label, row, receivedAt, nowMs));
      changed = true;
    }
    this.meta = {
      timestamp: receivedAt,
      feedStale: payload.feed_stale === true,
      lane: payload.price_lane && typeof payload.price_lane === 'object' ? payload.price_lane : null,
    };
    if (changed) this.listeners.forEach(listener => listener(this.snapshot()));
    return changed;
  }

  // Only a ticker carrying a provider event time may touch the shared row.
  // LiveQuoteRegistry's quote_ts is a receipt time and may pair an old last
  // trade with a newly received bid/ask; promoting it would fake freshness.
  mergeTickers(quotes) {
    let changed = false;
    const nowMs = this.nowFn();
    for (const quote of quotes || []) {
      const label = quote?.symbol;
      const current = this.rows.get(label);
      if (!current || quoteNumber(quote.price) === null) continue;
      if (!['dxlink', 'dxlink-live'].includes(quote.source)) continue;
      if (typeof quote.observed_at !== 'string') continue;
      const nextMs = Date.parse(quote.observed_at);
      if (!Number.isFinite(nextMs)) continue;
      if (current.observedMs !== null && nextMs < current.observedMs) continue;
      const updated = normalizeQuoteRow(label, {
        ...current.raw,
        price: quote.price,
        source: quote.source || current.source,
        prev_close: quote.prev_close ?? current.prev_close,
        chg_pct: quote.chg_pct ?? current.raw.chg_pct,
        observed_at: quote.observed_at,
      }, current.receivedAt, nowMs);
      this.rows.set(label, updated);
      changed = true;
    }
    if (changed) this.listeners.forEach(listener => listener(this.snapshot()));
    return changed;
  }

  row(label) {
    const stored = this.rows.get(label);
    return stored ? decorateQuoteRow(stored, this.nowFn()) : null;
  }

  snapshot() {
    const nowMs = this.nowFn();
    const rows = [...this.rows.values()].map(row => decorateQuoteRow(row, nowMs));
    return {
      rows,
      byLabel: new Map(rows.map(row => [row.label, row])),
      meta: this.meta,
      nowMs,
    };
  }
}

function quoteSessionMode(snapshot) {
  const { rows, meta, nowMs } = snapshot;
  const laneMode = meta?.lane?.mode;
  if (laneMode && QUOTE_MODE_LABELS[laneMode]) {
    return { mode: laneMode, label: QUOTE_MODE_LABELS[laneMode], source: 'producer' };
  }
  const sessions = new Set(rows.map(row => row.session).filter(Boolean));
  let mode = 'unknown';
  if (['premarket', 'regular', 'afterhours'].some(s => sessions.has(s))) mode = 'equities';
  else if (['overnight', 'maintenance'].some(s => sessions.has(s))) mode = 'futures';
  else if (sessions.has('continuous')) mode = 'crypto';
  else if (sessions.has('daily') || sessions.has('closed') || sessions.has('holiday')) mode = 'none';
  else mode = clockFallbackMode(nowMs);
  return { mode, label: QUOTE_MODE_LABELS[mode], source: 'rows_or_clock' };
}

function clockFallbackMode(nowMs) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(nowMs)).filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  const day = parts.weekday;
  if (day === 'Sat') return 'crypto';
  if (day === 'Sun') return minutes >= 18 * 60 ? 'futures' : 'crypto';
  if (minutes >= 4 * 60 && minutes < 20 * 60) return 'equities';
  if (day === 'Fri' && minutes >= 17 * 60) return 'crypto';
  return 'futures';
}

function quoteReferenceUsable(row) {
  if (row.asset_class === 'crypto') return quoteNumber(row.rollingReference?.value) > 0;
  if (row.profile === 'FU') return row.changeBasis === 'prior_day_close_same_contract';
  return quoteNumber(row.prev_close) > 0;
}

function computeMovers(snapshot, { mode = null, count = 3, maxAgeMs = QUOTE_MOVER_MAX_AGE_MS } = {}) {
  const group = mode ? QUOTE_MOVER_GROUPS[mode] : null;
  if (!group) return { up: [], down: [], eligible: 0, group: mode || 'none', label: null };
  const eligible = snapshot.rows.filter(row => {
    if (!row.asset_class || !group.classes.includes(row.asset_class)) return false;
    if (row.price === null || quoteNumber(row.changePct) === null) return false;
    if (row.stale || row.refreshError) return false;
    if (!quoteReferenceUsable(row)) return false;
    if (row.session && !QUOTE_ACTIVE_SESSIONS.includes(row.session)) return false;
    if (row.ageMs !== null && (row.ageMs < -30_000 || row.ageMs > maxAgeMs)) return false;
    return true;
  });
  const ordered = [...eligible].sort((a, b) =>
    (b.changePct - a.changePct) || a.label.localeCompare(b.label));
  const up = ordered.filter(row => row.changePct > 0).slice(0, count);
  const down = ordered.filter(row => row.changePct < 0).sort((a, b) =>
    (a.changePct - b.changePct) || a.label.localeCompare(b.label)).slice(0, count);
  return { up, down, eligible: eligible.length, group: mode, label: group.label };
}

function sanitizePins(list, { registry = QUOTE_INSTRUMENTS, core = QUOTE_CORE_LABELS, limit = QUOTE_PIN_LIMIT } = {}) {
  const seen = new Set();
  const kept = [];
  const dropped = [];
  for (const value of Array.isArray(list) ? list : []) {
    const label = typeof value === 'string' ? value : null;
    if (!label || seen.has(label) || !Object.hasOwn(registry, label) || core.includes(label)) {
      if (label) dropped.push(label);
      continue;
    }
    seen.add(label);
    kept.push(label);
    if (kept.length >= limit) break;
  }
  return { pins: kept, dropped };
}

class QuotePins {
  constructor({ storage = null, key = QUOTE_PIN_STORAGE_KEY, limit = QUOTE_PIN_LIMIT,
    defaults = QUOTE_DEFAULT_PINS, registry = QUOTE_INSTRUMENTS, core = QUOTE_CORE_LABELS } = {}) {
    this.storage = storage;
    this.key = key;
    this.limit = limit;
    this.registry = registry;
    this.core = core;
    this.persistent = Boolean(storage);
    this.dropped = [];
    this.pins = [];
    this._load(defaults);
  }

  _load(defaults) {
    let raw = null;
    try { raw = this.storage ? this.storage.getItem(this.key) : null; } catch (e) { this.persistent = false; }
    if (raw === null || raw === undefined) {
      this.pins = sanitizePins(defaults, this).pins;
      return;
    }
    let parsed = null;
    try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
    if (!Array.isArray(parsed)) {
      this.pins = sanitizePins(defaults, this).pins;
      return;
    }
    const sanitized = sanitizePins(parsed, this);
    this.pins = sanitized.pins;
    this.dropped = sanitized.dropped;
  }

  list() { return [...this.pins]; }
  has(label) { return this.pins.includes(label); }
  remaining() { return Math.max(0, this.limit - this.pins.length); }

  add(label) {
    if (!Object.hasOwn(this.registry, label)) return { ok: false, reason: 'unknown_instrument' };
    if (this.core.includes(label)) return { ok: false, reason: 'core_instrument' };
    if (this.pins.includes(label)) return { ok: false, reason: 'already_pinned' };
    if (this.pins.length >= this.limit) return { ok: false, reason: 'pin_limit' };
    this.pins.push(label);
    this._save();
    return { ok: true, pins: this.list() };
  }

  remove(label) {
    const index = this.pins.indexOf(label);
    if (index < 0) return { ok: false, reason: 'not_pinned' };
    this.pins.splice(index, 1);
    this._save();
    return { ok: true, pins: this.list() };
  }

  move(label, delta) {
    const index = this.pins.indexOf(label);
    const target = index + Number(delta);
    if (index < 0) return { ok: false, reason: 'not_pinned' };
    if (!Number.isInteger(target) || target < 0 || target >= this.pins.length) {
      return { ok: false, reason: 'edge' };
    }
    const [value] = this.pins.splice(index, 1);
    this.pins.splice(target, 0, value);
    this._save();
    return { ok: true, pins: this.list() };
  }

  _save() {
    if (!this.storage) return false;
    try {
      this.storage.setItem(this.key, JSON.stringify(this.pins));
      this.persistent = true;
      return true;
    } catch (e) {
      this.persistent = false;
      return false;
    }
  }
}

function quoteCandidates(snapshot, pins, query = '') {
  const needle = String(query || '').trim().toLowerCase();
  return snapshot.rows
    .filter(row => !QUOTE_CORE_LABELS.includes(row.label) && !pins.has(row.label))
    .filter(row => !needle || row.label.toLowerCase().includes(needle) || row.name.toLowerCase().includes(needle))
    .sort((a, b) => a.label.localeCompare(b.label));
}

function formatQuotePercent(value) {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function formatQuoteAge(ageMs) {
  if (ageMs === null || !Number.isFinite(ageMs)) return 'time unknown';
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

function formatQuotePrice(row) {
  if (!row || row.price === null) return '—';
  const value = row.price;
  if (row.label === 'JPY/USD') return value > 0 ? `¥${(1 / value).toFixed(2)}` : '—';
  if (row.label === 'BTC/USD') return value > 1000 ? value.toLocaleString('en-US', { maximumFractionDigits: 0 }) : value.toFixed(2);
  if (row.asset_class === 'fx') return value.toFixed(4);
  if (row.asset_class === 'rates' || row.unit === 'percent') {
    const normalized = value > 20 ? value / 10 : value;
    return `${normalized.toFixed(3)}%`;
  }
  return value.toFixed(2);
}

if (typeof module !== 'undefined') {
  module.exports = {
    QUOTE_PIN_STORAGE_KEY, QUOTE_PIN_LIMIT, QUOTE_STALE_MS, QUOTE_MOVER_MAX_AGE_MS,
    QUOTE_SESSION_LABELS, QUOTE_MODE_LABELS, QUOTE_MOVER_GROUPS,
    QuoteStore, QuotePins, normalizeQuoteRow, decorateQuoteRow, quoteSessionMode, computeMovers,
    sanitizePins, quoteCandidates, quoteReferenceUsable, clockFallbackMode,
    formatQuotePercent, formatQuoteAge, formatQuotePrice,
  };
}
