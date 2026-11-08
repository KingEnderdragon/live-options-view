"""Reuse the MOO-144 provider probe without depending on the caller's cwd.

``scripts/moo144_tradier_probe.py`` is the one-shot, read-only probe that owns
the Tradier client, the R2 helpers and ``select_symbols``. The daily collector
reuses those helpers rather than duplicating provider semantics, but the probe
still lives in ``scripts/`` (MOO-144's home). Importing it through this shim
keeps that dependency in one visible place instead of scattering sys.path
setup through the package.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from moo144_tradier_probe import (  # noqa: E402,F401
    DEFAULT_UNDERLYING,
    EXPIRATION_POLICIES,
    STREAM,
    Tradier,
    is_retryable,
    normalize,
    parse_epoch_ms,
    put_bytes_verified,
    r2_client,
    select_symbols,
    sha256_file,
    stream_payload,
    upload_file_verified,
    utc_now,
)
