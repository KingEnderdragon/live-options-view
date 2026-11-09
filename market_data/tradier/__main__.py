"""Run one archive session: python -m market_data.tradier."""
import json
import signal
import sys


from market_data.tradier import *  # noqa: F401,F403,E402
from market_data.tradier import (  # noqa: E402,F401
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
