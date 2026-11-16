// src/features/memory/components/MemoryJobStatusCard.tsx

/**
 * One-job status card for the extraction jobs list.
 *
 * A presentational component: no mutations are performed here.
 * Action callbacks (`onRetry`, `onCancel`) are forwarded to the
 * parent (`MemoryJobsList`), which owns the mutations and their
 * pending states. This separation lets a single card be reused
 * both in the list and in a "job detail" flyout without
 * duplicating mutation wiring.
 *
 * The card renders:
 *
 *   * Status pill (colored by status, icon + label).
 *   * Truncated `job_id` (monospace, for support correlation).
 *   * Trigger type (Auto per-turn / Auto session / Batch /
 *     Manual).
 *   * Turn range (`turns N..M`, or `turns —` when unbounded).
 *   * Attempts (`attempts A/M`).
 *   * Candidate / created memory counts (only when non-zero --
 *     a pending job has neither, and rendering "0 candidates"
 *     is noise).
 *   * Error code + message (only when the job failed).
 *   * Retry / Cancel buttons (only when the job's status
 *     permits; disabled during an in-flight mutation).
 */

'use client';

import {
  Ban,
  CheckCircle2,
  Clock,
  Loader2,
  RefreshCw,
  RotateCw,
  XCircle,
} from 'lucide-react';

import { cn } from '../lib/cn';
import type {
  MemoryJob,
  MemoryJobStatus,
} from '../types/jobs';
import {
  extractionTriggerLabel,
  isCancellableJobStatus,
  isRetryableJobStatus,
  jobStatusLabel,
} from '../types/jobs';

// ---------------------------------------------------------------------------
// Status styling
// ---------------------------------------------------------------------------
//
// Each status maps to a (background, foreground, icon) triple.
// The bg uses an alpha-modified standard Tailwind color so the
// same class works in light and dark mode (the foreground picks
// the darker shade for light, lighter for dark). This mirrors
// the `status-pill.tsx` UI primitive's approach without
// importing it -- keeping this component self-contained.

interface _StatusStyle {
  bg: string;
  text: string;
  Icon: typeof Clock;
}

