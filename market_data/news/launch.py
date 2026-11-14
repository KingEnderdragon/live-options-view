"""Bounded one-shot launcher for the scheduled News pilot cycle.

    python -m market_data.news.launch --config market_data/news/deploy/news-pilot.json [pilot-cycle options]

The scheduler runs this, not `pilot-cycle` directly. `cycle_budget_s` bounds only the collection
phase; nothing bounds a cycle's views, research, publication, store calls, SDK retries or a hung
connection. This launcher gives the whole process one explicit wall-clock deadline, from its own
start, covering every phase, retry and subprocess of the cycle, and always ends with exactly one
terminal record on stdout and a definite exit code:

  1. It loads and validates the pilot config (`launcher` block included). An invalid config is a
     terminal failure before anything runs.
  2. It runs the cycle as a child process in its own process group (`--run-cycle`, below), which runs
     `python -m market_data.news pilot-cycle --config <config> <options>` in-process.
  3. At `deadline_s` it interrupts the child's whole group: the child raises CycleInterrupted (a
     BaseException), so the cycle's existing failure handling runs and its writer/publish leases are
     released by their context managers, exactly as on any other exception. Nothing new is written.
  4. If the group is still alive `interrupt_grace_s` later, it is killed. A killed cycle cannot release
     its leases: the record says so, and later cycles exit non-zero ("lease held") until the lease
     expires and an operator who has confirmed no writer runs passes --break-stale-lease
     (publish-status reports stale_*_lease). Nothing here breaks a lease.
  5. A SIGTERM/SIGINT to the launcher (a scheduler stop) starts the same interrupt-then-kill sequence.

`load_pilot_config` requires deadline_s + interrupt_grace_s + kill_wait_s < schedule.interval_s and
<= the lease TTL, so a launcher always exits before the next scheduled tick (no self-overlap) and a
live cycle never holds a lease past its expiry. Exit codes:

  0    the cycle finished and its own exit code was 0
  1/2  the cycle finished with that exit code (pilot-cycle's own failure codes)
  2    the config is invalid (no child is started)
  124  deadline: the cycle was interrupted and exited (its leases were released by the cycle)
  137  deadline or stop: the cycle did not exit within the grace and was killed (leases may remain)
  143  stopped by a signal to the launcher; the cycle was interrupted and exited
  125  the child could not be started
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

LAUNCH_SCHEMA = "news-launch/v1"
CHILD_INTERRUPTED = 3
WINDOWS = os.name == "nt"


class CycleInterrupted(BaseException):
    """Raised in the cycle process when the launcher interrupts it (deadline or stop)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_cycle(argv: list[str]) -> int:
    """The child: run pilot-cycle in this process, turning the launcher's interrupt into an exception."""
    from .__main__ import main

    def interrupted(signum, frame):
        raise CycleInterrupted(f"signal {signum}")

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)
    try:
        return main(["pilot-cycle", *argv])
    except CycleInterrupted as exc:
        print(json.dumps({"status": "interrupted", "reason": str(exc)}), file=sys.stderr, flush=True)
        return CHILD_INTERRUPTED


def _spawn(cmd: list[str]) -> subprocess.Popen:
    if WINDOWS:
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(cmd, start_new_session=True)


def _interrupt(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def launch(config_path: str, cycle_args: list[str], *, monotonic=time.monotonic, cmd_prefix=None) -> dict:
    """Run one bounded cycle; return the terminal record (its exit_code is the process exit code)."""
    start = monotonic()
    record = {"schema": LAUNCH_SCHEMA, "config": config_path, "started_at": _now()}

    def finish(outcome: str, exit_code: int, **extra) -> dict:
        record.update(outcome=outcome, exit_code=exit_code, ended_at=_now(),
                      elapsed_s=round(monotonic() - start, 3), **extra)
        return record

    try:
        from .publish import load_pilot_config
        cfg = load_pilot_config(config_path)
    except Exception as exc:   # an unreadable or invalid config never starts a cycle
        return finish("config_invalid", 2, error=f"{type(exc).__name__}: {exc}"[:500])
    bounds = cfg["launcher"]
    deadline, grace, kill_wait = bounds["deadline_s"], bounds["interrupt_grace_s"], bounds["kill_wait_s"]
    record.update(deadline_s=deadline, interrupt_grace_s=grace, kill_wait_s=kill_wait)

    stop = {"signal": None}
    previous = {}

    def on_stop(signum, frame):
        stop["signal"] = signum

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name)
        previous[sig] = signal.signal(sig, on_stop)
    try:
        cmd = (cmd_prefix or [sys.executable, "-m", "market_data.news.launch"]) + \
            ["--run-cycle", "--config", config_path, *cycle_args]
        try:
            proc = _spawn(cmd)
        except OSError as exc:
            return finish("spawn_failed", 125, error=f"{type(exc).__name__}: {exc}"[:500])
        record["child_pid"] = proc.pid

        reason = None
        while True:
            remaining = deadline - (monotonic() - start)
            if stop["signal"] is not None:
                reason = "stopped"
                break
            if remaining <= 0:
                reason = "deadline"
                break
            try:
                code = proc.wait(timeout=min(remaining, 1.0))
            except subprocess.TimeoutExpired:
                continue
            if code == 0:
                return finish("ok", 0, child_exit=0)
            return finish("cycle_failed", code if code in (1, 2) else 1, child_exit=code)

        _interrupt(proc)
        record["interrupted_at"] = _now()
        try:
            code = proc.wait(timeout=grace)
            if reason == "stopped":
                return finish("stopped_interrupted", 143, child_exit=code, signal=stop["signal"])
            return finish("deadline_interrupted", 124, child_exit=code)
        except subprocess.TimeoutExpired:
            pass
        _kill(proc)
        try:
            code = proc.wait(timeout=kill_wait)
        except subprocess.TimeoutExpired:
            code = None
        return finish("killed", 137, child_exit=code, cause=reason,
                      lease_note="the cycle was killed: its writer/publish leases may remain held until they "
                                 "expire; later cycles exit non-zero until an operator confirms no writer runs "
                                 "and passes --break-stale-lease (publish-status reports stale_*_lease)")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--run-cycle"]:
        return run_cycle(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m market_data.news.launch",
                                 description="bounded one-shot launcher for the News pilot cycle")
    ap.add_argument("--config", required=True)
    args, cycle_args = ap.parse_known_args(argv)
    record = launch(args.config, cycle_args)
    print(json.dumps(record, sort_keys=True), flush=True)
    return record["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
