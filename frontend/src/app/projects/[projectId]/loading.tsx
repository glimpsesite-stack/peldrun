// frontend/src/app/projects/[projectId]/loading.tsx
"use client";

import React from "react";
import { motion } from "motion/react";
import { Loader2 } from "lucide-react";

/**
 * A unified shimmer primitive — a soft highlight sweeps once across
 * the element continuously. Cleaner than per-element pulse and
 * cheaper than animating backgroundPosition.
 */
function Shimmer({
  className = "",
  delay = 0,
}: {
  className?: string;
  delay?: number;
}) {
  return (
    <div className={`relative overflow-hidden rounded-md bg-muted ${className}`}>
      <motion.div
        className="absolute inset-0"
        style={{
          background:
            "linear-gradient(90deg, transparent 0%, hsl(var(--muted-foreground) / 0.14) 50%, transparent 100%)",
        }}
        animate={{ x: ["-100%", "100%"] }}
        transition={{
          duration: 1.6,
          repeat: Infinity,
          ease: "linear",
          delay,
        }}
      />
    </div>
  );
}

const fadeUp = {
  hidden: { opacity: 0, y: 10 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.4, ease: [0.16, 1, 0.3, 1] as const },
  },
};

const stagger = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.05, delayChildren: 0.05 } },
};

export default function ProjectDetailLoading() {
  return (
    <div className="flex h-screen">
      {/* Left / main content */}
      <motion.div
        className="flex-1 flex flex-col overflow-auto"
        variants={stagger}
        initial="hidden"
        animate="visible"
      >
        {/* Header */}
        <div className="border-b border-border bg-card p-6">
          <div className="flex items-start justify-between mb-6 gap-4">
            <motion.div variants={fadeUp} className="flex-1 space-y-3">
              <Shimmer className="h-8 w-64" />
              <Shimmer className="h-4 w-96 max-w-full" />
            </motion.div>
            <motion.div variants={fadeUp} className="flex items-center gap-2">
              <Shimmer className="h-9 w-20" />
              <Shimmer className="h-9 w-24" />
            </motion.div>
          </div>

          {/* Stats grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[1, 2, 3, 4].map((i) => (
              <motion.div
                key={i}
                variants={fadeUp}
                className="bg-muted/30 border border-border rounded-xl p-4 space-y-3"
              >
                <div className="flex items-center gap-2">
                  <Shimmer className="h-6 w-6 rounded-md" />
                  <Shimmer className="h-3.5 w-16" />
                </div>
                <Shimmer className="h-7 w-12" />
                <Shimmer className="h-3 w-24" />
              </motion.div>
            ))}
          </div>
        </div>

        {/* Quick actions */}
        <motion.div
          variants={fadeUp}
          className="p-6 border-b border-border"
        >
          <Shimmer className="h-3.5 w-28 mb-4" />
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="bg-muted/20 border border-border rounded-xl p-4 space-y-3"
              >
                <Shimmer className="h-9 w-9 rounded-lg" />
                <Shimmer className="h-3.5 w-24" />
                <Shimmer className="h-3 w-32 max-w-full" />
              </div>
            ))}
          </div>
        </motion.div>

        {/* Project info */}
        <motion.div variants={fadeUp} className="p-6">
          <Shimmer className="h-3.5 w-36 mb-4" />
          <div className="bg-muted/20 border border-border rounded-xl p-5 space-y-5">
            {[1, 2, 3].map((i) => (
              <div key={i} className="flex items-start gap-3">
                <Shimmer className="h-6 w-6 rounded-md mt-0.5" />
                <div className="flex-1 space-y-2">
                  <Shimmer className="h-3.5 w-32" />
                  <Shimmer className="h-3 w-64 max-w-full" />
                </div>
              </div>
            ))}
          </div>
        </motion.div>
      </motion.div>

      {/* Right / panel */}
      <motion.aside
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1], delay: 0.1 }}
        className="w-[500px] border-l border-border hidden lg:flex flex-col shrink-0"
      >
        <div className="h-12 border-b border-border bg-card px-4 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, scale: 0.85 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{
                  delay: 0.2 + i * 0.05,
                  duration: 0.3,
                  ease: [0.16, 1, 0.3, 1],
                }}
              >
                <Shimmer className="h-8 w-16" />
              </motion.div>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Shimmer className="h-7 w-7" />
            <Shimmer className="h-7 w-20" />
          </div>
        </div>

        <div className="flex-1 flex items-center justify-center bg-background">
          <motion.div
            initial={{ opacity: 0, scale: 0.94 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{
              delay: 0.35,
              duration: 0.45,
              ease: [0.16, 1, 0.3, 1],
            }}
            className="flex flex-col items-center gap-4"
          >
            <div className="relative">
              <motion.div
                className="absolute inset-0 rounded-full bg-accent/20 blur-xl"
                animate={{ scale: [1, 1.4, 1], opacity: [0.4, 0.7, 0.4] }}
                transition={{
                  duration: 2.2,
                  repeat: Infinity,
                  ease: "easeInOut",
                }}
              />
              <Loader2
                size={26}
                className="animate-spin text-accent relative"
              />
            </div>
            <span className="text-sm text-muted-foreground">
              Loading workspace
            </span>
          </motion.div>
        </div>
      </motion.aside>
    </div>
  );
}
