// frontend/src/features/source-control/hooks/useSourceControlAuth.ts

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  sourceControlClient,
  SourceControlApiError,
} from "../api/source-control-client";
import type {
  AuthSettingsUpdateRequest,
  DeviceFlowStartResponse,
  DeviceFlowStatusResponse,
} from "../api/schemas";
import type {
  AuthStatus,
  DeviceFlowPhase,
  DeviceFlowStatus,
} from "../types/authentication";

/**
 * Default provider for Device Flow. The managed path currently
 * supports GitHub only; the string is kept as a constant so a
 * future provider can be added without touching the hook's body.
 */
const DEFAULT_PROVIDER = "github";

/**
 * Local state of an in-flight Device Flow session.
 *
 * The `phase` field drives the UI state machine; see `DeviceFlowPhase`
 * in `types/authentication.ts` for the full set of values.
 *
 *   - `phase="idle"`        : no session (before start, after reset).
 *   - `phase="starting"`    : the start API call is in flight.
 *   - `phase="showing_code"`: a session is active; polling runs.
 *   - `phase="completed"`   : token persisted and connection
 *                             registered; `status.account` is set.
 *   - `phase="failed"`      : terminal error; `errorMessage` set.
 *   - `phase="cancelled"`   : user cancelled; no error message.
 *   - `phase="denied"`      : user denied on GitHub.
 *   - `phase="expired"`     : user code expired before approval.
 */
export interface DeviceFlowState {
  phase: DeviceFlowPhase;
  session: DeviceFlowStartResponse | null;
  status: DeviceFlowStatusResponse | null;
  errorMessage: string | null;
}

const INITIAL_DEVICE_FLOW: DeviceFlowState = {
  phase: "idle",
  session: null,
  status: null,
  errorMessage: null,
};

/**
 * Options accepted by `useSourceControlAuth`.
 */
export interface UseSourceControlAuthOptions {
  /**
   * Provider to target for Device Flow. Defaults to `"github"`.
   * The value is only used as the default for `startManagedFlow()`;
   * the caller can override it per-call.
   */
  provider?: string;
  /**
   * When `true` (default), fetch the auth status on mount. Set to
   * `false` when the caller wants to control the initial fetch
   * (e.g. lazy-open the auth section only when the user expands it).
   */
  autoLoad?: boolean;
}

/**
 * Public shape returned by `useSourceControlAuth`. Every field is
 * stable across renders except the ones that reflect actual state
 * changes (`status`, `loading`, `saving`, `error`, `deviceFlow`).
 * The callbacks are memoized so they can be safely used as
 * `useEffect` dependencies in consuming components.
 */
export interface UseSourceControlAuthResult {
  // --- Auth status ---
  status: AuthStatus | null;
  loading: boolean;
  saving: boolean;
  error: string | null;

  // --- Device Flow state ---
  deviceFlow: DeviceFlowState;

  // --- Actions ---
  refresh: () => Promise<void>;
  updateSettings: (payload: AuthSettingsUpdateRequest) => Promise<boolean>;
  startManagedFlow: (provider?: string) => Promise<boolean>;
  cancelFlow: () => Promise<void>;
  resetDeviceFlow: () => void;
  dismissError: () => void;
}

/**
 * Normalize any thrown value into a user-facing error message.
 *
 * `SourceControlApiError` carries the backend's redacted `detail`
 * string, which is already suitable for display. Plain `Error`
 * instances carry their own message. Anything else is stringified
 * as a last resort.
 */
