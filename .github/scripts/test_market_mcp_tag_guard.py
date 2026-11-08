"""Focused registry failure and unique-release-tag checks."""

import io
import unittest
from urllib.error import HTTPError, URLError

from market_mcp_tag_guard import GuardError, check_tag_absent, release_tag


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def http_error(request, status):
    return HTTPError(request.full_url, status, "mock", {}, io.BytesIO())


class Registry:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if request.get_method() == "GET":
            return Response(200, b'{"token":"test-public-pull-token"}')
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome >= 400:
            raise http_error(request, outcome)
        return Response(outcome)


class TagGuardTests(unittest.TestCase):
    def setUp(self):
        self.tag = release_tag("38210282264", "1")

    def check(self, outcomes):
        registry = Registry(outcomes)
        sleeps = []
        return registry, sleeps, lambda: check_tag_absent(
            self.tag, opener=registry, sleeper=sleeps.append
        )

    def test_explicit_404_is_absent(self):
        registry, _, check = self.check([404])
        check()
        self.assertEqual(len(registry.calls), 2)
        self.assertEqual(registry.calls[1][0].get_method(), "HEAD")

    def test_existing_tag_blocks_overwrite(self):
        _, _, check = self.check([200])
        with self.assertRaisesRegex(GuardError, "already exists"):
            check()

    def test_auth_failure_blocks_without_retry(self):
        registry, _, check = self.check([401])
        with self.assertRaisesRegex(GuardError, "HTTP 401"):
            check()
        self.assertEqual(len(registry.calls), 2)

    def test_timeout_fails_closed_after_bounded_retries(self):
        registry, sleeps, check = self.check([TimeoutError()] * 3)
        with self.assertRaisesRegex(GuardError, "lookup failed"):
            check()
        self.assertEqual(len(registry.calls), 4)
        self.assertEqual(sleeps, [1, 2])

    def test_server_error_retries_then_fails_closed(self):
        registry, sleeps, check = self.check([503] * 3)
        with self.assertRaisesRegex(GuardError, "HTTP 503"):
            check()
        self.assertEqual(len(registry.calls), 4)
        self.assertEqual(sleeps, [1, 2])

    def test_transient_server_error_then_explicit_absence(self):
        _, sleeps, check = self.check([503, 404])
        check()
        self.assertEqual(sleeps, [1])

    def test_transport_failure_blocks(self):
        _, _, check = self.check([URLError("offline")] * 3)
        with self.assertRaisesRegex(GuardError, "lookup failed"):
            check()

    def test_token_failure_cannot_be_misread_as_absence(self):
        def denied(request, timeout):
            raise http_error(request, 401)

        with self.assertRaisesRegex(GuardError, "token request failed"):
            check_tag_absent(self.tag, opener=denied)

    def test_unique_tag_per_run_and_retry(self):
        self.assertNotEqual(release_tag("100", "1"), release_tag("101", "1"))
        self.assertNotEqual(release_tag("100", "1"), release_tag("100", "2"))
        with self.assertRaisesRegex(GuardError, "invalid GitHub run identity"):
            release_tag("100;evil", "1")


if __name__ == "__main__":
    unittest.main()

