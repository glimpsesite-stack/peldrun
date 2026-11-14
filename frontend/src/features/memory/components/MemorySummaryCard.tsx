// src/features/memory/components/MemorySummaryCard.tsx

/**
 * A KPI card, styled after SearchTab's `<SummaryCard>`.
 *
 * The card is composed of:
 *
 *   * A subtle gradient line across the top (colored by `accent`).
 *   * A blurred corner glow that intensifies on hover.
 *   * A label (uppercase, tracking-wider, muted).
 *   * A large numeric value (mono, tabular-nums, colored).
 *   * An optional subtitle beneath the value.
 *   * An icon in the top-right corner.
 *
 * Hover behavior: the whole card lifts by 4px and the border
 * picks up a hint of primary color. This matches the interaction
 * language used by SearchTab's cards.
 */

'use client';

import { motion } from 'framer-motion';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';
import { EASE_OUT_EXPO } from '../lib/motion';

export interface MemorySummaryCardProps {
  /** Uppercase label above the value. */
  label: string;
  /** The numeric / textual value to display prominently. */
  value: string;
  /** Icon rendered in the top-right. */
  icon: ReactNode;
  /** Accent color for the top line, value, and icon. */
  accent?: string;
  /** Optional supporting text below the value. */
  subtitle?: string;
  /** Entry animation delay in seconds. */
  delay?: number;
  className?: string;
}

export function MemorySummaryCard({
  label,
  value,
  icon,
  accent = '#3b82f6',
  subtitle,
  delay = 0,
  className,
}: MemorySummaryCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: EASE_OUT_EXPO }}
      whileHover={{ y: -4, transition: { duration: 0.2 } }}
      className={cn(
        'group relative p-5 rounded-md border border-card bg-card overflow-hidden shadow-sm',
        'hover:shadow-lg hover:border-primary/30 transition-all',
        className,
      )}
    >
      <div
        className="absolute top-0 left-0 right-0 h-[1px] opacity-30 group-hover:opacity-100 transition-opacity"
        style={{
          background: `linear-gradient(90deg, transparent 0%, ${accent} 50%, transparent 100%)`,
        }}
        aria-hidden="true"
      />
      <div
        className="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-[0.08] group-hover:opacity-[0.15] transition-opacity blur-2xl"
        style={{ backgroundColor: accent }}
        aria-hidden="true"
      />
      <div className="relative flex items-start justify-between">
        <div className="flex-1 space-y-3 min-w-0">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block">
            {label}
          </span>
          <div
            className="text-xl lg:text-2xl font-bold font-mono tabular-nums truncate"
            style={{ color: accent }}
          >
            {value}
          </div>
          {subtitle && (
            <div
              className="text-[11px] text-muted-foreground truncate"
              title={subtitle}
            >
              {subtitle}
            </div>
          )}
        </div>
        <div
          className="p-2.5 rounded-sm shrink-0 ml-2"
          style={{ color: accent }}
          aria-hidden="true"
        >
          {icon}
        </div>
      </div>
    </motion.div>
  );
}
