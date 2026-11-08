"""
collector.py -- QQQ (and SPY) live chain snapshot service.

Authenticates with tastytrade, subscribes to the QQQ and SPY 0DTE option chains
via one DXLink websocket, and uploads snapshots to R2 every minute. Each
underlying keeps its own VWAP/RVOL/momentum state and archive (#121); QQQ keeps
every pre-SPY key unchanged.

The nearest end-of-week expiration is subscribed through the same feed and
archived at hourly regular-session slots for historical analysis.  It does not
replace or alter the tactical 0DTE stream.

R2 output:
  intraday/YYYYMMDD/snapshot_HHMMSSffffff.csv  -- archived snapshots (microsecond key)
  intraday/latest.json                   -- live QQQ feed for the web viewer
  intraday/spy/latest.json               -- live SPY feed (same contract, "symbol": "SPY")
  intraday/spy/YYYYMMDD/...              -- SPY archive, mirroring the QQQ intraday/YYYYMMDD/ layout
  intraday/prices.json                   -- macro price strip (every 10s; yfinance fill cached 60s)
  intraday/health.json                   -- lifecycle telemetry (every 15s)
  raw/weekly/YYYYMMDD/qqq_chain_*.csv    -- nearest-weekly snapshots (~hourly RTH)
  manifest.json                          -- existing pipeline manifest + weekly keys
  macro/eia_steo.json                    -- EIA STEO crude calibration + OVX regime
                                             (every EIA_STEO_POLL_SECS, optional feature --
                                             see collector/crude_calibration.py; skipped if EIA_API_KEY unset)
  baselines/eia_steo_vintages.json       -- rolling log of past STEO releases this
                                             collector has observed, for vintage-over-vintage diffs

Environment variables (set in Railway dashboard):
  TASTY_OAUTH_CLIENT_SECRET  tastytrade personal OAuth client secret
  TASTY_OAUTH_REFRESH_TOKEN tastytrade personal OAuth refresh token
  TASTY_OAUTH_SCOPES         must be exactly "read"
  R2_ACCOUNT_ID          Cloudflare account ID
  R2_ACCESS_KEY_ID       R2 access key
  R2_SECRET_ACCESS_KEY   R2 secret key
  R2_BUCKET_NAME         bucket name (default: pub-4d5c916b8cb74ffb8c0abd7dfadb02cf)
  LIVE_QUOTE_KEY         shared key for the Worker's read-only quote proxy
  PORT                   Railway HTTP port (default: 8080 locally)
  EIA_API_KEY            optional -- enables the macro/eia_steo.json feed (see
                          collector/crude_calibration.py); free signup at eia.gov/opendata.
                          Feature is silently skipped (logged once) if unset.
  COLLECT_SPY            optional -- "0" disables SPY chain collection (default on)
"""

import importlib.util
import io
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import boto3
import pandas as pd
import pytz
import requests
import websocket
import yfinance as yf

from . import crude_calibration as cc
from . import market_signals as ms
from .config import *
from .config import __all__ as _config_all
from .feed import *
from .feed import __all__ as _feed_all
from .oauth import *
from .oauth import __all__ as _oauth_all
from .tiers import *
from .tiers import __all__ as _tiers_all

# --- temporary bridge (removed in the final B4 slice) -----------------------
# Names not yet moved out of the flat file still live in root collector.py.
# That file is shadowed by this package for `import collector`, so it is
# loaded here by path as collector._legacy and its names re-exported below.
# Each slice moves one subsystem out, shrinks the legacy file, and the final
# slice deletes this bridge entirely.
def _load_legacy():
    path = Path(__file__).resolve().parent.parent / "collector.py"
    spec = importlib.util.spec_from_file_location("collector._legacy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["collector._legacy"] = module
    spec.loader.exec_module(module)
    return module


_legacy = _load_legacy()
globals().update(
    {name: value for name, value in vars(_legacy).items()
     if not name.startswith("__") and name != "_entry"}
)

# The runner imports its not-yet-moved callees from ._legacy, so it loads
# after the bridge (its own import is what pulls those names in).
from .runner import *
from .runner import __all__ as _runner_all

__all__ = sorted(
    set(_config_all) | set(_feed_all) | set(_oauth_all) | set(_tiers_all) | set(_runner_all)
    | {"cc", "ms"}
    | set(['BaseHTTPRequestHandler', 'Optional', 'ThreadingHTTPServer', 'boto3', 'date', 'datetime', 'io', 'json', 'logging', 'lru_cache', 'math', 'os', 'parse_qs', 'pd', 'pytz', 're', 'requests', 'secrets', 'sys', 'threading', 'time', 'timedelta', 'timezone', 'urlparse', 'websocket', 'yf'])
    | {name for name in vars(_legacy) if not name.startswith("__")}
)

# Bridge internals are not part of the collector surface.
del _load_legacy, _legacy, _config_all, _feed_all, _oauth_all, _tiers_all, _runner_all, Path, importlib
