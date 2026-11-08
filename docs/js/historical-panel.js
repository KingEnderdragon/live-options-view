// historical panel — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// `BROWSER_DATA` stays in the stale-page section; `parseCsv`, `TIER_COLORS`
// and `buildHeatmapRows` come from shared.js; `ranges` from the OIranges
// loader. `historyLoaded` is declared in the tab-switching module and
// assigned by loadHistoryManifest's failure path; `hideSimilarPopup` and
// `renderHistWidgets` are defined in the canvas-tooltip module — all
// runtime-only, after all scripts load.
// Callers: showTab('history') in the tab-switching module (runtime-only).
// Reads the same R2 bucket the live view polls, but pulls the settled daily
// chain archive (raw/qqq_chain_{date}.csv, or raw/opex/qqq_chain_{date}.csv
// for pre-pipeline seeded dates) instead of intraday/latest.json.
let histManifestDates = [];
let histRequest = 0;
const histCsvCache = {};   // date 'YYYY-MM-DD' -> normalized rows

function setHistStatus(text) {
  document.getElementById('hist-status').textContent = text;
}

// Tier classification from the expiration date alone (Friday = weekly,
// 3rd Friday of month = monthly opex) — avoids needing a full NYSE holiday
// calendar in the browser; matches classify_tier in the desktop viewer for
// the vast majority of dates (holiday-rollback edge cases excepted).
function classifyTierSimple(expDateStr) {
  const d = new Date(`${expDateStr}T00:00:00Z`);
  if (isNaN(d)) return '0DTE_Regular';
  if (d.getUTCDay() !== 5) return '0DTE_Regular';
  const dom = d.getUTCDate();
  return (dom >= 15 && dom <= 21) ? '0DTE_Monthly' : '0DTE_Weekly';
}

function normalizeHistRow(r) {
  return {
    ...r,
    Strike:          +r.Strike,
    OpenInterest:    +r.OpenInterest || 0,
    Volume:          +r.Volume || 0,
    Bid:             r.Bid  !== '' && r.Bid  != null ? +r.Bid  : null,
    Ask:             r.Ask  !== '' && r.Ask  != null ? +r.Ask  : null,
    UnderlyingPrice: +r.UnderlyingPrice,
    DTE:             +r.DTE,
  };
}

