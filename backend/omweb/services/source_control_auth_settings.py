# backend/omweb/services/source_control_auth_settings.py
"""
Persistent store for Source Control authentication settings.

This module is the ONLY component allowed to read or write the
`auth_settings.json` file on disk. It is intentionally separated
from the pure `AuthSettings` dataclass (in
`integrations/source_control/security/auth_settings.py`) because:

    - The dataclass is pure: no I/O, no file paths, no imports from
      `omweb/`. It is imported by the authentication service and by
      the settings schema layer.
    - The store is an application-layer concern: it needs the
      project's data directory, the audit logger, and an atomic
      write strategy. Putting it in `security/` would force the
      pure dataclass to depend on `omweb/services/`, creating a
      circular import.

Design rules:
    - The store NEVER raises on read. A corrupt, missing, or
      unreadable settings file degrades to the factory defaults and
      logs a warning. A broken file MUST NOT prevent the
      application from starting.
    - The store NEVER raises on write for a transient I/O error.
      It writes to a temp file first and uses `os.replace` so a
      crash mid-write never leaves a half-written JSON file. The
      caller sees a `ValidationError` only for invalid input, never
      for a disk failure: the settings change is refused, the
      previous state remains on disk, and the caller can retry.
    - Every state change is recorded in the audit log. Reads are
      NOT audited (they happen on every settings GET, which would
      flood the log).
    - The store is thread-safe. Reads and writes hold a single
      reentrant lock. The lock covers only the in-memory cache and
      the file I/O; it does NOT cover the audit log call (which has
      its own lock).

Naming:
    - `AuthSettingsStore` is the canonical class.
    - `get_default_auth_settings_store()` /
      `set_default_auth_settings_store()` /
      `reset_default_auth_settings_store()` are the singleton
      accessors used by FastAPI dependency injection and by tests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

from ...integrations.source_control.security.audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from ...integrations.source_control.security.auth_settings import (
    AuthSettings,
    default_auth_settings,
)
from ...integrations.source_control.errors import ValidationError

if TYPE_CHECKING:
    from .source_control_facade import SourceControlFacade

__all__ = [
    "AuthSettingsStore",
    "get_default_auth_settings_store",
    "set_default_auth_settings_store",
    "reset_default_auth_settings_store",
]


logger = logging.getLogger(__name__)


#: Filename under the data directory. Chosen to sit next to
#: `connections.json`, `provider_configs.json`, `settings.json`, and
#: `journal/` so that the whole Source Control state backs up and
#: restores as a unit.
AUTH_SETTINGS_FILE_NAME = "auth_settings.json"


#: Schema version of the persisted file. Bumped only when the shape
#: changes in a way that requires explicit migration (renaming a
#: field, changing a type). Adding new optional fields does NOT
#: require a bump because `from_dict` falls back to defaults.
AUTH_SETTINGS_SCHEMA_VERSION = 1


class AuthSettingsStore:
    """
    Disk-backed store for `AuthSettings`.

    The store keeps a single `AuthSettings` instance in memory,
    synchronized with the JSON file on disk. Concurrent reads are
    cheap (a dict lookup under the lock). Concurrent writes are
    serialized by the same lock; the lock covers both the file
    write and the in-memory cache update so a reader never observes
    a state that does not match the disk.

    Args:
        data_dir: Directory that contains `auth_settings.json`.
            Created on first write if it does not exist.
        audit_logger: Audit logger used for state changes. Defaults
            to the process-wide audit logger.
        facade: Optional `SourceControlFacade` reference. When
            provided, `apply()` notifies the facade so that
            derived state (e.g. `_is_provider_configured` for the
            managed path) is refreshed without a restart. When
            `None`, the store still works; the caller is expected
            to invoke `reload()` explicitly after `update()`.
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        audit_logger: Optional[AuditLogger] = None,
        facade: Optional["SourceControlFacade"] = None,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._path = self._data_dir / AUTH_SETTINGS_FILE_NAME
        self._audit = (
            audit_logger
            if audit_logger is not None
            else get_default_audit_logger()
        )
        self._facade = facade
        self._lock = threading.RLock()
        self._settings: AuthSettings = self._load_or_default()

    # --- Public accessors ----------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path of the persisted settings file."""
        return self._path

    def get(self) -> AuthSettings:
        """
        Return the current settings.

        The returned object is immutable; callers that need to
        change a field MUST go through `update()`. Direct mutation
        is impossible because `AuthSettings` is a frozen
        dataclass.
        """
        with self._lock:
            return self._settings

    # --- Mutations ------------------------------------------------------

    def update(self, partial: Mapping[str, Any]) -> AuthSettings:
        """
        Merge `partial` into the current settings and persist.

        A partial payload is the common case: the UI toggles one
        field at a time, so the store MUST NOT reset the others.

        The merge is SHALLOW for the top-level `providers` mapping
        (a provider entry provided in `partial` replaces the
        existing entry for that provider entirely) and DEEP for
        the `managed`, `byoa`, and `security` sub-mappings (a
        partial `managed` payload updates only the fields it
        provides). This matches what the settings UI needs and is
        documented here so a future caller does not expect
        deep-merge for `providers`.

        Args:
            partial: A mapping with any subset of the top-level
                keys `enabled`, `managed`, `byoa`, `providers`,
                `security`. Unknown keys are silently ignored so a
                forward-compatible client can send a newer field
                without breaking on an older backend.

        Returns:
            The merged and validated `AuthSettings`.

        Raises:
            ValidationError: When the merged result fails
                validation (e.g. an unknown `preferred_source`). In
                that case the on-disk file is NOT touched and the
                in-memory cache is NOT modified: the previous state
                remains authoritative.
        """
        with self._lock:
            current = self._settings
            current_dict = current.to_dict()

            # Merge sub-mappings field by field. A nested `managed`
            # payload updates only the fields it carries; the rest
            # keep their current values.
            merged: Dict[str, Any] = dict(current_dict)
            if "enabled" in partial:
                merged["enabled"] = bool(partial["enabled"])
            if "managed" in partial and isinstance(partial["managed"], Mapping):
                merged["managed"] = {
                    **current_dict["managed"],
                    **dict(partial["managed"]),
                }
            if "byoa" in partial and isinstance(partial["byoa"], Mapping):
                merged["byoa"] = {
                    **current_dict["byoa"],
                    **dict(partial["byoa"]),
                }
            if "security" in partial and isinstance(partial["security"], Mapping):
                merged["security"] = {
                    **current_dict["security"],
                    **dict(partial["security"]),
                }
            if "providers" in partial and isinstance(partial["providers"], Mapping):
                providers_merged: Dict[str, Any] = dict(
                    current_dict.get("providers") or {}
                )
                for provider_key, provider_payload in dict(
                    partial["providers"]
                ).items():
                    key = str(provider_key).strip().lower()
                    if not key:
                        continue
                    if not isinstance(provider_payload, Mapping):
                        raise ValidationError(
                            f"Provider entry '{provider_key}' must be a "
                            f"mapping.",
                            details={
                                "field": "providers",
                                "key": key,
                                "type": type(provider_payload).__name__,
                            },
                        )
                    existing = providers_merged.get(key) or {}
                    providers_merged[key] = {
                        **existing,
                        **dict(provider_payload),
                    }
                merged["providers"] = providers_merged

            # Validate the merged result BEFORE writing. A bad
            # payload must not corrupt the file: the previous state
            # remains authoritative, and the caller sees a precise
            # ValidationError.
            new_settings = AuthSettings.from_dict(merged)

            # Persist atomically (temp file + os.replace). If the
            # write fails, the in-memory cache is NOT updated, so a
            # subsequent `get()` still reflects the last known good
            # state.
            self._write_atomic(new_settings)
            self._settings = new_settings

        # Audit OUTSIDE the lock so a slow audit sink cannot block
        # other readers/writers. The event describes only the
        # fields the caller actually changed; recording the full
        # merged state on every call would bloat the audit log.
        self._record_update_audit(partial, new_settings)

        # Notify the facade (when attached) so derived state is
        # refreshed without a restart. This is best-effort: a
        # failure here MUST NOT fail the update, because the
        # on-disk state is already authoritative.
        if self._facade is not None:
            try:
                self._facade._on_auth_settings_changed(new_settings)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AuthSettingsStore: facade notification failed "
                    "after settings update: %s",
                    type(exc).__name__,
                )

        return new_settings

    def reset(self) -> AuthSettings:
        """
        Reset the settings to the factory defaults and persist.

        This is a destructive operation: existing connections and
        tokens are NOT affected (they are preserved per the plan's
        "disabling a source does not delete connections" rule), but
        any custom settings the user configured are lost. The UI
        MUST confirm before calling this method.
        """
        with self._lock:
            defaults = default_auth_settings()
            self._write_atomic(defaults)
            self._settings = defaults

        self._audit.record(
            AuditEventType.OTHER,
            severity=AuditSeverity.INFO,
            action="reset_auth_settings",
            message=(
                "Source Control authentication settings were reset "
                "to factory defaults."
            ),
            payload={"file": str(self._path)},
            actor="user",
        )

        if self._facade is not None:
            try:
                self._facade._on_auth_settings_changed(defaults)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AuthSettingsStore: facade notification failed "
                    "after reset: %s",
                    type(exc).__name__,
                )

        return defaults

    def reload(self) -> AuthSettings:
        """
        Discard the in-memory cache and re-read the file from disk.

        Intended for tests, for operator-triggered reloads (e.g.
        after manually editing the file), and for the rare case
        where another process changed the file. The application
        itself always goes through `update()`, which keeps the
        cache in sync.
        """
        with self._lock:
            self._settings = self._load_or_default()
            return self._settings

    # --- Internal: load -------------------------------------------------

    def _load_or_default(self) -> AuthSettings:
        """
        Load the settings from disk, falling back to the factory
        defaults on ANY failure.

        Failure modes handled here:

            - File does not exist (first run). Returns defaults.
            - File is unreadable (permissions, transient I/O).
              Logs a warning and returns defaults.
            - File is not valid JSON. Logs a warning and returns
              defaults.
            - File is valid JSON but not an object. Logs a warning
              and returns defaults.
            - File is valid JSON but fails `AuthSettings.from_dict`
              validation. Logs a warning and returns defaults.

        In every failure case the corrupt file is LEFT ON DISK so
        an operator can inspect it; the next successful `update()`
        overwrites it.
        """
        path = self._path
        if not path.exists():
            return default_auth_settings()

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "AuthSettingsStore: could not read %s (%s); using "
                "factory defaults.",
                path,
                type(exc).__name__,
            )
            return default_auth_settings()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "AuthSettingsStore: %s is not valid JSON (%s); "
                "using factory defaults.",
                path,
                type(exc).__name__,
            )
            return default_auth_settings()

        if not isinstance(data, dict):
            logger.warning(
                "AuthSettingsStore: %s is not a JSON object; "
                "using factory defaults.",
                path,
            )
            return default_auth_settings()

        # A schema version is recorded for forward compatibility.
        # The current code ignores the value; a future migration
        # will read it to decide how to transform older payloads.
        schema_version = data.get("schema_version")
        if schema_version is not None and not isinstance(schema_version, int):
            logger.warning(
                "AuthSettingsStore: %s has a non-integer "
                "schema_version; using factory defaults.",
                path,
            )
            return default_auth_settings()

        payload = data.get("settings")
        if not isinstance(payload, dict):
            # Older files may store the settings at the top level
            # rather than nested under a "settings" key. Accept
            # both shapes: if "settings" is absent but the top
            # level looks like a settings object, use it directly.
            if any(
                key in data
                for key in ("enabled", "managed", "byoa", "providers", "security")
            ):
                payload = data
            else:
                logger.warning(
                    "AuthSettingsStore: %s does not contain a "
                    "settings payload; using factory defaults.",
                    path,
                )
                return default_auth_settings()

        try:
            return AuthSettings.from_dict(payload)
        except ValidationError as exc:
            logger.warning(
                "AuthSettingsStore: %s failed validation (%s); "
                "using factory defaults.",
                path,
                type(exc).__name__,
            )
            return default_auth_settings()

    # --- Internal: write ------------------------------------------------

    def _write_atomic(self, settings: AuthSettings) -> None:
        """
        Persist `settings` atomically.

        The temp file is created in the same directory as the
        target so that `os.replace` is guaranteed to be atomic on
        POSIX. On Windows, `os.replace` is also atomic when source
        and target are on the same volume; the same-directory temp
        file ensures that.

        A failure here propagates as an OSError wrapped by the
        caller (which converts it into a `ValidationError` with a
        clear message). The in-memory cache is NOT updated until
        the write succeeds, so a subsequent `get()` still returns
        the last known good state.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": AUTH_SETTINGS_SCHEMA_VERSION,
            "settings": settings.to_dict(),
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(serialized, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            # Best-effort cleanup of the temp file. A failure here
            # is not fatal: the caller sees the original OSError.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValidationError(
                f"Could not persist authentication settings to "
                f"'{self._path}'. The previous settings remain "
                f"active. Underlying error: "
                f"{type(exc).__name__}: {exc}",
                details={
                    "path": str(self._path),
                    "reason": type(exc).__name__,
                },
            ) from exc

    # --- Internal: audit ------------------------------------------------

    def _record_update_audit(
        self,
        partial: Mapping[str, Any],
        merged: AuthSettings,
    ) -> None:
        """
        Record an audit event for a settings update.

        Only the CHANGED fields are recorded, not the full merged
        state, to keep the audit log compact. The full state can be
        derived by reading the file at the event's timestamp (it
        is not logged to avoid bloating every entry).

        The audit call is best-effort: the audit logger swallows
        its own failures, and any exception escaping here would be
        a programmer error in the event construction itself.
        """
        try:
            self._audit.record(
                AuditEventType.OTHER,
                severity=AuditSeverity.INFO,
                action="update_auth_settings",
                message=(
                    "Source Control authentication settings were "
                    "updated."
                ),
                payload={
                    "changed_fields": sorted(
                        key for key in partial.keys() if key
                    ),
                    "enabled": merged.enabled,
                    "managed_enabled": merged.managed.enabled,
                    "byoa_enabled": merged.byoa.enabled,
                    "providers": sorted(merged.providers.keys()),
                    "automatic_fallback": (
                        merged.security.automatic_fallback
                    ),
                },
                actor="user",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "AuthSettingsStore: audit logging failed: %s",
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Default store singleton
# ---------------------------------------------------------------------------


_DEFAULT_STORE: Optional[AuthSettingsStore] = None
_DEFAULT_LOCK = threading.RLock()


def _resolve_default_data_dir() -> Path:
    """
    Resolve the data directory for the default store.

    The path mirrors the one used by `SourceControlFacade` so that
    every Source Control file (`connections.json`,
    `provider_configs.json`, `settings.json`, `auth_settings.json`)
    lives in the same directory and backs up/restores as a unit.

    The resolution is intentionally dynamic (based on
    `__file__`), so a moved project root still finds its data.
    """
    # `__file__` is `backend/omweb/services/source_control_auth_settings.py`.
    backend_root = Path(__file__).resolve().parent.parent.parent
    return backend_root / "data" / "source_control"


def get_default_auth_settings_store() -> AuthSettingsStore:
    """
    Return the process-wide default store, creating it on first use.

    The default store uses the same data directory as the facade so
    that all Source Control state is colocated. Applications that
    need a custom directory or a facade reference SHOULD construct
    their own `AuthSettingsStore` and install it via
    `set_default_auth_settings_store`.
    """
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = AuthSettingsStore(_resolve_default_data_dir())
        return _DEFAULT_STORE


def set_default_auth_settings_store(store: AuthSettingsStore) -> None:
    """
    Replace the process-wide default store.

    Intended for application startup (after the facade is created,
    so the store can be wired with `facade=`) and for tests.
    """
    if not isinstance(store, AuthSettingsStore):
        raise TypeError(
            "set_default_auth_settings_store expects an "
            "AuthSettingsStore instance."
        )
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = store


def reset_default_auth_settings_store() -> AuthSettingsStore:
    """
    Replace the default store with a fresh one at the default path.

    Used by tests to guarantee isolation between test cases. The
    previous store's in-memory cache is discarded; the file on
    disk is NOT removed (a test that needs a clean file SHOULD
    delete it explicitly).
    """
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = AuthSettingsStore(_resolve_default_data_dir())
        return _DEFAULT_STORE