const STATUS_STYLES: Record<MemoryJobStatus, _StatusStyle> = {
  pending: {
    bg: 'bg-amber-500/10',
    text: 'text-amber-700 dark:text-amber-300',
    Icon: Clock,
  },
  running: {
    bg: 'bg-blue-500/10',
    text: 'text-blue-700 dark:text-blue-300',
    Icon: Loader2,
  },
  retry_pending: {
    bg: 'bg-orange-500/10',
    text: 'text-orange-700 dark:text-orange-300',
    Icon: RefreshCw,
  },
  completed: {
    bg: 'bg-emerald-500/10',
    text: 'text-emerald-700 dark:text-emerald-300',
    Icon: CheckCircle2,
  },
  failed: {
    bg: 'bg-red-500/10',
    text: 'text-red-700 dark:text-red-300',
    Icon: XCircle,
  },
  cancelled: {
    bg: 'bg-slate-500/10',
    text: 'text-slate-700 dark:text-slate-300',
    Icon: Ban,
  },
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface MemoryJobStatusCardProps {
  job: MemoryJob;

  /**
   * Called when the user clicks "Retry". Required for the
   * button to render -- omit it to hide the action entirely
   * (e.g. on a read-only diagnostics page).
   */
  onRetry?: (job: MemoryJob) => void;

  /**
   * Called when the user clicks "Cancel". Same "omit to hide"
   * contract as `onRetry`.
   */
  onCancel?: (job: MemoryJob) => void;

  /**
   * `true` while a retry mutation for THIS job is in flight.
   * Disables the Retry button -- the parent toggles it from
   * the mutation's `isPending`.
   */
  retryPending?: boolean;

  /**
   * `true` while a cancel mutation for THIS job is in flight.
   * Same contract as `retryPending`.
   */
  cancelPending?: boolean;

  /**
   * Extra classes for the outer container. Useful when a
   * parent wants to adjust spacing or state (e.g. `opacity-60`
   * while a sibling job is being mutated).
   */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryJobStatusCard({
  job,
  onRetry,
  onCancel,
  retryPending = false,
  cancelPending = false,
  className,
}: MemoryJobStatusCardProps) {
  const style = STATUS_STYLES[job.status];
  const Icon = style.Icon;

  const canRetry = isRetryableJobStatus(job.status) && onRetry;
  const canCancel = isCancellableJobStatus(job.status) && onCancel;
  const showActions = canRetry || canCancel;

  const rangeText =
    job.start_turn_index !== null && job.end_turn_index !== null
      ? `turns ${job.start_turn_index}..${job.end_turn_index}`
      : 'turns —';

  const attemptText = `attempts ${job.attempts}/${job.max_attempts}`;

  return (
    <div
      className={cn(
        'rounded-sm border border-border/60 bg-card p-3 space-y-2',
        'transition-colors hover:border-border',
        className,
      )}
    >
      {/* Header: status pill + job id + trigger label */}
      <div className="flex items-center gap-2">
        <span
          className={cn(
            'inline-flex items-center gap-1 rounded px-1.5 py-0.5',
            'text-[10px] font-medium',
            style.bg,
            style.text,
          )}
        >
          <Icon
            size={10}
            aria-hidden="true"
            className={job.status === 'running' ? 'animate-spin' : undefined}
          />
          {jobStatusLabel(job.status)}
        </span>

        <span className="font-mono text-[10px] text-muted-foreground truncate">
          {job.job_id}
        </span>

        <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
          {extractionTriggerLabel(job.trigger_type)}
        </span>
      </div>

      {/* Metadata row */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
        <span>{rangeText}</span>
        <span>{attemptText}</span>
        {job.candidates_count > 0 && (
          <span>{job.candidates_count} candidates</span>
        )}
        {job.created_memories_count > 0 && (
          <span>{job.created_memories_count} created</span>
        )}
        {job.cost_nano_usd > 0 && (
          <span className="font-mono">
            ${(job.cost_nano_usd / 1_000_000_000).toFixed(4)}
          </span>
        )}
      </div>

      {/* Error block (only on failed / retry_pending) */}
      {job.error_message && (
        <div
          className={cn(
            'rounded-sm border border-red-500/20 bg-red-500/5 px-2 py-1',
            'text-[10px] text-red-700 dark:text-red-300',
          )}
        >
          {job.error_code && (
            <span className="font-mono font-semibold">
              {job.error_code}:{' '}
            </span>
          )}
          {job.error_message}
        </div>
      )}

      {/* Actions */}
      {showActions && (
        <div className="flex gap-2 pt-1">
          {canRetry && (
            <button
              type="button"
              onClick={() => onRetry(job)}
              disabled={retryPending}
              className={cn(
                'inline-flex items-center gap-1 rounded-sm border border-border/60',
                'bg-background px-2 py-1 text-[10px]',
                'hover:bg-muted/40 transition-colors',
                'disabled:opacity-50 disabled:cursor-not-allowed',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
              )}
            >
              <RotateCw
                size={10}
                aria-hidden="true"
                className={retryPending ? 'animate-spin' : undefined}
              />
              Retry
            </button>
          )}

          {canCancel && (
            <button
              type="button"
              onClick={() => onCancel(job)}
              disabled={cancelPending}
              className={cn(
                'inline-flex items-center gap-1 rounded-sm border border-border/60',
                'bg-background px-2 py-1 text-[10px]',
                'hover:bg-muted/40 transition-colors',
                'disabled:opacity-50 disabled:cursor-not-allowed',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
              )}
            >
              <Ban
                size={10}
                aria-hidden="true"
                className={cancelPending ? 'animate-pulse' : undefined}
              />
              Cancel
            </button>
          )}
        </div>
      )}
    </div>
  );
}
