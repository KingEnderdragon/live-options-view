"""Shared exact-100 registry, with a separately preserved compact strip."""
import json
from pathlib import Path

UNIVERSE_PATH = Path(__file__).with_name('universe.json')

# The 18 identities the accepted first slice actually collects. Nothing else is
# approved for provider requests; membership and activation are separate.
COMPACT_LABELS = frozenset({
    'QQQ', 'SPY', 'SMH', 'IGV', 'USO', 'VIX', 'OVX', 'KOSPI', '10Y',
    'JPY/USD', 'BTC/USD', 'META', 'GOOGL', 'AMZN', 'TSLA', 'MU', 'AAPL', 'SPCX',
})


def _validate(config):
    members = config['instruments']
    if (config['schema_version'] != 1 or len(members) != 100
            or len({r['id'] for r in members}) != 100
            or len({r['label'] for r in members}) != 100
            or len({r['display_symbol'] for r in members}) != 100):
        raise ValueError('Invalid accepted exact-100 registry')
    if not all(r['enabled'] for r in members):
        raise ValueError('Full selected universe must be enabled')
    compact = {r['label'] for r in members if r['compact']}
    if compact != COMPACT_LABELS:
        raise ValueError('Compact strip membership changed')
    dx = [r['dxlink_symbol'] for r in members if r['dxlink_symbol']]
    yf = [r['yahoo_symbol'] for r in members if r['yahoo_symbol']]
    if len(set(dx)) != len(dx) or len(set(yf)) != len(yf):
        raise ValueError('Duplicate provider mappings')
    if any(r['label'] == 'JPY/USD' and r['yahoo_symbol'] for r in members):
        raise ValueError('Yen futures must not carry a spot fallback')
    return config


def load_universe():
    return _validate(json.loads(UNIVERSE_PATH.read_text(encoding='utf-8')))


_CONFIG = load_universe()
UNIVERSE_VERSION = _CONFIG['universe_version']
INSTRUMENTS = tuple(_CONFIG['instruments'])
ACTIVATION_ALLOWLIST = frozenset(r['label'] for r in INSTRUMENTS)


def producer_maps():
    """Return approved maps only; SPY keeps its existing dedicated producer."""
    enabled = [r for r in INSTRUMENTS
               if r['enabled'] and r['label'] != 'SPY']
    return ({r['label']: r['dxlink_symbol'] for r in enabled},
            {r['label']: r['yahoo_symbol'] for r in enabled if r['yahoo_symbol']})
