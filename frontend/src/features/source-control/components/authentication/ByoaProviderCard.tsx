// frontend/src/features/source-control/components/authentication/ByoaProviderCard.tsx

"use client";

import React from "react";

import type {
  AuthByoaStatus,
  AuthProviderStatus,
} from "../../types/authentication";
import { AuthSourceIndicator } from "./AuthSourceIndicator";

/**
 * Props for `ByoaProviderCard`.
 *
 * The BYOA card differs from the managed card in three ways:
 *
 *   1. It has TWO independent actions: "Configure credentials" and
 *      "Connect". Configuring credentials is a prerequisite for
 *      connecting; the card disables "Connect" until both the
 *      Client ID and Client Secret are present.
 *   2. It exposes a toggle to enable/disable the whole BYOA path
 *      (this is a settings change, not a connection change).
 *   3. It has a distinct "Configured but not connected" state
 *      that the managed card does not need: BYOA users often
 *      set up credentials once and connect later.
 *
 * The card is presentation-only: all state comes from props and
 * all actions are callbacks. The parent (AuthenticationSettingsPanel)
 * owns the state, the credential dialog, and the OAuth flow.
 */
export interface ByoaProviderCardProps {
  /** The BYOA block of the auth status. */
  byoa: AuthByoaStatus;
  /** The provider-specific status (byoa_ready, connection, ...). */
  provider: AuthProviderStatus;
  /**
   * `true` when the provider's BYOA credentials (Client ID +
   * Secret) are configured. This comes from the provider descriptor
   * (`provider.is_configured`), NOT from the auth status: the auth
   * status only reports `byoa_ready`, which combines the enabled
   * flag with credential presence. The card uses the raw
   * `is_configured` value so it can show "credentials missing"
   * distinctly from "path disabled".
   */
  credentialsConfigured: boolean;
  /** Disables every action while an operation is in flight. */
  busy?: boolean;
  /** Toggles the BYOA path on or off (a settings change). */
  onToggleEnabled: (enabled: boolean) => void;
  /** Opens the OAuth credential dialog. */
  onConfigureCredentials: () => void;
  /** Starts the BYOA OAuth flow (system browser). */
  onConnect: () => void;
  /** Optional: disconnect the current BYOA connection. */
  onDisconnect?: () => void;
  /** Optional: open the credential dialog in "delete" mode. */
  onDeleteCredentials?: () => void;
}

/**
 * Card that renders the status of the BYOA path and the actions to
 * configure, connect, or disconnect.
 *
 * Behavior matrix:
 *
 *   byoa.enabled  credentialsConfigured  connection  → UI
 *   ─────────────────────────────────────────────────────────────
 *   false         *                      *           → "Disabled"
 *                                                      + Enable toggle
 *   true          false                  null        → "Credentials
 *                                                      required"
 *                                                      + Configure btn
 *   true          true                   null        → "Ready"
 *                                                      + Connect btn
 *                                                      + Reconfigure
 *   true          true                   set         → "Connected as ..."
 *                                                      + Disconnect
 *                                                      + Delete creds
 *
 * The card does NOT enforce that `byoa.enabled === true` before
 * showing the Configure button: an operator may want to set up
 * credentials first and enable the path later. It DOES hide the
 * Connect button in every state where the backend would reject
 * the call, so the user is never surprised by a 403.
 */
