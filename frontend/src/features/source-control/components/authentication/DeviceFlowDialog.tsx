// frontend/src/features/source-control/components/authentication/DeviceFlowDialog.tsx

"use client";

import React, { useCallback, useEffect, useState } from "react";

import type {
  DeviceFlowStartResponse,
  DeviceFlowStatusResponse,
} from "../../api/schemas";
import type { DeviceFlowPhase } from "../../types/authentication";

/**
 * Props for `DeviceFlowDialog`.
 *
 * The dialog is fully controlled: `phase`, `session`, `status`, and
 * `errorMessage` mirror the `deviceFlow` state returned by
 * `useSourceControlAuth`, and every user action is routed through a
 * callback. The dialog holds only ephemeral UI state (the copy-to-
 * clipboard confirmation and the live countdown), which is reset
 * whenever the phase changes.
 */
export interface DeviceFlowDialogProps {
  /** `true` to show the dialog; `false` to hide it. */
  isOpen: boolean;
  /** Current phase of the Device Flow state machine. */
  phase: DeviceFlowPhase;
  /**
   * Session returned by `POST /device/start`. Populated from
   * `phase === "starting"` onwards; `null` before the first start
   * and after a reset.
   */
  session: DeviceFlowStartResponse | null;
  /**
   * Latest status payload. `null` while no poll has completed yet;
   * populated after the first `GET /device/{id}/status`.
   */
  status: DeviceFlowStatusResponse | null;
  /** Redacted, user-facing error message for non-terminal paths. */
  errorMessage: string | null;
  /** Close the dialog without cancelling (fires after `completed`). */
  onClose: () => void;
  /** Cancel the active session, then close. Fires in `showing_code`. */
  onCancel: () => Promise<void>;
  /** Restart a new Device Flow session from `failed`/`denied`/`expired`. */
  onRetry: () => Promise<void>;
}

/**
 * Modal dialog that drives the Managed GitHub App Device Flow.
 *
 * The dialog is the ONLY place the user sees the device code and
 * the verification URL. Everything in this component is designed
 * around three security invariants:
 *
 *   1. The verification URL is ALWAYS `https://github.com/login/device`
 *      (rendered by the backend). The dialog never proxies the
 *      page and never accepts a code through a form of its own.
 *      This is the whole point of Device Flow: the user types the
 *      code on GitHub's domain, not on ours.
 *   2. The code is displayed VERBATIM, with a single Copy button
 *      that uses the Clipboard API. If the API is unavailable, the
 *      button falls back to selecting the text so the user can
 *      copy it manually. The code is never truncated for layout.
 *   3. The dialog refuses to render a "Success" state on
 *      `phase="completed"` until the backend has confirmed that
 *      the token was persisted AND the connection was registered.
 *      That confirmation arrives via `phase` (which the hook only
 *      flips after the poll returns `status="completed"`), so the
 *      UI cannot show success before the backend is ready.
 *
 * Phases handled:
 *
 *   - `idle`         : renders `null` (dialog closed).
 *   - `starting`     : spinner + "Requesting a device code…".
 *   - `showing_code` : the main flow; countdown, copy, open GitHub.
 *   - `completed`    : success message + Close button.
 *   - `failed`       : error message + Retry + Close.
 *   - `denied`       : "authorization denied on GitHub" + Retry.
 *   - `expired`      : "code expired" + Retry.
 *   - `cancelled`    : transient state; the parent usually closes.
 */
