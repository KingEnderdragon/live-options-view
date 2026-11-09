function renderHeatmap(data, symbol = 'QQQ') {
  const { underlying_price, tier, snapshot_time, underlying_market } = data;
  const spot = underlying_price;
  const isQqq = symbol === 'QQQ';

  if (!spot) {
    setStatus('stale', snapshot_time);
    // A non-QQQ chain without its own spot is cleared, not left showing rows
    // it can't center -- and never filled with another underlying's price.
    if (!isQqq) clearChain(`${symbol} spot price unavailable in the latest snapshot.`);
    return;
  }

  const book = computeBook();   // computed once per render, not per-cell
  // Paper tickets, positions and QQQ's OIranges calibration are QQQ-only:
  // other underlyings render view-only, colored relative to their own
  // snapshot (see relativeOiRanges in docs/shared/chain.js).
  buildHeatmapRows(document.getElementById('heatmap-body'), data, isQqq ? ranges : relativeOiRanges(data.rows), {
    getPosition: (sym) => book[sym]?.pos || 0,
    getQuote: (sym) => liveQuotes.get(sym),
    onCellClick: (e, row) => openTicket(e, row),
  });
  document.getElementById('chain-note').textContent = isQqq ? '' : 'OI colors relative to this snapshot · execution requires a fresh contract quote';

  // Header
  document.getElementById('price').textContent = `$${spot?.toFixed(2) ?? '—'}`;
  const tierBadge = document.getElementById('tier-badge');
  tierBadge.textContent = (tier || '').replace('0DTE_', '');
  tierBadge.style.color  = TIER_COLORS[tier] || '#c9d1d9';
  tierBadge.style.display = '';

  // VWAP/RVOL -- same underlying_market block the QQQ tile's second line
  // reads (updateQqqVwapRvolLine below); both driven by this one 60s-cadence
  // render, not the 10s prices.json/live-quotes cadence (see
  // docs/plans/2026-07-vwap-rvol.md for why).
  const vr = formatVwapRvol(underlying_market);
  const vrEl = document.getElementById('vwap-rvol');
  vrEl.textContent = vr.text;
  vrEl.dataset.state = vr.state;

  const mo = formatMomentum(underlying_market);
  const moEl = document.getElementById('momentum');
  moEl.textContent = mo.text;
  moEl.dataset.state = mo.state;

  document.getElementById('no-data').classList.add('hidden');
  document.getElementById('heatmap-wrap').classList.remove('hidden');
}

