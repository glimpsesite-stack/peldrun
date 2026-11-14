// frontend/src/features/source-control/types/authentication.ts

/**
 * Type definitions for the Source Control authentication layer.
 *
 * These types mirror the backend's Python enums and dataclasses:
 *
 *   - `AuthSource`           ← backend `AuthSource` (str enum).
 *   - `DeviceFlowStatus`     ← backend `DeviceFlowStatus` (str enum).
 *   - `AuthSettings`         ← backend `AuthSettings.to_dict()`.
 *   - `AuthStatus`           ← backend `AuthService.get_auth_status()`.
 *   - `DeviceFlowStart`      ← backend `DeviceFlowStartResponse`.
 *   - `DeviceFlowStatus`     ← backend `DeviceFlowStatusResponse`.
 *
 * The wire format is the snake_case JSON produced by the backend
 * Pydantic models. The client-facing types below use camelCase for
 * ergonomics; the API client (in `api/source-control-client.ts`)
 * performs the transformation on both directions. This mirrors the
 * pattern used by every other feature in this folder.
 *
 * Design rules:
 *   - This file MUST NOT import from `react` or from any component.
 *     It is imported by hooks, components, and (indirectly) by the
 *     settings store. Keeping it pure avoids cycles.
 *   - Enum values MUST match the backend's `value` strings exactly.
 *     A mismatch would silently break the UI state machine: the
 *     frontend would treat every "pending" status as "unknown" if
 *     the spelling differed by a single character.
 *   - Types that describe a payload (Request/Response) MAY be
 *     replaced with a Zod schema in `api/schemas.ts`. The
 *     declarations here remain the single source of truth for the
 *     names and shapes consumed by components.
 */

// ----------------------------------------------------------------------
// Enums (must match backend values exactly)
// ----------------------------------------------------------------------

/**
 * The origin of an authentication credential.
 *
 *   - `managed`         : PELDRUN GitHub App via Device Flow.
 *   - `byoa`            : User-provided OAuth App.
 *   - `legacy_unknown`  : A connection that predates the
 *                         `auth_source` field. Preserved as-is;
 *                         the user must re-authorize to move it to
 *                         a known source.
 */
export type AuthSource = "managed" | "byoa" | "legacy_unknown";

/**
 * The set of sources a user may SELECT for a new connection.
 * `legacy_unknown` is intentionally excluded: it is a migration
 * marker, not a selectable strategy.
 */
export type SelectableAuthSource = "managed" | "byoa";

/**
 * Lifecycle of a Device Flow session.
 *
 * Transitions (single source of truth, mirrored from the backend):
 *
 *   pending   → completed | denied | expired | cancelled | error
 *   <any>     → terminal
 *
 * The `pending` state means the backend is still polling GitHub for
 * the token. Any other state is terminal; the frontend MUST stop
 * polling when it sees one.
 */
export type DeviceFlowStatus =
  | "pending"
  | "completed"
  | "denied"
  | "expired"
  | "cancelled"
  | "error";

// ----------------------------------------------------------------------
// Settings shape (matches backend `AuthSettings.to_dict()`)
// ----------------------------------------------------------------------

export interface ManagedAuthSettings {
  enabled: boolean;
  /**
   * Only `"github"` is supported in this phase. The backend rejects
   * any other value at validation time, so the field is typed as
   * `string` rather than a literal to allow a future provider
   * without a TypeScript change.
   */
  provider: string;
  /**
   * Only `"device_flow"` is supported. Same rationale as
   * `provider`: keep the field permissive to allow future strategies
   * (e.g. `"authorization_code"` for BYOA-only providers).
   */
  strategy: string;
}

export interface ByoaAuthSettings {
  enabled: boolean;
}

export interface ProviderAuthSettings {
  preferred_source: SelectableAuthSource;
  managed_enabled: boolean;
  byoa_enabled: boolean;
}

export interface AuthSecuritySettings {
  /**
   * When `true`, a flow that cannot start with the preferred source
   * MAY silently fall back to the other source. The backend
   * defaults this to `false` and the plan explicitly requires
   * `false`: silent fallback hides configuration errors.
   */
  automatic_fallback: boolean;
  /**
   * When `true`, the backend refuses to store tokens in any vault
   * that is not a real OS keychain. Defaults to `true`.
   */
  require_secure_vault: boolean;
}

/**
 * The full authentication settings aggregate.
 *
 * Wire shape: exactly matches `AuthSettings.to_dict()` in
 * `backend/integrations/source_control/security/auth_settings.py`.
 * Field names are snake_case because they round-trip through the
 * backend without transformation.
 */
export interface AuthSettings {
  enabled: boolean;
  managed: ManagedAuthSettings;
  byoa: ByoaAuthSettings;
  providers: Record<string, ProviderAuthSettings>;
  security: AuthSecuritySettings;
}

// ----------------------------------------------------------------------
// Status shape (matches backend `AuthService.get_auth_status()`)
// ----------------------------------------------------------------------

export interface AuthManagedStatus {
  enabled: boolean;
  provider: string;
  strategy: string;
  /**
   * `true` when the backend resolved a public Client ID for the
   * managed GitHub App. The client ID value itself is NEVER exposed
   * to the frontend: only this boolean.
   */
  client_id_configured: boolean;
}

