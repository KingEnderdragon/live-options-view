// sidebar widgets — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// `validArchiveDate`, `histManifestDates`, `fetchChainCsv` and
// `classifyTierSimple` stay declared in the historical panel (the page's
// shared scope); `BROWSER_DATA` stays in the stale-page section; `parseCsv`,
// `getThresh`/`oiBucket` and `ranges` come from shared.js / the OIranges
// loader. Callers live in the canvas-tooltip section (`renderHistWidgets`,
// via `loadDailyWindow` / `queryTopSimilar` / `nextDayMove`) — all
// runtime-only, after all scripts load.
// ── sidebar widgets: Top 5 OI, Intraday, Daily context, Similar Sessions ──────
// Ported from the desktop app's oi_viewer.py sidebar (render_top5, render_intraday,
// render_daily_context, SimilarityIndex). Each widget fetches/derives its data from
// R2 keys qqq-data-pipeline already writes — no new pipeline output required.
const intradayCache = {};   // dateStr -> [{t,o,h,l,c}] 5-min bars

async function loadIntradayPrices(dateStr) {
  if (!validArchiveDate(dateStr)) throw new Error('Invalid archive date');
  if (intradayCache[dateStr]) return intradayCache[dateStr];
  const key = dateStr.replace(/-/g, '');
  const resp = await fetch(`${BROWSER_DATA}/prices/qqq_intraday_${key}.csv?_=${Date.now()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const rows = parseCsv(await resp.text());
  if (!rows.length || rows.some(r => !r.datetime ||
      ![r.open, r.high, r.low, r.close].every(v => v !== '' && Number.isFinite(+v)))) {
    throw new Error('Malformed or empty intraday archive');
  }
  const bars = rows
    .map(r => ({ t: r.datetime, o: +r.open, h: +r.high, l: +r.low, c: +r.close }))
    .filter(b => [b.o, b.h, b.l, b.c].every(Number.isFinite));
  intradayCache[dateStr] = bars;
  return bars;
}

function aggregateDailyOHLC(bars) {
  if (!bars || !bars.length) return null;
  return {
    o: bars[0].o,
    h: Math.max(...bars.map(b => b.h)),
    l: Math.min(...bars.map(b => b.l)),
    c: bars[bars.length - 1].c,
  };
}

// ±window trading days around centerDateStr, using the manifest's date list (not
// calendar days) so weekends/holidays are naturally skipped — mirrors load_daily_window
// in oi_viewer.py but keyed off dates we know have archives rather than a yfinance range.
async function loadDailyWindow(centerDateStr, window = 2) {
  const idx = histManifestDates.indexOf(centerDateStr);
  if (idx === -1) return { candles: [], centerIndex: -1 };
  const lo = Math.max(0, idx - window);
  const hi = Math.min(histManifestDates.length - 1, idx + window);
  const windowDates = histManifestDates.slice(lo, hi + 1);
  const candles = [];
  for (const d of windowDates) {
    try {
      const agg = aggregateDailyOHLC(await loadIntradayPrices(d));
      if (agg) candles.push({ ...agg, label: d.slice(5).replace('-', '/'), date: d });
    } catch (e) { /* no intraday archive for this day — skip it */ }
  }
  return { candles, centerIndex: candles.findIndex(c => c.date === centerDateStr) };
}

// Port of build_feature_vector (oi_viewer.py:313-334): 46-element vector, bucket
// values for offsets +11..-11, calls then puts. Reuses the same getThresh()/oiBucket()
// the live heatmap already uses for coloring — no separate threshold logic.
function buildFeatureVector(rows, tier) {
  if (!rows || !rows.length) return null;
  const spot = rows[0].UnderlyingPrice;
  if (!spot) return null;
  const strikes = [...new Set(rows.map(r => r.Strike))].sort((a, b) => a - b);
  if (!strikes.length) return null;
  const atm = strikes.reduce((best, s) => Math.abs(s - spot) < Math.abs(best - spot) ? s : best, strikes[0]);
  const byStrike = {};
  for (const r of rows) {
    if (!byStrike[r.Strike]) byStrike[r.Strike] = {};
    byStrike[r.Strike][r.Type] = r;
  }
  const callBuckets = [], putBuckets = [];
  for (let o = 11; o >= -11; o--) {
    const sk = atm + o;
    const coi = byStrike[sk]?.call ? (+byStrike[sk].call.OpenInterest || 0) : 0;
    const poi = byStrike[sk]?.put  ? (+byStrike[sk].put.OpenInterest  || 0) : 0;
    callBuckets.push(oiBucket(coi, getThresh(ranges, tier || '0DTE_Regular', o, 'call')));
    putBuckets.push(oiBucket(poi, getThresh(ranges, tier || '0DTE_Regular', o, 'put')));
  }
  return callBuckets.concat(putBuckets);
}

// Vectors are cached forever in localStorage — a settled historical date's OI never
// changes, so once built a vector is valid indefinitely (mirrors the desktop's
// similarity_cache.json). Grown lazily/opportunistically rather than bulk-bootstrapped,
// so opening the Historical tab doesn't trigger fetching every archived date at once.
const SIM_KEY = 'sim_vectors_v1';
let simVectors = {};
try { simVectors = JSON.parse(localStorage.getItem(SIM_KEY)) || {}; } catch (e) { simVectors = {}; }
function saveSimVectors() { try { localStorage.setItem(SIM_KEY, JSON.stringify(simVectors)); } catch (e) {} }

async function ensureVector(dateStr) {
  if (simVectors[dateStr]) return simVectors[dateStr];
  const rows = await fetchChainCsv(dateStr);
  const sub = rows.filter(r => r.Expiration === dateStr);   // that day's own 0DTE chain
  if (!sub.length) return null;
  const vec = buildFeatureVector(sub, classifyTierSimple(dateStr));
  if (vec) { simVectors[dateStr] = vec; saveSimVectors(); }
  return vec;
}

function cosineSim(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  return (na === 0 || nb === 0) ? 0 : dot / (Math.sqrt(na) * Math.sqrt(nb));
}

async function queryTopSimilar(dateStr, n = 5) {
  const target = await ensureVector(dateStr);
  if (!target) return [];

  // Grow the corpus toward nearby uncached dates. Awaited (not fire-and-forget) so a
  // cold cache — e.g. the very first Historical-tab visit — still returns real matches
  // instead of an empty list; scoped to a handful of dates so it stays fast.
  const idx = histManifestDates.indexOf(dateStr);
  if (idx !== -1) {
    const toFetch = [];
    for (let d = 1; d <= 6; d++) {
      for (const j of [idx - d, idx + d]) {
        const cd = histManifestDates[j];
        if (cd && !simVectors[cd]) toFetch.push(ensureVector(cd).catch(() => {}));
      }
    }
    await Promise.all(toFetch);
  }

  return Object.entries(simVectors)
    .filter(([d]) => d !== dateStr)
    .map(([d, v]) => ({ date: d, score: cosineSim(target, v) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, n);
}

async function nextDayMove(dateStr) {
  const idx = histManifestDates.indexOf(dateStr);
  if (idx === -1 || idx === histManifestDates.length - 1) return null;
  const nextDate = histManifestDates[idx + 1];
  const [curRows, nextRows] = await Promise.all([fetchChainCsv(dateStr), fetchChainCsv(nextDate)]);
  const curSpot  = curRows.find(r => r.Expiration === dateStr)?.UnderlyingPrice;
  const nextSpot = nextRows[0]?.UnderlyingPrice;
  if (!curSpot || !nextSpot) return null;
  return (nextSpot - curSpot) / curSpot * 100;
}
