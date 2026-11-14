// frontend/src/features/source-control/components/authentication/ManagedGitHubCard.tsx

"use client";

import React from "react";

import type {
  AuthManagedStatus,
  AuthProviderStatus,
} from "../../types/authentication";
import { AuthSourceIndicator } from "./AuthSourceIndicator";

/**
 * Props for `ManagedGitHubCard`.
 *
 * The card is a PURE presentation component: it renders whatever
 * state the parent passes in, and delegates every action through
 * callbacks. This keeps it trivially testable and lets the parent
 * decide when to open the Device Flow dialog, when to disable the
 * button, and where to surface errors.
 */
export interface ManagedGitHubCardProps {
  /** The managed block of the auth status. */
  managed: AuthManagedStatus;
  /** The provider-specific status (managed_ready, connection, ...). */
  provider: AuthProviderStatus;
  /**
   * When `true`, the card disables its actions. Used while a
   * Device Flow session is starting or while the settings are
   * being saved, to prevent a double-start.
   */
  busy?: boolean;
  /**
   * Optional override for the primary action's label. Defaults to
   * a label derived from `provider.managed_ready` and the current
   * connection state. Callers rarely need to override it, but the
   * escape hatch exists for embedding scenarios (e.g. a wizard
   * step that wants "Continue" instead of "Connect").
   */
  actionLabel?: string;
  /** Called when the user clicks the primary action button. */
  onConnect: () => void;
  /** Called when the user clicks "Disconnect". Optional. */
  onDisconnect?: () => void;
  /** Called when the user clicks "Validate". Optional. */
  onValidate?: () => void;
}

/**
 * Card that renders the status of the managed GitHub App and the
 * primary actions to connect, validate, or disconnect.
 *
 * Behavior matrix:
 *
 *   managed.enabled  client_id_configured  connection  → UI
 *   ─────────────────────────────────────────────────────────────
 *   false            *                     *           → "Disabled" banner
 *                                                         no action
 *   true             false                 *           → "Not configured"
 *                                                         banner with an
 *                                                         operator hint
 *   true             true                  null        → "Connect" button
 *   true             true                  set         → "Connected as ..."
 *                                                         + Validate + Disconnect
 *
 * The `provider.managed_ready` flag (which combines enabled,
 * per-provider flag, and client_id presence) is what the parent
 * uses to decide whether to allow the click. This component does
 * NOT re-check it: if the parent opens the dialog, the backend
 * will still validate the state and reject a disabled source with
 * a typed error, so a stale UI cannot start a flow that the
 * backend forbids.
 */
export function ManagedGitHubCard({
  managed,
  provider,
  busy = false,
  actionLabel,
  onConnect,
  onDisconnect,
  onValidate,
}: ManagedGitHubCardProps) {
  const isConnected = provider.connection !== null;

  // ------------------------------------------------------------------
  // Disabled (managed path turned off in settings)
  // ------------------------------------------------------------------
  if (!managed.enabled) {
    return (
      <CardShell
        title="Managed GitHub App"
        subtitle="Connect with PELDRUN's official GitHub App. No OAuth credentials required."
        status={<StatusPill tone="neutral" label="Disabled" />}
      >
        <p className="text-sm text-gray-600">
          The managed GitHub App is disabled in settings. A developer
          with access to this installation can enable it from the
          settings panel above.
        </p>
      </CardShell>
    );
  }

  // ------------------------------------------------------------------
  // Enabled but no Client ID configured (operator problem)
  // ------------------------------------------------------------------
  if (!managed.client_id_configured) {
    return (
      <CardShell
        title="Managed GitHub App"
        subtitle="Connect with PELDRUN's official GitHub App. No OAuth credentials required."
        status={<StatusPill tone="warning" label="Not configured" />}
      >
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
          <p className="font-medium">
            Managed Client ID is not configured.
          </p>
          <p className="mt-1 text-xs text-amber-700">
            An operator must set the{" "}
            <code className="rounded bg-amber-100 px-1 py-0.5 font-mono text-[11px]">
              PELDRUN_GITHUB_CLIENT_ID
            </code>{" "}
            environment variable and restart PELDRUN. Until then,
            the managed path cannot start a connection.
          </p>
        </div>
      </CardShell>
    );
  }

  // ------------------------------------------------------------------
  // Ready: either connect or show connected state
  // ------------------------------------------------------------------
  const primaryLabel = actionLabel
    ? actionLabel
    : isConnected
    ? "Reconnect with GitHub"
    : "Connect with GitHub";

  return (
    <CardShell
      title="Managed GitHub App"
      subtitle="Connect with PELDRUN's official GitHub App. No OAuth credentials required."
      status={
        isConnected ? (
          <StatusPill tone="success" label="Connected" />
        ) : (
          <StatusPill tone="neutral" label="Not connected" />
        )
      }
    >
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

      {/* Explanation when not connected */}
      {!isConnected && (
        <p className="text-sm text-gray-600">
          Click <strong>Connect with GitHub</strong> to start a
          Device Flow. GitHub will show a short code that you enter
          on{" "}
          <code className="rounded bg-gray-100 px-1 py-0.5 font-mono text-[11px]">
            github.com/login/device
          </code>
          . No Client Secret is used on this path.
        </p>
      )}

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2 pt-2">
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
          {primaryLabel}
        </button>

        {isConnected && onValidate && (
          <button
            type="button"
            onClick={onValidate}
            disabled={busy}
            className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Validate
          </button>
        )}

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
    </CardShell>
  );
}

// ----------------------------------------------------------------------
// Internal presentational helpers
// ----------------------------------------------------------------------

interface CardShellProps {
  title: string;
  subtitle: string;
  status: React.ReactNode;
  children: React.ReactNode;
}

/**
 * The outer chrome shared by every branch of the card. Extracting
 * it keeps the three return paths above focused on their content
 * and guarantees a consistent layout across states.
 */
function CardShell({ title, subtitle, status, children }: CardShellProps) {
  return (
    <div className="flex flex-col rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-3 border-b border-gray-100 p-4">
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-gray-900">
            {title}
          </h3>
          <p className="mt-1 text-xs text-gray-500">{subtitle}</p>
        </div>
        <div className="shrink-0">{status}</div>
      </div>
      <div className="space-y-3 p-4">{children}</div>
    </div>
  );
}

interface StatusPillProps {
  tone: "neutral" | "success" | "warning" | "danger";
  label: string;
}

/**
 * Small status badge shown in the card header. Four tones cover
 * every state the card can be in:
 *
 *   - `neutral` : not connected, or the managed path is disabled.
 *   - `success` : connected.
 *   - `warning` : enabled but not configured (operator action required).
 *   - `danger`  : a transient failure (kept for symmetry; the card
 *                 currently uses `failed` state via the parent's
 *                 error message, not a pill).
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
