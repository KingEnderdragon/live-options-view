#!/usr/bin/env python3
"""collector.py -- thin entry shim for the collector service.

The service lives in the collector/ package; `import collector` resolves
to collector/__init__.py, which re-exports the full surface. This file
stays only so the Railway start command `python collector.py` keeps
working.
"""

from collector import *  # noqa: F401,F403 -- full service surface
from collector import main

if __name__ == "__main__":
    main()
