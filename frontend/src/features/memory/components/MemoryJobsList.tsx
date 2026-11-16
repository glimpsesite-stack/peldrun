// src/features/memory/components/MemoryJobsList.tsx

/**
 * Paginated list of extraction jobs with filters and inline actions.
 *
 * Renders a compact table of `MemoryJobStatusCard` items, one per
 * job. Provides status / trigger / chat filters, "Load more"
 * pagination, and live-polling while any visible job is still
 * active. Actions (Retry / Cancel) are per-job and go through the
 * corresponding mutations; the parent only needs to mount this
 * component, no wiring required.
 *
 * Polling policy
 * --------------
 * `useAutomationPolling` (via `useJobsList` + a local
 * `shouldPoll` flag) refreshes the list every 5s while ANY
 * visible job is `pending` / `running` / `retry_pending`. Once
 * every visible job is terminal, polling stops -- the list
 * remains static until the user changes a filter or explicitly
 * refreshes.
 *
 * The "any active" check is computed from the CURRENT accumulated
 * `entries` (see `MemoryListResult`-style accumulation below),
 * not from the raw page response. This matters because a "Load
 * more" that returns only terminal jobs should NOT keep
 * polling just because the FIRST page had active ones (and
 * vice versa).
 *
 * Accumulation semantics
 * ----------------------
 * Same pattern as `useMemoryList` (see `hooks/useMemoryList.ts`):
 * clicking "Load more" bumps `offset` by `limit`; the new page's
 * items are APPENDED to the accumulated list. When a filter
 * changes, `offset` resets to 0 and the accumulated list is
 * cleared. Duplicate `job_id` values (possible if a job's
 * status changed between pages) are de-duplicated on append.
 *
 * Why not `useInfiniteQuery`
 * --------------------------
 * Two reasons:
 *   1. The backend's `JobsListResponse` is already offset-based
 *      (`limit` + `offset` + `total`); TanStack Query's
 *      `useInfiniteQuery` expects a cursor-style `getNextPageParam`,
 *      which would need to be derived from `total` and
 *      `offset + jobs.length`. That works, but the extra
 *      indirection adds code without matching the actual API
 *      contract.
 *   2. The current Phase 3 UI needs only two views: the first
 *      page (default) and a manually-clicked "Load more".
 *      `useInfiniteQuery`'s automatic prefetch does not help
 *      here -- the user wants to see cost BEFORE pulling more
 *      rows (some deployments cap daily cost; a prefetch would
 *      be misleading).
 *
 * Empty / loading states
 * ----------------------
 *   * Loading (first page, no data yet): skeleton row.
 *   * Empty (loaded, zero jobs matching filters): EmptyState.
 *   * Loaded: table + "Load more" (only when `has_more`).
 */

'use client';

import { useEffect, useState } from 'react';
import {
  AlertCircle,
  ChevronDown,
  Loader2,
  RefreshCw,
} from 'lucide-react';

import { cn } from '../lib/cn';
import {
  useCancelJob,
  useRetryJob,
} from '../api/mutations';
import { useJobsList } from '../api/queries';
import { useAutomationPolling } from '../hooks/useAutomationPolling';
import {
  EXTRACTION_TRIGGERS,
  MEMORY_JOB_STATUSES,
  extractionTriggerLabel,
  jobStatusLabel,
  type ExtractionTrigger,
  type MemoryJob,
  type MemoryJobStatus,
} from '../types/jobs';
import { MemoryJobStatusCard } from './MemoryJobStatusCard';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Page size for the jobs list. Matches the backend's recommended default. */
const PAGE_SIZE = 25;

