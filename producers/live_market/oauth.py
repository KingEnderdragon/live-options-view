"""Tastytrade OAuth: token management and auth-error handling.

WHY this module: auth is the security boundary -- token refresh, the
fail-closed auth circuit, and secret redaction in broker errors. It reads
no other collector subsystem (only TASTY_BASE and the shared log), so it
moves out as a leaf.
"""

import os
import re
import threading
import time

import requests

from .config import TASTY_BASE, log

# -- tastytrade auth ----------------------------------------------------------

TASTY_USER_AGENT = "live-options-view/1.0"
OAUTH_REFRESH_BUFFER_SECS = 60
OAUTH_DEFAULT_LIFETIME_SECS = 900


class TastyAuthError(RuntimeError):
    """A failed auth chain that must not be retried during this session date."""

    def __init__(self, phase: str, *, status: int | None = None,
                 detail: str = "unavailable", challenge_present: bool = False):
        self.phase = phase
        self.status = status
        self.detail = detail
        self.challenge_present = challenge_present
        super().__init__(
            f"tasty auth failed phase={phase} status={status if status is not None else 'n/a'} "
            f"challenge_present={challenge_present} error={detail}"
        )


_SAFE_AUTH_ERROR_KEYS = {
    "code",
    "error-code",
    "error-description",
    "message",
    "reason",
}


def _redact_auth_secrets(value: str) -> str:
    """Remove configured tastytrade secrets from a diagnostic string."""
    redacted = value
    for name in (
        "TASTY_OAUTH_CLIENT_SECRET",
        "TASTY_OAUTH_REFRESH_TOKEN",
    ):
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(
        r"(?i)\b(token|password|otp|secret)\b\s*(?:[:=]\s*|\s+)[^\s,;]+",
        r"\1=[redacted]",
        redacted,
    )
    redacted = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[redacted]", redacted)
    return redacted[:500]


def _safe_broker_error(resp: requests.Response) -> str:
    """Extract allow-listed broker error fields without logging response bodies."""
    try:
        payload = resp.json()
    except Exception:
        return "unavailable"

    fields: list[str] = []

    def visit(value):
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key).lower().replace("_", "-")
                if key in _SAFE_AUTH_ERROR_KEYS and isinstance(child, (str, int, float, bool)):
                    fields.append(f"{key}={_redact_auth_secrets(str(child))}")
                elif key in {"error", "errors"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value[:5]:
                visit(child)

    visit(payload)
    return "; ".join(fields[:8]) or "unavailable"


def _auth_error_from_response(phase: str, resp: requests.Response) -> TastyAuthError:
    challenge_present = bool(resp.headers.get("X-Tastyworks-Challenge-Token"))
    return TastyAuthError(
        phase,
        status=resp.status_code,
        detail=_safe_broker_error(resp),
        challenge_present=challenge_present,
    )


def _bearer_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": TASTY_USER_AGENT,
    }


