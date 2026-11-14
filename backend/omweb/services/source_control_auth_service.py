# backend/omweb/services/source_control_auth_service.py
"""
High-level authentication orchestrator for the Source Control
Integration subsystem.

This service is the SINGLE coordination point for the Managed GitHub
App + Device Flow authentication path. It ties together:

    - `AuthSettingsStore`            : the persisted settings.
    - `GitHubDeviceFlowClient`       : the raw GitHub HTTP client.
    - `DeviceFlowSessionManager`     : the in-memory session store.
    - `poll_for_token`               : the backend polling loop.
    - `GitHubAuthService`            : account identity resolution.
    - `TokenService`                 : token persistence in the vault.
    - `SourceControlFacade`          : the connection registry.

The facade is the ONLY component allowed to own a `_ConnectionRecord`.
This service does NOT duplicate that responsibility: it obtains the
current record from `facade._get_connection(...)` and installs a new
one via `facade._set_connection(...)`.

Design rules:
    - This service NEVER stores `device_code` or `access_token` in
      any field, log, or audit payload. The token is written to the
      vault via `TokenService.store_token(...)` exactly once, in the
      `_on_device_completed` callback.
    - The `client_id` is PUBLIC (it is the GitHub App's public
      identifier); the `client_secret` is NEVER used on the managed
      path. Both facts are enforced by construction: this module does
      not accept a secret parameter.
    - The managed path requires BOTH `settings.is_source_enabled(
      provider, MANAGED)` AND a resolved `client_id`. When either is
      missing, `begin_managed(...)` raises a typed error with an
      actionable message.
    - The BYOA path is NOT implemented here: it continues to flow
      through `facade.begin_connection(...)` and the existing
      `OAuthFlowCoordinator`. This service only reports BYOA
      READINESS so the UI can show the correct enable/disable state.
    - The service is thread-safe for the small operations that touch
      its own state (`is_managed_ready`, `get_auth_status`). The
      session manager is itself thread-safe; the polling task runs
      on the event loop.

Singleton accessors:
    - `get_default_auth_service()` returns the process-wide service,
      lazily wired with the default facade and settings store.
    - `set_default_auth_service(...)` / `reset_default_auth_service()`
      are used by tests to guarantee isolation.

Environment:
    - `PELDRUN_GITHUB_CLIENT_ID` : the public Client ID of the
      PELDRUN GitHub App. When unset, `is_managed_ready` returns
      False and `begin_managed` raises a clear configuration error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

from ...integrations.source_control.domain.enums import ProviderId
from ...integrations.source_control.errors import (
    AuthDisabledError,
    AuthSourceDisabledError,
    DeviceFlowError,
    SourceControlError,
    UnsupportedAuthSourceError,
    ValidationError,
)
from ...integrations.source_control.providers.github.auth import (
    GitHubAuthService,
)
from ...integrations.source_control.security.audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from ...integrations.source_control.security.auth_settings import (
    AuthSettings,
)
from ...integrations.source_control.security.auth_types import (
    AuthSource,
    DeviceFlowStatus,
)
from ...integrations.source_control.security.github_device_flow import (
    DeviceFlowSessionManager,
    DeviceFlowSessionView,
    GitHubDeviceFlowClient,
    poll_for_token,
)
from ...integrations.source_control.security.token_service import (
    TokenService,
    get_default_token_service,
)
from .source_control_auth_settings import AuthSettingsStore

if TYPE_CHECKING:
    from .source_control_facade import SourceControlFacade, _ConnectionRecord

__all__ = [
    "SourceControlAuthService",
    "get_default_auth_service",
    "set_default_auth_service",
    "reset_default_auth_service",
    "ENV_MANAGED_GITHUB_CLIENT_ID",
]


logger = logging.getLogger(__name__)


#: Environment variable that carries the public Client ID of the
#: PELDRUN GitHub App. This is NOT a secret; it is the same value
#: the user sees in the "Connect GitHub" dialog. The actual
#: `client_secret` MUST NOT be provided on the managed path.
ENV_MANAGED_GITHUB_CLIENT_ID = "PELDRUN_GITHUB_CLIENT_ID"


#: Default public Client ID of the official PELDRUN GitHub App.
#:
#: This is a PUBLIC identifier, not a secret: it is embedded in
#: the `POST /login/device/code` request and visible to the user
#: in the GitHub authorization page. Shipping it in the binary
#: is intentional and mirrors how tools like the official GitHub
#: CLI (`gh`) distribute their Client ID.
#:
#: The environment variable `PELDRUN_GITHUB_CLIENT_ID` takes
#: PRECEDENCE over this value, so a self-hosted installation (or
#: a developer testing against a personal GitHub App) can override
#: it without modifying the source. When both are absent, the
#: service reports `client_id_configured=False` and refuses to
#: start a managed connection.
#:
#: REPLACE THIS WITH YOUR ACTUAL PRODUCTION CLIENT ID BEFORE
#: SHIPPING A PUBLIC RELEASE. The value below is a placeholder
#: used during development; a production build MUST contain the
#: Client ID of the App you registered at
#: https://github.com/settings/apps.
DEFAULT_MANAGED_GITHUB_CLIENT_ID = "Iv23li4VdUfmb9PwjZRM"


class SourceControlAuthService:
    """
    Orchestrates the Managed GitHub App + Device Flow path.

    See the module docstring for the full dependency graph and the
    design rules. This class is intentionally thin: every method is a
    short orchestration over the injected collaborators.
    """

    def __init__(
        self,
        *,
        settings_store: AuthSettingsStore,
        facade: "SourceControlFacade",
        device_flow_client: Optional[GitHubDeviceFlowClient] = None,
        session_manager: Optional[DeviceFlowSessionManager] = None,
        token_service: Optional[TokenService] = None,
        github_auth_service: Optional[GitHubAuthService] = None,
        audit_logger: Optional[AuditLogger] = None,
        managed_client_id: Optional[str] = None,
    ) -> None:
        self._settings_store = settings_store
        self._facade = facade
        self._device_flow_client = (
            device_flow_client or GitHubDeviceFlowClient()
        )
        self._session_manager = (
            session_manager or DeviceFlowSessionManager()
        )
        self._token_service = (
            token_service or get_default_token_service()
        )
        self._github_auth_service = (
            github_auth_service or GitHubAuthService()
        )
        self._audit = (
            audit_logger or get_default_audit_logger()
        )

        # Resolve the managed client ID once at construction time.
        # Precedence:
        #   1. Explicit parameter (used by tests and by embedding
        #      applications that pass the ID programmatically).
        #   2. `PELDRUN_GITHUB_CLIENT_ID` environment variable.
        #   3. `DEFAULT_MANAGED_GITHUB_CLIENT_ID` (the public Client
        #      ID of the official PELDRUN GitHub App, hardcoded so
        #      that a normal installation works out of the box).
        #
        # A None value does NOT raise here: the service still
        # constructs successfully, and `is_managed_ready(...)`
        # reports False. `begin_managed(...)` raises a clear error
        # so the failure is never silent.
        env_client_id = (
            os.environ.get(ENV_MANAGED_GITHUB_CLIENT_ID, "").strip()
            or None
        )
        if managed_client_id is not None:
            resolved_client_id = managed_client_id.strip() or None
        elif env_client_id:
            resolved_client_id = env_client_id
        else:
            resolved_client_id = (
                DEFAULT_MANAGED_GITHUB_CLIENT_ID.strip() or None
            )

        self._managed_client_id: Optional[str] = resolved_client_id

        # A reentrant lock protects the tiny amount of mutable state
        # (`_polling_tasks`). The session manager has its own lock;
        # we do NOT hold both at the same time to avoid a lock-order
        # inversion.
        self._lock = threading.RLock()
        #: session_id -> asyncio.Task currently polling GitHub.
        #: Used so `cancel_device` can deterministically cancel
        #: the task without relying on the session manager's
        #: internal field (which the poll loop also updates).
        self._polling_tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 1. Readiness helpers
    # ------------------------------------------------------------------

    @property
    def managed_client_id(self) -> Optional[str]:
        """
        Return the resolved managed Client ID, or None when not
        configured.

        Exposed for diagnostics and for the settings/status endpoint.
        Safe to log: it is a PUBLIC identifier, not a secret.
        """
        return self._managed_client_id

    def is_managed_ready(
        self, provider: ProviderId | str = ProviderId.GITHUB
    ) -> bool:
        """
        Return True iff the managed path may be used to START a new
        connection for `provider`.

        Conditions (all required):
            1. `settings.enabled` is True.
            2. `settings.managed.enabled` is True.
            3. The per-provider `managed_enabled` flag is True.
            4. A managed Client ID is configured.

        Disabling a source NEVER deletes existing connections or
        tokens; it only blocks new starts. This method reflects the
        start-gate only.
        """
        settings = self._settings_store.get()
        try:
            provider_id = self._coerce_provider_id(provider)
        except ValidationError:
            return False
        if not settings.is_source_enabled(provider_id, AuthSource.MANAGED):
            return False
        if not self._managed_client_id:
            return False
        return True

    def is_byoa_ready(
        self, provider: ProviderId | str = ProviderId.GITHUB
    ) -> bool:
        """
        Return True iff the BYOA path may be used to START a new
        connection for `provider`.

        Conditions (all required):
            1. `settings.enabled` is True.
            2. `settings.byoa.enabled` is True.
            3. The per-provider `byoa_enabled` flag is True.
            4. The BYOA Client ID + Client Secret are configured in
               the facade (Client ID in JSON, Secret in the vault).

        The BYOA flow itself is NOT implemented in this service: it
        continues to flow through
        `facade.begin_connection(...)`. This method only reports
        readiness so the UI can show the correct state.
        """
        settings = self._settings_store.get()
        try:
            provider_id = self._coerce_provider_id(provider)
        except ValidationError:
            return False
        if not settings.is_source_enabled(provider_id, AuthSource.BYOA):
            return False
        try:
            from .source_control_facade import get_source_control_facade
            current_facade = get_source_control_facade()
            return bool(
                current_facade._is_provider_configured(
                    provider_id.value
                )
            )
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 2. Status endpoint
    # ------------------------------------------------------------------

    def get_auth_status(self) -> Dict[str, Any]:
        """
        Return a JSON-safe snapshot of the authentication layer.

        The shape is stable and consumed by
        `GET /api/source-control/auth/status`. It is also returned
        by the settings GET endpoint (which the frontend uses to
        render the Integrations page) so a single round-trip is
        enough to know what to show.

        The return value intentionally excludes:
            - The Client ID value (only whether it is configured).
            - The BYOA Client Secret.
            - Any token or device code.
        """
        settings = self._settings_store.get()
        vault = getattr(self._token_service, "_vault", None)
        try:
            vault_backend = (
                getattr(vault, "backend_name", "unknown")
                if vault is not None
                else "unknown"
            )
            vault_available = (
                bool(vault.is_available()) if vault is not None else False
            )
        except Exception:  # noqa: BLE001
            vault_backend = "unknown"
            vault_available = False

        providers_status: Dict[str, Any] = {}
        for raw_provider_id in {p.value for p in ProviderId}:
            try:
                provider_id = ProviderId(raw_provider_id)
            except ValueError:
                continue
            conn = None
            try:
                # Always fetch the current singleton to avoid
                # reading from a stale duplicate instance.
                from .source_control_facade import get_source_control_facade
                current_facade = get_source_control_facade()
                conn = current_facade._get_connection(provider_id)
            except Exception:  # noqa: BLE001
                conn = None
            providers_status[provider_id.value] = {
                "managed_ready": self.is_managed_ready(provider_id),
                "byoa_ready": self.is_byoa_ready(provider_id),
                "preferred_source": settings.preferred_source_for(
                    provider_id
                ).value,
                "connection": (
                    {
                        "auth_source": getattr(
                            conn, "auth_source", "legacy_unknown"
                        ),
                        "username": getattr(conn, "username", None),
                        "account_id": getattr(conn, "account_id", None),
                        "connected_at": (
                            getattr(conn, "connected_at", None).isoformat()
                            if getattr(conn, "connected_at", None)
                            else None
                        ),
                    }
                    if conn is not None
                    else None
                ),
            }

        return {
            "settings": settings.to_dict(),
            "managed": {
                "enabled": settings.managed.enabled,
                "provider": settings.managed.provider,
                "strategy": settings.managed.strategy,
                "client_id_configured": self._managed_client_id
                is not None,
            },
            "byoa": {
                "enabled": settings.byoa.enabled,
            },
            "vault": {
                "backend": vault_backend,
                "available": vault_available,
            },
            "providers": providers_status,
        }

    # ------------------------------------------------------------------
    # 3. Managed Device Flow
    # ------------------------------------------------------------------

    async def begin_managed(
        self, provider: ProviderId | str = ProviderId.GITHUB
    ) -> DeviceFlowSessionView:
        """
        Start a Managed GitHub App Device Flow session.

        Returns the initial session view (with `status=PENDING`).
        The frontend MUST display `user_code` and `verification_uri`
        and then poll
        `GET /api/source-control/connections/{provider}/device/{session_id}/status`
        until the status becomes terminal.

        The backend starts an `asyncio.Task` that polls GitHub. The
        task is tracked in `_polling_tasks` so `cancel_device(...)`
        can cancel it deterministically.

        Raises:
            AuthDisabledError:
                The whole authentication subsystem is disabled.
            AuthSourceDisabledError:
                The managed source (or the per-provider managed
                flag) is disabled.
            AuthSourceDisabledError:
                The managed Client ID is not configured.
            DeviceFlowDisabledError:
                GitHub reports `device_flow_disabled` for the app.
            ValidationError:
                The provider argument is not valid.
            NetworkError / ProviderError:
                Transport failure or malformed response from GitHub.
        """
        provider_id = self._coerce_provider_id(provider)
        settings = self._settings_store.get()

        # --- Gate 1: subsystem enabled -------------------------------
        if not settings.enabled:
            raise AuthDisabledError(
                "Source Control authentication is disabled in "
                "settings. Enable it in Integrations to connect."
            )

        # --- Gate 2: managed source enabled --------------------------
        if not settings.is_source_enabled(
            provider_id, AuthSource.MANAGED
        ):
            raise AuthSourceDisabledError(
                f"Managed GitHub App is disabled for "
                f"'{provider_id.value}'. Enable it in Integrations "
                f"settings to connect.",
                details={
                    "provider": provider_id.value,
                    "source": AuthSource.MANAGED.value,
                },
            )

        # --- Gate 3: managed Client ID configured --------------------
        if not self._managed_client_id:
            raise AuthSourceDisabledError(
                f"Managed Client ID is not configured. Set the "
                f"{ENV_MANAGED_GITHUB_CLIENT_ID} environment "
                f"variable and restart PELDRUN.",
                details={
                    "env_var": ENV_MANAGED_GITHUB_CLIENT_ID,
                    "provider": provider_id.value,
                },
            )

        # --- Gate 4: vault availability (when strict) ----------------
        if settings.security.require_secure_vault:
            vault = getattr(self._token_service, "_vault", None)
            try:
                vault_ok = (
                    bool(vault.is_available())
                    if vault is not None
                    else False
                )
            except Exception:  # noqa: BLE001
                vault_ok = False
            if not vault_ok:
                raise SourceControlError(
                    "A secure credential vault is required by "
                    "settings, but no OS keychain backend is "
                    "available. Install `keyring` and ensure a "
                    "supported OS credential manager is reachable, "
                    "or set `security.require_secure_vault=false` "
                    "in auth_settings.json (NOT recommended in "
                    "production).",
                    details={
                        "vault_backend": getattr(
                            vault, "backend_name", "unknown"
                        ),
                    },
                )

        # --- Ask GitHub for a device code ----------------------------
        device_code_request = (
            await self._device_flow_client.request_device_code(
                client_id=self._managed_client_id,
            )
        )

        # --- Create the in-memory session ----------------------------
        view = self._session_manager.create(
            provider=provider_id,
            request=device_code_request,
        )

        # --- Start the backend polling loop --------------------------
        #
        # The task is tracked BOTH in the session manager (so it can
        # be cancelled by `session_manager.cancel`) and in
        # `_polling_tasks` (so `cancel_device` has a second, direct
        # handle). Either path cancels the same underlying task.
        task = asyncio.create_task(
            self._run_polling(
                session_id=view.session_id,
                client_id=self._managed_client_id,
            ),
            name=f"device-flow-poll-{view.session_id}",
        )
        with self._lock:
            self._polling_tasks[view.session_id] = task

        self._audit.record(
            AuditEventType.AUTH_BEGIN,
            severity=AuditSeverity.INFO,
            action="begin_managed_device_flow",
            message=(
                f"Started managed Device Flow for "
                f"'{provider_id.value}'."
            ),
            payload={
                "provider": provider_id.value,
                "session_id": view.session_id,
                "user_code": view.user_code,
                "verification_uri": view.verification_uri,
                "expires_at": view.expires_at.isoformat(),
                "interval": view.interval,
            },
            actor="user",
        )

        return view

    def get_device_status(
        self, session_id: str
    ) -> DeviceFlowSessionView:
        """
        Return the current state of a Device Flow session.

        This method is idempotent and cheap; the frontend polls it
        while the backend task drives the session to a terminal
        state. Terminal sessions are pruned on the next
        `begin_managed` call (to keep the store bounded) but remain
        queryable until then so the frontend can display a clear
        final status.

        Raises:
            DeviceFlowSessionNotFoundError:
                The session ID is unknown, expired, or already
                pruned.
        """
        # Do NOT prune here. Pruning before reading would delete
        # a COMPLETED session before the frontend has a chance
        # to observe the success, producing a spurious "session
        # not found" error right after a successful GitHub
        # authorization. Pruning happens on `begin_managed`
        # instead, which is the correct time to clean up old
        # sessions.
        return self._session_manager.get_view(session_id)

    def cancel_device(
        self, session_id: str
    ) -> DeviceFlowSessionView:
        """
        Cancel a PENDING Device Flow session.

        Idempotent: canceling an already-terminal session returns
        its terminal view. Canceling a PENDING session:

            1. Sets the session status to CANCELLED.
            2. Cancels the polling task (if still running).
            3. Removes the task handle from `_polling_tasks`.
            4. Records an audit event.

        The underlying `device_code` is NOT sent to GitHub for
        revocation: GitHub does not provide a revocation endpoint
        for unapproved device codes, and the code expires on its
        own after the `expires_in` interval.
        """
        view = self._session_manager.cancel(session_id)

        with self._lock:
            task = self._polling_tasks.pop(session_id, None)
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass

        self._audit.record(
            AuditEventType.AUTH_FAILURE,
            severity=AuditSeverity.INFO,
            action="cancel_managed_device_flow",
            message=(
                f"Managed Device Flow session '{session_id}' was "
                f"cancelled."
            ),
            payload={
                "session_id": session_id,
                "provider": (
                    view.provider.value
                    if isinstance(view.provider, ProviderId)
                    else str(view.provider)
                ),
            },
            actor="user",
        )
        return view

    # ------------------------------------------------------------------
    # 4. Internal: polling driver
    # ------------------------------------------------------------------

    async def _run_polling(
        self,
        *,
        session_id: str,
        client_id: str,
    ) -> None:
        """
        Wrapper around `poll_for_token` that:

            - Catches any unexpected exception and marks the session
              as ERROR (so the frontend is never left polling a
              session that no longer has a live task).
            - Removes the task handle from `_polling_tasks` on exit.
            - Swallows `CancelledError` cleanly: the session is
              already marked CANCELLED by `cancel_device` /
              `session_manager.cancel`, and re-raising would surface
              as an "unhandled exception in task" warning.

        The actual state transitions happen inside `poll_for_token`;
        this wrapper only adds the two guarantees above.
        """
        try:
            await poll_for_token(
                client=self._device_flow_client,
                session_manager=self._session_manager,
                session_id=session_id,
                client_id=client_id,
                on_completed=self._on_device_completed,
            )
        except asyncio.CancelledError:
            # Terminal state was already set by `cancel_device` or
            # by `session_manager.cancel`. Re-raise is optional;
            # we deliberately return cleanly to avoid log noise.
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "DeviceFlow polling task for session '%s' failed "
                "unexpectedly: %s",
                session_id,
                type(exc).__name__,
            )
            try:
                self._session_manager.internal_update(
                    session_id,
                    status=DeviceFlowStatus.ERROR,
                    error_message=(
                        f"Internal error during polling: "
                        f"{type(exc).__name__}"
                    ),
                    clear_polling_task=True,
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self._lock:
                self._polling_tasks.pop(session_id, None)

    # ------------------------------------------------------------------
    # 5. Internal: on-completed callback
    # ------------------------------------------------------------------

    async def _on_device_completed(
        self,
        view: DeviceFlowSessionView,
        token_payload: Mapping[str, Any],
    ) -> None:
        """
        Persist the token and update the connection record when a
        Device Flow session completes.

        This callback is invoked by `poll_for_token` AFTER GitHub
        has returned the token but BEFORE the session status is
        updated to COMPLETED. This ordering ensures:

            - The frontend never sees `COMPLETED` before the token
              is in the vault and the connection is registered.
            - Any failure here surfaces as `status=ERROR` on the
              session, so the user knows the flow did not fully
              succeed even though GitHub approved it.

        Steps:

            1. Extract `access_token`, `refresh_token`, `expires_in`,
               `refresh_token_expires_in` from the payload.
            2. Call `GitHubAuthService.get_authenticated_account`
               to obtain the account identity.
            3. Store the token pair in the vault via
               `TokenService.store_token`.
            4. Install a `_ConnectionRecord` with
               `auth_source="managed"`.
            5. Audit the successful completion.

        Failures:
            - Step 1: payload is malformed. Raise `ProviderError`
              (the outer wrapper will set status=ERROR).
            - Step 2: GitHub identity call fails. Raise
              `SourceControlError` (the token is NOT stored;
              `device_code` remains valid on GitHub's side until it
              expires, but we do not retry).
            - Step 3: vault is unavailable. Raise a typed error
              (the session becomes ERROR; the user must retry after
              fixing the vault).
            - Step 4: connection update fails. The token IS in the
              vault (so `TokenService.get_access_token` works), but
              the facade has no connection. Logged clearly; the
              session becomes ERROR.

        The raw `access_token` and `refresh_token` values are
        extracted into local variables and passed directly to
        `TokenService.store_token`. They are NEVER assigned to
        instance attributes, logged, or included in audit payloads.
        """
        # --- Step 1: extract the token fields ------------------------
        access_token_raw = token_payload.get("access_token")
        if not isinstance(access_token_raw, str) or not access_token_raw:
            raise SourceControlError(
                "Device Flow completion payload did not contain an "
                "access_token.",
                details={
                    "keys": sorted(token_payload.keys()),
                    "session_id": view.session_id,
                },
            )
        access_token: str = access_token_raw

        refresh_token_raw = token_payload.get("refresh_token")
        refresh_token: Optional[str] = (
            refresh_token_raw
            if isinstance(refresh_token_raw, str) and refresh_token_raw
            else None
        )

        # GitHub's device flow returns `expires_in` for the access
        # token and `refresh_token_expires_in` for the refresh
        # token. We convert both to absolute UTC datetimes.
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        access_expires_at = None
        expires_in = token_payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            access_expires_at = now + timedelta(seconds=int(expires_in))

        refresh_expires_at = None
        refresh_expires_in = token_payload.get(
            "refresh_token_expires_in"
        )
        if (
            isinstance(refresh_expires_in, (int, float))
            and refresh_expires_in > 0
        ):
            refresh_expires_at = now + timedelta(
                seconds=int(refresh_expires_in)
            )
        # `refresh_expires_at` is not used by the current
        # TokenService (it only tracks the access token's expiry),
        # but it is included in the audit payload for diagnostics.

        # --- Step 2: resolve the account identity -------------------
        try:
            account = await self._github_auth_service.get_authenticated_account(
                access_token
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DeviceFlow: could not resolve GitHub account for "
                "session '%s': %s",
                view.session_id,
                type(exc).__name__,
            )
            raise SourceControlError(
                "Device Flow completed, but PELDRUN could not "
                "resolve the GitHub account identity. The token was "
                "not stored. Please retry the connection.",
                details={
                    "session_id": view.session_id,
                    "reason": type(exc).__name__,
                },
            ) from exc

        provider_id = (
            account.provider
            if isinstance(account.provider, ProviderId)
            else ProviderId(str(account.provider))
        )
        base_url = account.base_url or "default"

        # --- Step 3: store the token pair ---------------------------
        try:
            self._token_service.store_token(
                provider=provider_id,
                account_id=account.account_id,
                base_url=base_url,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=access_expires_at,
                scopes=tuple(account.scopes or ()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "DeviceFlow: failed to store token for '%s': %s",
                provider_id.value,
                type(exc).__name__,
            )
            raise SourceControlError(
                "Device Flow completed, but PELDRUN could not store "
                "the token in the credential vault. Please fix the "
                "vault configuration and retry.",
                details={
                    "session_id": view.session_id,
                    "provider": provider_id.value,
                    "reason": type(exc).__name__,
                },
            ) from exc

        # --- Step 4: install the connection record ------------------
        #
        # The record is created with `auth_source="managed"`. This
        # is the ONLY place the managed source is stamped onto a
        # record; BYOA installs `auth_source="byoa"` in
        # `facade.complete_oauth_callback`.
        #
        # The `_ConnectionRecord` class lives in
        # `source_control_facade.py`; importing it lazily avoids a
        # module-level cycle (the facade does not import this
        # service at module scope, but the lazy import is cheap and
        # keeps the layering obvious).
        from .source_control_facade import _ConnectionRecord

        record = _ConnectionRecord(
            provider=provider_id.value,
            account_id=account.account_id,
            base_url=base_url,
            username=account.username or "",
            connected_at=datetime.now(timezone.utc),
            last_validated_at=datetime.now(timezone.utc),
            auth_source=AuthSource.MANAGED.value,
        )

        try:
            from .source_control_facade import get_source_control_facade
            current_facade = get_source_control_facade()
            current_facade._set_connection(record)
        except Exception as exc:  # noqa: BLE001
            # The token IS stored; the user can still use it via
            # `_require_token` once a record is installed. Surface
            # the failure so the session becomes ERROR and the
            # frontend shows an actionable message.
            logger.exception(
                "DeviceFlow: token stored but connection record "
                "update failed for '%s': %s",
                provider_id.value,
                type(exc).__name__,
            )
            raise SourceControlError(
                "Device Flow completed and the token was stored, "
                "but PELDRUN could not register the connection. "
                "Retry the connection; existing tokens will be "
                "reused.",
                details={
                    "session_id": view.session_id,
                    "provider": provider_id.value,
                    "reason": type(exc).__name__,
                },
            ) from exc

        # --- Step 5: audit ------------------------------------------
        self._audit.record(
            AuditEventType.AUTH_COMPLETE,
            severity=AuditSeverity.INFO,
            action="complete_managed_device_flow",
            message=(
                f"Managed Device Flow completed for "
                f"'{provider_id.value}' as '{account.username}'."
            ),
            payload={
                "provider": provider_id.value,
                "session_id": view.session_id,
                "account_id": account.account_id,
                "username": account.username,
                "base_url": base_url,
                "has_refresh_token": refresh_token is not None,
                "access_expires_at": (
                    access_expires_at.isoformat()
                    if access_expires_at
                    else None
                ),
                "refresh_expires_at": (
                    refresh_expires_at.isoformat()
                    if refresh_expires_at
                    else None
                ),
                "auth_source": AuthSource.MANAGED.value,
            },
            provider=provider_id.value,
            actor="user",
        )

        # Best-effort: notify the facade so any derived cache is
        # invalidated. The facade's default implementation is a
        # no-op; a future one might, for example, refresh a
        # provider-readiness cache.
        try:
            self._facade._on_auth_settings_changed(  # type: ignore[attr-defined]
                self._settings_store.get()
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 6. Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_provider_id(
        provider: ProviderId | str,
    ) -> ProviderId:
        """
        Normalize a provider argument to a `ProviderId`.

        Raises `ValidationError` with a clear message for unknown
        values. This is deliberately distinct from
        `coerce_auth_source`: a typo in a provider ID is a caller
        error, not a settings issue, and MUST NOT be silently
        defaulted.
        """
        if isinstance(provider, ProviderId):
            return provider
        if isinstance(provider, str):
            normalized = provider.strip().lower()
            if not normalized:
                raise ValidationError(
                    "provider must not be empty.",
                    details={"field": "provider"},
                )
            try:
                return ProviderId(normalized)
            except ValueError as exc:
                raise ValidationError(
                    f"Unknown provider '{provider}'.",
                    details={
                        "provider": provider,
                        "valid": [p.value for p in ProviderId],
                    },
                ) from exc
        raise ValidationError(
            "provider must be a ProviderId or a string.",
            details={"type": type(provider).__name__},
        )


# ---------------------------------------------------------------------------
# Default service singleton
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE: Optional[SourceControlAuthService] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_auth_service() -> SourceControlAuthService:
    """
    Return the process-wide default auth service, creating it on
    first use.

    The default service is wired with:

        - The default settings store (`auth_settings.json` under
          `backend/data/source_control/`).
        - The default facade (`get_source_control_facade()`), so
          connection records live in the same registry the HTTP
          router already uses.
        - The default token service and its vault.
        - A fresh `GitHubAuthService` and `GitHubDeviceFlowClient`.
        - The managed Client ID resolved from the
          `PELDRUN_GITHUB_CLIENT_ID` environment variable.

    Applications that need a different wiring (a custom facade, a
    non-default vault, or a programmatically-provided Client ID)
    SHOULD construct their own `SourceControlAuthService` and
    install it via `set_default_auth_service`.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            # Lazy imports to avoid a module-level cycle: the
            # facade module does NOT import this service, but the
            # default settings store module ALSO does not import
            # it. Importing here keeps the layering explicit and
            # the import cost off the module load path.
            from .source_control_facade import (
                get_source_control_facade,
            )
            from .source_control_auth_settings import (
                get_default_auth_settings_store,
            )

            _DEFAULT_SERVICE = SourceControlAuthService(
                settings_store=get_default_auth_settings_store(),
                facade=get_source_control_facade(),
            )
        return _DEFAULT_SERVICE


def set_default_auth_service(
    service: SourceControlAuthService,
) -> None:
    """
    Replace the process-wide default auth service.

    Intended for application startup (after the facade and the
    settings store are constructed, so the service can be wired
    with the correct references) and for tests.
    """
    if not isinstance(service, SourceControlAuthService):
        raise TypeError(
            "set_default_auth_service expects a "
            "SourceControlAuthService instance."
        )
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = service


def reset_default_auth_service() -> SourceControlAuthService:
    """
    Replace the default service with a fresh one.

    Used by tests to guarantee isolation between test cases. The
    previous service's in-memory polling tasks are NOT cancelled
    (cancellation is the caller's responsibility); a test that
    leaves a task running SHOULD cancel it explicitly.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = None
        return get_default_auth_service()
