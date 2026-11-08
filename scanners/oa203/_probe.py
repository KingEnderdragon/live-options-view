"""The R2 helpers this scanner shares with the Tradier probe.

``scripts/moo144_tradier_probe.py`` owns the verified-put and bucket helpers.
It still lives in ``scripts/`` (MOO-144's home), so this shim is the one place
that puts that directory on ``sys.path`` for the package.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from moo144_tradier_probe import (  # noqa: E402,F401
    put_bytes_verified,
    r2_client,
    upload_file_verified,
)
