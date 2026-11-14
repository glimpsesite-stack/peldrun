# backend/integrations/source_control/security/auth_types.py
"""
Authentication type definitions for the Source Control Integration
subsystem.

This module is the single source of truth for the vocabulary used by
the Source Control authentication layer:

    - `AuthSource`       : managed (PELDRUN GitHub App) vs byoa
                           (Bring Your Own App) vs legacy_unknown.
    - `DeviceFlowStatus` : lifecycle of a Device Flow session.

Error classes are NOT defined here. They live in
`integrations.source_control.errors` alongside every other error in
the subsystem, so that callers can catch them with a single
`from ..errors import ...` regardless of which layer raised them.

Design rules:
    - This module MUST NOT import from anywhere else in the package
      except the standard library and `..errors`. It is imported by
      settings, services, and adapters, and MUST stay at the bottom of
      the dependency graph to avoid cycles.
    - Enums use `str` as a base class so that values serialize to
      JSON transparently (no custom encoder required).
    - Enum values are stable identifiers used for persistence, audit
      logs, and API contracts. Adding a member requires a migration
      note; removing or renaming one requires a migration.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..errors import ValidationError

__all__ = [
    # Enums
    "AuthSource",
    "DeviceFlowStatus",
    # Constants
    "VALID_AUTH_SOURCES",
    "DEFAULT_AUTH_SOURCE",
    # Helpers
    "coerce_auth_source",
    "auth_source_value",
    "is_selectable_source",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AuthSource(str, Enum):
    """
    The origin of an authentication credential.

    A connection is always associated with exactly one source. The
    source determines:

        - Which strategy class handles the flow (managed vs byoa).
        - Which settings gate must be enabled for a new connection
          to be created.
        - Whether the connection can be refreshed without a
          client_secret (managed Device Flow) or requires one (byoa
          Authorization Code).

    Values:
        MANAGED:
            PELDRUN's official GitHub App, authenticated via Device
            Flow. Requires only a public Client ID. No Client Secret
            and no Private Key are present in the application.
        BYOA:
            A user-provided OAuth App. Requires the user to configure
            a Client ID and a Client Secret before any connection can
            begin.
        LEGACY_UNKNOWN:
            A connection that existed before the introduction of
            `auth_source`. It is preserved as-is and never converted
            automatically. The user must explicitly re-authorize to
            move it to MANAGED or BYOA.
    """

    MANAGED = "managed"
    BYOA = "byoa"
    LEGACY_UNKNOWN = "legacy_unknown"


class DeviceFlowStatus(str, Enum):
    """
    Lifecycle of a GitHub Device Flow session.

    Transitions (single source of truth):

        PENDING   -> COMPLETED | DENIED | EXPIRED | CANCELLED | ERROR
        COMPLETED -> terminal
        DENIED    -> terminal
        EXPIRED   -> terminal
        CANCELLED -> terminal
        ERROR     -> terminal

    `PENDING` means the backend is still polling GitHub for the
    token. Any other state is terminal and the session MUST be
    removed from the in-memory store after its terminal state has
    been observed by the frontend (or after its TTL elapses,
    whichever is later).
    """

    PENDING = "pending"
    COMPLETED = "completed"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        """Return True if no further state change is possible."""
        return self != DeviceFlowStatus.PENDING


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The set of sources that may be selected for a NEW connection.
#: `LEGACY_UNKNOWN` is intentionally excluded: it is a migration
#: marker for existing connections, not a selectable strategy.
VALID_AUTH_SOURCES: frozenset[AuthSource] = frozenset(
    {AuthSource.MANAGED, AuthSource.BYOA}
)


#: Default source for a brand-new installation. Matches the plan's
#: "Managed GitHub App ON by default, BYOA OFF by default" decision.
DEFAULT_AUTH_SOURCE: AuthSource = AuthSource.MANAGED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def coerce_auth_source(value: Optional[str]) -> AuthSource:
    """
    Convert a string (or `None`) into an `AuthSource`.

    Unknown strings raise `ValidationError` with a precise message.
    `None` and empty strings fall back to `DEFAULT_AUTH_SOURCE`, which
    is the value used by new installations when the caller does not
    specify a source.

    This function is intentionally strict about unknown values: a
    typo in a settings file or an API payload must never silently
    select a different strategy.
    """
    if value is None:
        return DEFAULT_AUTH_SOURCE
    if isinstance(value, AuthSource):
        return value
    if not isinstance(value, str):
        raise ValidationError(
            "auth_source must be a string or AuthSource.",
            details={"type": type(value).__name__},
        )
    normalized = value.strip().lower()
    if not normalized:
        return DEFAULT_AUTH_SOURCE
    try:
        return AuthSource(normalized)
    except ValueError as exc:
        raise ValidationError(
            f"Unknown auth_source '{value}'. "
            f"Valid values are: "
            f"{', '.join(s.value for s in AuthSource)}.",
            details={"auth_source": value},
        ) from exc


def auth_source_value(source: AuthSource | str) -> str:
    """
    Return the string form of an `AuthSource`.

    Accepts either the enum or a raw string so that callers that
    already validated the value do not need to unwrap it manually.
    """
    if isinstance(source, AuthSource):
        return source.value
    return coerce_auth_source(source).value


def is_selectable_source(source: AuthSource | str) -> bool:
    """
    Return True if `source` may be used to start a NEW connection.

    `LEGACY_UNKNOWN` returns False because it is a migration marker
    for existing connections, not a selectable strategy. The function
    accepts the raw string form so callers can validate user input
    before coercing it.

    Raises `ValidationError` for values that are not a valid
    `AuthSource` member at all (e.g. typos). Returning False for
    those would hide a bug in the caller.
    """
    return coerce_auth_source(source) in VALID_AUTH_SOURCES
