"""The accepted #132 universe. Selection is independent of activation.

Both the prices producer and MCP consumer read universe.json. `enabled` gates
collector subscriptions, not membership: all 100 remain discoverable even when
no provider route is approved.

Activation is pinned to the reviewed original strip. Editing `enabled` in the
JSON alone therefore cannot add a provider subscription; widening it is a code
change with its own review, which is the production gate issue #132 reserved.
"""
import json
from pathlib import Path

UNIVERSE_PATH = Path(__file__).with_name('universe.json')

# The 18 identities the accepted first slice actually collects. Nothing else is
# approved for provider requests; membership and activation are separate.
ACTIVATION_ALLOWLIST = frozenset({
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
    enabled = {r['label'] for r in members if r['enabled']}
    compact = {r['label'] for r in members if r['compact']}
    if enabled != ACTIVATION_ALLOWLIST or compact != ACTIVATION_ALLOWLIST:
        raise ValueError('Activated set differs from the reviewed original strip')
    active = [r for r in members if r['enabled'] and r['label'] != 'SPY']
    dx = [r['dxlink_symbol'] for r in active if r['dxlink_symbol']]
    yf = [r['yahoo_symbol'] for r in active if r['yahoo_symbol']]
    if len(dx) != 17 or len(set(dx)) != 17 or len(yf) != 16 or len(set(yf)) != 16:
        raise ValueError('Activated provider mappings are not the reviewed set')
    if any(r['label'] == 'JPY/USD' and r['yahoo_symbol'] for r in members):
        raise ValueError('Yen futures must not carry a spot fallback')
    return config


def load_universe():
    return _validate(json.loads(UNIVERSE_PATH.read_text(encoding='utf-8')))


_CONFIG = load_universe()
UNIVERSE_VERSION = _CONFIG['universe_version']
INSTRUMENTS = tuple(_CONFIG['instruments'])


def producer_maps():
    """Return approved maps only; SPY keeps its existing dedicated producer."""
    enabled = [r for r in INSTRUMENTS
               if r['enabled'] and r['label'] in ACTIVATION_ALLOWLIST and r['label'] != 'SPY']
    return ({r['label']: r['dxlink_symbol'] for r in enabled if r['dxlink_symbol']},
            {r['label']: r['yahoo_symbol'] for r in enabled if r['yahoo_symbol']})
