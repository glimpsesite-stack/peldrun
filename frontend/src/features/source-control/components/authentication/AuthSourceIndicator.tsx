// frontend/src/features/source-control/components/authentication/AuthSourceIndicator.tsx

"use client";

import React from "react";

import type { AuthSource } from "../../types/authentication";

/**
 * Props for `AuthSourceIndicator`.
 *
 * The `source` value MUST be one of:
 *
 *   - `"managed"`        : PELDRUN GitHub App via Device Flow.
 *   - `"byoa"`           : User-provided OAuth App.
 *   - `"legacy_unknown"` : Predates the `auth_source` field.
 *   - `null` / undefined : Provider is not connected.
 *
 * The component renders `null` when `source` is null or undefined,
 * so callers do not need to guard the render themselves.
 */
export interface AuthSourceIndicatorProps {
  source: AuthSource | null | undefined;
  /**
   * When `true`, the indicator renders as a compact chip suitable
   * for inline use inside a header row (e.g. the "Connected via"
   * label of a provider card). When `false` (default), it renders
   * as a slightly larger badge with a short description below it.
   */
  compact?: boolean;
  className?: string;
}

/**
 * Visual presentation for each auth source. Kept as a lookup table
 * so a future source (e.g. a managed GitLab App) only requires a
 * new entry here, not a change to the component's logic.
 */
const SOURCE_PRESENTATION: Record<
  AuthSource,
  {
    label: string;
    shortDescription: string;
    iconColor: string;
    bgClass: string;
    textClass: string;
    borderClass: string;
    icon: React.ReactNode;
  }
> = {
  managed: {
    label: "PELDRUN GitHub App",
    shortDescription:
      "Connected via PELDRUN's official GitHub App (Device Flow).",
    iconColor: "text-blue-600",
    bgClass: "bg-blue-50",
    textClass: "text-blue-800",
    borderClass: "border-blue-200",
    icon: (
      // Shield-check icon. Represents the official, centrally
      // maintained app identity; the path is a standard
      // Heroicons v2 outline glyph.
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
          d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
        />
      </svg>
    ),
  },
  byoa: {
    label: "Your OAuth App",
    shortDescription:
      "Connected via a personal OAuth App you configured.",
    iconColor: "text-purple-600",
    bgClass: "bg-purple-50",
    textClass: "text-purple-800",
    borderClass: "border-purple-200",
    icon: (
      // Key icon. Represents user-owned credentials; distinct
      // from the managed shield at a glance.
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
          d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"
        />
      </svg>
    ),
  },
  legacy_unknown: {
    label: "Legacy connection",
    shortDescription:
      "This connection was created before auth source tracking. Re-authorize to move it to a known source.",
    iconColor: "text-amber-600",
    bgClass: "bg-amber-50",
    textClass: "text-amber-800",
    borderClass: "border-amber-200",
    icon: (
      // Question-mark-circle icon. Communicates "unknown
      // provenance" without implying an error.
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
          d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
        />
      </svg>
    ),
  },
};

/**
 * A small, reusable indicator that shows which authentication source
 * a provider connection was created under.
 *
 * Renders `null` when `source` is null or undefined, so callers can
 * pass the raw `connection?.auth_source` value without a conditional
 * wrapper. This matches how the backend reports the field: it is
 * `None` for disconnected providers and a string for connected ones.
 *
 * The component is purely presentational: it does not read from
 * state, does not fetch, and does not dispatch events. That keeps it
 * trivially testable and safe to render anywhere.
 */
export function AuthSourceIndicator({
  source,
  compact = false,
  className = "",
}: AuthSourceIndicatorProps) {
  if (!source) return null;

  const presentation = SOURCE_PRESENTATION[source];
  if (!presentation) return null;

  if (compact) {
    return (
      <span
        className={[
          "inline-flex items-center gap-1 rounded-full border px-2 py-0.5",
          "text-[11px] font-medium",
          presentation.bgClass,
          presentation.textClass,
          presentation.borderClass,
          className,
        ]
          .filter(Boolean)
          .join(" ")}
        title={presentation.shortDescription}
      >
        <span className={presentation.iconColor}>{presentation.icon}</span>
        <span>{presentation.label}</span>
      </span>
    );
  }

  return (
    <div
      className={[
        "flex items-start gap-2 rounded-md border p-3",
        presentation.bgClass,
        presentation.borderClass,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span className={`mt-0.5 shrink-0 ${presentation.iconColor}`}>
        {presentation.icon}
      </span>
      <div className="min-w-0">
        <p className={`text-sm font-semibold ${presentation.textClass}`}>
          {presentation.label}
        </p>
        <p className={`mt-0.5 text-xs ${presentation.textClass} opacity-80`}>
          {presentation.shortDescription}
        </p>
      </div>
    </div>
  );
}