class OAuthTokenManager:
    """Thread-safe cache for tastytrade's short-lived OAuth access token."""

    def __init__(self, client_secret: str, refresh_token: str, scopes: str,
                 *, clock=time.time):
        if not client_secret or not refresh_token:
            raise TastyAuthError("oauth-config", detail="missing-oauth-credential")

        parsed_scopes = {
            scope for scope in re.split(r"[\s,]+", scopes.strip()) if scope
        }
        if parsed_scopes != {"read"}:
            raise TastyAuthError("oauth-config", detail="scope-must-be-exactly-read")

        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._clock = clock
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._generation = 0

    @classmethod
    def from_env(cls) -> "OAuthTokenManager":
        return cls(
            os.environ.get("TASTY_OAUTH_CLIENT_SECRET", ""),
            os.environ.get("TASTY_OAUTH_REFRESH_TOKEN", ""),
            os.environ.get("TASTY_OAUTH_SCOPES", ""),
        )

    def _token_is_fresh(self) -> bool:
        return bool(
            self._access_token
            and self._clock() < self._expires_at - OAUTH_REFRESH_BUFFER_SECS
        )

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        observed_generation = self._generation
        if not force_refresh and self._token_is_fresh():
            return self._access_token  # type: ignore[return-value]

        with self._lock:
            # A concurrent caller may have completed the needed refresh while
            # this caller waited. This second check makes refresh single-flight,
            # including concurrent force-refresh requests.
            if self._token_is_fresh() and (
                not force_refresh or self._generation != observed_generation
            ):
                return self._access_token  # type: ignore[return-value]

            try:
                resp = requests.post(
                    f"{TASTY_BASE}/oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": TASTY_USER_AGENT,
                    },
                    timeout=15,
                )
            except Exception as exc:
                raise TastyAuthError("oauth-token", detail=type(exc).__name__) from exc

            if not 200 <= resp.status_code < 300:
                raise _auth_error_from_response("oauth-token", resp)

            try:
                data = resp.json()
                access_token = data["access_token"]
                expires_in = data.get("expires_in", OAUTH_DEFAULT_LIFETIME_SECS)
                token_type = str(data.get("token_type", "Bearer"))
                if (
                    not isinstance(access_token, str)
                    or not access_token
                    or isinstance(expires_in, bool)
                    or not isinstance(expires_in, (int, float))
                    or expires_in <= OAUTH_REFRESH_BUFFER_SECS
                    or token_type.lower() != "bearer"
                ):
                    raise ValueError("invalid OAuth token response")
            except Exception as exc:
                raise TastyAuthError(
                    "oauth-token",
                    status=resp.status_code,
                    detail="malformed-success-response",
                ) from exc

            self._access_token = access_token
            self._expires_at = self._clock() + float(expires_in)
            self._generation += 1
            log.info(f"tastytrade OAuth access token refreshed; expires in {int(expires_in)}s")
            return access_token


def _get_quote_token(access_token: str) -> requests.Response:
    try:
        return requests.get(
            f"{TASTY_BASE}/api-quote-tokens",
            headers=_bearer_headers(access_token),
            timeout=10,
        )
    except Exception as exc:
        raise TastyAuthError("quote-token", detail=type(exc).__name__) from exc


def tasty_auth(token_manager: OAuthTokenManager, *, force_refresh: bool = False) -> dict:
    """Obtain OAuth API access and a DXLink quote token without legacy auth."""
    try:
        access_token = token_manager.get_access_token(force_refresh=force_refresh)
        quote_resp = _get_quote_token(access_token)

        # A cached access token can be revoked before its advertised expiry.
        # Retry exactly once with a forced refresh, then let the session-day
        # circuit breaker stop all further authentication attempts.
        if quote_resp.status_code == 401 and not force_refresh:
            access_token = token_manager.get_access_token(force_refresh=True)
            quote_resp = _get_quote_token(access_token)

        if quote_resp.status_code != 200:
            raise _auth_error_from_response("quote-token", quote_resp)

        try:
            quote_data = quote_resp.json()["data"]
            streamer_token = quote_data["token"]
            streamer_url = (
                quote_data.get("dxlink-url")
                or quote_data.get("websocket-url")
                or "wss://tasty-openapi-ws.dxfeed.com/realtime"
            )
            if not isinstance(streamer_token, str) or not streamer_token:
                raise ValueError("invalid quote token")
        except Exception as exc:
            raise TastyAuthError(
                "quote-token",
                status=quote_resp.status_code,
                detail="malformed-success-response",
            ) from exc

        log.info(f"streamer token obtained  url={streamer_url}")
        return {
            "access_token": access_token,
            "streamer_token": streamer_token,
            "streamer_url": streamer_url,
        }
    except TastyAuthError:
        raise
    except Exception as exc:
        # Unknown failures in the auth path are safety failures. Converting them
        # keeps main() from treating them as transient session errors and retrying.
        raise TastyAuthError("unexpected", detail=type(exc).__name__) from exc



__all__ = [
    "TASTY_USER_AGENT",
    "OAUTH_REFRESH_BUFFER_SECS",
    "OAUTH_DEFAULT_LIFETIME_SECS",
    "_SAFE_AUTH_ERROR_KEYS",
    "TastyAuthError",
    "_redact_auth_secrets",
    "_safe_broker_error",
    "_auth_error_from_response",
    "_bearer_headers",
    "OAuthTokenManager",
    "_get_quote_token",
    "tasty_auth",
]
