// frontend/src/app/projects/[projectId]/loading.tsx

/**
 * Project Detail Loading State
 *
 * Displayed by Next.js when navigating to /projects/[projectId]/*
 * while the page component is being loaded.
 *
 * Design:
 * - Two-column layout skeleton matching the project dashboard structure
 * - Left side: project header + stats + quick actions
 * - Right side: workspace panel placeholder
 * - Consistent with the application's dark theme
 * - Uses skeleton loaders for visual continuity
 *
 * This file is automatically used by Next.js App Router
 * when any child route of /projects/[projectId]/* is loading.
 */

import React from "react";
import { Loader2 } from "lucide-react";

export default function ProjectDetailLoading() {
  return (
    <div className="flex h-screen">
      {/* Main Content Area Skeleton */}
      <div className="flex-1 flex flex-col overflow-auto">
        {/* Header Skeleton */}
        <div className="border-b border-border bg-card p-6">
          <div className="flex items-start justify-between mb-4">
            <div className="flex-1 space-y-3">
              <div className="h-8 w-64 bg-muted rounded animate-pulse" />
              <div className="h-4 w-96 bg-muted rounded animate-pulse" />
            </div>
            <div className="flex items-center gap-2">
              <div className="h-9 w-20 bg-muted rounded animate-pulse" />
              <div className="h-9 w-24 bg-muted rounded animate-pulse" />
            </div>
          </div>

          {/* Project Stats Skeleton */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="bg-muted/50 border border-border rounded-lg p-4 space-y-3"
              >
                <div className="flex items-center gap-2">
                  <div className="h-4 w-4 bg-muted rounded animate-pulse" />
                  <div className="h-4 w-16 bg-muted rounded animate-pulse" />
                </div>
                <div className="h-8 w-12 bg-muted rounded animate-pulse" />
                <div className="h-3 w-24 bg-muted rounded animate-pulse" />
              </div>
            ))}
          </div>
        </div>

        {/* Quick Actions Skeleton */}
        <div className="p-6 border-b border-border">
          <div className="h-4 w-32 bg-muted rounded animate-pulse mb-3" />
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="h-24 bg-muted/30 border border-border rounded-lg animate-pulse"
              />
            ))}
          </div>
        </div>

        {/* Project Info Skeleton */}
        <div className="p-6">
          <div className="h-4 w-40 bg-muted rounded animate-pulse mb-3" />
          <div className="bg-muted/30 border border-border rounded-lg p-4 space-y-4">
            {[1, 2, 3].map((i) => (
              <div key={i} className="flex items-start gap-3">
                <div className="h-4 w-4 bg-muted rounded animate-pulse mt-0.5" />
                <div className="flex-1 space-y-2">
                  <div className="h-4 w-32 bg-muted rounded animate-pulse" />
                  <div className="h-3 w-64 bg-muted rounded animate-pulse" />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Workspace Panel Skeleton (Right Side) */}
      <div className="w-[500px] border-l border-border hidden lg:flex flex-col">
        {/* Panel Header */}
        <div className="h-12 border-b border-border bg-card px-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <div
                key={i}
                className="h-8 w-20 bg-muted rounded animate-pulse"
              />
            ))}
          </div>
          <div className="flex items-center gap-2">
            <div className="h-7 w-7 bg-muted rounded animate-pulse" />
            <div className="h-7 w-24 bg-muted rounded animate-pulse" />
          </div>
        </div>

        {/* Panel Content */}
        <div className="flex-1 flex items-center justify-center bg-background">
          <div className="flex flex-col items-center gap-3">
            <Loader2 size={24} className="animate-spin text-primary" />
            <span className="text-sm text-muted-foreground">
              Loading workspace...
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
