// OIranges loader — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── OIranges loader ───────────────────────────────────────────────────────────
// parseRangesCsv / parseCsv / getThresh / oiBucket / buildHeatmapRows / DISPLAY /
// TIER_COLORS / fmt*/quoteHtml/flowHtml live in shared/heatmap.js (also used by diag.html).
async function loadRanges() {
  try {
    const resp = await fetch(`${BROWSER_DATA}/derived/OIranges.csv`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    ranges = parseRangesCsv(await resp.text());
    document.getElementById('cal-error').style.display = 'none';
  } catch (e) {
    console.warn('OIranges load failed:', e);
    // No periodic timer of its own (unlike the heatmap/prices/health fetches) —
    // a single transient failure here used to mean the calibration banner (and
    // "wall" coloring for every nonzero cell) stayed broken for the rest of the
    // session. Retried from the visibility handler below and a slow safety-net
    // interval so a one-off blip self-heals instead of needing a manual reload.
    document.getElementById('cal-error').style.display = 'block';
  }
}
