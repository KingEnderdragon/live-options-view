// csv-ranges.js — CSV parsing + OI-range thresholds, moved out of docs/shared.js
// (B3 slice 1, Nov 2026). Verbatim move: the code below is byte-identical to
// what lived in shared.js lines 712-765. Plain classic script (no module
// system, no build step), like the file it came from: the live page and
// diag.html load it with its own <script> tag (see docs/index.html, right
// after the shared.js tag), where it defines parseCsv / parseRangesCsv /
// getThresh / oiBucket as globals; under Node it exports the same four names
// (see the guard at the bottom). No load-time dependencies on any other
// shared concern — getThresh takes `ranges` explicitly, and the other three
// are self-contained. No ESM conversion in this step.

// ── CSV parser ────────────────────────────────────────────────────────────────
function parseCsv(text) {
  const lines = text.trim().split('\n');
  const headers = lines[0].split(',').map(h => h.trim());
  return lines.slice(1).map(line => {
    const vals = line.split(',');
    const obj = {};
    headers.forEach((h, i) => obj[h] = vals[i]?.trim() ?? '');
    return obj;
  });
}

// ── OIranges.csv -> { tier: { offset: { call:[p25,p50,p75,p90], put:[...] } } } ──
function parseRangesCsv(text) {
  const rows = parseCsv(text);
  const out = {};
  for (const r of rows) {
    const tier   = r.Tier;
    const offset = parseInt(r.StrikeOffset);
    if (!out[tier]) out[tier] = {};
    out[tier][offset] = {
      call: [+r.Call_p25, +r.Call_p50, +r.Call_p75, +r.Call_p90],
      put:  [+r.Put_p25,  +r.Put_p50,  +r.Put_p75,  +r.Put_p90],
      call_adj: +r.Call_adj || 1.0,
      put_adj:  +r.Put_adj  || 1.0,
    };
  }
  return out;
}

// ── threshold lookup (mirrors oi_viewer.py effective_thresholds) ──────────────
// `ranges` is passed explicitly (not a closed-over global) so this is callable
// from any page that has fetched+parsed its own OIranges.csv.
function getThresh(ranges, tier, offset, side) {
  if (!ranges) return [0, 0, 0, 0];
  const reg = ranges['0DTE_Regular']?.[offset];
  if (!reg) return [0, 0, 0, 0];
  const base = reg[side];
  if (tier === '0DTE_Regular') return base;
  const adj_key = side === 'call' ? 'call_adj' : 'put_adj';
  const t = ranges[tier]?.[offset];
  const mult = t ? (t[adj_key] || 1.0) : 1.0;
  return base.map(v => v * mult);
}

function oiBucket(oi, thresh) {
  if (oi === 0) return 0;
  const [p25, p50, p75, p90] = thresh;
  if (oi < p25) return 1;
  if (oi < p50) return 2;
  if (oi < p75) return 3;
  if (oi < p90) return 4;
  return 5;
}

if (typeof module !== 'undefined') {
  module.exports = { parseCsv, parseRangesCsv, getThresh, oiBucket };
}