export function ByoaProviderCard({
  byoa,
  provider,
  credentialsConfigured,
  busy = false,
  onToggleEnabled,
  onConfigureCredentials,
  onConnect,
  onDisconnect,
  onDeleteCredentials,
}: ByoaProviderCardProps) {
  const isConnected = provider.connection !== null;
  const isEnabled = byoa.enabled;

  return (
    <div className="flex flex-col rounded-lg border border-gray-200 bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 border-b border-gray-100 p-4">
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-gray-900">
            Bring Your Own App (BYOA)
          </h3>
          <p className="mt-1 text-xs text-gray-500">
            Use your own OAuth App credentials for advanced
            configurations. Disabled by default.
          </p>
        </div>
        <div className="shrink-0">
          <StatusPill
            tone={isEnabled ? "success" : "neutral"}
            label={isEnabled ? "Enabled" : "Disabled"}
          />
        </div>
      </div>

      {/* Body */}
      <div className="space-y-3 p-4">
        {/* Enable toggle */}
        <div className="flex items-start justify-between gap-3 rounded-md border border-gray-200 bg-gray-50 p-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-gray-900">
              Enable BYOA path
            </p>
            <p className="mt-0.5 text-xs text-gray-500">
              When disabled, no new BYOA connection can be started.
              Existing connections and tokens are preserved.
            </p>
          </div>
          <ToggleSwitch
            checked={isEnabled}
            onChange={onToggleEnabled}
            disabled={busy}
            ariaLabel="Enable BYOA authentication path"
          />
        </div>

        {/* Connected state summary */}
        {isConnected && provider.connection && (
          <div className="rounded-md border border-gray-200 bg-gray-50 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-gray-700">
                  Connected as{" "}
                  <span className="font-medium text-gray-900">
                    {provider.connection.username ||
                      provider.connection.account_id ||
                      "unknown account"}
                  </span>
                </p>
                {provider.connection.connected_at && (
                  <p className="mt-0.5 text-xs text-gray-500">
                    Since{" "}
                    {new Date(
                      provider.connection.connected_at
                    ).toLocaleString()}
                  </p>
                )}
              </div>
              <AuthSourceIndicator
                source={provider.connection.auth_source}
                compact
              />
            </div>
          </div>
        )}

        {/* Credentials status row (always visible when enabled) */}
        {isEnabled && (
          <div className="rounded-md border border-gray-200 bg-white p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-900">
                  OAuth credentials
                </p>
                <p className="mt-0.5 text-xs text-gray-500">
                  {credentialsConfigured
                    ? "Client ID and Client Secret are stored."
                    : "Client ID and Client Secret are not configured."}
                </p>
              </div>
              <StatusPill
                tone={credentialsConfigured ? "success" : "warning"}
                label={
                  credentialsConfigured ? "Configured" : "Missing"
                }
              />
            </div>
          </div>
        )}

        {/* Explanation for the not-connected case */}
        {!isConnected && isEnabled && !credentialsConfigured && (
          <p className="text-sm text-gray-600">
            Configure your OAuth App credentials first, then click{" "}
            <strong>Connect</strong> to launch the authorization
            flow in your browser.
          </p>
        )}

        {!isConnected && isEnabled && credentialsConfigured && (
          <p className="text-sm text-gray-600">
            Click <strong>Connect</strong> to launch the
            authorization flow in your browser. PELDRUN will receive
            an authorization code via a loopback callback and
            exchange it for a token.
          </p>
        )}

        {!isEnabled && (
          <p className="text-sm text-gray-600">
            Enable the BYOA path above to configure credentials and
            connect an OAuth App. Existing connections are not
            affected by this toggle.
          </p>
        )}
      </div>

      {/* Footer actions */}
      <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 bg-gray-50 p-4">
        {/* Configure credentials: always available when enabled */}
        {isEnabled && (
          <button
            type="button"
            onClick={onConfigureCredentials}
            disabled={busy}
            className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {credentialsConfigured
              ? "Reconfigure credentials"
              : "Configure credentials"}
          </button>
        )}

        {/* Connect: only when enabled, configured, and not connected */}
        {isEnabled && credentialsConfigured && !isConnected && (
          <button
            type="button"
            onClick={onConnect}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-md border border-transparent bg-blue-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy && (
              <svg
                className="h-4 w-4 animate-spin text-white"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
                />
              </svg>
            )}
            Connect
          </button>
        )}

        {/* Delete credentials: only when configured and enabled */}
        {isEnabled && credentialsConfigured && onDeleteCredentials && (
          <button
            type="button"
            onClick={onDeleteCredentials}
            disabled={busy}
            className="inline-flex items-center rounded-md border border-transparent bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Delete credentials
          </button>
        )}

        {/* Disconnect: only when connected */}
        {isConnected && onDisconnect && (
          <button
            type="button"
            onClick={onDisconnect}
            disabled={busy}
            className="inline-flex items-center rounded-md border border-transparent bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Disconnect
          </button>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Internal presentational helpers
// ----------------------------------------------------------------------

interface StatusPillProps {
  tone: "neutral" | "success" | "warning" | "danger";
  label: string;
}

/**
 * Status badge shared with the managed card's visual language.
 * Kept local to this file because the two cards currently diverge
 * in layout enough that a shared component would need more props
 * than it saves.
 */
function StatusPill({ tone, label }: StatusPillProps) {
  const toneClasses: Record<StatusPillProps["tone"], string> = {
    neutral: "bg-gray-100 text-gray-800 border-gray-200",
    success: "bg-green-100 text-green-800 border-green-200",
    warning: "bg-amber-100 text-amber-800 border-amber-200",
    danger: "bg-red-100 text-red-800 border-red-200",
  };
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-2.5 py-0.5",
        "text-xs font-medium",
        toneClasses[tone],
      ].join(" ")}
    >
      {label}
    </span>
  );
}

interface ToggleSwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
}

/**
 * Minimal accessible toggle. Uses `role="switch"` so screen readers
 * announce the state correctly, and keeps the implementation free
 * of external dependencies.
 */
function ToggleSwitch({
  checked,
  onChange,
  disabled = false,
  ariaLabel,
}: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors",
        "focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2",
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
        checked ? "bg-blue-600" : "bg-gray-300",
      ].join(" ")}
    >
      <span
        className={[
          "inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-6" : "translate-x-1",
        ].join(" ")}
      />
    </button>
  );
}
