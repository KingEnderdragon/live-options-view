// OI skew meter renderer (Skew + Direction H1, Nov 2026). Classic script,
// loaded after data-fetch-cycle.js; data-fetch-cycle calls
// updateOiSkewMeter(qqq.data) on the existing 60s QQQ refresh, so the meter
// adds no request of its own. All state/formatting decisions live in the pure
// site/shared/oi-skew.js; this file only writes them into #oi-skew-meter.
//
// The bot-signal row stays "not published" until the runner publishes its
// read-only projection; the raw reference reading is never presented as the
// bot's state.

function _oiSkewEtTime(ms) {
  if (ms == null) return 'time unknown';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', second: '2-digit',
    }).format(new Date(ms)) + ' ET';
  } catch (_) {
    return new Date(ms).toISOString().slice(11, 19) + ' UTC';
  }
}

function _oiSkewSigned(value) {
  // Typographic minus so a negative reading cannot be misread as a dash.
  return value > 0 ? `+${value.toFixed(3)}` : value < 0 ? `−${Math.abs(value).toFixed(3)}` : '0.000';
}

const OI_SKEW_AVAILABILITY_LABEL = {
  live: 'live', stale: 'stale', closed: 'session closed', unavailable: 'unavailable', no_data: 'no data',
};

function updateOiSkewMeter(payload, nowMs = Date.now(), botProjection = null) {
  const root = document.getElementById('oi-skew-meter');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const raw = describeReferenceOiSkew(payload, nowMs);
  const bot = describeBotOiSkew(botProjection, nowMs);

  root.dataset.state = raw.availability;
  root.dataset.lean = raw.lean || 'none';

  const track = $('oi-skew-track');
  const marker = $('oi-skew-marker');
  const pct = oiSkewMeterPercent(raw.value);
  if (pct == null) {
    marker.hidden = true;
    $('oi-skew-value').textContent = '—';
    $('oi-skew-lean').textContent = OI_SKEW_AVAILABILITY_LABEL[raw.availability] || 'unavailable';
    track.setAttribute('aria-valuenow', '0');
    track.setAttribute('aria-valuetext', `OI skew ${OI_SKEW_AVAILABILITY_LABEL[raw.availability] || 'unavailable'}`);
  } else {
    marker.hidden = false;
    marker.style.left = `${pct}%`;
    $('oi-skew-value').textContent = _oiSkewSigned(raw.value);
    const lean = raw.lean.charAt(0).toUpperCase() + raw.lean.slice(1);
    $('oi-skew-lean').textContent = lean;
    track.setAttribute('aria-valuenow', raw.value.toFixed(3));
    track.setAttribute('aria-valuetext',
      `OI skew ${raw.value.toFixed(3)}, ${raw.lean}, ${OI_SKEW_AVAILABILITY_LABEL[raw.availability]}`);
  }

  const status = OI_SKEW_AVAILABILITY_LABEL[raw.availability] || raw.availability;
  $('oi-skew-meta').textContent =
    `${raw.symbol || 'QQQ'} · reference ±${((raw.band_pct ?? 0.02) * 100).toFixed(0)}% band · `
    + `snapshot ${_oiSkewEtTime(raw.snapshot_ms)} · ${status}`;
  root.title = raw.detail;

  const botEl = $('oi-skew-bot');
  botEl.textContent = bot.text;
  botEl.dataset.state = bot.state;
  botEl.dataset.availability = bot.availability;
  $('oi-skew-bot-detail').textContent = bot.detail;

  const p = bot.params || OI_SKEW_DEFAULTS;
  $('oi-skew-thresholds').textContent =
    `Bot gates${bot.params ? '' : ' (strategy defaults; account overrides not published)'}: `
    + `|skew| ≥ ${p.imbalance_threshold}, moved ≥ ${p.min_change_from_session_start} since its session start, `
    + `≥ ${p.min_band_strikes} strikes with OI in a ±${(p.near_money_pct * 100).toFixed(0)}% band.`;
}
