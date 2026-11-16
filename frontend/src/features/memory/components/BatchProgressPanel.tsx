// src/features/memory/components/BatchProgressPanel.tsx

/**
 * Live progress panel for one batch.
 *
 * Uses a SINGLE `useQuery` call with a DYNAMIC `refetchInterval`
 * that flips from `false` to 3000ms while the batch is active
 * (running / pending / paused) and back to `false` once it
 * reaches a terminal state. The polling interval comes from
 * `useAutomationPolling`, which also pauses when the browser
 * tab is hidden.
 *
 * Why `useQuery` directly (not `useBatchDetail`)
 * ----------------------------------------------
 * The wrapper hook (`useBatchDetail` in `queries.ts`) forwards
 * only `enabled` and `staleTime`. This panel needs to modify
 * `refetchInterval` dynamically as the batch's status evolves.
 * Rather than extend the wrapper's option surface (which would
 * leak polling semantics into a generic read hook), the panel
 * uses `useQuery` with the same `memoryKeys.automation.batch.
 * detail(batchId)` cache key -- so it shares cache with any
 * other caller of `useBatchDetail`, but controls its OWN polling.
 *
 * Progress source
 * ---------------
 * `progress` (from `BatchProgress`) is the AUTHORITATIVE live
 * view -- computed from `memory_jobs` on every request. The
 * panel renders "N of M" and the percentage from THAT, not from
 * `batch.completed_jobs` / `batch.job_count` (which are
 * historical snapshots and can lag).
 *
 * Actions
 * -------
 * Pause / Resume / Cancel buttons render ONLY when the backend
 * would accept the corresponding transition (see `types/batch.ts`
 * predicates). A disabled-state button would be misleading: the
 * user cannot "try again in a moment" on a 409.
 */

'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  AlertCircle,
  Loader2,
  Pause,
  Play,
  Square,
} from 'lucide-react';

