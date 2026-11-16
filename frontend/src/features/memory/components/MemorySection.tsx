// src/features/memory/components/MemorySection.tsx

/**
 * A reusable "section" container for memory pages.
 *
 * Replaces the ad-hoc `rounded-lg border bg-card p-3` pattern
 * that predates the design alignment. Provides:
 *
 *   * The `bg-gigbg` surface used by SearchTab.
 *   * A 2px accent line at the top (color-coded by `accent`).
 *   * An optional header: icon + title + subtitle + action.
 *   * `FADE_UP` entry animation.
 *
 * The section does NOT impose a layout on its children -- callers
 * decide whether to render a single column, a grid, or a list.
 * This keeps the wrapper useful across every context (KPI grids,
 * form sections, chart containers, provider lists).
 */

'use client';

import { motion } from 'framer-motion';
import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';
import { EASE_OUT_EXPO } from '../lib/motion';

export type MemorySectionAccent =
  | 'primary'
  | 'emerald'
  | 'rose'
  | 'amber'
  | 'cyan'
  | 'purple';

const ACCENT_STYLES: Record<
  MemorySectionAccent,
  { line: string; icon: string }
> = {
  primary: { line: 'via-primary/10', icon: 'text-primary' },
  emerald: { line: 'via-emerald-500/10', icon: 'text-emerald-500' },
  rose: { line: 'via-rose-500/10', icon: 'text-rose-500' },
  amber: { line: 'via-amber-500/10', icon: 'text-amber-500' },
  cyan: { line: 'via-cyan-500/10', icon: 'text-cyan-500' },
  purple: { line: 'via-purple-500/10', icon: 'text-purple-500' },
};

export interface MemorySectionProps {
  /** Optional icon to display in the section header. */
  icon?: LucideIcon;
  /** Optional section title (rendered uppercase with tracking). */
  title?: string;
  /** Optional subtitle (rendered small, muted). */
  subtitle?: string;
  /** Optional action slot (toggles, buttons) aligned to the right. */
  action?: ReactNode;
  /** Accent color for the top line and icon. Defaults to `primary`. */
  accent?: MemorySectionAccent;
  /** Delay before the entry animation starts, in seconds. */
  delay?: number;
  /** When false, hides the top accent line (useful for nested sections). */
  showAccentLine?: boolean;
  children: ReactNode;
  className?: string;
}

export function MemorySection({
  icon: Icon,
  title,
  subtitle,
  action,
  accent = 'primary',
  delay = 0,
  showAccentLine = true,
  children,
  className,
}: MemorySectionProps) {
  const styles = ACCENT_STYLES[accent];
  const hasHeader = Boolean(Icon || title || action);

  return (
    <motion.section
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: EASE_OUT_EXPO }}
      className={cn(
        'relative rounded-md bg-muted shadow-sm',
        hasHeader ? 'space-y-5 p-5' : 'p-5',
        className,
      )}
    >
      {showAccentLine && (
        <div
          className={cn(
            'pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md',
            'bg-gradient-to-r from-transparent to-transparent shadow-sm',
            styles.line,
          )}
          aria-hidden="true"
        />
      )}

      {hasHeader && (
        <div className="flex items-center justify-between flex-wrap gap-3">
          {(Icon || title) && (
            <div className="flex items-center gap-3">
              {Icon && (
                <div className="p-2 icon">
                  <Icon
                    size={16}
                    className={styles.icon}
                    aria-hidden="true"
                  />
                </div>
              )}
              {title && (
                <div>
                  <h3 className="text-md font-semibold font-heading uppercase tracking-wider text-foreground">
                    {title}
                  </h3>
                  {subtitle && (
                    <p className="text-[11px] text-muted-foreground">
                      {subtitle}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
          {action}
        </div>
      )}

      {children}
    </motion.section>
  );
}
