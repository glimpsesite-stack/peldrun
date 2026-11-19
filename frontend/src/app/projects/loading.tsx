// frontend/src/app/projects/loading.tsx

/**
 * Projects Loading State
 *
 * Displayed by Next.js when navigating between project pages
 * while the page component is being loaded.
 *
 * Design:
 * - Consistent with the application's dark theme
 * - Uses skeleton loaders for visual continuity
 * - Provides clear feedback that content is being fetched
 * - Matches the layout structure of the projects page
 *
 * This file is automatically used by Next.js App Router
 * when the corresponding page.tsx is loading.
 */

import React from "react";
import { Loader2, Folder, Plus } from "lucide-react";

export default function ProjectsLoading() {
  return (
    <div className="container mx-auto px-4 py-8 max-w-7xl">
      {/* Header Skeleton */}
      <div className="flex items-center justify-between mb-8">
        <div className="space-y-2">
          <div className="h-8 w-48 bg-muted rounded animate-pulse" />
          <div className="h-4 w-64 bg-muted rounded animate-pulse" />
        </div>
        <div className="h-10 w-40 bg-muted rounded animate-pulse" />
      </div>

      {/* Toolbar Skeleton */}
      <div className="flex items-center gap-4 mb-6">
        <div className="flex-1 h-10 bg-muted rounded animate-pulse" />
        <div className="h-10 w-32 bg-muted rounded animate-pulse" />
        <div className="h-10 w-20 bg-muted rounded animate-pulse" />
      </div>

      {/* Projects Grid Skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div
            key={i}
            className="bg-card border border-border rounded-lg p-6 space-y-4"
          >
            {/* Project Header */}
            <div className="flex items-start justify-between">
              <div className="flex-1 space-y-2">
                <div className="h-5 w-3/4 bg-muted rounded animate-pulse" />
                <div className="h-4 w-full bg-muted rounded animate-pulse" />
              </div>
              <div className="h-6 w-6 bg-muted rounded animate-pulse" />
            </div>

            {/* Project Stats */}
            <div className="flex items-center gap-4">
              <div className="h-3 w-20 bg-muted rounded animate-pulse" />
              <div className="h-3 w-16 bg-muted rounded animate-pulse" />
            </div>

            {/* Project Actions */}
            <div className="flex items-center gap-2 pt-4 border-t border-border">
              <div className="flex-1 h-8 bg-muted rounded animate-pulse" />
              <div className="h-8 w-8 bg-muted rounded animate-pulse" />
              <div className="h-8 w-8 bg-muted rounded animate-pulse" />
            </div>
          </div>
        ))}
      </div>

      {/* Loading Indicator Overlay (optional) */}
      <div className="fixed bottom-4 right-4 bg-card border border-border rounded-lg shadow-lg p-3 flex items-center gap-2">
        <Loader2 size={16} className="animate-spin text-primary" />
        <span className="text-sm text-muted-foreground">Loading projects...</span>
      </div>
    </div>
  );
}