import { cn } from '../lib/cn';
import { memoryClient } from '../api/client';
import { memoryKeys } from '../api/keys';
import {
  useCancelBatch,
  usePauseBatch,
  useResumeBatch,
} from '../api/mutations';
import { useAutomationPolling } from '../hooks/useAutomationPolling';
import {
  batchStatusLabel,
  isCancellableBatchStatus,
  isPausableBatchStatus,
  isResumableBatchStatus,
  isTerminalBatchStatus,
  nanoUsdToUsd,
} from '../types/batch';
import type {
  BatchProgress,
  MemoryBatch,
} from '../types/batch';
import type { BatchGetResponse } from '../types/responses';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface BatchProgressPanelProps {
  /** The batch to display. */
  batchId: string;

  /**
   * Extra classes for the outer container. Passed through so
   * the page can position the panel without wrapping it in
   * another div.
   */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BatchProgressPanel({
  batchId,
  className,
}: BatchProgressPanelProps) {
  // Polling state: start enabled so the first paint shows fresh
  // data; the effect below flips it off once a terminal status
  // is observed.
  const [shouldPoll, setShouldPoll] = useState(true);

  const polling = useAutomationPolling({
    shouldPoll,
    intervalMs: 3000,
  });

  const query = useQuery<BatchGetResponse, Error>({
    queryKey: memoryKeys.automation.batch.detail(batchId),
    queryFn: ({ signal }) =>
      memoryClient.automation.batch.get(batchId, { signal }),
    staleTime: 0,
    ...polling,
  });

  // Stop polling once the batch reaches a terminal state.
  useEffect(() => {
    if (query.data?.batch) {
      setShouldPoll(!isTerminalBatchStatus(query.data.batch.status));
    }
  }, [query.data?.batch?.status]);

  const pauseMutation = usePauseBatch();
  const resumeMutation = useResumeBatch();
  const cancelMutation = useCancelBatch();

  // -------------------------------------------------------------------------
  // Loading / error branches
  // -------------------------------------------------------------------------

  if (query.isPending && !query.data) {
    return (
      <div
        className={cn(
          'rounded-sm border border-border/60 bg-card p-4',
          className,
        )}
      >
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          Loading batch…
        </div>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div
        className={cn(
          'rounded-sm border border-red-500/20 bg-red-500/5 p-4',
          'text-[11px] text-red-700 dark:text-red-300',
          className,
        )}
      >
        <AlertCircle size={12} className="inline mr-1" aria-hidden="true" />
        Failed to load batch: {query.error?.message ?? 'unknown error'}
      </div>
    );
  }

  if (!query.data) return null;

  const batch = query.data.batch;
  const progress = query.data.progress;

  // -------------------------------------------------------------------------
  // Action gating
  // -------------------------------------------------------------------------

  const canPause =
    isPausableBatchStatus(batch.status) && !pauseMutation.isPending;
  const canResume =
    isResumableBatchStatus(batch.status) && !resumeMutation.isPending;
  const canCancel =
    isCancellableBatchStatus(batch.status) && !cancelMutation.isPending;

  return (
    <div
      className={cn(
        'rounded-sm border border-border/60 bg-card p-4 space-y-3',
        className,
      )}
    >
      <_Header
        batch={batch}
        canPause={canPause}
        canResume={canResume}
        canCancel={canCancel}
        isPausing={pauseMutation.isPending}
        isResuming={resumeMutation.isPending}
        isCancelling={cancelMutation.isPending}
        onPause={() => pauseMutation.mutate({ batchId })}
        onResume={() => resumeMutation.mutate({ batchId })}
        onCancel={() => cancelMutation.mutate({ batchId })}
      />

      <_ProgressBar progress={progress} />

      <_StatusBreakdown progress={progress} />

      <_CostFooter batch={batch} progress={progress} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function _Header({
  batch,
  canPause,
  canResume,
  canCancel,
  isPausing,
  isResuming,
  isCancelling,
  onPause,
  onResume,
  onCancel,
}: {
  batch: MemoryBatch;
  canPause: boolean;
  canResume: boolean;
  canCancel: boolean;
  isPausing: boolean;
  isResuming: boolean;
  isCancelling: boolean;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="flex items-start justify-between gap-2">
      <div className="min-w-0">
        <div className="font-mono text-[11px] font-semibold text-foreground truncate">
          {batch.batch_id}
        </div>
        <div className="text-[10px] text-muted-foreground">
          {batchStatusLabel(batch.status)} • {batch.job_count} jobs
          {batch.model_provider_id && batch.model_id && (
            <>
              {' '}•{' '}
              <span className="font-mono">
                {batch.model_provider_id}::{batch.model_id}
              </span>
            </>
          )}
        </div>
      </div>

      <div className="flex gap-1.5 shrink-0">
        {canPause && (
          <_ActionButton onClick={onPause} pending={isPausing}>
            <Pause size={10} aria-hidden="true" />
            Pause
          </_ActionButton>
        )}
        {canResume && (
          <_ActionButton onClick={onResume} pending={isResuming}>
            <Play size={10} aria-hidden="true" />
            Resume
          </_ActionButton>
        )}
        {canCancel && (
          <_ActionButton onClick={onCancel} pending={isCancelling}>
            <Square size={10} aria-hidden="true" />
            Cancel
          </_ActionButton>
        )}
      </div>
    </div>
  );
}

function _ActionButton({
  onClick,
  pending,
  children,
}: {
  onClick: () => void;
  pending: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      className={cn(
        'inline-flex items-center gap-1 rounded-sm border border-border/60',
        'bg-background px-2 py-1 text-[10px]',
        'hover:bg-muted/40 transition-colors',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
      )}
    >
      {pending ? (
        <Loader2 size={10} className="animate-spin" aria-hidden="true" />
      ) : (
        children
      )}
    </button>
  );
}

function _ProgressBar({ progress }: { progress: BatchProgress }) {
  const pct =
    progress.total > 0
      ? Math.round((progress.completed / progress.total) * 100)
      : 0;

  return (
    <div>
      <div className="flex justify-between text-[10px] text-muted-foreground mb-1">
        <span>
          {progress.completed} / {progress.total} completed
        </span>
        <span className="font-mono">{pct}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted/40">
        <div
          className="h-full bg-emerald-500 transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function _StatusBreakdown({ progress }: { progress: BatchProgress }) {
  const pending =
    (progress.by_status.pending ?? 0) +
    (progress.by_status.retry_pending ?? 0);
  const running = progress.by_status.running ?? 0;

  return (
    <div className="grid grid-cols-3 gap-2 text-[10px]">
      <_StatCell label="Pending" value={pending} />
      <_StatCell label="Running" value={running} />
      <_StatCell label="Failed" value={progress.failed} />
    </div>
  );
}

function _StatCell({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-sm border border-border/40 bg-muted/20 px-2 py-1">
      <div className="text-muted-foreground">{label}</div>
      <div className="font-semibold text-foreground">{value}</div>
    </div>
  );
}

function _CostFooter({
  batch,
  progress,
}: {
  batch: MemoryBatch;
  progress: BatchProgress;
}) {
  const actual = nanoUsdToUsd(progress.actual_cost_nano_usd);
  const estimated = nanoUsdToUsd(batch.estimated_cost_nano_usd);
  const showEstimate = batch.estimated_cost_nano_usd > 0;

  return (
    <div className="flex justify-between text-[10px] pt-1 border-t border-border/40">
      <span className="text-muted-foreground">Actual cost</span>
      <span className="font-mono text-foreground">
        ${actual.toFixed(4)}
        {showEstimate && (
          <span className="text-muted-foreground">
            {' '}/ ~${estimated.toFixed(4)} est.
          </span>
        )}
      </span>
    </div>
  );
}
