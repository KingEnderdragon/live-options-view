"""Fail-closed source guard for the reviewed market MCP publisher.

A reviewed consumer image may only be built from the exact accepted consumer
commit, an ancestor of master, checked out cleanly, whose own diff against its
first parent contains only the reviewed consumer paths. This prevents a
dispatch from publishing an arbitrary master commit, a producer commit, or a
working tree containing uncommitted changes.
"""

import argparse
import re
import subprocess
import sys


CONSUMER_PATHS = frozenset({
    "market_mcp/overview.js",
    "market_mcp/overview-v2.test.js",
})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GuardError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", repo, *args],
            check=False, capture_output=True, text=True,
        )
    except OSError as exc:
        raise GuardError(f"git unavailable: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise GuardError(f"git {' '.join(args)} failed: {detail[-1] if detail else 'unknown'}")
    return completed.stdout.strip()


def validate_source(repo: str, sha: str, master_ref: str = "FETCH_HEAD",
                    allowed=CONSUMER_PATHS) -> list[str]:
    """Return the changed paths only when every publication guard passes."""
    if not SHA_RE.fullmatch(sha or ""):
        raise GuardError("source SHA must be a 40-character lowercase commit id")
    allowed = frozenset(allowed)
    if not allowed:
        raise GuardError("no reviewed paths configured")

    head = _git(repo, "rev-parse", "HEAD")
    if head != sha:
        raise GuardError(f"checkout HEAD {head} does not match requested source {sha}")

    dirty = _git(repo, "status", "--porcelain")
    if dirty:
        raise GuardError("source checkout is not clean")

    ancestor = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", sha, master_ref],
        check=False, capture_output=True, text=True,
    )
    if ancestor.returncode != 0:
        raise GuardError(f"source {sha} is not an ancestor of {master_ref}")

    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) < 2:
        raise GuardError("source commit has no parent to review against")
    parent = parents[1]

    changed = [line for line in _git(repo, "diff", "--name-only", parent, sha).splitlines() if line]
    if not changed:
        raise GuardError("source commit has no changed paths")
    unexpected = sorted(path for path in changed if path not in allowed)
    if unexpected:
        raise GuardError("source commit contains unreviewed paths: " + ", ".join(unexpected))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--master-ref", default="FETCH_HEAD")
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args()
    allowed = frozenset(args.allow) if args.allow else CONSUMER_PATHS
    try:
        changed = validate_source(args.repo, args.sha, args.master_ref, allowed)
    except GuardError as exc:
        print(f"Market MCP publication blocked: {exc}", file=sys.stderr)
        return 1
    print(f"Reviewed consumer source accepted: {args.sha}")
    for path in changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
