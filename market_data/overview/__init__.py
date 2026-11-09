"""The accepted #132 universe. Selection is independent of activation.

Both the prices producer and MCP consumer read universe.json. `enabled` gates
collector subscriptions, not membership: all 100 remain discoverable even when
no provider route is approved. New mappings require a reviewed config change.
"""
import json
from pathlib import Path

UNIVERSE_PATH = Path(__file__).with_name('universe.json')

def load_universe():
    config = json.loads(UNIVERSE_PATH.read_text(encoding='utf-8'))
    members = config['instruments']
    if (config['schema_version'] != 1 or len(members) != 100
            or len({r['id'] for r in members}) != 100
            or len({r['label'] for r in members}) != 100
            or sum(bool(r['compact']) for r in members) != 18):
        raise ValueError('Invalid accepted exact-100 registry')
    return config

_CONFIG = load_universe()
UNIVERSE_VERSION = _CONFIG['universe_version']
INSTRUMENTS = tuple(_CONFIG['instruments'])

def producer_maps():
    """Return approved maps only; SPY keeps its existing dedicated producer."""
    enabled = [r for r in INSTRUMENTS if r['enabled'] and r['label'] != 'SPY']
    return ({r['label']: r['dxlink_symbol'] for r in enabled if r['dxlink_symbol']},
            {r['label']: r['yahoo_symbol'] for r in enabled if r['yahoo_symbol']})
