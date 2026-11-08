// data fetch cycle — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── data fetch cycle ──────────────────────────────────────────────────────────
// One fetch of one underlying's latest snapshot, validated against the
// requested symbol before anything can render it.
async function fetchChain(symbol) {
  const path = chainLatestPath(symbol);
  if (!path) return { error: `${symbol} is not a supported underlying` };
  try {
    const resp = await fetch(`${BROWSER_DATA}/${path}?_=${Date.now()}`);
    if (resp.status === 404) return { error: `No ${symbol} snapshot has been published yet.` };
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const check = validateChainPayload(data, symbol);
    if (!check.ok) return { error: `${symbol} snapshot rejected: ${check.reason}.` };
    return { data };
  } catch (e) {
    console.error(e);
    return { error: `${symbol} snapshot could not be loaded.`, transient: true };
  }
}

let latestQqqRequest = 0;
async function fetchLatest() {
  const token = chainSelection.token();
  // QQQ is always refreshed: paper trading, expiry settlement marks and the
  // QQQ price tile depend on it regardless of which chain is on screen.
  const qqqRequest = ++latestQqqRequest;
  const qqq = await fetchChain('QQQ');
  // QQQ refreshes independently of the selected chain, but an older refresh
  // must not replace newer paper-trading, indicator or cached chain state.
  if (qqqRequest !== latestQqqRequest) return;
  if (qqq.data && qqq.data.rows.length) {
    lastTs = qqq.data.timestamp;
    lastData = qqq.data;
    chainState.QQQ = { data: qqq.data, error: null };
    ingestSnapshotQuotes(qqq.data);
    updateQqqVwapRvolLine(qqq.data.underlying_market);
    updateQqqMomentumLine(qqq.data.underlying_market);
  } else {
    chainState.QQQ = { data: chainState.QQQ?.data ?? null, error: qqq.error || 'No QQQ rows in the latest snapshot.' };
  }

  if (token.symbol !== 'QQQ') {
    const other = await fetchChain(token.symbol);
    // The selection changed while this was in flight: drop the reply rather
    // than render (or cache) it under whatever is selected now.
    if (!chainSelection.accepts(token)) return;
    if (other.data) {
      chainState[token.symbol] = { data: other.data, error: null };
      // Interests first: the quote cache only keeps wanted (visible) symbols.
      updateQuoteInterests();
      publishSnapshotQuotes(other.data);
    } else {
      chainState[token.symbol] = {
        data: other.transient ? chainState[token.symbol]?.data ?? null : null,
        error: other.error,
      };
    }
  }
  if (!chainSelection.accepts(token)) return;
  updateQuoteInterests();
  renderSelectedChain();
  renderPaper();
}

function selectedChainData() {
  return chainState[chainSelection.symbol]?.data ?? null;
}

function clearChain(message) {
  document.getElementById('heatmap-body').innerHTML = '';
  document.getElementById('heatmap-wrap').classList.add('hidden');
  const noData = document.getElementById('no-data');
  noData.textContent = message;
  noData.classList.remove('hidden');
  for (const id of ['vwap-rvol', 'momentum', 'chain-note']) document.getElementById(id).textContent = '';
  document.getElementById('price').textContent = '—';
  document.getElementById('tier-badge').style.display = 'none';
}

function renderChainTitle(symbol) {
  document.getElementById('title').textContent = `${symbol} 0DTE Live OI`;
  document.title = `${symbol} Live OI — moopertonic`;
  const select = document.getElementById('underlying-select');
  if (select.value !== symbol) select.value = symbol;
}

// Renders whichever underlying is selected, from that underlying's own
// validated snapshot only. Each symbol's stale/unavailable state is its own.
function renderSelectedChain() {
  const symbol = chainSelection.symbol;
  renderChainTitle(symbol);
  const state = chainState[symbol];
  const data = state?.data;
  if (!data) {
    clearChain(state?.error || (symbol === 'QQQ' ? NO_DATA_TEXT : `Loading ${symbol}…`));
    setStatus(state?.error ? 'stale' : '', state?.error ? 'unavailable' : 'Loading…');
    return;
  }
  if (!data.rows.length) {
    clearChain(`No ${symbol} contracts in the latest snapshot.`);
    setStatus('stale', data.snapshot_time || '—');
    return;
  }
  renderHeatmap(data, symbol);
  if (!data.underlying_price) return;  // renderHeatmap already marked it stale
  const snapAge = Math.round((Date.now() - new Date(data.timestamp).getTime()) / 1000 / 60);
  const hasBids = data.rows.some(r => r.Bid != null);
  // Dot color carries live/stale; text is just a small timestamp note —
  // deeper diagnosis lives in diag.html, not the main view.
  setStatus(snapAge > 2 || !hasBids || state.error ? 'stale' : 'live', data.snapshot_time);
}

function selectUnderlying(symbol) {
  if (!chainSelection.select(symbol)) return;
  try { localStorage.setItem(UNDERLYING_KEY, chainSelection.symbol); } catch (e) {}
  // Clear immediately so nothing from the previous underlying stays on screen
  // under the new heading, then fetch the new one right away.
  clearChain(`Loading ${chainSelection.symbol}…`);
  renderSelectedChain();
  updateQuoteInterests();
  fetchLatest();
  startCountdown();
}

function setStatus(type, text) {
  const dot  = document.getElementById('status-dot');
  const span = document.getElementById('status-text');
  dot.className  = type;  // 'live' | 'stale' | ''
  span.textContent = text;
}