function extractErrorMessage(err: unknown): string {
  if (err instanceof SourceControlApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * The backend returns `AuthStatusResponse` whose nested mappings
 * are typed as `Record<string, any>`. The typed `AuthStatus` in
 * `types/authentication.ts` uses the same wire shape but with
 * concrete types. The cast is safe because the wire shape is
 * guaranteed by the Zod schema at the network boundary (the
 * top-level keys are objects, and the nested keys are stable).
 */
function asAuthStatus(raw: unknown): AuthStatus {
  return raw as AuthStatus;
}

/**
 * Map the backend's `DeviceFlowStatus` to the dialog's
 * `DeviceFlowPhase`.
 *
 * The two vocabularies differ on purpose:
 *
 *   - `DeviceFlowStatus` is the PROTOCOL state returned by the
 *     backend. It includes `"pending"` (which is a polling state,
 *     not a terminal phase) and `"error"` (a generic failure
 *     reported by GitHub or by our own completion callback).
 *   - `DeviceFlowPhase` is the UI state machine that drives the
 *     dialog. It collapses those two into the closest user-visible
 *     phase:
 *         "pending" → "showing_code"  (still waiting; UI polls)
 *         "error"   → "failed"        (show the error + Retry)
 *
 * The mapping is exhaustive on `DeviceFlowStatus` and falls back
 * to `"failed"` for unknown future values, so a backend that adds
 * a new status code cannot silently park the dialog in a state it
 * does not know how to render.
 */
function mapStatusToPhase(status: DeviceFlowStatus): DeviceFlowPhase {
  switch (status) {
    case "pending":
      return "showing_code";
    case "completed":
      return "completed";
    case "denied":
      return "denied";
    case "expired":
      return "expired";
    case "cancelled":
      return "cancelled";
    case "error":
      return "failed";
    default:
      return "failed";
  }
}

/**
 * React hook that drives the Source Control authentication layer.
 *
 * Responsibilities:
 *   1. Load the auth status on mount (unless `autoLoad: false`).
 *   2. Expose `refresh()` so the caller can reload on demand.
 *   3. Expose `updateSettings()` so the settings UI can persist
 *      changes and immediately see the new state.
 *   4. Drive the Managed Device Flow:
 *      - `startManagedFlow()` calls `POST /device/start`.
 *      - An internal `useEffect` polls `GET /device/{id}/status`
 *        at the server-suggested interval until a terminal state.
 *      - `cancelFlow()` calls `POST /device/{id}/cancel`.
 *   5. Refresh the auth status once the flow completes, so the
 *      caller's UI reflects the newly created connection without
 *      needing a second manual refresh.
 *
 * State safety:
 *   - Every `setState` call from inside an async operation is
 *     guarded by a `mountedRef`, so a late-arriving response
 *     cannot trigger a React warning or a stale update.
 *   - The polling callback captures its session id and re-checks
 *     it against the current state before updating, so a new
 *     flow started mid-poll cannot be overwritten by the old
 *     flow's result.
 *   - Cleanup on unmount clears the timer and sets the `cancelled`
 *     flag, so the polling loop stops immediately.
 *
 * Error handling:
 *   - Errors from `refresh()` and `updateSettings()` surface via
 *     the top-level `error` string, which the caller can show in
 *     a banner.
 *   - Errors from `startManagedFlow()` and the polling loop
 *     surface via `deviceFlow.errorMessage` and move the phase
 *     to `"failed"`, so the Device Flow dialog can show them in
 *     place without affecting the rest of the settings UI.
 */
export function useSourceControlAuth(
  options: UseSourceControlAuthOptions = {}
): UseSourceControlAuthResult {
  const { provider = DEFAULT_PROVIDER, autoLoad = true } = options;

  // --- Auth status state ------------------------------------------
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // --- Device Flow state ------------------------------------------
  const [deviceFlow, setDeviceFlow] =
    useState<DeviceFlowState>(INITIAL_DEVICE_FLOW);

  // --- Mount guard ------------------------------------------------
  // Used to skip state updates after unmount. React 18 no longer
  // warns about setState-after-unmount, but the guard prevents
  // leaking results from an in-flight request into a component
  // that has already been replaced.
  const mountedRef = useRef<boolean>(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ------------------------------------------------------------------
  // 1. refresh
  // ------------------------------------------------------------------
  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const raw = await sourceControlClient.getAuthStatus();
      if (!mountedRef.current) return;
      setStatus(asAuthStatus(raw));
    } catch (err) {
      if (!mountedRef.current) return;
      setError(extractErrorMessage(err));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  // Auto-load on mount (or whenever `autoLoad` flips back to true).
  useEffect(() => {
    if (autoLoad) void refresh();
  }, [autoLoad, refresh]);

  // Listen for global auth-reconnected events. When the banner
  // (or any other component) successfully reconnects, this event
  // is dispatched. We must refresh our own state to reflect the
  // new connection immediately, otherwise the UI will show stale
  // "Not connected" status and hide the Disconnect button.
  useEffect(() => {
    const handleReconnected = () => {
      void refresh();
    };
    window.addEventListener("peldrun:auth-reconnected", handleReconnected);
    return () => {
      window.removeEventListener("peldrun:auth-reconnected", handleReconnected);
    };
  }, [refresh]);

  // ------------------------------------------------------------------
  // 2. updateSettings
  // ------------------------------------------------------------------
  const updateSettings = useCallback(
    async (payload: AuthSettingsUpdateRequest): Promise<boolean> => {
      setSaving(true);
      setError(null);
      try {
        await sourceControlClient.updateAuthSettings(payload);
        // The PUT response includes the merged settings, but
        // going through `refresh()` gives us the full status
        // object (vault, readiness, connection record) in one
        // round-trip, which is what the UI actually needs.
        await refresh();
        return true;
      } catch (err) {
        if (mountedRef.current) setError(extractErrorMessage(err));
        return false;
      } finally {
        if (mountedRef.current) setSaving(false);
      }
    },
    [refresh]
  );

  // ------------------------------------------------------------------
  // 3. startManagedFlow
  // ------------------------------------------------------------------
  const startManagedFlow = useCallback(
    async (overrideProvider?: string): Promise<boolean> => {
      const targetProvider = overrideProvider || provider;

      // If a previous flow is still showing its code, cancel it
      // first. The backend's `cancel` is idempotent: a terminal
      // session is returned unchanged, so this is safe even if
      // the previous flow has already finished between our
      // reading the state and calling cancel.
      if (deviceFlow.session && deviceFlow.phase === "showing_code") {
        try {
          await sourceControlClient.cancelDeviceFlow(
            deviceFlow.session.provider,
            deviceFlow.session.session_id
          );
        } catch {
          // Best-effort: the flow is being replaced anyway, and
          // the backend will expire the orphaned session on its
          // own TTL. Logging here would only add noise.
        }
      }

      setDeviceFlow({
        phase: "starting",
        session: null,
        status: null,
        errorMessage: null,
      });
      setError(null);

      try {
        const session = await sourceControlClient.startDeviceFlow(
          targetProvider
        );
        if (!mountedRef.current) return false;
        setDeviceFlow({
          phase: "showing_code",
          session,
          status: null,
          errorMessage: null,
        });
        return true;
      } catch (err) {
        if (mountedRef.current) {
          setDeviceFlow({
            phase: "failed",
            session: null,
            status: null,
            errorMessage: extractErrorMessage(err),
          });
        }
        return false;
      }
    },
    [provider, deviceFlow.session, deviceFlow.phase]
  );

  // ------------------------------------------------------------------
  // 4. cancelFlow
  // ------------------------------------------------------------------
  const cancelFlow = useCallback(async (): Promise<void> => {
    const session = deviceFlow.session;
    if (!session) {
      // Nothing to cancel on the backend, but reflect the intent
      // in the UI so the dialog closes immediately.
      if (mountedRef.current) {
        setDeviceFlow((prev) => ({ ...prev, phase: "cancelled" }));
      }
      return;
    }

    try {
      const result = await sourceControlClient.cancelDeviceFlow(
        session.provider,
        session.session_id
      );
      if (!mountedRef.current) return;
      setDeviceFlow((prev) => {
        // Guard against a stale response if the user started a
        // new flow while the cancel was in flight.
        if (prev.session?.session_id !== session.session_id) return prev;
        return {
          ...prev,
          status: result,
          phase: "cancelled",
        };
      });
    } catch (err) {
      if (!mountedRef.current) return;
      setDeviceFlow((prev) => {
        if (prev.session?.session_id !== session.session_id) return prev;
        return {
          ...prev,
          phase: "failed",
          errorMessage: extractErrorMessage(err),
        };
      });
    }
  }, [deviceFlow.session]);

  // ------------------------------------------------------------------
  // 5. resetDeviceFlow / dismissError
  // ------------------------------------------------------------------
  const resetDeviceFlow = useCallback(() => {
    setDeviceFlow(INITIAL_DEVICE_FLOW);
  }, []);

  const dismissError = useCallback(() => {
    setError(null);
  }, []);

  // ------------------------------------------------------------------
  // 6. Polling effect
  // ------------------------------------------------------------------
  //
  // This effect is the ONLY place that talks to
  // `getDeviceFlowStatus`. It is keyed on the session id and the
  // phase, so:
  //
  //   - Starting a new flow (new session id) tears down the old
  //     effect and starts a new one.
  //   - Reaching a terminal phase (completed, denied, expired,
  //     cancelled, failed) makes the effect return early on its
  //     next run; the cleanup also cancels the pending timer so
  //     no further requests are made.
  //   - Unmounting the component runs the cleanup and stops the
  //     loop immediately.
  //
  // The backend polling task continues independently; this hook
  // only observes its outcome. If the component unmounts mid-flow,
  // the backend task will still terminate on its own (either by
  // the user approving on GitHub or by the session TTL elapsing).
  useEffect(() => {
    if (deviceFlow.phase !== "showing_code") return;
    const session = deviceFlow.session;
    if (!session) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const scheduleNext = (ms: number) => {
      timer = setTimeout(poll, ms);
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const result = await sourceControlClient.getDeviceFlowStatus(
          session.provider,
          session.session_id
        );
        if (cancelled) return;

        // Update the observed status. The guard protects against
        // a new session having been started while the request was
        // in flight.
        setDeviceFlow((prev) => {
          if (prev.session?.session_id !== session.session_id) {
            return prev;
          }
          return { ...prev, status: result };
        });

        if (result.status === "pending") {
          // Use the server's suggestion on every iteration, so
          // the "slow_down" adjustment is honored.
          scheduleNext(Math.max(1, result.interval_seconds) * 1000);
          return;
        }

        // Terminal: no more polling.
        setDeviceFlow((prev) => {
          if (prev.session?.session_id !== session.session_id) {
            return prev;
          }
          return {
            ...prev,
            phase: mapStatusToPhase(result.status),
            errorMessage: result.error_message ?? null,
          };
        });

        if (result.status === "completed") {
          // Refresh the auth status so the caller's UI sees the
          // new connection record without waiting for the user
          // to reload the page.
          void refresh();
        }
      } catch (err) {
        if (cancelled) return;

        // Handle the specific case where the backend restarted
        // or the session TTL expired in the background.
        const isNotFound =
          (err instanceof SourceControlApiError && err.status === 404) ||
          (err instanceof Error && err.message.includes("SESSION_NOT_FOUND"));

        setDeviceFlow((prev) => {
          if (prev.session?.session_id !== session.session_id) {
            return prev;
          }
          return {
            ...prev,
            phase: "failed",
            errorMessage: isNotFound
              ? "The session was lost (backend restarted). Please click Retry."
              : extractErrorMessage(err),
          };
        });
      }
    };

    // First poll waits for the server-suggested interval. Polling
    // immediately would almost always return `authorization_pending`
    // and contribute to GitHub's secondary rate limits, which is
    // exactly what RFC 8628 warns against.
    scheduleNext(Math.max(1, session.interval_seconds) * 1000);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // `refresh` is stable (useCallback with empty deps); `session`
    // and `phase` are the real drivers of this effect.
  }, [
    deviceFlow.phase,
    deviceFlow.session?.session_id,
    deviceFlow.session?.provider,
    deviceFlow.session?.interval_seconds,
    refresh,
  ]);

  // ------------------------------------------------------------------
  // 7. Return
  // ------------------------------------------------------------------
  return {
    status,
    loading,
    saving,
    error,
    deviceFlow,
    refresh,
    updateSettings,
    startManagedFlow,
    cancelFlow,
    resetDeviceFlow,
    dismissError,
  };
}