export function DeviceFlowDialog({
  isOpen,
  phase,
  session,
  status,
  errorMessage,
  onClose,
  onCancel,
  onRetry,
}: DeviceFlowDialogProps) {
  // --- Ephemeral UI state ------------------------------------------
  const [copied, setCopied] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState<number>(0);

  // Reset the copy confirmation whenever the code changes, so the
  // user does not see "Copied!" for a stale session.
  useEffect(() => {
    setCopied(false);
  }, [session?.session_id]);

  // Reset the busy flags whenever the phase changes; otherwise a
  // `cancelling` flag left over from a previous session would make
  // the Cancel button of the new session permanently disabled.
  useEffect(() => {
    setCancelling(false);
    setRetrying(false);
  }, [phase]);

  // ------------------------------------------------------------------
  // Live countdown
  // ------------------------------------------------------------------
  //
  // The countdown reflects the SERVER's view of the remaining time,
  // recomputed from `expires_in_seconds` on every status poll. The
  // local interval only ticks down between polls, so the display
  // stays accurate even if the browser tab is backgrounded and the
  // setInterval is throttled.
  useEffect(() => {
    if (phase !== "showing_code") {
      setSecondsLeft(0);
      return;
    }
    const initial =
      status?.expires_in_seconds ??
      session?.expires_in_seconds ??
      0;
    setSecondsLeft(Math.max(0, initial));

    const id = setInterval(() => {
      setSecondsLeft((prev) => {
        if (prev <= 0) return 0;
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [
    phase,
    status?.expires_in_seconds,
    session?.expires_in_seconds,
    session?.session_id,
  ]);

  // ------------------------------------------------------------------
  // Copy handler
  // ------------------------------------------------------------------
  const handleCopy = useCallback(async () => {
    const code = session?.user_code;
    if (!code) return;
    try {
      if (
        typeof navigator !== "undefined" &&
        navigator.clipboard &&
        typeof navigator.clipboard.writeText === "function"
      ) {
        await navigator.clipboard.writeText(code);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } else {
        // Fallback: select the code element so the user can copy
        // it with the keyboard. This is intentionally minimal:
        // adding a hidden textarea + execCommand is deprecated and
        // no longer necessary in any supported browser.
        const el = document.getElementById("device-user-code");
        if (el) {
          const range = document.createRange();
          range.selectNodeContents(el);
          const selection = window.getSelection();
          if (selection) {
            selection.removeAllRanges();
            selection.addRange(range);
          }
        }
      }
    } catch {
      // Clipboard permission denied. The user can still select
      // the code manually; we do not show an error for this case,
      // because the code itself is fully visible.
    }
  }, [session?.user_code]);

  // ------------------------------------------------------------------
  // Open GitHub handler
  // ------------------------------------------------------------------
  const handleOpenGitHub = useCallback(() => {
    const url = session?.verification_uri;
    if (!url) return;
    // `noopener` prevents the new tab from accessing `window.opener`.
    // `noreferrer` is belt-and-braces.
    window.open(url, "_blank", "noopener,noreferrer");
  }, [session?.verification_uri]);

  // ------------------------------------------------------------------
  // Cancel handler
  // ------------------------------------------------------------------
  const handleCancel = useCallback(async () => {
    setCancelling(true);
    try {
      await onCancel();
    } finally {
      // The parent may or may not close the dialog; either way,
      // clearing the local flag prevents a stuck spinner if the
      // backend call fails asynchronously.
      setCancelling(false);
    }
  }, [onCancel]);

  // ------------------------------------------------------------------
  // Retry handler
  // ------------------------------------------------------------------
  const handleRetry = useCallback(async () => {
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  }, [onRetry]);

  // ------------------------------------------------------------------
  // Render guards
  // ------------------------------------------------------------------
  if (!isOpen || phase === "idle") return null;

  // ------------------------------------------------------------------
  // Backdrop + dialog chrome
  // ------------------------------------------------------------------
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-gray-900 bg-opacity-50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="device-flow-title"
    >
      <div className="relative w-full max-w-md rounded-lg bg-white shadow-xl">
        {/* -------------------------------------------------------- */}
        {/* Header                                                    */}
        {/* -------------------------------------------------------- */}
        <div className="border-b border-gray-200 px-6 py-4">
          <h2
            id="device-flow-title"
            className="text-lg font-semibold text-gray-900"
          >
            {phase === "completed"
              ? "Connected"
              : phase === "starting"
              ? "Connecting…"
              : phase === "failed" || phase === "denied"
              ? "Connection failed"
              : phase === "expired"
              ? "Code expired"
              : phase === "cancelled"
              ? "Connection cancelled"
              : "Connect GitHub"}
          </h2>
        </div>

        {/* -------------------------------------------------------- */}
        {/* Body                                                      */}
        {/* -------------------------------------------------------- */}
        <div className="px-6 py-5">
          {/* ---- starting ---- */}
          {phase === "starting" && (
            <div className="flex flex-col items-center justify-center py-6">
              <svg
                className="h-8 w-8 animate-spin text-blue-600"
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
              <p className="mt-3 text-sm text-gray-600">
                Requesting a device code from GitHub…
              </p>
            </div>
          )}

          {/* ---- showing_code ---- */}
          {phase === "showing_code" && session && (
            <div className="space-y-4">
              <p className="text-sm text-gray-700">
                Open GitHub&apos;s official device authorization
                page and enter this code. GitHub will ask you to
                authorize PELDRUN.
              </p>

              {/* The user code */}
              <div className="rounded-md border-2 border-dashed border-blue-300 bg-blue-50 p-4 text-center">
                <p className="text-xs font-medium uppercase tracking-wide text-blue-700">
                  Your device code
                </p>
                <p
                  id="device-user-code"
                  className="mt-2 select-all break-all font-mono text-3xl font-bold tracking-widest text-blue-900"
                >
                  {session.user_code}
                </p>
              </div>

              {/* Verification URL */}
              <div className="rounded-md border border-gray-200 bg-gray-50 p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-gray-600">
                  Verification URL
                </p>
                <p className="mt-1 select-all break-all font-mono text-sm text-gray-900">
                  {session.verification_uri}
                </p>
              </div>

              {/* Security warning */}
              <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                <svg
                  className="mt-0.5 h-4 w-4 shrink-0 text-amber-600"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
                    clipRule="evenodd"
                  />
                </svg>
                <p>
                  Only enter this code on{" "}
                  <strong>github.com/login/device</strong>. PELDRUN
                  will never ask you to enter the code anywhere
                  else.
                </p>
              </div>

              {/* Action buttons */}
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={handleCopy}
                  className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
                >
                  {copied ? "Copied!" : "Copy code"}
                </button>
                <button
                  type="button"
                  onClick={handleOpenGitHub}
                  className="inline-flex items-center gap-1 rounded-md border border-transparent bg-blue-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
                >
                  Open GitHub
                  <svg
                    className="h-3.5 w-3.5"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth="2"
                    aria-hidden="true"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
                    />
                  </svg>
                </button>
              </div>

              {/* Status footer */}
              <div className="border-t border-gray-200 pt-3 text-xs text-gray-500">
                {secondsLeft > 0 ? (
                  <p>
                    Waiting for authorization…{" "}
                    <span className="font-mono">
                      {formatCountdown(secondsLeft)}
                    </span>{" "}
                    remaining.
                  </p>
                ) : (
                  <p className="text-amber-700">
                    The code has expired. Click Retry to get a new
                    one.
                  </p>
                )}
              </div>
            </div>
          )}

          {/* ---- completed ---- */}
          {phase === "completed" && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800">
                <svg
                  className="mt-0.5 h-5 w-5 shrink-0 text-green-600"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                    clipRule="evenodd"
                  />
                </svg>
                <div>
                  <p className="font-medium">
                    Successfully connected to GitHub.
                  </p>
                  {status?.account &&
                    typeof status.account.username === "string" && (
                      <p className="mt-1 text-xs text-green-700">
                        Connected as{" "}
                        <span className="font-medium">
                          {status.account.username}
                        </span>
                        .
                      </p>
                    )}
                </div>
              </div>
              <p className="text-sm text-gray-600">
                Your token is now stored in the OS credential vault.
                You can close this dialog and continue using
                PELDRUN.
              </p>
            </div>
          )}

          {/* ---- failed ---- */}
          {phase === "failed" && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                <svg
                  className="mt-0.5 h-5 w-5 shrink-0 text-red-600"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z"
                    clipRule="evenodd"
                  />
                </svg>
                <div>
                  <p className="font-medium">Connection failed.</p>
                  {errorMessage && (
                    <p className="mt-1 break-words text-xs text-red-700">
                      {errorMessage}
                    </p>
                  )}
                </div>
              </div>
              <p className="text-sm text-gray-600">
                The device flow did not complete. You can retry, or
                cancel and check the PELDRUN settings before trying
                again.
              </p>
            </div>
          )}

          {/* ---- denied ---- */}
          {phase === "denied" && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                <svg
                  className="mt-0.5 h-5 w-5 shrink-0 text-red-600"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z"
                    clipRule="evenodd"
                  />
                </svg>
                <div>
                  <p className="font-medium">
                    Authorization was denied on GitHub.
                  </p>
                  <p className="mt-1 text-xs text-red-700">
                    PELDRUN never received a token. You can retry
                    and approve the request on GitHub.
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* ---- expired ---- */}
          {phase === "expired" && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                <svg
                  className="mt-0.5 h-5 w-5 shrink-0 text-amber-600"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z"
                    clipRule="evenodd"
                  />
                </svg>
                <div>
                  <p className="font-medium">The code expired.</p>
                  <p className="mt-1 text-xs text-amber-700">
                    The device code was not approved on GitHub
                    before it expired. Click Retry to start a new
                    session.
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* ---- cancelled ---- */}
          {phase === "cancelled" && (
            <div className="space-y-3">
              <p className="text-sm text-gray-700">
                The connection was cancelled. No token was stored.
              </p>
            </div>
          )}
        </div>

        {/* -------------------------------------------------------- */}
        {/* Footer                                                    */}
        {/* -------------------------------------------------------- */}
        <div className="flex items-center justify-end gap-2 border-t border-gray-200 bg-gray-50 px-6 py-3">
          {/* showing_code: Cancel only */}
          {phase === "showing_code" && (
            <button
              type="button"
              onClick={handleCancel}
              disabled={cancelling}
              className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}

          {/* completed: Close only */}
          {phase === "completed" && (
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center rounded-md border border-transparent bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
            >
              Done
            </button>
          )}

          {/* failed / denied / expired: Retry + Cancel */}
          {(phase === "failed" ||
            phase === "denied" ||
            phase === "expired") && (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={retrying}
                className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleRetry}
                disabled={retrying}
                className="inline-flex items-center gap-2 rounded-md border border-transparent bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {retrying && (
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
                Retry
              </button>
            </>
          )}

          {/* cancelled: Close only */}
          {phase === "cancelled" && (
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center rounded-md border border-gray-300 bg-white px-4 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
            >
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Small helpers
// ----------------------------------------------------------------------

/**
 * Format a countdown in `M:SS` form. Used only for the
 * "Waiting for authorization…" footer.
 */
function formatCountdown(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
