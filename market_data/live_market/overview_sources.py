"""Bounded source discovery and official daily Treasury observations."""
import threading
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests

from market_data.overview import INSTRUMENTS
from .config import PRICE_TICKERS, TASTY_BASE, log
from .oauth import _bearer_headers

RESOLVED_CONTRACTS = {}
_RESOLVE_LOCK = threading.Lock()
_RESOLVED_FOR_DATE = None
TREASURY_URL = 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml'


def resolve_futures(access_token, *, force=False):
    """Resolve provider-designated active months once per UTC day; never guess a roll.

    The continuous price lane and the option-chain session both need this, so a
    repeated call on the same day is a cache read rather than a second network
    resolution that could briefly clear the lane's symbols mid-publish.
    """
    global _RESOLVED_FOR_DATE
    futures = [r for r in INSTRUMENTS if r['profile'] == 'FU' and r['enabled']]
    today = datetime.now(timezone.utc).date().isoformat()
    with _RESOLVE_LOCK:
        if not force and _RESOLVED_FOR_DATE == today and RESOLVED_CONTRACTS:
            return dict(RESOLVED_CONTRACTS)
        for r in futures:
            PRICE_TICKERS[r['label']] = None
        RESOLVED_CONTRACTS.clear()
        _RESOLVED_FOR_DATE = today
        try:
            response = requests.get(TASTY_BASE + '/instruments/futures',
                params={'product-code[]': [r['id'][4:].upper() for r in futures]},
                headers=_bearer_headers(access_token), timeout=15)
            response.raise_for_status()
            items = response.json()['data']['items']
            for r in futures:
                root = r['id'][4:].upper()
                candidates = [i for i in items if i.get('active-month') and i.get('active')
                    and not i.get('is-closing-only') and i.get('expiration-date', '') >= today
                    and i.get('future-product', {}).get('code') == root]
                if len(candidates) != 1:
                    log.warning('overview future %s unresolved: %s active candidates', root, len(candidates))
                    continue
                i = candidates[0]
                symbol = i.get('streamer-symbol')
                if not symbol or ':' not in symbol:
                    continue
                RESOLVED_CONTRACTS[r['label']] = {'symbol': symbol, 'root': root,
                    'expiry': i['expiration-date'], 'exchange': symbol.split(':', 1)[1],
                    'selection': 'provider_active_month'}
                PRICE_TICKERS[r['label']] = symbol
        except Exception:
            log.warning('overview futures discovery unavailable; no guessed contracts subscribed')
        return dict(RESOLVED_CONTRACTS)


def futures_snapshot():
    """Atomic label->streamer-symbol view of the shared futures mapping.

    The price lane compares this each cycle so a chain-session resolution (or a
    daily roll) is reconciled into its own subscription instead of being read
    only at authentication.
    """
    with _RESOLVE_LOCK:
        return {r['label']: PRICE_TICKERS.get(r['label'])
                for r in INSTRUMENTS if r['profile'] == 'FU' and r['enabled']}


def treasury_observation(xml, now=None):
    """2-year constant-maturity yield, percent; official dated observation, not a trade."""
    now = now or datetime.now(timezone.utc).isoformat()
    values = {}
    for node in ElementTree.fromstring(xml).iter():
        if node.tag.split('}')[-1] != 'properties':
            continue
        fields = {c.tag.split('}')[-1]: c.text for c in node}
        try:
            date = datetime.fromisoformat(fields['NEW_DATE']).date().isoformat()
            value = float(fields['BC_2YEAR'])
            import math
            if math.isfinite(value):
                values[date] = value
        except (TypeError, ValueError, KeyError):
            continue
    ordered = sorted(values.items())
    if not ordered:
        raise ValueError('no dated Treasury observations')
    def pair(item):
        date, value = item
        return {'instrument_id': 'yld.us02y.cmt', 'value': value, 'source': 'treasury',
            'source_symbol': 'BC_2YEAR', 'price_basis': 'daily_close',
            'observed_at': None, 'observation_time_reason': 'official_daily_series_has_no_intraday_time',
            'observation_date': date, 'observation_timezone': 'America/New_York',
            'observation_granularity': 'day', 'session': 'daily',
            'adjustment_basis': 'unadjusted', 'resolved_contract': None}
    row = pair(ordered[-1])
    row['price'] = row.pop('value')
    row.update(bid=None, ask=None, volume=None, prev_close=None, quote_ts=None,
        collector_received_at=None, acquired_at=now, last_success_at=now,
        last_attempt_at=now, refresh_error=None,
        reference=pair(ordered[-2]) if len(ordered)>1 else None)
    return row


def fetch_treasury():
    now = datetime.now(timezone.utc)
    # Include December at the year boundary so a prior observation is retained.
    years = [now.year] + ([now.year-1] if now.month == 1 else [])
    rows = []
    for year in years:
        response = requests.get(TREASURY_URL, params={'data': 'daily_treasury_yield_curve',
            'field_tdr_date_value': str(year)}, timeout=20)
        response.raise_for_status()
        rows.append(treasury_observation(response.content, now.isoformat()))
    current = max(rows, key=lambda r: r['observation_date'])
    if current['reference'] is None:
        earlier = [r for r in rows if r['observation_date'] < current['observation_date']]
        if earlier:
            ref = max(earlier, key=lambda r: r['observation_date']).copy()
            ref['value'] = ref.pop('price')
            ref.pop('reference', None)
            current['reference'] = ref
    return current
