// src/features/memory/components/MemoryBanner.tsx

/**
 * Alert banners for inline messaging (info / warning / error / success).
 *
 * Replaces the ad-hoc `rounded-md border border-{color}-500/40
 * bg-{color}-500/10` pattern scattered throughout the memory
 * pages. Centralizing the palette here ensures:
 *
 *   * Consistent color tokens across every page.
 *   * Consistent icon usage.
 *   * Consistent title/body typography.
 *
 * Usage:
 *
 *     <MemoryBanner
 *       tone="warning"
 *       title="Memory is disabled"
 *       message="Enable memory to save entries."
 *       action={<Link href="/memory/settings">Enable</Link>}
 *     />
 */

'use client';

import { AlertCircle, CheckCircle2, Info, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';

export type MemoryBannerTone = 'info' | 'warning' | 'error' | 'success';

const TONE_STYLES = {
  info: {
    icon: Info,
    bg: 'bg-cyan-500/10',
    border: 'border-cyan-500/40',
    text: 'text-cyan-700 dark:text-cyan-300',
  },
  warning: {
    icon: AlertCircle,
    bg: 'bg-amber-500/10',
    border: 'border-amber-500/40',
    text: 'text-amber-700 dark:text-amber-300',
  },
  error: {
    icon: XCircle,
    bg: 'bg-rose-500/10',
    border: 'border-rose-500/40',
    text: 'text-rose-600 dark:text-rose-400',
  },
  success: {
    icon: CheckCircle2,
    bg: 'bg-emerald-500/10',
    border: 'border-emerald-500/40',
    text: 'text-emerald-600 dark:text-emerald-400',
  },
} as const;

export interface MemoryBannerProps {
  tone: MemoryBannerTone;
  title?: string;
  message: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function MemoryBanner({
  tone,
  title,
  message,
  action,
  className,
}: MemoryBannerProps) {
  const styles = TONE_STYLES[tone];
  const Icon = styles.icon;
  return (
    <div
      role="alert"
      className={cn(
        'flex items-start gap-3 p-3.5 rounded-md border text-xs',
        styles.bg,
        styles.border,
        styles.text,
        className,
      )}
    >
      <Icon size={16} className="shrink-0 mt-0.5" aria-hidden="true" />
      <div className="flex-1 min-w-0 space-y-0.5">
        {title && <div className="font-semibold">{title}</div>}
        <div className={title ? 'opacity-90 text-[11px]' : ''}>
          {message}
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
