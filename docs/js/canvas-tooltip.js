// canvas hover/touch tooltip — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// `fmtOI` comes from shared.js; `histRequest` from the historical panel;
// `loadIntradayPrices`, `loadDailyWindow`, `queryTopSimilar` and `nextDayMove`
// from the sidebar-widgets module — all runtime-only, after all scripts load.
// Callers: `renderHistWidgets` is called by the historical panel's
// loadHistoryDate; `hideSimilarPopup` by loadHistoryManifest's failure path.
// ── canvas hover/touch tooltip ────────────────────────────────────────────────
// Shared by all three canvas widgets (Top 5, Intraday, Daily). Pointer Events
// unify mouse and touch into one code path: for a mouse, pointermove fires on
// any hover; for touch, it only fires while a finger is down and dragging
// (touch has no hover concept), so the same handler gives desktop hover and
// mobile touch-and-drag ("scrub the chart") for free. A tap alone (no drag)
// is covered by also handling pointerdown. `hitTest(x, y)` receives
// canvas-pixel coordinates and returns tooltip HTML for whatever's under that
// point, or null/empty to hide it.
//
// Each widget's canvas is re-rendered (new bars/positions) every time the
// selected date changes, but it's the same <canvas> element throughout — so
// listeners are bound to it exactly once (guarded by _tooltipBound) and every
// later call just swaps in a fresh hitTest closure via _hitTest, rather than
// accumulating a new set of listeners (and a new floating tooltip div) on
// every render.
function attachCanvasTooltip(canvas, hitTest) {
  canvas._hitTest = hitTest;
  if (canvas._tooltipBound) return;
  canvas._tooltipBound = true;

  let tip = null;
  function ensureTip() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'canvas-tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }
  function show(clientX, clientY, html) {
    const t = ensureTip();
    t.innerHTML = html;
    t.style.display = 'block';
    const pad = 14;
    const maxLeft = window.scrollX + document.documentElement.clientWidth - t.offsetWidth - 8;
    t.style.left = Math.min(clientX + window.scrollX + pad, maxLeft) + 'px';
    // Above the pointer/finger, not below — on touch, the finger itself sits
    // right at (and covers) the point below it, so anchoring downward hides
    // the tooltip under the very thing pointing at it. Falls back to below
    // only if there isn't room above (near the top of the viewport).
    const above = clientY + window.scrollY - t.offsetHeight - pad;
    t.style.top = (above < window.scrollY + 4 ? clientY + window.scrollY + pad : above) + 'px';
  }
  function hide() { if (tip) tip.style.display = 'none'; }

  function onPointer(e) {
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const y = (e.clientY - rect.top) * (canvas.height / rect.height);
    const html = canvas._hitTest ? canvas._hitTest(x, y) : null;
    if (html) show(e.clientX, e.clientY, html);
    else hide();
  }
  canvas.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'touch') e.preventDefault();   // don't let the tap scroll/zoom the page
    onPointer(e);
  });
  canvas.addEventListener('pointermove', onPointer);
  canvas.addEventListener('pointerup', hide);
  canvas.addEventListener('pointercancel', hide);
  canvas.addEventListener('pointerleave', hide);
}

// datetime string ("2026-06-25T09:30:00") -> "9:30"; passes through anything
// that isn't a recognizable timestamp (e.g. already just a label).
function fmtBarTime(t) {
  if (!t) return '';
  const m = String(t).match(/T?(\d{1,2}):(\d{2})/);
  return m ? `${+m[1]}:${m[2]}` : String(t);
}

function candleTooltipHtml(b) {
  const label = b.label || fmtBarTime(b.t);
  return `<div class="tt-title">${label}</div>` +
    `O <b>${b.o.toFixed(2)}</b>&nbsp;&nbsp;H <b>${b.h.toFixed(2)}</b><br>` +
    `L <b>${b.l.toFixed(2)}</b>&nbsp;&nbsp;C <b>${b.c.toFixed(2)}</b>`;
}

