// frontend/src/app/projects/[projectId]/error.tsx
"use client";

import React, { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion } from "motion/react";
import {
  AlertTriangle,
  RefreshCw,
  ArrowLeft,
  Home,
  Folder,
  ChevronDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";

interface ProjectErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

export default function ProjectError({ error, reset }: ProjectErrorProps) {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  useEffect(() => {
    console.error("[ProjectError] Rendering error:", error);
    if (error.digest) {
      console.error("[ProjectError] Error digest:", error.digest);
    }
  }, [error]);

  return (
    <motion.div
      className="container mx-auto px-4 py-16 max-w-2xl"
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: EASE_OUT }}
    >
      <div className="bg-card border border-border rounded-xl p-8 shadow-sm">
        {/* Header */}
        <div className="flex items-start gap-4 mb-6">
          <motion.div
            className="p-3 bg-destructive/10 rounded-xl shrink-0"
            initial={{ scale: 0.8, rotate: -6 }}
            animate={{ scale: 1, rotate: [0, -4, 4, -2, 0] }}
            transition={{
              scale: { type: "spring", stiffness: 260, damping: 18 },
              rotate: { duration: 0.5, delay: 0.15, ease: "easeOut" },
            }}
          >
            <AlertTriangle size={22} className="text-destructive" />
          </motion.div>
          <div className="flex-1">
            <h2 className="text-xl font-semibold mb-1">Project Error</h2>
            <p className="text-sm text-muted-foreground">
              An unexpected error occurred while loading this project.
              You can try again or navigate to a different section.
            </p>
          </div>
        </div>

        {/* Context */}
        <div className="mb-5 p-3 bg-muted/40 border border-border rounded-lg">
          <div className="flex items-center gap-2 text-sm">
            <Folder size={13} className="text-muted-foreground shrink-0" />
            <span className="text-muted-foreground">Project ID:</span>
            <code className="font-mono text-xs bg-background px-2 py-0.5 rounded border border-border truncate">
              {projectId}
            </code>
          </div>
        </div>

        {/* Details */}
        <details className="group mb-6 border border-border rounded-lg overflow-hidden">
          <summary className="flex items-center justify-between px-4 py-2.5 bg-muted/50 text-xs font-mono cursor-pointer hover:bg-muted transition-colors">
            <span>Error Details (for debugging)</span>
            <ChevronDown
              size={14}
              className="transition-transform group-open:rotate-180"
            />
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

        {/* Actions */}
        <motion.div
          className="flex items-center gap-3 flex-wrap"
          initial="hidden"
          animate="visible"
          variants={{
            hidden: {},
            visible: { transition: { staggerChildren: 0.06, delayChildren: 0.15 } },
          }}
        >
          {[
            {
              key: "retry",
              node: (
                <Button onClick={reset} className="gap-2 flex-1 min-w-[140px]">
                  <RefreshCw size={15} />
                  Try Again
                </Button>
              ),
            },
            {
              key: "back",
              node: (
                <Button
                  variant="outline"
                  onClick={() => router.back()}
                  className="gap-2"
                >
                  <ArrowLeft size={15} />
                  Go Back
                </Button>
              ),
            },
            {
              key: "all",
              node: (
                <Button
                  variant="outline"
                  onClick={() => router.push("/projects")}
                  className="gap-2"
                >
                  <Folder size={15} />
                  All Projects
                </Button>
              ),
            },
            {
              key: "home",
              node: (
                <Button
                  variant="ghost"
                  onClick={() => router.push("/")}
                  className="gap-2"
                >
                  <Home size={15} />
                  Home
                </Button>
              ),
            },
          ].map(({ key, node }) => (
            <motion.div
              key={key}
              variants={{
                hidden: { opacity: 0, y: 8 },
                visible: {
                  opacity: 1,
                  y: 0,
                  transition: { duration: 0.32, ease: EASE_OUT },
                },
              }}
              whileHover={{ y: -1 }}
              whileTap={{ scale: 0.97 }}
              className="contents"
            >
              {node}
            </motion.div>
          ))}
        </motion.div>

        {/* Help */}
        <div className="mt-6 pt-5 border-t border-border">
          <p className="text-xs text-muted-foreground">
            If this problem persists, please check the browser console for
            more details or contact support with the error digest above.
          </p>
        </div>
      </div>
    </motion.div>
  );
}
