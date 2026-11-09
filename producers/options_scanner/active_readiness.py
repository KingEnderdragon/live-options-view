"""One-shot offline replay on the deployed scanner host; no provider or R2 writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import resource
import producers.options_scanner as s
from producers.options_scanner.active_budget import constrain_builder


def main():
    cfg = s.Config.from_env()
    days = sorted(p for p in cfg.spool_dir.iterdir()
                  if p.is_dir() and (p/"summary.json").is_file()
                  and (p/"sweeps/manifest.jsonl").is_file())
    if not days:
        raise RuntimeError("no completed scanner session for host replay")
    source = days[-1]
    summary = json.loads((source/"summary.json").read_text())
    manifest = s._read_manifest(source)
    sweep = min(96, max(m["sweep"] for m in manifest))
    with tempfile.TemporaryDirectory(prefix="oa203-active-readiness-") as temp:
        day = Path(temp)/source.name
        day.mkdir()
        shutil.copytree(source/"sweeps", day/"sweeps")
        code = (
            "import sys,json;"
            "import producers.options_scanner as s;from pathlib import Path;from datetime import date;"
            f"a=s.DayArchive(Path({temp!r}),date.fromisoformat({source.name!r}),'oa203/scanner',False);"
            f"p=s.build_active_snapshot(a,s.Config(upload=False),{sweep},"
            f"{summary['session_open_ms']},{summary['session_close_ms']});"
            "print(json.dumps(p))")
        started = time.monotonic()
        r = subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,
                           timeout=240,preexec_fn=constrain_builder,check=True)
        pointer = json.loads(r.stdout)
        output = {"event":"oa203_active_host_readiness_pass",
                  "revision":os.getenv("RAILWAY_GIT_COMMIT_SHA"),
                  "source_session":source.name,"sweep":sweep,
                  "seconds":round(time.monotonic()-started,3),
                  "max_rss_kib":resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  "members":pointer["members"],"publication":False,
                  "limits":{"cores":1,"address_bytes":2*1024**3,"cpu_seconds":230,"wall_seconds":240}}
        print(json.dumps(output),flush=True)


if __name__ == "__main__":
    main()