// Shared candle renderer for both the Intraday and Daily-context widgets (the desktop
// app duplicates this drawing logic across render_intraday/render_daily_context —
// collapsed into one function here instead of porting the duplication).
function renderCandles(canvas, bars, opts = {}) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);
  if (!bars || !bars.length) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('no data', W / 2, H / 2);
    canvas._hitTest = null;   // clear any tooltip data from a previous render with data
    return;
  }
  const n = bars.length;
  const labelH = opts.labels ? 11 : 0;
  const lo0 = Math.min(...bars.map(b => b.l));
  const hi0 = Math.max(...bars.map(b => b.h));
  const pad = (hi0 - lo0) * 0.08 || 1;
  const lo = lo0 - pad, hi = hi0 + pad;
  const padX = 3, padY = 3;
  const plotW = W - padX * 2, plotH = H - padY * 2 - labelH;
  const slot = plotW / n;
  const cw = Math.max(1, slot * 0.6);
  const y = v => padY + (hi - v) / (hi - lo) * plotH;

  bars.forEach((b, i) => {
    const cx = padX + slot * i + slot / 2;
    const color = b.c >= b.o ? '#00cc55' : '#ee3300';
    ctx.globalAlpha = (opts.highlightIndex != null && i !== opts.highlightIndex) ? 0.4 : 1;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, y(b.h));
    ctx.lineTo(cx, y(b.l));
    ctx.stroke();
    const top = y(Math.max(b.o, b.c));
    const bh = Math.max(1, Math.abs(y(b.o) - y(b.c)));
    ctx.fillRect(cx - cw / 2, top, cw, bh);
    ctx.globalAlpha = 1;
    if (i === opts.highlightIndex) {
      ctx.strokeStyle = '#cccccc';
      ctx.strokeRect(cx - cw / 2 - 1, top - 1, cw + 2, bh + 2);
    }
  });

  if (opts.labels) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '9px sans-serif';
    ctx.textAlign = 'center';
    [0, Math.floor((n - 1) / 2), n - 1].forEach(i => {
      if (bars[i]?.label) ctx.fillText(bars[i].label, padX + slot * i + slot / 2, H - 1);
    });
  }

  attachCanvasTooltip(canvas, (x, y) => {
    if (x < padX || x > W - padX) return null;
    const idx = Math.max(0, Math.min(n - 1, Math.round((x - padX - slot / 2) / slot)));
    return candleTooltipHtml(bars[idx]);
  });
}

// Port of render_top5 (oi_viewer.py:518-603): bubble scatter of the 5 highest-OI
// strikes, positioned by % distance from spot, sized by OI, with a rank number and a
// simple collision bump for overlapping x positions.
function renderTop5Widget(rows, spot) {
  const canvas = document.getElementById('widget-top5');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);
  if (!rows || !rows.length || !spot) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('no data', W / 2, H / 2);
    canvas._hitTest = null;   // clear any tooltip data from a previous render with data
    return;
  }

  const top5 = rows.slice().sort((a, b) => b.OpenInterest - a.OpenInterest).slice(0, 5);
  const pts = top5.map((r, i) => ({
    rank: i + 1, strike: r.Strike, type: r.Type, oi: r.OpenInterest,
    pct: (r.Strike - spot) / spot * 100,
  }));
  const maxAbs = Math.max(6, Math.ceil(Math.max(...pts.map(p => Math.abs(p.pct)))));
  const padX = 14, padY = 16;
  const plotW = W - padX * 2, plotH = H - padY * 2;
  const x = pct => padX + (pct + maxAbs) / (2 * maxAbs) * plotW;

  ctx.strokeStyle = '#30363d';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padX, H - padY); ctx.lineTo(W - padX, H - padY); ctx.stroke();
  ctx.strokeStyle = '#6e7681';
  ctx.setLineDash([2, 2]);
  ctx.beginPath(); ctx.moveTo(x(0), padY / 2); ctx.lineTo(x(0), H - padY); ctx.stroke();
  ctx.setLineDash([]);

  const collThr = maxAbs * 0.22;
  const placed = [];
  for (const p of pts.slice().sort((a, b) => a.pct - b.pct)) {
    let bump = 0;
    while (placed.some(q => Math.abs(q.pct - p.pct) < collThr && q.bump === bump)) bump++;
    placed.push({ ...p, bump });
  }
  const maxBump = Math.max(...placed.map(p => p.bump));
  const rowGap = maxBump > 0 ? (plotH - 20) / (maxBump + 1) : 0;

  const oiVals = placed.map(p => p.oi);
  const minOi = Math.min(...oiVals), maxOi = Math.max(...oiVals);
  const rMin = 6, rMax = 13;
  const radius = oi => maxOi === minOi ? (rMin + rMax) / 2 : rMin + (oi - minOi) / (maxOi - minOi) * (rMax - rMin);

  placed.forEach(p => {
    const cx = x(p.pct);
    const cy = H - padY - 8 - p.bump * rowGap;
    const color = p.type === 'call' ? '#00cc55' : '#ee3300';
    const r = radius(p.oi);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = color + '33';
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = 'bold 9px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(p.rank), cx, cy);
  });

  ctx.fillStyle = '#6e7681';
  ctx.font = '8px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  [-maxAbs, 0, maxAbs].forEach(v => ctx.fillText(`${v > 0 ? '+' : ''}${v}%`, x(v), H - 4));

  attachCanvasTooltip(canvas, (mx, my) => {
    // +6px tolerance beyond the drawn radius so touch taps (less precise
    // than a mouse pointer) still land on a bubble.
    const hit = placed.find(p =>
      Math.hypot(mx - x(p.pct), my - (H - padY - 8 - p.bump * rowGap)) <= radius(p.oi) + 6);
    if (!hit) return null;
    return `<div class="tt-title">Strike ${hit.strike} · ${hit.type.toUpperCase()}</div>OI <b>${fmtOI(hit.oi)}</b>`;
  });
}