function validArchiveDate(value) {
  if (typeof value !== 'string' || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function resetHistoryView(message) {
  document.getElementById('hist-heatmap-wrap').classList.add('hidden');
  document.getElementById('hist-sidebar').classList.add('hidden');
  document.getElementById('hist-no-data').classList.remove('hidden');
  document.getElementById('hist-no-data').textContent = message;
  document.getElementById('hist-price').textContent = '—';
  document.getElementById('hist-tier-badge').style.display = 'none';
  hideSimilarPopup();
}

async function loadHistoryManifest() {
  ++histRequest;
  histManifestDates = [];
  const sel = document.getElementById('hist-date');
  sel.innerHTML = '';
  sel.disabled = true;
  resetHistoryView('Loading archive index…');
  setHistStatus('Loading archive index…');
  try {
    const resp = await fetch(`${BROWSER_DATA}/manifest.json?_=${Date.now()}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const m = await resp.json();
    if (!m || !Array.isArray(m.dates) || !m.dates.every(validArchiveDate)) {
      throw new Error('Invalid archive date index');
    }
    histManifestDates = [...new Set(m.dates)].sort();
    if (!histManifestDates.length) {
      setHistStatus('Archive is empty.');
      resetHistoryView('No archived dates are available.');
      return;
    }
    sel.disabled = false;
    sel.innerHTML = histManifestDates.map(d => `<option value="${d}">${d}</option>`).join('');
    const latest = histManifestDates[histManifestDates.length - 1];
    sel.value = latest;
    await loadHistoryDate(latest);
  } catch (e) {
    historyLoaded = false; // Reopening Historical retries a failed index.
    setHistStatus(`Failed to load archive index: ${e.message}`);
    document.getElementById('hist-no-data').textContent = 'Could not load historical archive.';
  }
}

// Fetch + cache one date's full chain CSV (all expirations for that trade date).
// Shared by the date-select flow and the sidebar widgets (intraday/daily/similarity),
// which need other dates' chains besides the one currently on screen.
async function fetchChainCsv(dateStr) {
  if (!validArchiveDate(dateStr)) throw new Error('Invalid archive date');
  if (histCsvCache[dateStr]) return histCsvCache[dateStr];
  const key = dateStr.replace(/-/g, '');
  let resp = await fetch(`${BROWSER_DATA}/raw/qqq_chain_${key}.csv?_=${Date.now()}`);
  if (resp.status === 404) resp = await fetch(`${BROWSER_DATA}/raw/opex/qqq_chain_${key}.csv?_=${Date.now()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const raw = parseCsv(await resp.text());
  // Name the first failing field so the panel says why a dated archive is unusable
  // (e.g. a file with blank OpenInterest throughout must not render as zero OI).
  const badField = r => !validArchiveDate(r.Expiration) ? 'Expiration'
    : !['call', 'put'].includes(r.Type) ? 'Type'
    : ['Strike', 'UnderlyingPrice', 'OpenInterest'].find(c => r[c] === '' || !Number.isFinite(+r[c])) ||
      (+r.Strike <= 0 ? 'Strike' : +r.UnderlyingPrice <= 0 ? 'UnderlyingPrice' : +r.OpenInterest < 0 ? 'OpenInterest' : null);
  const bad = raw.filter(badField);
  if (bad.length) {
    throw new Error(`Malformed chain archive: ${badField(bad[0])} invalid in ${bad.length} of ${raw.length} rows`);
  }
  if (!raw.length) throw new Error('Empty chain archive');
  const rows = raw.map(normalizeHistRow);
  histCsvCache[dateStr] = rows;
  return rows;
}

// Only the trade date's own same-day (0DTE) chain is shown — no expiration
// picker, no timeframe slider. Mirrors the desktop app's design (oi_viewer.py
// has no expiration selector either; its similarity index always builds its
// feature vector from the capture date's own same-day expiration). A chain
// CSV can contain other expirations' rows too, but those aren't surfaced
// here — extending to other timeframes is a deliberate future step, not an
// oversight.
async function loadHistoryDate(dateStr) {
  const request = ++histRequest;
  setHistStatus(`Loading ${dateStr}…`);
  resetHistoryView(`Loading ${dateStr}…`);
  try {
    const rows = await fetchChainCsv(dateStr);
    if (request !== histRequest) return;
    const sub = rows.filter(r => r.Expiration === dateStr);
    if (!sub.length) {
      setHistStatus(`No 0DTE chain found for ${dateStr}.`);
      document.getElementById('hist-no-data').textContent = `No 0DTE chain for ${dateStr}.`;
      return;
    }
    const spot = sub[0].UnderlyingPrice || null;
    const tier = classifyTierSimple(dateStr);
    const ok = renderHistoryHeatmap({ rows: sub, underlying_price: spot, tier, expiration: dateStr, snapshot_time: dateStr });
    if (ok) {
      document.getElementById('hist-sidebar').classList.remove('hidden');
      renderHistWidgets(sub, spot, tier, dateStr);
    }
  } catch (e) {
    if (request !== histRequest) return;
    setHistStatus(`Error loading ${dateStr}: ${e.message}`);
    document.getElementById('hist-no-data').textContent = e.message === 'HTTP 404'
      ? `No archived chain for ${dateStr}.`
      : /^(Malformed|Empty) chain archive/.test(e.message)
        ? `The archived chain for ${dateStr} is unusable and is not shown.`
        : `Could not load the archived chain for ${dateStr}.`;
  }
}

function renderHistoryHeatmap(data) {
  const { underlying_price: spot, tier, snapshot_time } = data;

  const ok = buildHeatmapRows(document.getElementById('hist-heatmap-body'), data, ranges);
  if (!ok) {
    setHistStatus(`No usable spot/strike data for ${snapshot_time}.`);
    document.getElementById('hist-heatmap-wrap').classList.add('hidden');
    document.getElementById('hist-no-data').classList.remove('hidden');
    return false;
  }

  document.getElementById('hist-price').textContent = spot ? `$${spot.toFixed(2)}` : '—';
  const tierBadge = document.getElementById('hist-tier-badge');
  tierBadge.textContent = (tier || '').replace('0DTE_', '');
  tierBadge.style.color = TIER_COLORS[tier] || '#c9d1d9';
  tierBadge.style.display = '';

  document.getElementById('hist-no-data').classList.add('hidden');
  document.getElementById('hist-heatmap-wrap').classList.remove('hidden');
  setHistStatus(`Trade date ${snapshot_time}`);
  return true;
}
