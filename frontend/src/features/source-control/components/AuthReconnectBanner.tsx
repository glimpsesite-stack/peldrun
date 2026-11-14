// frontend/src/features/source-control/components/AuthReconnectBanner.tsx

"use client";

import React, { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, RefreshCw } from "lucide-react";
import { useSourceControlAuth } from "../hooks/useSourceControlAuth";
import { DeviceFlowDialog } from "./authentication/DeviceFlowDialog";

/**
 * Inline banner shown when any Source Control API call returns 401.
 *
 * This banner now dynamically routes the "Reconnect" action:
 * 1. If the Managed Device Flow is ready, it uses it directly.
 * 2. If not, it falls back to the legacy BYOA flow.
 */
export function AuthReconnectBanner() {
  const auth = useSourceControlAuth();
  const [needsReconnect, setNeedsReconnect] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [localPhase, setLocalPhase] = useState<"idle" | "opening" | "waiting" | "success">("idle");
  const [connectError, setConnectError] = useState<string | null>(null);

  const pollTimerRef = useRef<number | null>(null);
  const successFiredRef = useRef(false);

  // --- Listen for 401s from the API client ---
  useEffect(() => {
    const onAuthRequired = (e: Event) => {
      if (localPhase !== "idle" || auth.deviceFlow.phase !== "idle") return;

      const detail = (e as CustomEvent<{ message?: string }>).detail || {};
      setNeedsReconnect(true);
      setMessage(detail.message || null);
    };
    window.addEventListener("peldrun:auth-required", onAuthRequired);
    return () => window.removeEventListener("peldrun:auth-required", onAuthRequired);
  }, [localPhase, auth.deviceFlow.phase]);

  // --- Handle successful connection from either flow ---
  const triggerSuccess = () => {
    if (successFiredRef.current) return;
    successFiredRef.current = true;
    setLocalPhase("success");

    if (pollTimerRef.current) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    window.setTimeout(() => {
      setNeedsReconnect(false);
      setMessage(null);
      setLocalPhase("idle");
      successFiredRef.current = false;
      window.dispatchEvent(new CustomEvent("peldrun:auth-reconnected"));
    }, 900);
  };

  // --- Monitor Device Flow for completion ---
  useEffect(() => {
    if (auth.deviceFlow.phase === "completed") {
      triggerSuccess();
    } else if (auth.deviceFlow.phase === "failed" || auth.deviceFlow.phase === "denied") {
      setConnectError(auth.deviceFlow.errorMessage || "Device Flow failed.");
    }
  }, [auth.deviceFlow.phase, auth.deviceFlow.errorMessage]);

  // --- Legacy BYOA Polling ---
  async function checkByoaOnce(): Promise<boolean> {
    try {
      const connections = await auth.refresh().then(() => true).catch(() => false);
      if (connections) {
        // If refresh succeeded, check if we have a connection.
        const isConnected = auth.status?.providers?.github?.connection !== null;
        if (isConnected) {
          triggerSuccess();
          return true;
        }
      }
      return false;
    } catch {
      return false;
    }
  }

  // --- Main Reconnect Handler ---
  const handleReconnect = async () => {
    setConnectError(null);
    setLocalPhase("idle");

    const managedReady = auth.status?.providers?.github?.managed_ready ?? false;
    const byoaReady = auth.status?.providers?.github?.byoa_ready ?? false;

    // 1. Prefer Device Flow
    if (managedReady) {
      void auth.startManagedFlow("github");
      return;
    }

    // 2. Refuse if nothing is enabled
    if (!byoaReady) {
      setConnectError(
        "No authentication method is enabled. Please go to Integrations settings to enable Managed GitHub App or BYOA."
      );
      return;
    }

    // 3. Fallback to BYOA
    setLocalPhase("opening");
    try {
      const res = await fetch("/api/source-control/connections/github/begin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "github" }),
      });

      if (!res.ok) throw new Error("Failed to start BYOA flow");
      const response = await res.json();

      const win = window.open(response.authorization_url, "_blank");
      if (!win) throw new Error("Pop-up blocked. Please allow pop-ups.");

      setLocalPhase("waiting");

      let attempts = 0;
      const tick = async () => {
        attempts++;
        if (await checkByoaOnce()) return;

        if (attempts >= 90) {
          setLocalPhase("idle");
          setConnectError("Connection timed out.");
          return;
        }
        pollTimerRef.current = window.setTimeout(tick, 2000);
      };
      void tick();
    } catch (err) {
      setLocalPhase("idle");
      setConnectError(err instanceof Error ? err.message : "Failed to start OAuth.");
    }
  };

  if (!needsReconnect) return null;

  return (
    <>
      {localPhase === "success" && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-green-300 bg-green-50 text-xs text-green-900 shrink-0">
          <CheckCircle2 className="w-4 h-4 shrink-0 text-green-700" />
          <span className="font-medium">Connected! Reloading…</span>
        </div>
      )}

      {localPhase === "opening" || localPhase === "waiting" ? (
        <div className="flex items-start gap-2 px-3 py-2 border-b border-blue-300 bg-blue-50 text-xs text-blue-900 shrink-0">
          <Loader2 className="w-4 h-4 shrink-0 mt-0.5 text-blue-700 animate-spin" />
          <div className="flex-1">
            <div className="font-medium">
              {localPhase === "opening" ? "Opening GitHub…" : "Waiting for authorization…"}
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              if (pollTimerRef.current) window.clearTimeout(pollTimerRef.current);
              setLocalPhase("idle");
            }}
            className="shrink-0 text-[11px] px-2 py-1 rounded border border-blue-400 bg-blue-100 hover:bg-blue-200"
          >
            Cancel
          </button>
        </div>
      ) : localPhase === "idle" && (
        <div className="flex items-start gap-2 px-3 py-2 border-b border-amber-300 bg-amber-50 text-xs text-amber-900 shrink-0">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-700" />
          <div className="flex-1">
            <div className="font-medium">GitHub authentication required</div>
            <div className="text-[11px] mt-0.5 text-amber-800">
              {message || "Your GitHub token has expired or been revoked."}
            </div>
            {connectError && <div className="text-[11px] mt-1 text-red-700">{connectError}</div>}
          </div>
          <button
            type="button"
            onClick={handleReconnect}
            className="shrink-0 text-[11px] px-2 py-1 rounded border border-amber-400 bg-amber-100 hover:bg-amber-200 flex items-center gap-1"
          >
            <RefreshCw className="w-3 h-3" /> Reconnect
          </button>
        </div>
      )}

      {/* Render Device Flow Dialog */}
      <DeviceFlowDialog
        isOpen={auth.deviceFlow.phase !== "idle"}
        phase={auth.deviceFlow.phase}
        session={auth.deviceFlow.session}
        status={auth.deviceFlow.status}
        errorMessage={auth.deviceFlow.errorMessage}
        onClose={auth.resetDeviceFlow}
        onCancel={auth.cancelFlow}
        onRetry={async () => {
          await auth.startManagedFlow("github");
        }}
      />
    </>
  );
}
