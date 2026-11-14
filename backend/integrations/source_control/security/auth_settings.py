# backend/integrations/source_control/security/auth_settings.py
"""
Authentication settings model for the Source Control Integration
subsystem.

This module defines the full, validated configuration of the Source
Control authentication layer:

    - `ManagedAuthSettings`  : managed GitHub App + Device Flow.
    - `ByoaAuthSettings`     : user-provided OAuth App (BYOA).
    - `ProviderAuthSettings` : per-provider overrides (preferred
                               source, per-provider enable flags).
    - `AuthSecuritySettings` : cross-cutting security flags
                               (automatic fallback, strict vault).
    - `AuthSettings`         : root aggregate.

Design rules:
    - This module is pure: no I/O, no environment access, no
      imports from `omweb/` or `schemas/`. It is imported by both
      the settings store (`omweb/services/...`) and by the
      authentication service. Keeping it pure avoids a circular
      import.
    - All models are frozen dataclasses. Any change produces a new
      instance via `dataclasses.replace`. This matches the
      immutability convention used by `domain.models`.
    - The `to_dict()` methods produce JSON-safe mappings that match
      the shape persisted on disk and returned by the API. They do
      NOT include secrets: managed Client ID is not secret (it is
      public), and BYOA Client Secret is stored only in the vault,
      never here.
    - Validation happens in `__post_init__` and raises
      `ValidationError` (from `..errors`) with a precise message
      and `details`. The settings store wraps the call so a bad
      file on disk never crashes the application at import time.

Interaction with `AuthSource`:
    - The per-provider `preferred_source` MUST be a selectable
      source (`MANAGED` or `BYOA`). `LEGACY_UNKNOWN` is rejected
      here, at settings time, so that a settings file can never
      produce an unstartable flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional

from ..domain.enums import ProviderId
from ..errors import ValidationError
from .auth_types import (
    AuthSource,
    VALID_AUTH_SOURCES,
    auth_source_value,
    coerce_auth_source,
)

__all__ = [
    # Value objects
    "ManagedAuthSettings",
    "ByoaAuthSettings",
    "ProviderAuthSettings",
    "AuthSecuritySettings",
    "AuthSettings",
    # Factories
    "default_auth_settings",
    # Constants
    "SUPPORTED_MANAGED_PROVIDERS",
    "SUPPORTED_MANAGED_STRATEGIES",
    "ALLOWED_PUSH_TO_PROTECTED_MODES",
]


#: Providers for which the managed GitHub App strategy is defined.
#: Extending this set requires a corresponding managed Client ID and
#: a Device Flow callback registration in `source_control_auth_service`.
SUPPORTED_MANAGED_PROVIDERS: frozenset[str] = frozenset({"github"})


#: Device Flow is the only managed strategy supported in this phase.
#: Authorization Code is intentionally excluded because it requires
#: a Client Secret, which the managed path MUST NOT carry.
SUPPORTED_MANAGED_STRATEGIES: frozenset[str] = frozenset({"device_flow"})


#: Allowed values for `allow_push_to_protected` in the broader
#: Source Control settings. Duplicated here ONLY for validation of
#: the auth settings; the authoritative value lives in
#: `source_control_facade.read_settings()`.
ALLOWED_PUSH_TO_PROTECTED_MODES: frozenset[str] = frozenset(
    {"never", "ask", "always"}
)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedAuthSettings:
    """
    Settings for the managed GitHub App strategy.

    Fields:
        enabled:  When True, new connections may use the managed
                  GitHub App. Disabling it does NOT remove existing
                  connections or tokens; it only blocks new starts.
        provider: The provider this managed strategy targets. Only
                  values in `SUPPORTED_MANAGED_PROVIDERS` are
                  accepted. The canonical value is `"github"`.
        strategy: The flow used by the managed path. Only
                  `"device_flow"` is supported in this phase.
    """

    enabled: bool = True
    provider: str = "github"
    strategy: str = "device_flow"

    def __post_init__(self) -> None:
        provider = (self.provider or "").strip().lower()
        if provider not in SUPPORTED_MANAGED_PROVIDERS:
            raise ValidationError(
                f"Unsupported managed provider '{self.provider}'. "
                f"Supported: {sorted(SUPPORTED_MANAGED_PROVIDERS)}.",
                details={"field": "managed.provider", "value": self.provider},
            )
        object.__setattr__(self, "provider", provider)

        strategy = (self.strategy or "").strip().lower()
        if strategy not in SUPPORTED_MANAGED_STRATEGIES:
            raise ValidationError(
                f"Unsupported managed strategy '{self.strategy}'. "
                f"Supported: {sorted(SUPPORTED_MANAGED_STRATEGIES)}.",
                details={"field": "managed.strategy", "value": self.strategy},
            )
        object.__setattr__(self, "strategy", strategy)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "strategy": self.strategy,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ManagedAuthSettings":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=str(data.get("provider", "github")),
            strategy=str(data.get("strategy", "device_flow")),
        )


@dataclass(frozen=True)
class ByoaAuthSettings:
    """
    Settings for the BYOA (Bring Your Own App) strategy.

    Fields:
        enabled: When True, users may configure their own OAuth App
                 credentials and start a connection with them.
                 Disabling it does NOT remove existing BYOA
                 connections or credentials; it only blocks new
                 starts.

    No other fields are defined in this phase. BYOA credentials
    (Client ID + Client Secret) are managed through the existing
    `/connections/{provider}/config` endpoints and stored in the
    vault, NOT here.
    """

    enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ByoaAuthSettings":
        if not data:
            return cls()
        return cls(enabled=bool(data.get("enabled", False)))


@dataclass(frozen=True)
class ProviderAuthSettings:
    """
    Per-provider override of the global auth settings.

    Fields:
        preferred_source: The source used when the user clicks
                          "Connect" without explicitly choosing one.
                          MUST be a selectable source
                          (MANAGED or BYOA); LEGACY_UNKNOWN is
                          rejected.
        managed_enabled:  Provider-specific override of
                          `managed.enabled`. When False, managed is
                          disabled for THIS provider even if the
                          global flag is True.
        byoa_enabled:     Provider-specific override of
                          `byoa.enabled`.

    Note on the interaction between the global and per-provider
    flags: a source is enabled for a provider if and only if BOTH
    the global flag AND the per-provider flag are True. This lets an
    operator disable a source globally without touching every
    provider, while still allowing fine-grained control per
    provider.
    """

    preferred_source: AuthSource = AuthSource.MANAGED
    managed_enabled: bool = True
    byoa_enabled: bool = False

    def __post_init__(self) -> None:
        # Normalize to AuthSource enum. A raw string from a settings
        # file is accepted; an unknown value raises ValidationError.
        source = coerce_auth_source(self.preferred_source)
        if source not in VALID_AUTH_SOURCES:
            raise ValidationError(
                f"preferred_source '{source.value}' is not selectable. "
                f"Allowed: {sorted(s.value for s in VALID_AUTH_SOURCES)}.",
                details={
                    "field": "providers.*.preferred_source",
                    "value": source.value,
                },
            )
        object.__setattr__(self, "preferred_source", source)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preferred_source": self.preferred_source.value,
            "managed_enabled": self.managed_enabled,
            "byoa_enabled": self.byoa_enabled,
        }

    @classmethod
    def from_dict(
        cls, data: Optional[Mapping[str, Any]]
    ) -> "ProviderAuthSettings":
        if not data:
            return cls()
        raw_source = data.get("preferred_source", AuthSource.MANAGED.value)
        return cls(
            preferred_source=coerce_auth_source(str(raw_source)),
            managed_enabled=bool(data.get("managed_enabled", True)),
            byoa_enabled=bool(data.get("byoa_enabled", False)),
        )


@dataclass(frozen=True)
class AuthSecuritySettings:
    """
    Cross-cutting security flags for the authentication layer.

    Fields:
        automatic_fallback:
            When True, a flow that cannot start with the preferred
            source MAY silently fall back to the other source.
            Default is False because silent fallback hides
            configuration errors from the user and can lead to a
            connection being created under a source the user did
            not choose. The plan explicitly requires
            `automatic_fallback=false`.
        require_secure_vault:
            When True (default), the authentication layer refuses to
            store tokens in any vault that is not a real OS keychain
            (`InMemoryVault` and `EncryptedFileVault` are rejected
            in production). When False, the development fallback is
            allowed.
    """

    automatic_fallback: bool = False
    require_secure_vault: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "automatic_fallback": self.automatic_fallback,
            "require_secure_vault": self.require_secure_vault,
        }

    @classmethod
    def from_dict(
        cls, data: Optional[Mapping[str, Any]]
    ) -> "AuthSecuritySettings":
        if not data:
            return cls()
        return cls(
            automatic_fallback=bool(data.get("automatic_fallback", False)),
            require_secure_vault=bool(data.get("require_secure_vault", True)),
        )


# ---------------------------------------------------------------------------
# Root aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthSettings:
    """
    Root authentication settings aggregate.

    Fields:
        enabled:   Master switch. When False, no authentication flow
                   may start at all (neither managed nor byoa). This
                   is DISTINCT from the per-source flags: a user
                   whose token is still valid can keep using the
                   connected provider for read operations, but no
                   new connection may be created.
        managed:   Managed GitHub App settings.
        byoa:      BYOA settings.
        providers: Per-provider overrides. Providers absent from the
                   mapping use the default overrides (managed on,
                   byoa off, preferred=managed).
        security:  Cross-cutting security flags.

    The root class also provides convenience methods
    (`is_source_enabled`, `preferred_source_for`) that apply the
    global-and-per-provider AND-rule described in
    `ProviderAuthSettings`.
    """

    enabled: bool = True
    managed: ManagedAuthSettings = field(default_factory=ManagedAuthSettings)
    byoa: ByoaAuthSettings = field(default_factory=ByoaAuthSettings)
    providers: Mapping[str, ProviderAuthSettings] = field(default_factory=dict)
    security: AuthSecuritySettings = field(
        default_factory=AuthSecuritySettings
    )

    def __post_init__(self) -> None:
        # Validate and normalize the providers mapping. The keys are
        # provider IDs; unknown keys are preserved (not rejected) so
        # that a future provider can be added to the file before the
        # code knows about it, but their values MUST be valid
        # ProviderAuthSettings instances.
        normalized: Dict[str, ProviderAuthSettings] = {}
        for raw_key, raw_value in dict(self.providers or {}).items():
            key = str(raw_key).strip().lower()
            if not key:
                raise ValidationError(
                    "Provider key must not be empty.",
                    details={"field": "providers"},
                )
            if isinstance(raw_value, ProviderAuthSettings):
                normalized[key] = raw_value
            elif isinstance(raw_value, Mapping):
                normalized[key] = ProviderAuthSettings.from_dict(raw_value)
            else:
                raise ValidationError(
                    f"Provider entry '{raw_key}' must be a mapping or "
                    f"ProviderAuthSettings.",
                    details={
                        "field": "providers",
                        "key": key,
                        "type": type(raw_value).__name__,
                    },
                )
        object.__setattr__(self, "providers", normalized)

    # --- Query helpers --------------------------------------------------

    def provider_settings(
        self, provider: ProviderId | str
    ) -> ProviderAuthSettings:
        """
        Return the effective settings for a provider.

        Providers absent from `self.providers` receive the default
        overrides (managed on, byoa off, preferred=managed), so
        callers never have to check for a missing key.
        """
        key = (
            provider.value
            if isinstance(provider, ProviderId)
            else str(provider).strip().lower()
        )
        return self.providers.get(key, ProviderAuthSettings())

    def is_source_enabled(
        self, provider: ProviderId | str, source: AuthSource | str
    ) -> bool:
        """
        Return True iff `source` may be used for a NEW connection to
        `provider`.

        Rules (in order):
            1. `self.enabled` MUST be True. Otherwise no source is
               enabled at all.
            2. `source` MUST be selectable (`MANAGED` or `BYOA`).
               `LEGACY_UNKNOWN` is never selectable.
            3. The global per-source flag (`self.managed.enabled`
               or `self.byoa.enabled`) MUST be True.
            4. The per-provider flag (`managed_enabled` /
               `byoa_enabled`) MUST be True.

        Disabling a source via this method NEVER touches existing
        connections or tokens; it only blocks new starts. The
        distinction is enforced at the settings layer, and the
        connection record (`auth_source`) preserves which source a
        connection was created under.
        """
        if not self.enabled:
            return False
        coerced = coerce_auth_source(source)
        if coerced not in VALID_AUTH_SOURCES:
            return False
        provider_cfg = self.provider_settings(provider)
        if coerced == AuthSource.MANAGED:
            return bool(self.managed.enabled and provider_cfg.managed_enabled)
        if coerced == AuthSource.BYOA:
            return bool(self.byoa.enabled and provider_cfg.byoa_enabled)
        # Exhaustive; kept for safety if AuthSource gains members.
        return False

    def preferred_source_for(
        self, provider: ProviderId | str
    ) -> AuthSource:
        """
        Return the source that should be pre-selected for a provider.

        The preferred source is a HINT for the UI, not an
        authorization: the caller MUST still check
        `is_source_enabled` before starting a flow. If the
        preferred source is not currently enabled, this method
        returns the other source when it IS enabled, so the UI can
        show a usable default. If neither source is enabled, the
        configured preferred source is returned unchanged so the
        UI can render a clear "everything is disabled" state.
        """
        provider_cfg = self.provider_settings(provider)
        preferred = provider_cfg.preferred_source
        if self.is_source_enabled(provider, preferred):
            return preferred
        other = (
            AuthSource.BYOA
            if preferred == AuthSource.MANAGED
            else AuthSource.MANAGED
        )
        if self.is_source_enabled(provider, other):
            return other
        return preferred

    # --- Serialization --------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "managed": self.managed.to_dict(),
            "byoa": self.byoa.to_dict(),
            "providers": {
                key: cfg.to_dict()
                for key, cfg in self.providers.items()
            },
            "security": self.security.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "AuthSettings":
        """
        Reconstruct an `AuthSettings` from a mapping produced by
        `to_dict` (or written by hand / by an older release).

        Missing fields fall back to their defaults. Unknown fields
        are silently ignored. Invalid values raise `ValidationError`
        with a precise message; the settings store catches this and
        falls back to the defaults so a corrupt file never crashes
        the application.
        """
        if not data:
            return default_auth_settings()
        return cls(
            enabled=bool(data.get("enabled", True)),
            managed=ManagedAuthSettings.from_dict(data.get("managed")),
            byoa=ByoaAuthSettings.from_dict(data.get("byoa")),
            providers={
                str(k): ProviderAuthSettings.from_dict(v)
                for k, v in dict(data.get("providers") or {}).items()
            },
            security=AuthSecuritySettings.from_dict(data.get("security")),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def default_auth_settings() -> AuthSettings:
    """
    Return the factory defaults for a new installation.

    Matches the plan's decision table:

        Source Control Authentication  ON
        Managed GitHub App             ON
        BYOA                           OFF
        Preferred Source               Managed
        Automatic Fallback             OFF
        Device Flow                    ON for the managed path
        Client Secret in Managed       NOT USED
        Private Key in Managed         NOT USED
    """
    return AuthSettings(
        enabled=True,
        managed=ManagedAuthSettings(
            enabled=True,
            provider="github",
            strategy="device_flow",
        ),
        byoa=ByoaAuthSettings(enabled=False),
        providers={
            "github": ProviderAuthSettings(
                preferred_source=AuthSource.MANAGED,
                managed_enabled=True,
                byoa_enabled=False,
            ),
        },
        security=AuthSecuritySettings(
            automatic_fallback=False,
            require_secure_vault=True,
        ),
    )
