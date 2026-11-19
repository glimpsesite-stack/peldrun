// frontend/src/app/projects/error.tsx

/**
 * Projects Error Boundary
 *
 * Next.js App Router error boundary for the /projects/* route segment.
 * Automatically catches rendering errors in child components and
 * provides a user-friendly recovery interface.
 *
 * Design Principles:
 * - Consistent with the application's dark theme
 * - Provides clear error context without exposing internals
 * - Offers actionable recovery options (retry, go back)
 * - Logs errors to console for debugging
 * - Does NOT catch errors in layout.tsx or loading.tsx (Next.js behavior)
 *
 * This file is automatically used by Next.js App Router when a
 * child component of /projects/* throws during rendering.
 */

"use client";

import React, { useEffect } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, RefreshCw, ArrowLeft, Home } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ProjectsErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ProjectsError({ error, reset }: ProjectsErrorProps) {
  const router = useRouter();

  // Log error to console for debugging
  useEffect(() => {
    console.error("[ProjectsError] Rendering error:", error);
    if (error.digest) {
      console.error("[ProjectsError] Error digest:", error.digest);
    }
  }, [error]);

  const handleGoBack = () => {
    router.back();
  };

  const handleGoHome = () => {
    router.push("/");
  };

  return (
    <div className="container mx-auto px-4 py-16 max-w-2xl">
      <div className="bg-card border border-border rounded-lg p-8 shadow-lg">
        {/* Error Header */}
        <div className="flex items-start gap-4 mb-6">
          <div className="p-3 bg-destructive/10 rounded-lg">
            <AlertTriangle
              size={24}
              className="text-destructive"
            />
          </div>
          <div className="flex-1">
            <h2 className="text-xl font-semibold mb-2">
              Something went wrong
            </h2>
            <p className="text-sm text-muted-foreground">
              An unexpected error occurred while loading the projects page.
              You can try again or navigate to a different section.
            </p>
          </div>
        </div>

        {/* Error Details (collapsible) */}
        <details className="mb-6 border border-border rounded-md overflow-hidden">
          <summary className="px-4 py-2 bg-muted/50 text-xs font-mono cursor-pointer hover:bg-muted transition-colors">
            Error Details (for debugging)
          </summary>
          <div className="p-4 bg-background border-t border-border">
            <p className="text-xs font-mono text-destructive mb-2 break-words">
              {error.message || "Unknown error"}
            </p>
            {error.digest && (
              <p className="text-xs font-mono text-muted-foreground">
                Digest: {error.digest}
              </p>
            )}
            {error.stack && (
              <pre className="mt-3 p-3 bg-muted/30 rounded text-[10px] font-mono overflow-auto max-h-48 text-muted-foreground">
                {error.stack}
              </pre>
            )}
          </div>
        </details>

        {/* Action Buttons */}
        <div className="flex items-center gap-3">
          <Button
            onClick={reset}
            className="gap-2 flex-1"
          >
            <RefreshCw size={16} />
            Try Again
          </Button>
          <Button
            variant="outline"
            onClick={handleGoBack}
            className="gap-2"
          >
            <ArrowLeft size={16} />
            Go Back
          </Button>
          <Button
            variant="ghost"
            onClick={handleGoHome}
            className="gap-2"
          >
            <Home size={16} />
            Home
          </Button>
        </div>

        {/* Help Text */}
        <div className="mt-6 pt-6 border-t border-border">
          <p className="text-xs text-muted-foreground">
            If this problem persists, please check the browser console for
            more details or contact support with the error digest above.
          </p>
        </div>
      </div>
    </div>
  );
}
