# backend/integrations/source_control/errors.py
"""
Error taxonomy for the Source Control Integration subsystem.

All exceptions raised by the subsystem MUST inherit from
`SourceControlError`. This guarantees that callers can:

    - Catch every subsystem error with a single `except` clause.
    - Map every error to a stable `SourceControlErrorCode` for API
      responses, audit logs, and agent tool results.
    - Detect retryable errors via the `is_retryable` property.
    - Obtain a redacted, human-readable message that never leaks
      tokens, credentials, or provider-internal identifiers.

Design rules:
    - Exceptions MUST NOT carry secrets in `details`.
    - Messages MUST be redacted before being surfaced to the user or
      the model. The `redact_message` helper is invoked automatically
      in the constructor.
    - The error hierarchy is intentionally shallow (2 levels) to keep
      `except` clauses predictable.
    - The `code` attribute is a `SourceControlErrorCode` enum member,
      not a string, so that HTTP routers can switch on it safely.

Backward-compatibility aliases are provided at the bottom of the module
so that callers using the older `Authentication*` and `Rate*` names
continue to work. These names appear in the HTTP layer, in the schemas,
and in the GitHub adapter's error translation.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .domain.enums import SourceControlErrorCode

__all__ = [
    # Base
    "SourceControlError",
    # Authentication / authorization
    "AuthRequiredError",
    "AuthExpiredError",
    "PermissionDeniedError",
    # Resource existence
    "NotFoundError",
    # Rate limiting and transport
    "RateLimitError",
    "NetworkError",
    "ProviderError",
    # State and validation
    "ConflictError",
    "ValidationError",
    "RemoteStateChangedError",
    "UnsupportedCapabilityError",
    # Local workspace and Git
    "WorkspaceNotFoundError",
    "WorkspaceDirtyError",
    "PathTraversalError",
    "CommandTimeoutError",
    "CommandFailedError",
    # Approval and policy
    "ApprovalRequiredError",
    "ApprovalInvalidError",
    "ApprovalExpiredError",
    "ApprovalConsumedError",
    "PolicyDeniedError",
    "ProtectedBranchError",
    "WorkflowFileError",
    # Source Control authentication (Managed + BYOA + Device Flow)
    "AuthDisabledError",
    "AuthSourceDisabledError",
    "UnsupportedAuthSourceError",
    "DeviceFlowError",
    "DeviceFlowDisabledError",
    "DeviceFlowPendingError",
    "DeviceFlowSlowDownError",
    "DeviceFlowDeniedError",
    "DeviceFlowExpiredError",
    "DeviceFlowSessionNotFoundError",
    # Helpers
    "to_source_control_error",
    "error_code_of",
    "is_retryable_error",
    "redact_message",
    # Backward-compatibility aliases
    "AuthenticationRequiredError",
    "AuthenticationExpiredError",
    "RateLimitedError",
    "RateLimitExceededError",
]


# ---------------------------------------------------------------------------
# Message redaction
# ---------------------------------------------------------------------------


# Patterns applied to every error message before it is exposed to the
# outside world. Each pattern targets a specific class of secret. The
# list is intentionally conservative: it errs on the side of redacting
# too much rather than leaking a credential.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,255}\b"),
    # GitLab tokens
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,255}\b"),
    # Bitbucket app passwords / tokens
    re.compile(r"\bATBB[A-Za-z0-9]{16,}\b"),
    # Generic bearer tokens in Authorization headers
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{20,}"),
    # Basic auth
    re.compile(r"(?i)authorization\s*:\s*basic\s+[A-Za-z0-9+/=]{8,}"),
    # URLs with embedded credentials: scheme://user:pass@host
    re.compile(r"([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"),
    # Private key markers
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    # OAuth client secrets in query strings
    re.compile(r"(?i)(client_secret=)[^&\s]+"),
    re.compile(r"(?i)(code=)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(refresh_token=)[^&\s]+"),
    re.compile(r"(?i)(access_token=)[^&\s]+"),
)


def redact_message(message: str) -> str:
    """
    Return `message` with any recognizable secret replaced by
    `***REDACTED***`.

    The function is idempotent and safe to call multiple times. It is
    applied automatically in `SourceControlError.__init__`, but callers
    may also invoke it directly before logging.
    """
    if not message:
        return message
    redacted = message
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match[str]) -> str:
    """
    Redaction callback that preserves non-secret prefixes when the
    pattern captures them (e.g. `client_secret=`).
    """
    if match.lastindex and match.lastindex >= 1:
        prefix = match.group(1)
        return f"{prefix}***REDACTED***"
    return "***REDACTED***"


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------


class SourceControlError(Exception):
    """
    Base class for every error raised by the subsystem.

    Attributes:
        code:         Stable error category from `SourceControlErrorCode`.
        message:      Redacted, human-readable message.
        details:      Immutable mapping of non-secret context.
        retryable:    Whether the operation may be retried safely.
        http_status:  Suggested HTTP status code for API responses.
    """

    #: Overridden by subclasses to declare the error category.
    default_code: SourceControlErrorCode = SourceControlErrorCode.PROVIDER_ERROR

    #: Overridden by subclasses to declare retry semantics.
    default_retryable: bool = False

    #: Overridden by subclasses to declare an HTTP status hint.
    default_http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[SourceControlErrorCode] = None,
        details: Optional[Mapping[str, Any]] = None,
        retryable: Optional[bool] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        safe_message = redact_message(message)
        super().__init__(safe_message)
        self.message = safe_message
        self.code = code or self.default_code
        self.details: Mapping[str, Any] = dict(details or {})
        self.retryable = (
            self.default_retryable if retryable is None else retryable
        )
        self.http_status = self.default_http_status
        if cause is not None:
            self.__cause__ = cause

    @property
    def is_retryable(self) -> bool:
        """Return True if the caller may retry the operation safely."""
        return bool(self.retryable)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the error for API responses and audit logs.

        The returned mapping is safe to log and to send to the
        frontend: `message` is already redacted, and `details` MUST
        NOT contain secrets by contract.
        """
        return {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"[{self.code.value}] {self.message}"


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------


class AuthRequiredError(SourceControlError):
    """Raised when no credential is present for the requested action."""

    default_code = SourceControlErrorCode.AUTH_REQUIRED
    default_retryable = False
    default_http_status = 401


class AuthExpiredError(SourceControlError):
    """Raised when the current credential has expired."""

    default_code = SourceControlErrorCode.AUTH_EXPIRED
    default_retryable = False
    default_http_status = 401


class PermissionDeniedError(SourceControlError):
    """Raised when the credential lacks the required permission."""

    default_code = SourceControlErrorCode.PERMISSION_DENIED
    default_retryable = False
    default_http_status = 403


# ---------------------------------------------------------------------------
# Resource existence
# ---------------------------------------------------------------------------


class NotFoundError(SourceControlError):
    """Raised when a repository, branch, or file cannot be found."""

    default_code = SourceControlErrorCode.NOT_FOUND
    default_retryable = False
    default_http_status = 404


# ---------------------------------------------------------------------------
# Rate limiting and transport
# ---------------------------------------------------------------------------


class RateLimitError(SourceControlError):
    """
    Raised when the provider rate limit has been exceeded.

    `details["retry_after_seconds"]` MAY carry the server's suggested
    backoff. Callers SHOULD respect it before retrying.
    """

    default_code = SourceControlErrorCode.RATE_LIMITED
    default_retryable = True
    default_http_status = 429


class NetworkError(SourceControlError):
    """Raised on transient transport failures (timeouts, DNS, TLS)."""

    default_code = SourceControlErrorCode.NETWORK_ERROR
    default_retryable = True
    default_http_status = 502


class ProviderError(SourceControlError):
    """
    Raised when the provider returns an unexpected error that does not
    map cleanly to any other category.
    """

    default_code = SourceControlErrorCode.PROVIDER_ERROR
    default_retryable = False
    default_http_status = 502


# ---------------------------------------------------------------------------
# State and validation
# ---------------------------------------------------------------------------


class ConflictError(SourceControlError):
    """Raised when the local or remote state conflicts with the request."""

    default_code = SourceControlErrorCode.CONFLICT
    default_retryable = False
    default_http_status = 409


class ValidationError(SourceControlError):
    """
    Raised when input fails validation before any side effect occurs.

    Note: this class is intentionally named `ValidationError` (not
    `SourceControlValidationError`) because it is referenced by that
    name across the subsystem and the test suite.
    """

    default_code = SourceControlErrorCode.VALIDATION_ERROR
    default_retryable = False
    default_http_status = 422


class RemoteStateChangedError(SourceControlError):
    """
    Raised when the remote branch or base commit changed between
    approval and execution. Always invalidates the approval ticket.
    """

    default_code = SourceControlErrorCode.REMOTE_STATE_CHANGED
    default_retryable = False
    default_http_status = 409


class UnsupportedCapabilityError(SourceControlError):
    """Raised when a provider does not support the requested operation."""

    default_code = SourceControlErrorCode.UNSUPPORTED_CAPABILITY
    default_retryable = False
    default_http_status = 501


# ---------------------------------------------------------------------------
# Local workspace and Git
# ---------------------------------------------------------------------------


class WorkspaceNotFoundError(SourceControlError):
    """Raised when a workspace ID does not resolve to a known workspace."""

    default_code = SourceControlErrorCode.WORKSPACE_NOT_FOUND
    default_retryable = False
    default_http_status = 404


class WorkspaceDirtyError(SourceControlError):
    """
    Raised when an operation requires a clean workspace but uncommitted
    changes are present. Never discards user changes automatically.
    """

    default_code = SourceControlErrorCode.WORKSPACE_DIRTY
    default_retryable = False
    default_http_status = 409


class PathTraversalError(SourceControlError):
    """Raised when a path escapes the configured workspace root."""

    default_code = SourceControlErrorCode.PATH_TRAVERSAL_DETECTED
    default_retryable = False
    default_http_status = 400


class CommandTimeoutError(SourceControlError):
    """Raised when a Git subprocess exceeds its allowed runtime."""

    default_code = SourceControlErrorCode.COMMAND_TIMEOUT
    default_retryable = True
    default_http_status = 504


class CommandFailedError(SourceControlError):
    """
    Raised when a Git subprocess exits with a non-zero status for a
    reason that is not covered by a more specific error class.
    """

    default_code = SourceControlErrorCode.COMMAND_FAILED
    default_retryable = False
    default_http_status = 500


# ---------------------------------------------------------------------------
# Approval and policy
# ---------------------------------------------------------------------------


class ApprovalRequiredError(SourceControlError):
    """Raised when an operation requires an approval that is missing."""

    default_code = SourceControlErrorCode.APPROVAL_REQUIRED
    default_retryable = False
    default_http_status = 428  # Precondition Required


class ApprovalInvalidError(SourceControlError):
    """
    Raised when an approval does not match the current operation
    snapshot (e.g. diff changed after approval).
    """

    default_code = SourceControlErrorCode.APPROVAL_INVALID
    default_retryable = False
    default_http_status = 409


class ApprovalExpiredError(SourceControlError):
    """Raised when an approval ticket has passed its expiry time."""

    default_code = SourceControlErrorCode.APPROVAL_EXPIRED
    default_retryable = False
    default_http_status = 410  # Gone


class ApprovalConsumedError(SourceControlError):
    """Raised when an approval ticket has already been consumed."""

    default_code = SourceControlErrorCode.APPROVAL_CONSUMED
    default_retryable = False
    default_http_status = 409


class PolicyDeniedError(SourceControlError):
    """Raised when the active policy forbids the requested operation."""

    default_code = SourceControlErrorCode.POLICY_DENIED
    default_retryable = False
    default_http_status = 403


class ProtectedBranchError(SourceControlError):
    """Raised when a push targets a protected branch without approval."""

    default_code = SourceControlErrorCode.PROTECTED_BRANCH
    default_retryable = False
    default_http_status = 403


class WorkflowFileError(SourceControlError):
    """
    Raised when a change modifies a workflow/CI file and the required
    elevated approval is missing.
    """

    default_code = SourceControlErrorCode.WORKFLOW_FILE_CHANGE
    default_retryable = False
    default_http_status = 428


# ---------------------------------------------------------------------------
# Source Control authentication (Managed GitHub App + BYOA + Device Flow)
# ---------------------------------------------------------------------------
#
# These classes cover three distinct failure modes that must remain
# distinguishable both for the UI and for the polling loop:
#
#   1. Configuration states (`AuthDisabledError`,
#      `AuthSourceDisabledError`, `UnsupportedAuthSourceError`):
#      the user's request cannot even begin because the relevant
#      settings forbid it, or the caller requested an invalid
#      strategy. None of these is a credential failure.
#
#   2. Protocol errors (`DeviceFlowError` and its subclasses): the
#      Device Flow handshake itself failed, or returned a
#      GitHub-defined error code that maps to a specific
#      user-visible outcome (`access_denied`, `expired_token`,
#      `slow_down`, `device_flow_disabled`).
#
#   3. Session errors (`DeviceFlowSessionNotFoundError`): the
#      caller referenced a session that is not (or no longer) in
#      the in-memory store. This is a caller-bookkeeping error,
#      not a protocol failure.


class AuthDisabledError(SourceControlError):
    """
    Raised when the entire Source Control authentication subsystem
    is disabled by settings.

    Distinct from `AuthSourceDisabledError`: this error means NO
    authentication is possible at all (neither managed nor byoa),
    while the source-specific error means only that one strategy is
    disabled and the other may still be usable.
    """

    default_code = SourceControlErrorCode.AUTH_DISABLED
    default_retryable = False
    default_http_status = 403


class AuthSourceDisabledError(SourceControlError):
    """
    Raised when a caller requests a specific auth source that is
    disabled in settings.

    The error message names the source so that the UI can render a
    targeted message. No automatic fallback is performed: if the
    user selected MANAGED and it is disabled, the flow fails here
    rather than silently using BYOA.
    """

    default_code = SourceControlErrorCode.AUTH_SOURCE_DISABLED
    default_retryable = False
    default_http_status = 403


class UnsupportedAuthSourceError(SourceControlError):
    """
    Raised when the requested source is not a selectable strategy.

    The canonical case is `AuthSource.LEGACY_UNKNOWN`: it may appear
    on an existing connection record, but it can NEVER be used to
    start a new flow.
    """

    default_code = SourceControlErrorCode.UNSUPPORTED_AUTH_SOURCE
    default_retryable = False
    default_http_status = 400


class DeviceFlowError(SourceControlError):
    """
    Base class for Device Flow protocol errors.

    Subclasses cover the specific GitHub error codes. A plain
    `DeviceFlowError` is raised only for unexpected conditions that
    do not match any known code.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_ERROR
    default_retryable = False
    default_http_status = 502


class DeviceFlowDisabledError(DeviceFlowError):
    """
    Raised when GitHub reports `device_flow_disabled`.

    This means the GitHub App itself does not have Device Flow
    enabled in its settings. It is a configuration error on the
    PELDRUN operator side, NOT a transient failure: retrying does
    not help. The UI must surface an actionable message that points
    the operator at the GitHub App settings page.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_DISABLED
    default_retryable = False
    default_http_status = 503


class DeviceFlowPendingError(DeviceFlowError):
    """
    Raised when GitHub returns `authorization_pending`.

    This is the NORMAL response while the user has not yet approved
    the request on GitHub. The polling loop treats it as a signal
    to sleep and retry, not as a failure.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_PENDING
    default_retryable = True
    default_http_status = 202


class DeviceFlowSlowDownError(DeviceFlowError):
    """
    Raised when GitHub returns `slow_down`.

    Per RFC 8628 and GitHub's documentation, the polling interval
    MUST be increased by 5 seconds before the next attempt. The
    polling loop implements this; the error is exposed so callers
    that implement their own loop can honor the same rule.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_SLOW_DOWN
    default_retryable = True
    default_http_status = 429


class DeviceFlowDeniedError(DeviceFlowError):
    """
    Raised when GitHub returns `access_denied`.

    The user explicitly rejected the authorization request on
    GitHub. The session is terminal and the user code MUST NOT be
    reused.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_DENIED
    default_retryable = False
    default_http_status = 403


class DeviceFlowExpiredError(DeviceFlowError):
    """
    Raised when GitHub returns `expired_token`, or when the local
    session TTL elapses before GitHub reports completion.

    The user code is no longer valid. The user must restart the
    flow to obtain a fresh code.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_EXPIRED
    default_retryable = False
    default_http_status = 410


class DeviceFlowSessionNotFoundError(SourceControlError):
    """
    Raised when a caller references a Device Flow session ID that
    is not (or no longer) in the in-memory store.

    The canonical causes are: a frontend that polled after the
    session was cleaned up, a backend restart (sessions are
    deliberately not persisted), or a typo in the session ID.
    """

    default_code = SourceControlErrorCode.DEVICE_FLOW_SESSION_NOT_FOUND
    default_retryable = False
    default_http_status = 404


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


# Mapping from `SourceControlErrorCode` to the corresponding class. Used
# by `to_source_control_error` to preserve the original category when
# wrapping foreign exceptions.
_CODE_TO_CLASS: dict[SourceControlErrorCode, type[SourceControlError]] = {
    SourceControlErrorCode.AUTH_REQUIRED: AuthRequiredError,
    SourceControlErrorCode.AUTH_EXPIRED: AuthExpiredError,
    SourceControlErrorCode.PERMISSION_DENIED: PermissionDeniedError,
    SourceControlErrorCode.NOT_FOUND: NotFoundError,
    SourceControlErrorCode.RATE_LIMITED: RateLimitError,
    SourceControlErrorCode.NETWORK_ERROR: NetworkError,
    SourceControlErrorCode.CONFLICT: ConflictError,
    SourceControlErrorCode.VALIDATION_ERROR: ValidationError,
    SourceControlErrorCode.REMOTE_STATE_CHANGED: RemoteStateChangedError,
    SourceControlErrorCode.UNSUPPORTED_CAPABILITY: UnsupportedCapabilityError,
    SourceControlErrorCode.PROVIDER_ERROR: ProviderError,
    SourceControlErrorCode.WORKSPACE_NOT_FOUND: WorkspaceNotFoundError,
    SourceControlErrorCode.WORKSPACE_DIRTY: WorkspaceDirtyError,
    SourceControlErrorCode.PATH_TRAVERSAL_DETECTED: PathTraversalError,
    SourceControlErrorCode.COMMAND_TIMEOUT: CommandTimeoutError,
    SourceControlErrorCode.COMMAND_FAILED: CommandFailedError,
    SourceControlErrorCode.APPROVAL_REQUIRED: ApprovalRequiredError,
    SourceControlErrorCode.APPROVAL_INVALID: ApprovalInvalidError,
    SourceControlErrorCode.APPROVAL_EXPIRED: ApprovalExpiredError,
    SourceControlErrorCode.APPROVAL_CONSUMED: ApprovalConsumedError,
    SourceControlErrorCode.POLICY_DENIED: PolicyDeniedError,
    SourceControlErrorCode.PROTECTED_BRANCH: ProtectedBranchError,
    SourceControlErrorCode.WORKFLOW_FILE_CHANGE: WorkflowFileError,
    # Source Control authentication (Managed + BYOA + Device Flow)
    SourceControlErrorCode.AUTH_DISABLED: AuthDisabledError,
    SourceControlErrorCode.AUTH_SOURCE_DISABLED: AuthSourceDisabledError,
    SourceControlErrorCode.UNSUPPORTED_AUTH_SOURCE: UnsupportedAuthSourceError,
    SourceControlErrorCode.DEVICE_FLOW_ERROR: DeviceFlowError,
    SourceControlErrorCode.DEVICE_FLOW_DISABLED: DeviceFlowDisabledError,
    SourceControlErrorCode.DEVICE_FLOW_PENDING: DeviceFlowPendingError,
    SourceControlErrorCode.DEVICE_FLOW_SLOW_DOWN: DeviceFlowSlowDownError,
    SourceControlErrorCode.DEVICE_FLOW_DENIED: DeviceFlowDeniedError,
    SourceControlErrorCode.DEVICE_FLOW_EXPIRED: DeviceFlowExpiredError,
    SourceControlErrorCode.DEVICE_FLOW_SESSION_NOT_FOUND: DeviceFlowSessionNotFoundError,
}


def to_source_control_error(
    exc: BaseException,
    *,
    default_code: Optional[SourceControlErrorCode] = None,
    default_message: Optional[str] = None,
) -> SourceControlError:
    """
    Normalize any exception into a `SourceControlError`.

    Rules:
        - If `exc` is already a `SourceControlError`, return it as-is.
        - If `exc` is a `TimeoutError` or `asyncio.TimeoutError`,
          return a `CommandTimeoutError`.
        - If `exc` is a `PermissionError`, return a `PermissionDeniedError`.
        - If `exc` is a `ValueError` or `TypeError`, return a
          `ValidationError`.
        - Otherwise, return a `ProviderError` (or the class matching
          `default_code`).
    """
    if isinstance(exc, SourceControlError):
        return exc

    # asyncio.TimeoutError is an alias of TimeoutError on Python 3.11+.
    if isinstance(exc, TimeoutError):
        return CommandTimeoutError(
            default_message or "Operation timed out.",
            cause=exc,
        )

    if isinstance(exc, PermissionError):
        return PermissionDeniedError(
            default_message or "Permission denied.",
            cause=exc,
        )

    if isinstance(exc, (ValueError, TypeError)):
        return ValidationError(
            default_message or redact_message(str(exc)),
            cause=exc,
        )

    if isinstance(exc, OSError):
        return NetworkError(
            default_message or "Network or OS error.",
            cause=exc,
        )

    if default_code is not None:
        cls = _CODE_TO_CLASS.get(default_code, ProviderError)
        return cls(
            default_message or redact_message(str(exc)),
            cause=exc,
        )

    return ProviderError(
        default_message or redact_message(str(exc)),
        cause=exc,
    )


def error_code_of(exc: BaseException) -> SourceControlErrorCode:
    """
    Return the `SourceControlErrorCode` associated with `exc`.

    Foreign exceptions are mapped using `to_source_control_error` so
    that callers always receive a stable code.
    """
    if isinstance(exc, SourceControlError):
        return exc.code
    return to_source_control_error(exc).code


def is_retryable_error(exc: BaseException) -> bool:
    """
    Return True if `exc` may be retried safely.

    The decision is delegated to `SourceControlError.is_retryable`
    after normalization.
    """
    if isinstance(exc, SourceControlError):
        return exc.is_retryable
    return to_source_control_error(exc).is_retryable


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
#
# The HTTP layer, the schemas, and some adapters were written using the
# names below. They refer to the SAME classes as their canonical
# counterparts, so `except` and `isinstance` checks continue to work
# across the codebase. New code SHOULD use the canonical names.

#: Alias for `AuthRequiredError`.
#:
#: Older callers used a more descriptive `Authentication*` prefix.
AuthenticationRequiredError = AuthRequiredError

#: Alias for `AuthExpiredError`.
AuthenticationExpiredError = AuthExpiredError

#: Alias for `RateLimitError`.
#:
#: Some callers used `RateLimitedError` (past tense), others used
#: `RateLimitExceededError`. Both point to the canonical class.
RateLimitedError = RateLimitError
RateLimitExceededError = RateLimitError