export interface AuthByoaStatus {
  enabled: boolean;
}

export interface AuthVaultStatus {
  /**
   * Backend name of the active credential vault (e.g. `"keyring"`,
   * `"encrypted-file"`, `"fallback"`).
   */
  backend: string;
  /** `true` when the vault backend reports itself operational. */
  available: boolean;
}

export interface AuthProviderConnection {
  /**
   * Which source created the current connection. `"legacy_unknown"`
   * for connections that predate the `auth_source` field.
   */
  auth_source: AuthSource;
  username: string | null;
  account_id: string | null;
  connected_at: string | null;
}

export interface AuthProviderStatus {
  /** `true` when the managed path may be used to START a new connection. */
  managed_ready: boolean;
  /** `true` when the BYOA path may be used to START a new connection. */
  byoa_ready: boolean;
  /** The source the UI should pre-select for the Connect button. */
  preferred_source: SelectableAuthSource;
  /**
   * The current connection record, or `null` when the provider is
   * not connected.
   */
  connection: AuthProviderConnection | null;
}

/**
 * The full authentication status payload.
 *
 * Wire shape: exactly matches `GET /auth/settings` and
 * `GET /auth/status`. Both endpoints return the same structure;
 * the duplication is intentional (see the backend router comments).
 */
export interface AuthStatus {
  settings: AuthSettings;
  managed: AuthManagedStatus;
  byoa: AuthByoaStatus;
  vault: AuthVaultStatus;
  providers: Record<string, AuthProviderStatus>;
}

// ----------------------------------------------------------------------
// Device Flow payloads
// ----------------------------------------------------------------------

/**
 * Response of `POST /connections/{provider}/device/start`.
 *
 * The `session_id` is opaque to the frontend; it is only used to
 * poll the status endpoint. The `user_code` and `verification_uri`
 * are displayed verbatim to the user. No secret material is
 * present in this payload: `device_code` never leaves the backend.
 */
export interface DeviceFlowStart {
  session_id: string;
  user_code: string;
  verification_uri: string;
  expires_in_seconds: number;
  interval_seconds: number;
  status: "pending";
  provider: string;
}

/**
 * Response of `GET /connections/{provider}/device/{session_id}/status`
 * and `POST /connections/{provider}/device/{session_id}/cancel`.
 *
 * The `account` field is populated only when `status === "completed"`.
 * Its shape is intentionally loose (`Record<string, unknown>`) because
 * the backend currently returns `{provider, account_id, username}`
 * but the exact keys may grow; a strict type here would force a
 * matching change in two places for every future extension.
 */
export interface DeviceFlowStatusPayload {
  session_id: string;
  provider: string;
  status: DeviceFlowStatus;
  user_code: string | null;
  verification_uri: string | null;
  expires_in_seconds: number;
  interval_seconds: number;
  error_message: string | null;
  account: Record<string, unknown> | null;
}

// ----------------------------------------------------------------------
// Settings update payload
// ----------------------------------------------------------------------

/**
 * Partial payload for `PUT /auth/settings`.
 *
 * Every field is optional: the backend deep-merges `managed`,
 * `byoa`, and `security`, and shallow-merges `providers`. The
 * caller SHOULD only include the fields the user actually changed;
 * sending the whole `AuthSettings` object is supported (it will
 * overwrite with identical values) but discouraged because it
 * produces a noisier audit log.
 */
export interface AuthSettingsUpdate {
  enabled?: boolean;
  managed?: Partial<ManagedAuthSettings>;
  byoa?: Partial<ByoaAuthSettings>;
  providers?: Record<string, Partial<ProviderAuthSettings>>;
  security?: Partial<AuthSecuritySettings>;
}

// ----------------------------------------------------------------------
// UI-only types
// ----------------------------------------------------------------------

/**
 * Which provider the "Connect" button is targeting on the
 * Integrations page. Used to decide which dialog to open.
 *
 *   - `managed` : open `DeviceFlowDialog`.
 *   - `byoa`    : open `ProviderConnectDialog` + ProviderConfigDialog
 *                 if credentials are not yet configured.
 *
 * The UI MUST NOT open both dialogs simultaneously; the choice is
 * made on the preferred source, which the backend already computes
 * via `AuthSettings.preferred_source_for(...)`.
 */
export type ConnectTarget = "managed" | "byoa";

/**
 * Local state of the Device Flow dialog, driven by `useSourceControlAuth`.
 *
 * The dialog shows different UI based on the phase:
 *
 *   - `starting`  : the start API call is in flight; show a spinner.
 *   - `showing_code`: the user code is displayed; poll status.
 *   - `completed` : the connection succeeded; close on success.
 *   - `failed`    : terminal error; show the message and a retry.
 *   - `cancelled` : the user cancelled; close immediately.
 *
 * The `idle` phase is used before the dialog opens and after it
 * closes; it is never rendered by the dialog itself.
 */
export type DeviceFlowPhase =
  | "idle"
  | "starting"
  | "showing_code"
  | "completed"
  | "failed"
  | "denied"
  | "expired"
  | "cancelled";