/** How often to poll while any visible job is active. */
const POLL_INTERVAL_MS = 5000;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface MemoryJobsListProps {
  /**
   * Optional initial chat_id filter. Useful when embedding the
   * list into a chat view (auto-filters to that chat).
   */
  initialChatId?: string | null;

  /**
   * Optional initial status filter. Useful when embedding the
   * list into a "Failed jobs" panel.
   */
  initialStatus?: MemoryJobStatus | null;

  /**
   * Extra classes for the outer container.
   */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryJobsList({
  initialChatId = null,
  initialStatus = null,
  className,
}: MemoryJobsListProps) {
  // Filter state -----------------------------------------------------------
  const [statusFilter, setStatusFilter] = useState<MemoryJobStatus | null>(
    initialStatus,
  );
  const [triggerFilter, setTriggerFilter] = useState<ExtractionTrigger | null>(
    null,
  );
  const [chatFilter, setChatFilter] = useState<string>(initialChatId ?? '');
  const [offset, setOffset] = useState(0);
  const [accumulated, setAccumulated] = useState<MemoryJob[]>([]);

  // Reset pagination and accumulated rows whenever a filter changes.
  useEffect(() => {
    setOffset(0);
    setAccumulated([]);
  }, [statusFilter, triggerFilter, chatFilter]);

  // Determine whether polling should be active based on the
  // jobs we have on screen. Uses the ACCUMULATED list, not the
  // raw page response -- see the module docstring for why.
  const shouldPoll = accumulated.some(
    (j) =>
      j.status === 'pending' ||
      j.status === 'running' ||
      j.status === 'retry_pending',
  );
  const polling = useAutomationPolling({
    shouldPoll,
    intervalMs: POLL_INTERVAL_MS,
  });

  // Main query -------------------------------------------------------------
  const listQuery = useJobsList(
    {
      status: statusFilter,
      chat_id: chatFilter.trim() || null,
      trigger_type: triggerFilter,
      limit: PAGE_SIZE,
      offset,
    },
    {
      // `staleTime` is overridden by polling's `refetchInterval`
      // when active; when inactive, 5s is the default (see
      // MEMORY_STALE_TIMES.jobs).
      select: undefined,
    },
  );

  // Accumulation -----------------------------------------------------------
  useEffect(() => {
    const page = listQuery.data;
    if (!page) return;
    // Guard against stale responses when the user changes a
    // filter mid-flight: only apply if the response's offset
    // matches the CURRENT offset (same pattern as useMemoryList).
    if (page.offset !== offset) return;

    if (offset === 0) {
      setAccumulated(page.jobs);
      return;
    }
    setAccumulated((prev) => {
      const seen = new Set(prev.map((j) => j.job_id));
      const fresh = page.jobs.filter((j) => !seen.has(j.job_id));
      return fresh.length === 0 ? prev : [...prev, ...fresh];
    });
  }, [listQuery.data, offset]);

  // Mutations -------------------------------------------------------------
  const retryMutation = useRetryJob();
  const cancelMutation = useCancelJob();

  // Handlers --------------------------------------------------------------
  const handleRetry = (job: MemoryJob) => {
    retryMutation.mutate({ jobId: job.job_id });
  };
  const handleCancel = (job: MemoryJob) => {
    cancelMutation.mutate({ jobId: job.job_id });
  };

  // Derived state ---------------------------------------------------------
  const total = listQuery.data?.total ?? 0;
  const hasMore = offset + (listQuery.data?.jobs.length ?? 0) < total;
  const isLoadingFirst = listQuery.isPending && offset === 0;

  // Render ----------------------------------------------------------------
  return (
    <div className={cn('space-y-3', className)}>
      {/* ---------- Filter bar ---------- */}
      <div className="flex flex-wrap items-center gap-2">
        <_FilterSelect
          value={statusFilter ?? ''}
          onChange={(v) =>
            setStatusFilter((v === '' ? null : v) as MemoryJobStatus | null)
          }
          placeholder="All statuses"
          options={[
            { value: '', label: 'All statuses' },
            ...MEMORY_JOB_STATUSES.map((s) => ({
              value: s,
              label: jobStatusLabel(s),
            })),
          ]}
        />

        <_FilterSelect
          value={triggerFilter ?? ''}
          onChange={(v) =>
            setTriggerFilter((v === '' ? null : v) as ExtractionTrigger | null)
          }
          placeholder="All triggers"
          options={[
            { value: '', label: 'All triggers' },
            ...EXTRACTION_TRIGGERS.map((t) => ({
              value: t,
              label: extractionTriggerLabel(t),
            })),
          ]}
        />

        <input
          type="text"
          value={chatFilter}
          onChange={(e) => setChatFilter(e.target.value)}
          placeholder="Filter by chat id…"
          className={cn(
            'flex-1 min-w-[140px] rounded-sm border border-border/60 bg-background',
            'px-2 py-1 text-[11px] text-foreground placeholder:text-muted-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          )}
        />

        <button
          type="button"
          onClick={() => listQuery.refetch()}
          disabled={listQuery.isFetching}
          title="Refresh"
          className={cn(
            'inline-flex items-center gap-1 rounded-sm border border-border/60',
            'bg-background px-2 py-1 text-[10px]',
            'hover:bg-muted/40 transition-colors',
            'disabled:opacity-50 disabled:cursor-not-allowed',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          )}
        >
          <RefreshCw
            size={10}
            aria-hidden="true"
            className={listQuery.isFetching ? 'animate-spin' : undefined}
          />
          Refresh
        </button>
      </div>

      {/* ---------- Content ---------- */}
      {isLoadingFirst ? (
        <_SkeletonList />
      ) : listQuery.isError ? (
        <div
          className={cn(
            'rounded-sm border border-red-500/20 bg-red-500/5 p-3',
            'text-[11px] text-red-700 dark:text-red-300',
          )}
        >
          <AlertCircle size={11} className="inline mr-1" aria-hidden="true" />
          Failed to load jobs: {listQuery.error?.message ?? 'unknown error'}
        </div>
      ) : accumulated.length === 0 ? (
        <div className="rounded-sm border border-dashed border-border/60 bg-muted/10 p-6 text-center">
          <p className="text-[11px] text-muted-foreground">
            No jobs match the current filters.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {accumulated.map((job) => (
            <MemoryJobStatusCard
              key={job.job_id}
              job={job}
              onRetry={handleRetry}
              onCancel={handleCancel}
              retryPending={
                retryMutation.isPending &&
                retryMutation.variables?.jobId === job.job_id
              }
              cancelPending={
                cancelMutation.isPending &&
                cancelMutation.variables?.jobId === job.job_id
              }
            />
          ))}

          {hasMore && (
            <button
              type="button"
              onClick={() => setOffset((o) => o + PAGE_SIZE)}
              disabled={listQuery.isFetching}
              className={cn(
                'w-full inline-flex items-center justify-center gap-1',
                'rounded-sm border border-border/60 bg-background',
                'px-3 py-1.5 text-[11px] text-muted-foreground',
                'hover:bg-muted/40 hover:text-foreground transition-colors',
                'disabled:opacity-50 disabled:cursor-not-allowed',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
              )}
            >
              {listQuery.isFetching ? (
                <Loader2 size={11} className="animate-spin" aria-hidden="true" />
              ) : (
                <ChevronDown size={11} aria-hidden="true" />
              )}
              Load more ({accumulated.length} / {total})
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function _FilterSelect({
  value,
  onChange,
  placeholder,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={placeholder}
      className={cn(
        'rounded-sm border border-border/60 bg-background',
        'px-2 py-1 text-[11px] text-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
      )}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function _SkeletonList() {
  return (
    <div className="space-y-2" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-16 animate-pulse rounded-sm bg-muted/40"
        />
      ))}
    </div>
  );
}
