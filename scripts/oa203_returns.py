"""Return and leaderboard metrics for OA-203.

The implementation moved with the scanner to ``scanners/oa203/returns.py``.
This module stays because the multi-session window tools in this directory
import it by name; it re-exports the same objects, so class identity is
preserved for callers that mix both paths.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanners.oa203.returns import *  # noqa: F401,F403,E402

from scanners.oa203.returns import (  # noqa: F401
    _isolated,
    _leg_row,
    _qualified_endpoint,
    _relative_spread,
    _series,
)
