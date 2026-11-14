// frontend/src/features/source-control/components/authentication/AuthenticationSettingsPanel.tsx

"use client";

import React, { useEffect, useRef } from "react";

import type {
  ConnectionStatusResponse,
  ProviderDescriptorResponse,
} from "../../api/schemas";
import type { UseSourceControlAuthResult } from "../../hooks/useSourceControlAuth";
import { ManagedGitHubCard } from "./ManagedGitHubCard";
import { ByoaProviderCard } from "./ByoaProviderCard";
import { DeviceFlowDialog } from "./DeviceFlowDialog";
import type { AuthProviderConnection } from "../../types/authentication";

export interface AuthenticationSettingsPanelProps {
  providers: ProviderDescriptorResponse[];
  connections: ConnectionStatusResponse[];
  savingConfigProvider: string | null;
  connectingProvider: string | null;
  onConfigureByoa: (provider: ProviderDescriptorResponse) => void;
  onStartByoaConnect: (provider: ProviderDescriptorResponse) => void;
  onDisconnectProvider: (providerId: string) => void;
  onRefreshProviders: () => Promise<void>;
  auth: UseSourceControlAuthResult;
}

const MANAGED_PROVIDER_IDS = new Set(["github"]);

export function AuthenticationSettingsPanel({
  providers,
  connections,
  savingConfigProvider,
  connectingProvider,
  onConfigureByoa,
  onStartByoaConnect,
  onDisconnectProvider,
  onRefreshProviders,
  auth,
}: AuthenticationSettingsPanelProps) {
  const {
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
  } = auth;

  const prevPhaseRef = useRef(deviceFlow.phase);
  useEffect(() => {
    const wasCompleted = prevPhaseRef.current === "completed";
    const isCompleted = deviceFlow.phase === "completed";
    prevPhaseRef.current = deviceFlow.phase;
    if (!wasCompleted && isCompleted) {
      void onRefreshProviders().catch(() => {});
    }
  }, [deviceFlow.phase, onRefreshProviders]);

  if (!status) {
    if (error) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="text-sm font-medium text-red-800">
            Could not load authentication settings.
          </p>
          <p className="mt-1 break-words text-xs text-red-700">{error}</p>
          <button
            type="button"
            onClick={() => void refresh()}
            className="mt-3 rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 shadow-sm hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2"
          >
            Retry
          </button>
        </div>
      );
    }
    return <LoadingSkeleton />;
  }

  const managedProviders = providers.filter((p) =>
    MANAGED_PROVIDER_IDS.has(p.provider_id)
  );
  const legacyConnections = connections.filter(
    (c) => c.is_connected && c.auth_source === "legacy_unknown"
  );
  const vaultUnavailable = !status.vault.available;

  // DEBUG LOG: Look at this in your browser console (F12) to verify what data is arriving.
  console.log("[AUTH_PANEL_DEBUG] status.providers:", status.providers);
  console.log("[AUTH_PANEL_DEBUG] connections:", connections);

  return (
    <div className="space-y-4">
      {vaultUnavailable && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          <div className="flex items-start gap-2">
            <svg
              className="mt-0.5 h-5 w-5 shrink-0 text-red-600"
              fill="currentColor"
              viewBox="0 0 20 20"
              aria-hidden="true"
            >
              <path
                fillRule="evenodd"
                d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z"
                clipRule="evenodd"
              />
            </svg>
            <div>
              <p className="font-medium">Credential vault unavailable</p>
              <p className="mt-1 text-xs text-red-700">
                The configured vault backend (&ldquo;{status.vault.backend}&rdquo;)
                is not reporting itself available. Tokens cannot be
                stored until this is fixed. A developer with access
                to this installation must repair the vault
                configuration.
              </p>
            </div>
          </div>
        </div>
      )}

      {legacyConnections.length > 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
          <div className="flex items-start gap-2">
            <svg
              className="mt-0.5 h-5 w-5 shrink-0 text-amber-600"
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
            <div>
              <p className="font-medium">Legacy connection detected</p>
              <p className="mt-1 text-xs text-amber-700">
                {legacyConnections.length} connection
                {legacyConnections.length > 1 ? "s were" : " was"}{" "}
                created before authentication source tracking was
                introduced. Reconnect to move{" "}
                {legacyConnections.length > 1 ? "them" : "it"} to a
                known source (Managed or BYOA).
              </p>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          <div className="flex items-start justify-between gap-2">
            <p className="break-words">{error}</p>
            <button
              type="button"
              onClick={dismissError}
              className="shrink-0 text-xs font-medium text-red-700 underline hover:text-red-900"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-gray-900">
              Source Control Authentication
            </h3>
            <p className="mt-1 text-xs text-gray-500">
              Master switch for all authentication flows. Disabling
              this prevents new connections from being created.
              Existing connections and tokens are preserved.
            </p>
          </div>
          <ToggleSwitch
            checked={status.settings.enabled}
            onChange={(next) => void updateSettings({ enabled: next })}
            disabled={saving}
            ariaLabel="Enable Source Control authentication"
          />
        </div>
      </div>

      {!status.settings.enabled && (
        <div className="rounded-md border border-gray-200 bg-gray-50 p-3 text-sm text-gray-600">
          Authentication is disabled. Enable it above to configure
          providers.
        </div>
      )}

      {status.settings.enabled && (
        <>
          {managedProviders.map((provider) => {
            const providerStatus = status.providers[provider.provider_id];
            if (!providerStatus) return null;

            // Resolve the connection safely
            let managedConnection: AuthProviderConnection | null = null;

            if (providerStatus.connection?.auth_source === "managed") {
              managedConnection = providerStatus.connection;
            } else {
              const fallback = connections.find((c) => c.provider === provider.provider_id);
              if (fallback?.auth_source === "managed") {
                managedConnection = {
                  auth_source: "managed",
                  username: fallback.username ?? null,
                  account_id: fallback.account_id ?? null,
                  connected_at: fallback.last_validated_at ?? null, // Map correctly
                };
              }
            }

            const managedProviderStatus = { ...providerStatus, connection: managedConnection };

            return (
              <div
                key={`managed-${provider.provider_id}`}
                className="space-y-3"
              >
                <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
                  <div className="min-w-0">
                    <span className="text-sm font-medium text-gray-900">
                      Managed GitHub App
                    </span>
                    <p className="mt-0.5 text-xs text-gray-500">
                      When disabled, new managed connections cannot
                      be started.
                    </p>
                  </div>
                  <ToggleSwitch
                    checked={status.managed.enabled}
                    onChange={(next) =>
                      void updateSettings({ managed: { enabled: next } })
                    }
                    disabled={saving}
                    ariaLabel="Enable managed GitHub App"
                  />
                </div>

                <ManagedGitHubCard
                  managed={status.managed}
                  provider={managedProviderStatus}
                  busy={saving || deviceFlow.phase === "starting"}
                  onConnect={() => {
                    void startManagedFlow(provider.provider_id);
                  }}
                  onDisconnect={
                    managedConnection
                      ? () =>
                          onDisconnectProvider(provider.provider_id)
                      : undefined
                  }
                />
              </div>
            );
          })}

          {providers.map((provider) => {
            const providerStatus = status.providers[provider.provider_id];
            if (!providerStatus) return null;

            let byoaConnection: AuthProviderConnection | null = null;

            if (providerStatus.connection?.auth_source === "byoa") {
              byoaConnection = providerStatus.connection;
            } else {
              const fallback = connections.find((c) => c.provider === provider.provider_id);
              if (fallback?.auth_source === "byoa") {
                byoaConnection = {
                  auth_source: "byoa",
                  username: fallback.username ?? null,
                  account_id: fallback.account_id ?? null,
                  connected_at: fallback.last_validated_at ?? null, // Map correctly
                };
              }
            }

            const byoaProviderStatus = { ...providerStatus, connection: byoaConnection };

            return (
              <ByoaProviderCard
                key={`byoa-${provider.provider_id}`}
                byoa={status.byoa}
                provider={byoaProviderStatus}
                credentialsConfigured={provider.is_configured}
                busy={
                  saving ||
                  savingConfigProvider === provider.provider_id ||
                  connectingProvider === provider.provider_id
                }
                onToggleEnabled={(enabled) => {
                  void updateSettings({ byoa: { enabled } });
                }}
                onConfigureCredentials={() => onConfigureByoa(provider)}
                onConnect={() => onStartByoaConnect(provider)}
                onDisconnect={
                  byoaConnection
                    ? () => onDisconnectProvider(provider.provider_id)
                    : undefined
                }
                onDeleteCredentials={
                  provider.is_configured
                    ? () => onConfigureByoa(provider)
                    : undefined
                }
              />
            );
          })}
        </>
      )}

      <DeviceFlowDialog
        isOpen={deviceFlow.phase !== "idle"}
        phase={deviceFlow.phase}
        session={deviceFlow.session}
        status={deviceFlow.status}
        errorMessage={deviceFlow.errorMessage}
        onClose={resetDeviceFlow}
        onCancel={cancelFlow}
        onRetry={async () => {
          await startManagedFlow("github");
        }}
      />

      {loading && (
        <p className="text-center text-xs text-gray-400">
          Refreshing authentication state…
        </p>
      )}
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-4">
      <div className="h-20 animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
      <div className="h-32 animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
      <div className="h-40 animate-pulse rounded-lg border border-gray-200 bg-gray-100" />
    </div>
  );
}

interface ToggleSwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
}

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
