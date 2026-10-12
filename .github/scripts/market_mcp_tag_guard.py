"""Fail-closed GHCR tag check for the one-time, run-tagged market MCP release."""

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCE_SHA = "1dde53f78709a41c41e17af37c1f9e422626f5af"
PACKAGE = "owentanzer/market-mcp-read-only"
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class GuardError(RuntimeError):
    pass


def release_tag(run_id: str, run_attempt: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(
        r"[1-9][0-9]*", run_attempt
    ):
        raise GuardError("invalid GitHub run identity")
    return f"{SOURCE_SHA}-run-{run_id}-{run_attempt}"


def check_tag_absent(tag: str, opener=urlopen, sleeper=time.sleep) -> None:
    """Only an explicit manifest 404 allows publication; every other failure blocks."""
    if not re.fullmatch(rf"{SOURCE_SHA}-run-[1-9][0-9]*-[1-9][0-9]*", tag):
        raise GuardError("unexpected release tag")

    token_url = "https://ghcr.io/token?" + urlencode(
        {"scope": f"repository:{PACKAGE}:pull", "service": "ghcr.io"}
    )
    try:
        with opener(Request(token_url), timeout=8) as response:
            if response.status != 200:
                raise GuardError("registry token request failed")
            token = json.loads(response.read())["token"]
            if not isinstance(token, str) or not token:
                raise GuardError("registry token missing")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        raise GuardError("registry token request failed") from exc

    manifest_url = f"https://ghcr.io/v2/{PACKAGE}/manifests/{tag}"
    request = Request(
        manifest_url,
        method="HEAD",
        headers={"Accept": ACCEPT, "Authorization": f"Bearer {token}"},
    )
    for attempt in range(3):
        try:
            with opener(request, timeout=8) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                sleeper(attempt + 1)
                continue
            raise GuardError("registry lookup failed") from exc

        if status == 404:
            return
        if status == 200:
            raise GuardError("release tag already exists")
        if status in (500, 502, 503, 504) and attempt < 2:
            sleeper(attempt + 1)
            continue
        raise GuardError(f"registry lookup returned HTTP {status}")

    raise GuardError("registry lookup failed")


def main() -> int:
    try:
        expected = release_tag(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
        if os.environ["RELEASE_TAG"] != expected:
            raise GuardError("release tag does not match this run attempt")
        check_tag_absent(expected)
    except (GuardError, KeyError) as exc:
        print(f"GHCR publication blocked: {exc}", file=sys.stderr)
        return 1
    print(f"GHCR tag is absent: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

