"""MOO-169 Tradier collector -- deployed entry point.

The implementation lives in ``collectors/tradier/``. This module stays at the
path the Railway start command and ``tradier_cron_launcher.py`` name, and
re-exports the package so existing importers keep working unchanged:

    python -u scripts/moo144_tradier_collector.py

The process stop flag lives in ``collectors.tradier.config.STOP``. It is
deliberately not re-exported here: a copy on this module would be set by a
caller expecting to stop capture and would do nothing.
"""
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.tradier import *  # noqa: F401,F403,E402
from collectors.tradier import (  # noqa: E402,F401
    _is_confirmed_absent,
    _is_readable_gzip_ndjson,
    _read_lease,
    _remote_content_matches,
    _require_live_lease_owner,
    main,
    on_stop,
)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({
            "event": "collector_failed",
            "error": type(exc).__name__,
            "message": str(exc),
        }), flush=True)
        raise
