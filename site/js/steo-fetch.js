// EIA STEO crude calibration + OVX regime. Optional feed -- collector.py
// only writes macro/eia_steo.json when EIA_API_KEY is configured. A 404
// means the optional publication is absent, not proof of why it is absent;
// keep the panel hidden without treating absence as a transport failure.
// fmtSteoDelta/findRevision live in docs/shared/format.js (pure, tested directly there).
async function fetchSteoCalibration() {
  const panel = document.getElementById('steo-panel');
  try {
    const resp = await fetch(`${BROWSER_DATA}/macro/eia_steo.json?_=${Date.now()}`);
    if (resp.status === 404) { panel.classList.add('hidden'); return; }
    if (!resp.ok) throw new Error(`STEO publication returned HTTP ${resp.status}`);
    const data = await resp.json();
    const points = data?.points;
    if (!Array.isArray(points)) throw new Error('Invalid STEO points');
    if (!points.length) { panel.classList.add('hidden'); return; }
    const isPeriod = value => typeof value === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(value);
    if (!isPeriod(data.current_release) || (data.prior_release != null && !isPeriod(data.prior_release)) ||
        points.some(p => !p || !isPeriod(p.period))) {
      throw new Error('Invalid STEO release or forecast period');
    }

    // Nearest forecast period still ahead of (or equal to) the current
    // calendar month -- the most relevant single number for "what does the
    // latest STEO say is coming," same reasoning a reader would use pulling
    // one row out of the full forecast table.
    const nowPeriod = new Date().toISOString().slice(0, 7);
    const nearest = points.find(p => p.period >= nowPeriod) || points[points.length - 1];
    const revision = findRevision(data.revisions, nearest.period);
    document.getElementById('steo-period').textContent = nearest.period;

    document.getElementById('steo-release').textContent =
      data.current_release + (data.prior_release ? ` (vs ${data.prior_release})` : '');
    document.getElementById('steo-brent').textContent = nearest.brent != null ? `$${nearest.brent.toFixed(2)}` : '—';
    document.getElementById('steo-wti').textContent = nearest.wti != null ? `$${nearest.wti.toFixed(2)}` : '—';
    document.getElementById('steo-balance').textContent =
      nearest.balance != null ? `${nearest.balance.toFixed(2)} Mbbl/d` : '—';

    const brentD = fmtSteoDelta(revision?.brent_delta, '');
    const wtiD = fmtSteoDelta(revision?.wti_delta, '');
    const balD = fmtSteoDelta(revision?.balance_delta, '');
    for (const [id, d] of [['steo-brent-delta', brentD], ['steo-wti-delta', wtiD], ['steo-balance-delta', balD]]) {
      const el = document.getElementById(id);
      el.textContent = d.text;
      el.className = `steo-delta ${d.cls}`;
    }

    const ovx = data.ovx || {};
    document.getElementById('steo-ovx').textContent = ovx.value != null ? ovx.value.toFixed(2) : '—';
    const regimeEl = document.getElementById('steo-ovx-regime');
    regimeEl.textContent = ovx.regime || 'no_data';
    regimeEl.className = `ovx-regime ${ovx.regime || 'no_data'}`;

    panel.classList.remove('hidden');
  } catch (e) {
    console.warn('eia_steo.json fetch failed:', e);
    panel.classList.add('hidden');
  }
}
