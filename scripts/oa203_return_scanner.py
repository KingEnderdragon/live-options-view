"""OA-203 options return scanner -- deployed entry point.

The implementation lives in ``scanners/oa203/``. This module stays at the path
the Railway start command names and re-exports the package, so existing
importers keep working unchanged:

    python -u scripts/oa203_return_scanner.py run

The process stop flag lives in ``scanners.oa203.runtime.STOP``; it is
deliberately not re-exported here, because a copy on this module would be set
by a caller expecting to stop the scan and would do nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanners.oa203 import *  # noqa: F401,F403,E402
from scanners.oa203 import (  # noqa: E402,F401
    _active_cutoff_ms,
    _as_list,
    _bounds_from_summary,
    _build_active_snapshot,
    _cell,
    _content_type,
    _contract_paths,
    _env_int,
    _hour_bucket,
    _is_date,
    _moneyness_bucket,
    _num,
    _r2_uploader,
    _read_jsonl_gz,
    _read_manifest,
    _session_ms,
    _sha256_file,
    _sleep_until,
    _spot,
    main,
)

if __name__ == "__main__":
    sys.exit(main())