async function renderIntradayWidget(dateStr) {
  const request = histRequest;
  document.getElementById('widget-intraday-label').textContent = dateStr;
  const canvas = document.getElementById('widget-intraday');
  renderCandles(canvas, []);
  try {
    const bars = await loadIntradayPrices(dateStr);
    if (request !== histRequest) return;
    renderCandles(canvas, bars);
  } catch (e) {
    if (request !== histRequest) return;
    renderCandles(canvas, []);
  }
}

async function renderDailyWidget(dateStr) {
  const request = histRequest;
  const canvas = document.getElementById('widget-daily');
  renderCandles(canvas, []);
  try {
    const { candles, centerIndex } = await loadDailyWindow(dateStr, 2);
    if (request !== histRequest) return;
    renderCandles(canvas, candles, { highlightIndex: centerIndex >= 0 ? centerIndex : null, labels: true });
  } catch (e) {
    if (request !== histRequest) return;
    renderCandles(canvas, []);
  }
}

let simPopupEl = null;
function hideSimilarPopup() {
  if (simPopupEl) { simPopupEl.remove(); simPopupEl = null; }
}
async function showSimilarPopup(e, dateStr) {
  hideSimilarPopup();
  const div = document.createElement('div');
  div.id = 'similar-popup';
  div.innerHTML = '<canvas width="180" height="70"></canvas>';
  document.body.appendChild(div);
  const r = e.currentTarget.getBoundingClientRect();
  div.style.left = (r.right + window.scrollX + 6) + 'px';
  div.style.top  = (r.top + window.scrollY) + 'px';
  simPopupEl = div;
  try {
    const bars = await loadIntradayPrices(dateStr);
    if (simPopupEl === div) renderCandles(div.querySelector('canvas'), bars);
  } catch (e2) {
    if (simPopupEl === div) renderCandles(div.querySelector('canvas'), []);
  }
}

// Port of the Similar Sessions panel (oi_viewer.py SimilarityIndex + _sim_popup):
// top-5 historically similar 0DTE sessions by cosine similarity of the bucketed OI
// shape, each with its next-day % move and a hover popup of that day's intraday chart.
async function renderSimilarWidget(dateStr) {
  const request = histRequest;
  const listEl = document.getElementById('similar-list');
  listEl.innerHTML = '<div class="widget-empty">Loading…</div>';
  try {
    const matches = await queryTopSimilar(dateStr, 5);
    if (request !== histRequest) return;
    if (!matches.length) {
      listEl.innerHTML = '<div class="widget-empty">Not enough historical sessions cached yet.</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const m of matches) {
      const row = document.createElement('div');
      row.className = 'similar-row';
      row.innerHTML = `<span class="similar-date">${m.date}</span><span class="similar-score">${(m.score * 100).toFixed(0)}%</span><span class="similar-move">…</span>`;
      row.onmouseenter = (e) => showSimilarPopup(e, m.date);
      row.onmouseleave = hideSimilarPopup;
      listEl.appendChild(row);
      const moveEl = row.querySelector('.similar-move');
      nextDayMove(m.date).then(mv => {
        if (mv == null) { moveEl.textContent = '—'; return; }
        moveEl.textContent = `${mv >= 0 ? '+' : ''}${mv.toFixed(2)}%`;
        moveEl.classList.add(mv >= 0 ? 'up' : 'down');
      }).catch(() => { moveEl.textContent = '—'; });
    }
  } catch (e) {
    if (request !== histRequest) return;
    listEl.innerHTML = '<div class="widget-empty">Similarity unavailable.</div>';
  }
}

// Each widget degrades independently — a missing intraday archive for an old date
// shouldn't blank out the heatmap or the other widgets.
function renderHistWidgets(sub, spot, tier, dateStr) {
  try { renderTop5Widget(sub, spot); } catch (e) { console.warn('top5 widget failed', e); }
  renderIntradayWidget(dateStr).catch(e => console.warn('intraday widget failed', e));
  renderDailyWidget(dateStr).catch(e => console.warn('daily widget failed', e));
  renderSimilarWidget(dateStr).catch(e => console.warn('similar widget failed', e));
}
