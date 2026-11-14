# backend/integrations/source_control/security/github_device_flow.py
"""
GitHub Device Flow client and session manager.

This module implements RFC 8628 (OAuth 2.0 Device Authorization Grant)
for GitHub Apps, as documented at:

    https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app

Responsibilities:
    - `GitHubDeviceFlowClient`
        Thin HTTP client for the two GitHub endpoints:
            POST https://github.com/login/device/code
            POST https://github.com/login/oauth/access_token
        It does NOT know about sessions, polling loops, or state.
        It returns raw parsed JSON and raises a typed error on
        protocol failures.
    - `DeviceFlowSession` / `DeviceFlowSessionManager`
        In-memory, TTL-bounded store for active sessions. The
        `device_code` is the ONLY secret in this module and it MUST
        never leave the process. Every method that returns a
        session-derived object returns a `DeviceFlowSessionView`
        that intentionally omits `device_code`.
    - `poll_for_token()`
        The canonical async polling loop. The backend runs this as
        an `asyncio.Task` (one per session) while the frontend
        polls `GET .../device/{session_id}/status` for the outcome.

Design rules:
    - `device_code` MUST NOT be logged, persisted, or returned to
      the caller. It only travels between
      `GitHubDeviceFlowClient.request_device_code` and
      `GitHubDeviceFlowClient.poll_for_token`.
    - `client_id` is PUBLIC (it is the app's public identifier);
      it is safe to log and to return in error details.
    - `client_secret` MUST NOT be present in this module. GitHub
      explicitly allows Device Flow token requests with only the
      client_id, and we rely on that: no secret, no private key.
    - All HTTP errors are redacted before they are wrapped in a
      `SourceControlError` (the base class redacts messages
      automatically, but we also avoid putting raw bodies into
      details).
    - The polling loop honors `interval` from the device code
      response and increases it by 5 seconds on every `slow_down`
      per RFC 8628 § 3.5, capped at a sane maximum.

Naming:
    - `DeviceFlowSessionView` is what callers see.
    - `_InternalSession` is the private record that adds
      `device_code`; it is never exposed.

Interaction with error taxonomy:
    All errors raised here are `SourceControlError` subclasses from
    `integrations.source_control.errors`, so callers can catch them
    with a single `except SourceControlError` and switch on
    `exc.code` (a `SourceControlErrorCode` enum value) rather than
    using `isinstance` chains.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import httpx

from ..domain.enums import ProviderId
from ..errors import (
    DeviceFlowDeniedError,
    DeviceFlowDisabledError,
    DeviceFlowError,
    DeviceFlowExpiredError,
    DeviceFlowPendingError,
    DeviceFlowSessionNotFoundError,
    DeviceFlowSlowDownError,
    NetworkError,
    ProviderError,
    SourceControlError,
    ValidationError,
)
from .auth_types import DeviceFlowStatus
from .redaction import redact_text

__all__ = [
    # Client
    "GitHubDeviceFlowClient",
    "DeviceCodeRequest",
    # Sessions
    "DeviceFlowSessionView",
    "DeviceFlowSessionManager",
    # Polling
    "poll_for_token",
    # Constants
    "GITHUB_DEVICE_CODE_URL",
    "GITHUB_DEVICE_TOKEN_URL",
    "GITHUB_DEVICE_VERIFICATION_URI",
    "DEVICE_FLOW_GRANT_TYPE",
    "DEVICE_FLOW_SCOPE",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SESSION_TTL_SECONDS",
    "MAX_POLL_INTERVAL_SECONDS",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: GitHub endpoint that issues a device code + user code.
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"

#: GitHub endpoint that exchanges a device code (or refreshes a
#: token) for an access token. Same URL as the Authorization Code
#: token endpoint; the `grant_type` disambiguates.
GITHUB_DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"

#: Official page where the user enters the user code. Shown in the
#: UI verbatim; we never proxy this page.
GITHUB_DEVICE_VERIFICATION_URI = "https://github.com/login/device"

#: RFC 8628 grant type for Device Flow.
DEVICE_FLOW_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

#: The ONLY scope supported by GitHub App Device Flow. Traditional
#: scopes (`repo`, `read:user`, ...) are NOT honored: permissions
#: come from the GitHub App's own configuration and the user's
#: consent. Requesting any other scope is a no-op at best and a
#: protocol error at worst. `offline_access` asks GitHub for a
#: refresh token alongside the access token.
DEVICE_FLOW_SCOPE = "offline_access"

#: Default interval (seconds) if GitHub omits it. GitHub always
#: returns one, but we keep a safe default for robustness.
DEFAULT_POLL_INTERVAL_SECONDS: int = 5

#: Default session TTL in seconds. Matches GitHub's documented
#: 15-minute device code lifetime.
DEFAULT_SESSION_TTL_SECONDS: int = 15 * 60

#: Cap for the poll interval. `slow_down` increases the interval
#: by 5 seconds per occurrence; the cap prevents a pathological
#: sequence of slow_downs from pushing the loop into multi-minute
#: sleeps that would defeat the session TTL.
MAX_POLL_INTERVAL_SECONDS: int = 60

#: HTTP timeout for a single request to GitHub. Device Flow
#: requests are small; 15 seconds is generous.
_HTTP_TIMEOUT_SECONDS: float = 15.0

#: User-Agent sent on every request. GitHub rejects requests
#: without one.
_USER_AGENT = "PELDRUN-SourceControl/1.0"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCodeRequest:
    """
    The response of `POST /login/device/code`.

    Fields:
        device_code:                Long opaque code. NEVER exposed to
                                    the frontend, NEVER logged.
        user_code:                  Short human code (e.g. WDJB-MJHT)
                                    shown to the user. Safe to expose.
        verification_uri:           URL the user opens. Always
                                    `GITHUB_DEVICE_VERIFICATION_URI`.
        expires_in:                 Lifetime of the device code in
                                    seconds (typically 900).
        interval:                   Polling interval in seconds.

    The constructor stores `device_code` as a private attribute so
    accidental `repr()` of this object does not leak it. Use
    `device_code_for_polling()` to obtain the raw value; only the
    polling loop and the client need it.
    """

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    _device_code: str = field(repr=False)

    def device_code_for_polling(self) -> str:
        """
        Return the raw device code.

        This is the ONLY accessor for `device_code`. It exists so
        that a `repr()` of a `DeviceCodeRequest` never includes the
        secret, while the polling loop can still retrieve it.
        """
        return self._device_code


class GitHubDeviceFlowClient:
    """
    Thin async HTTP client for GitHub's Device Flow endpoints.

    The client does NOT hold any state. Every call is a fresh
    `httpx.AsyncClient` request; the overhead is negligible
    compared to the network round-trip and it keeps the client
    trivially thread-safe.

    The `client_id` is passed per-call rather than stored on the
    instance so that a single client can serve multiple GitHub
    Apps (e.g. one managed and one BYOA) in the same process.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = float(timeout_seconds)
        if self._timeout <= 0:
            raise ValidationError(
                "timeout_seconds must be positive.",
                details={"timeout_seconds": self._timeout},
            )

    # --- Device code request ------------------------------------------

    async def request_device_code(
        self,
        *,
        client_id: str,
        scope: str = DEVICE_FLOW_SCOPE,
    ) -> DeviceCodeRequest:
        """
        Ask GitHub to issue a device code + user code.

        Sends:
            POST https://github.com/login/device/code
            Accept: application/json
            User-Agent: PELDRUN-SourceControl/1.0
            client_id=<public client id>
            scope=offline_access

        On success returns a `DeviceCodeRequest`.

        Raises:
            ValidationError: if `client_id` is empty.
            DeviceFlowDisabledError: if GitHub reports
                `device_flow_disabled` (a configuration problem on
                the GitHub App).
            DeviceFlowError: on any other GitHub-reported error.
            NetworkError: on transport failure or timeout.
            ProviderError: on a non-JSON response or an unexpected
                shape.
        """
        clean_client_id = (client_id or "").strip()
        if not clean_client_id:
            raise ValidationError(
                "client_id is required to request a device code."
            )

        payload = {
            "client_id": clean_client_id,
            "scope": scope,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }

        response_data = await self._post(
            GITHUB_DEVICE_CODE_URL, payload, headers
        )
        return self._parse_device_code_response(response_data)

    # --- Token poll ----------------------------------------------------

    async def poll_for_token(
        self,
        *,
        client_id: str,
        device_code: str,
    ) -> Mapping[str, Any]:
        """
        Send a single token poll request to GitHub.

        Sends:
            POST https://github.com/login/oauth/access_token
            Accept: application/json
            User-Agent: PELDRUN-SourceControl/1.0
            client_id=<public client id>
            device_code=<opaque>
            grant_type=urn:ietf:params:oauth:grant-type:device_code

        This is a SINGLE request. The polling LOOP lives in
        `poll_for_token()` (module-level function) so that callers
        can drive it themselves when needed.

        Returns the parsed JSON body on success (contains
        access_token, token_type, scope, optional refresh_token,
        expires_in, refresh_token_expires_in).

        Raises:
            DeviceFlowPendingError:  `authorization_pending`
            DeviceFlowSlowDownError: `slow_down`
            DeviceFlowDeniedError:   `access_denied`
            DeviceFlowExpiredError:  `expired_token`
            DeviceFlowDisabledError: `device_flow_disabled`
            DeviceFlowError:         any other OAuth error
            NetworkError:            on transport failure
            ProviderError:           on malformed response
        """
        clean_client_id = (client_id or "").strip()
        clean_device_code = (device_code or "").strip()
        if not clean_client_id or not clean_device_code:
            raise ValidationError(
                "client_id and device_code are required to poll for "
                "a token."
            )

        payload = {
            "client_id": clean_client_id,
            "device_code": clean_device_code,
            "grant_type": DEVICE_FLOW_GRANT_TYPE,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }

        response_data = await self._post(
            GITHUB_DEVICE_TOKEN_URL, payload, headers
        )

        # GitHub signals "still pending" / "slow down" / "denied" /
        # "expired" via an `error` field even on HTTP 200. We MUST
        # inspect it BEFORE looking for `access_token`.
        error_code = response_data.get("error")
        if error_code:
            self._raise_for_oauth_error(
                str(error_code),
                response_data.get("error_description"),
            )

        access_token = response_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError(
                "GitHub token response did not contain an "
                "access_token.",
                details={"keys": sorted(response_data.keys())},
            )

        return response_data

    # --- Internal HTTP --------------------------------------------------

    async def _post(
        self,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        """
        Send a POST and return the parsed JSON body.

        GitHub's device flow endpoints accept form-encoded bodies,
        but `httpx` will encode a `data=` dict automatically. We
        use `data=` (not `json=`) to match GitHub's documented
        examples and to keep the request compatible with the OAuth
        spec's `application/x-www-form-urlencoded` default.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout
            ) as client:
                response = await client.post(
                    url,
                    data=dict(payload),
                    headers=dict(headers),
                )
        except httpx.TimeoutException as exc:
            raise NetworkError(
                "Timed out while contacting GitHub's Device Flow "
                "endpoint.",
                details={"url": url, "timeout_seconds": self._timeout},
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(
                "Network error while contacting GitHub's Device "
                "Flow endpoint.",
                details={"url": url, "reason": type(exc).__name__},
                cause=exc,
            ) from exc

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                "GitHub returned a non-JSON response from the "
                "Device Flow endpoint.",
                details={
                    "url": url,
                    "status": response.status_code,
                },
                cause=exc,
            ) from exc

        if not isinstance(body, dict):
            raise ProviderError(
                "GitHub returned an unexpected JSON shape from the "
                "Device Flow endpoint.",
                details={
                    "url": url,
                    "status": response.status_code,
                    "type": type(body).__name__,
                },
            )

        # Non-2xx with a JSON body still carries the OAuth error
        # envelope; let the caller's `error` handling take over.
        if response.status_code >= 400 and "error" not in body:
            raise ProviderError(
                f"GitHub Device Flow endpoint returned HTTP "
                f"{response.status_code} without an error envelope.",
                details={
                    "url": url,
                    "status": response.status_code,
                    "keys": sorted(body.keys()),
                },
            )

        return body

    # --- Internal parsing ----------------------------------------------

    @staticmethod
    def _parse_device_code_response(
        data: Mapping[str, Any],
    ) -> DeviceCodeRequest:
        """Validate and normalize the device-code response."""
        # GitHub returns `error` on this endpoint too (e.g. when
        # Device Flow is disabled for the app). Inspect it first.
        error_code = data.get("error")
        if error_code:
            GitHubDeviceFlowClient._raise_for_oauth_error(
                str(error_code),
                data.get("error_description"),
            )

        device_code = data.get("device_code")
        user_code = data.get("user_code")
        verification_uri = data.get("verification_uri")
        expires_in = data.get("expires_in")
        interval = data.get("interval", DEFAULT_POLL_INTERVAL_SECONDS)

        if not isinstance(device_code, str) or not device_code:
            raise ProviderError(
                "GitHub device-code response did not contain a "
                "device_code.",
                details={"keys": sorted(data.keys())},
            )
        if not isinstance(user_code, str) or not user_code:
            raise ProviderError(
                "GitHub device-code response did not contain a "
                "user_code.",
                details={"keys": sorted(data.keys())},
            )
        if not isinstance(verification_uri, str) or not verification_uri:
            raise ProviderError(
                "GitHub device-code response did not contain a "
                "verification_uri.",
                details={"keys": sorted(data.keys())},
            )

        try:
            expires_in_int = int(expires_in)
        except (TypeError, ValueError):
            expires_in_int = DEFAULT_SESSION_TTL_SECONDS
        if expires_in_int <= 0:
            expires_in_int = DEFAULT_SESSION_TTL_SECONDS

        try:
            interval_int = int(interval)
        except (TypeError, ValueError):
            interval_int = DEFAULT_POLL_INTERVAL_SECONDS
        if interval_int <= 0:
            interval_int = DEFAULT_POLL_INTERVAL_SECONDS

        return DeviceCodeRequest(
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in=expires_in_int,
            interval=interval_int,
            _device_code=device_code,
        )

    @staticmethod
    def _raise_for_oauth_error(
        error_code: str,
        error_description: Any,
    ) -> None:
        """
        Map a GitHub OAuth error code to the typed exception.

        Unknown codes surface as a plain `DeviceFlowError` carrying
        the code and a redacted description; they are NOT mapped to
        `authorization_pending` (which would silently retry a
        genuinely broken flow) and NOT mapped to a terminal error
        (which would hide a protocol extension).
        """
        description = (
            redact_text(str(error_description))
            if error_description
            else None
        )
        details: Dict[str, Any] = {"github_error": error_code}
        if description:
            details["github_error_description"] = description

        if error_code == "authorization_pending":
            raise DeviceFlowPendingError(
                "GitHub is waiting for the user to approve the "
                "device authorization.",
                details=details,
            )
        if error_code == "slow_down":
            raise DeviceFlowSlowDownError(
                "GitHub asked the client to slow down polling.",
                details=details,
            )
        if error_code == "access_denied":
            raise DeviceFlowDeniedError(
                "The user denied the device authorization request.",
                details=details,
            )
        if error_code == "expired_token":
            raise DeviceFlowExpiredError(
                "The device code has expired.",
                details=details,
            )
        if error_code == "device_flow_disabled":
            raise DeviceFlowDisabledError(
                "Device Flow is disabled for this GitHub App. An "
                "operator must enable it in the GitHub App settings.",
                details=details,
            )
        if error_code in {
            "incorrect_client_credentials",
            "invalid_client",
        }:
            # Configuration error on our side: the client_id is
            # wrong or the app is suspended. Treated as a
            # non-retryable flow error.
            raise DeviceFlowError(
                f"GitHub rejected the client_id ({error_code}).",
                details=details,
            )
        if error_code == "unsupported_grant_type":
            raise DeviceFlowError(
                "GitHub rejected the device code grant type.",
                details=details,
            )

        # Unknown OAuth error. Surface it verbatim (redacted) so an
        # operator can investigate; do NOT map it to a retryable or
        # terminal class blindly.
        raise DeviceFlowError(
            f"GitHub returned an unknown Device Flow error "
            f"('{error_code}').",
            details=details,
        )


# ---------------------------------------------------------------------------
# Session view and internal session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceFlowSessionView:
    """
    Public view of a Device Flow session.

    This is the ONLY representation callers may see. It omits
    `device_code` entirely; the raw value stays inside
    `_InternalSession`.
    """

    session_id: str
    provider: ProviderId
    user_code: str
    verification_uri: str
    expires_at: datetime
    interval: int
    status: DeviceFlowStatus
    account_id: Optional[str] = None
    username: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def expires_in_seconds(self) -> int:
        """Seconds remaining until the session expires (>= 0)."""
        remaining = (
            self.expires_at - datetime.now(timezone.utc)
        ).total_seconds()
        return max(0, int(remaining))


@dataclass(frozen=True)
class _InternalSession:
    """
    Private session record.

    `device_code` is the only secret in this module. It MUST never
    be returned to a caller, persisted, or logged. The `repr`
    deliberately excludes it.
    """

    session_id: str
    provider: ProviderId
    user_code: str
    verification_uri: str
    expires_at: datetime
    interval: int
    status: DeviceFlowStatus
    device_code: str = field(repr=False)
    account_id: Optional[str] = None
    username: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    #: The asyncio.Task that is currently polling GitHub for this
    #: session, if any. Stored as an object so the manager can
    #: cancel it deterministically on `cancel()` or on eviction.
    polling_task: Optional[asyncio.Task] = field(
        default=None, repr=False, compare=False
    )
    #: Monotonic deadline used by the polling loop to bound the
    #: total time spent waiting. Derived from `expires_at`.
    deadline_monotonic: float = field(default=0.0, repr=False)

    def to_view(self) -> DeviceFlowSessionView:
        return DeviceFlowSessionView(
            session_id=self.session_id,
            provider=self.provider,
            user_code=self.user_code,
            verification_uri=self.verification_uri,
            expires_at=self.expires_at,
            interval=self.interval,
            status=self.status,
            account_id=self.account_id,
            username=self.username,
            error_message=self.error_message,
        )


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class DeviceFlowSessionManager:
    """
    In-memory, TTL-bounded store for Device Flow sessions.

    Thread-safe. The lock covers every mutation; reads also take
    the lock to return a consistent snapshot, but the work under
    the lock is O(1) (dict lookup).

    A `max_sessions` cap is enforced on insert: the oldest PENDING
    session is evicted if the cap would be exceeded. Terminal
    sessions are pruned automatically on every insert and on every
    `prune_expired()` call, so a burst of abandoned sessions does
    not fill the store.
    """

    def __init__(self, max_sessions: int = 32) -> None:
        if max_sessions <= 0:
            raise ValidationError(
                "max_sessions must be positive.",
                details={"max_sessions": max_sessions},
            )
        self._max_sessions = int(max_sessions)
        self._sessions: Dict[str, _InternalSession] = {}
        self._lock = threading.RLock()

    # --- Creation -------------------------------------------------------

    def create(
        self,
        *,
        provider: ProviderId,
        request: DeviceCodeRequest,
    ) -> DeviceFlowSessionView:
        """
        Create a new session from a device-code response.

        The `session_id` is a strong random hex string, independent
        from `user_code` and `device_code`. The frontend uses it
        to poll; it never sees the raw codes.
        """
        self._prune_locked()

        if len(self._sessions) >= self._max_sessions:
            # Evict the oldest PENDING session. Terminal sessions
            # were already pruned by `_prune_locked`.
            oldest_id = min(
                self._sessions,
                key=lambda k: self._sessions[k].created_at,
            )
            evicted = self._sessions.pop(oldest_id, None)
            self._cancel_task(evicted)
            logger.warning(
                "DeviceFlowSessionManager: evicted session '%s' "
                "to stay under max_sessions=%d.",
                oldest_id,
                self._max_sessions,
            )

        session_id = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=request.expires_in)
        deadline_monotonic = (
            time.monotonic() + max(1, request.expires_in)
        )

        session = _InternalSession(
            session_id=session_id,
            provider=provider,
            user_code=request.user_code,
            verification_uri=request.verification_uri,
            expires_at=expires_at,
            interval=request.interval,
            status=DeviceFlowStatus.PENDING,
            device_code=request.device_code_for_polling(),
            created_at=now,
            deadline_monotonic=deadline_monotonic,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session.to_view()

    # --- Lookups --------------------------------------------------------

    def get_view(
        self, session_id: str
    ) -> DeviceFlowSessionView:
        """
        Return the public view of a session.

        Raises:
            DeviceFlowSessionNotFoundError: if the ID is unknown.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise DeviceFlowSessionNotFoundError(
                    f"Device Flow session '{session_id}' was not "
                    f"found. It may have expired or been cancelled.",
                    details={"session_id": session_id},
                )
            return session.to_view()

    # --- Internal accessors for the polling loop ----------------------

    def internal_get(
        self, session_id: str
    ) -> Optional[_InternalSession]:
        """
        Return the internal session record (with `device_code`).

        This is PRIVATE. Only `poll_for_token()` and the auth
        service may call it. Returning `None` for an unknown ID
        lets the polling loop exit cleanly if the session was
        cancelled or pruned while it was running.
        """
        with self._lock:
            return self._sessions.get(session_id)

    def internal_update(
        self,
        session_id: str,
        *,
        status: Optional[DeviceFlowStatus] = None,
        interval: Optional[int] = None,
        account_id: Optional[str] = None,
        username: Optional[str] = None,
        error_message: Optional[str] = None,
        polling_task: Optional[asyncio.Task] = None,
        clear_polling_task: bool = False,
    ) -> Optional[DeviceFlowSessionView]:
        """
        Update fields of an existing session atomically.

        Returns the updated view, or `None` if the session no
        longer exists. A `None` return lets the polling loop exit
        without raising when the session was cancelled concurrently.
        """
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                return None
            updates: Dict[str, Any] = {}
            if status is not None:
                updates["status"] = status
            if interval is not None:
                updates["interval"] = interval
            if account_id is not None:
                updates["account_id"] = account_id
            if username is not None:
                updates["username"] = username
            if error_message is not None:
                updates["error_message"] = error_message
            if clear_polling_task:
                updates["polling_task"] = None
            elif polling_task is not None:
                updates["polling_task"] = polling_task
            if not updates:
                return current.to_view()
            # Frozen dataclass: build a new instance via replace.
            from dataclasses import replace as _replace

            updated = _replace(current, **updates)
            self._sessions[session_id] = updated
            return updated.to_view()

    # --- Cancellation ---------------------------------------------------

    def cancel(self, session_id: str) -> DeviceFlowSessionView:
        """
        Cancel a PENDING session.

        If the session was already terminal, the call is a no-op
        and returns the terminal view. If the session was PENDING,
        its status becomes `CANCELLED` and any polling task is
        cancelled deterministically.

        Raises:
            DeviceFlowSessionNotFoundError: if the ID is unknown.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise DeviceFlowSessionNotFoundError(
                    f"Device Flow session '{session_id}' was not "
                    f"found.",
                    details={"session_id": session_id},
                )
            if session.status != DeviceFlowStatus.PENDING:
                return session.to_view()

            self._cancel_task(session)
            from dataclasses import replace as _replace

            updated = _replace(
                session,
                status=DeviceFlowStatus.CANCELLED,
                polling_task=None,
            )
            self._sessions[session_id] = updated
            return updated.to_view()

    # --- Pruning --------------------------------------------------------

    def prune_expired(self) -> int:
        """Remove every terminal or expired session. Returns the count."""
        with self._lock:
            return self._prune_locked()

    def _prune_locked(self) -> int:
        """
        Prune terminal and expired sessions.

        Called under the lock from `create()` and `prune_expired()`.
        Also cancels any still-running polling task whose session
        is being removed, so a task cannot outlive its session.
        """
        now = datetime.now(timezone.utc)
        to_remove = []
        for session_id, session in self._sessions.items():
            if session.status.is_terminal:
                to_remove.append(session_id)
                continue
            if session.expires_at <= now:
                to_remove.append(session_id)
        for session_id in to_remove:
            evicted = self._sessions.pop(session_id, None)
            self._cancel_task(evicted)
        return len(to_remove)

    # --- Task management ------------------------------------------------

    @staticmethod
    def _cancel_task(session: Optional[_InternalSession]) -> None:
        """
        Cancel the session's polling task, if any.

        Best-effort: a task that has already completed, or one
        whose loop has already exited, is left alone.
        """
        if session is None:
            return
        task = session.polling_task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass

    # --- Diagnostics ----------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


async def poll_for_token(
    *,
    client: GitHubDeviceFlowClient,
    session_manager: DeviceFlowSessionManager,
    session_id: str,
    client_id: str,
    on_completed: Optional[Any] = None,
) -> None:
    """
    Drive a Device Flow session from PENDING to a terminal state.

    This function is designed to run as an `asyncio.Task`. It:

        1. Reads the session's `device_code`, `interval`, and
           deadline from the session manager (the only place the
           raw device code is accessible).
        2. Loops until the session reaches a terminal state, the
           deadline elapses, or the caller cancels the task.
        3. On each iteration, calls `client.poll_for_token`. It
           maps the resulting `authorization_pending` /
           `slow_down` / `access_denied` / `expired_token` /
           `device_flow_disabled` responses to updates on the
           session's status.
        4. On success, invokes `on_completed(view, token_payload)`
           so the auth service can persist the token and update the
           connection record. The function itself does NOT touch
           the vault or the facade.

    The loop always exits with the session in a terminal state
    (`COMPLETED`, `DENIED`, `EXPIRED`, `CANCELLED`, or `ERROR`).
    A `CancelledError` from the task manager is caught and
    reflected as `CANCELLED` on the session.

    `on_completed` is optional: when `None`, the function only
    updates the session status and the caller is expected to poll
    for `COMPLETED` and retrieve the token itself. This is the
    mode used when the token payload MUST NOT leave the vault: the
    auth service will call `on_completed` explicitly to perform
    the persistence before the status becomes visible to the
    frontend.
    """
    session = session_manager.internal_get(session_id)
    if session is None:
        # The session was pruned between `create()` and the task
        # being scheduled. Nothing to do.
        return

    # Register the current task so `cancel()` can cancel it.
    current_task = asyncio.current_task()
    if current_task is not None:
        session_manager.internal_update(
            session_id, polling_task=current_task
        )

    interval = max(1, session.interval)
    deadline = session.deadline_monotonic

    while True:
        # Check for caller-driven cancellation. The task's
        # `cancelled` state is set by `cancel()`.
        live = session_manager.internal_get(session_id)
        if live is None:
            return
        if live.status.is_terminal:
            return
        if live.status == DeviceFlowStatus.CANCELLED:
            return

        # Check for local expiry. The deadline is monotonic, so a
        # system clock change does not affect it.
        if time.monotonic() >= deadline:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.EXPIRED,
                error_message=(
                    "The device authorization session expired "
                    "before the user approved it."
                ),
                clear_polling_task=True,
            )
            return

        # Sleep BEFORE the first poll. GitHub recommends this:
        # polling immediately returns `authorization_pending` in
        # virtually every case and contributes to secondary rate
        # limiting.
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.CANCELLED,
                clear_polling_task=True,
            )
            return

        try:
            token_payload = await client.poll_for_token(
                client_id=client_id,
                device_code=session.device_code,
            )
        except DeviceFlowPendingError:
            # Normal state; keep polling.
            continue
        except DeviceFlowSlowDownError:
            # Per RFC 8628 § 3.5, add 5 seconds to the interval.
            interval = min(
                interval + 5, MAX_POLL_INTERVAL_SECONDS
            )
            session_manager.internal_update(
                session_id, interval=interval
            )
            logger.info(
                "DeviceFlow: GitHub asked to slow down; interval "
                "is now %d seconds.",
                interval,
            )
            continue
        except DeviceFlowDeniedError as exc:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.DENIED,
                error_message=str(exc),
                clear_polling_task=True,
            )
            return
        except DeviceFlowExpiredError as exc:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.EXPIRED,
                error_message=str(exc),
                clear_polling_task=True,
            )
            return
        except DeviceFlowDisabledError as exc:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.ERROR,
                error_message=str(exc),
                clear_polling_task=True,
            )
            return
        except SourceControlError as exc:
            # Any other typed error (including DeviceFlowError for
            # unknown GitHub codes) is terminal: retrying would
            # spam GitHub without any chance of success.
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.ERROR,
                error_message=str(exc),
                clear_polling_task=True,
            )
            return
        except asyncio.CancelledError:
            session_manager.internal_update(
                session_id,
                status=DeviceFlowStatus.CANCELLED,
                clear_polling_task=True,
            )
            return

        # Success. Hand the payload to the completion hook BEFORE
        # updating the status to COMPLETED, so the frontend never
        # observes COMPLETED before the token has been persisted.
        if on_completed is not None:
            try:
                await on_completed(
                    session_manager.get_view(session_id),
                    token_payload,
                )
            except Exception as exc:  # noqa: BLE001
                session_manager.internal_update(
                    session_id,
                    status=DeviceFlowStatus.ERROR,
                    error_message=(
                        f"Failed to persist the token: "
                        f"{type(exc).__name__}"
                    ),
                    clear_polling_task=True,
                )
                return

        session_manager.internal_update(
            session_id,
            status=DeviceFlowStatus.COMPLETED,
            clear_polling_task=True,
        )
        return


# ---------------------------------------------------------------------------
# Fingerprint helper (used in audit events)
# ---------------------------------------------------------------------------


def fingerprint(value: str) -> str:
    """
    Return a short, non-reversible fingerprint of a secret.

    Used to correlate audit events without storing the raw value.
    The full SHA-256 is overkill for correlation; 12 hex chars
    (~48 bits) is sufficient and keeps log lines short.
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
