// src/app/memory/automation/batch/page.tsx

/**
 * Batch extraction page (`/memory/automation/batch`).
 *
 * Three vertically stacked sections:
 *
 *   1. **Start new batch** -- shows the current estimate (sessions
 *      to scan, jobs to create, tokens, cost) and a "Start Batch"
 *      button. Hitting Start creates the batch synchronously; the
 *      button shows a spinner for the duration.
 *
 *   2. **Recent batches** -- a compact list of every batch the
 *      operator has ever started, newest first. Clicking a row
 *      sets it as the SELECTED batch below.
 *
 *   3. **Selected batch detail** -- renders `<BatchProgressPanel>`
 *      for the selected batch, which self-polls while the batch
 *      is active. Defaults to the most recent batch on first load.
 *
 * Why three sections instead of one big list
 * ------------------------------------------
 *   * The "start new batch" panel needs the estimate query, which
 *     is unrelated to any batch row (it's a projection over the
 *     workspace state, not a batch).
 *   * The "recent batches" list uses pagination and filters that
 *     do not apply to a single batch's detail panel.
 *   * The detail panel polls at a different cadence (3s) than the
 *     list (5s). Separating them keeps each query's lifecycle
 *     independent and cache-able.
 */

'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import Link from 'next/link';
import {
  AlertCircle,
  ArrowRight,
  Boxes,
  ChevronRight,
  ListChecks,
  Loader2,
  Play,
  Sparkles,
} from 'lucide-react';

import { cn } from '../../../../features/memory/lib/cn';
import { BatchProgressPanel } from '../../../../features/memory/components/BatchProgressPanel';
import { MemorySection } from '../../../../features/memory/components/MemorySection';
import {
  useBatchEstimate,
  useBatchesList,
} from '../../../../features/memory/api/queries';
import { useCreateBatch } from '../../../../features/memory/api/mutations';
import { useAIEnabled } from '../../../../features/memory/hooks/useAIEnabled';
import {
  batchStatusLabel,
  nanoUsdToUsd,
} from '../../../../features/memory/types/batch';
import type { MemoryBatch } from '../../../../features/memory/types/batch';

export default function BatchExtractionPage() {
  const { t } = useTranslation('memory');

  // Which batch is shown in the detail panel. `null` on first
  // load; a `useEffect` below auto-selects the newest batch
  // once the list resolves.
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(
    null,
  );

  const ai = useAIEnabled();

  const estimateQuery = useBatchEstimate({ enabled: ai.isAvailable });
  const batchesQuery = useBatchesList({
    status: null,
    limit: 50,
    offset: 0,
  });
  const createMutation = useCreateBatch({
    onSuccess: (data) => {
      // Auto-select the batch we just created so the user sees
      // its progress immediately without a second click.
      setSelectedBatchId(data.batch.batch_id);
    },
  });

  // Auto-select the newest batch on first successful load.
  useEffect(() => {
    if (selectedBatchId !== null) return;
    const firstBatch = batchesQuery.data?.batches[0];
    if (firstBatch) {
      setSelectedBatchId(firstBatch.batch_id);
    }
  }, [batchesQuery.data, selectedBatchId]);

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('batch.title', 'Batch Extraction')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t(
            'batch.description',
            'Run retroactive extraction across every pending session.',
          )}
        </p>
      </div>

      {/* Availability banner */}
      {!ai.isLoading && !ai.isAvailable && (
        <_UnavailableBanner
          reason={
            !ai.isEnabled
              ? 'AI extraction is disabled in settings.'
              : 'No extraction model is configured.'
          }
        />
      )}

      {/* Create section */}
      <MemorySection
        icon={Sparkles}
        title={t('batch.createSection', 'Start a New Batch')}
        subtitle={t(
          'batch.createSubtitle',
          'The estimate below covers every session that currently has pending turns.',
        )}
        delay={0.1}
      >
        <_CreatePanel
          estimateQuery={estimateQuery}
          createMutation={createMutation}
          aiEnabled={ai.isAvailable}
          aiLoading={ai.isLoading}
        />
      </MemorySection>

      {/* Recent batches */}
      <MemorySection
        icon={Boxes}
        title={t('batch.listSection', 'Recent Batches')}
        subtitle={t(
          'batch.listSubtitle',
          'Every batch run, newest first.',
        )}
        delay={0.2}
      >
        <_BatchesList
          query={batchesQuery}
          selectedBatchId={selectedBatchId}
          onSelect={setSelectedBatchId}
        />
      </MemorySection>

      {/* Selected batch detail */}
      {selectedBatchId && (
        <MemorySection
          icon={ListChecks}
          title={t('batch.detailSection', 'Batch Progress')}
          subtitle={t(
            'batch.detailSubtitle',
            'Live progress — updates automatically while the batch is active.',
          )}
          delay={0.3}
        >
          <BatchProgressPanel batchId={selectedBatchId} />
        </MemorySection>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function _UnavailableBanner({ reason }: { reason: string }) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-sm border border-amber-500/30 bg-amber-500/5',
        'px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300',
      )}
    >
      <AlertCircle size={12} className="shrink-0 mt-0.5" aria-hidden="true" />
      <div className="flex-1">
        <div className="font-medium">{reason}</div>
        <Link
          href="/memory/settings"
          className="inline-flex items-center gap-0.5 underline underline-offset-2"
        >
          Configure AI extraction
          <ArrowRight size={10} aria-hidden="true" />
        </Link>
      </div>
    </div>
  );
}

function _CreatePanel({
  estimateQuery,
  createMutation,
  aiEnabled,
  aiLoading,
}: {
  estimateQuery: ReturnType<typeof useBatchEstimate>;
  createMutation: ReturnType<typeof useCreateBatch>;
  aiEnabled: boolean;
  aiLoading: boolean;
}) {
  const estimate = estimateQuery.data?.estimate;
  const isEstimateLoading = estimateQuery.isPending;
  const isEmpty = estimate && estimate.session_count === 0;

  return (
    <div className="space-y-3">
      {/* Estimate grid */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <_EstimateCell
          label="Sessions"
          value={estimate?.session_count ?? '—'}
          loading={isEstimateLoading}
        />
        <_EstimateCell
          label="Jobs"
          value={estimate?.estimated_jobs ?? '—'}
          loading={isEstimateLoading}
        />
        <_EstimateCell
          label="Turns"
          value={estimate?.total_pending_turns ?? '—'}
          loading={isEstimateLoading}
        />
        <_EstimateCell
          label="Tokens"
          value={estimate?.estimated_tokens ?? '—'}
          loading={isEstimateLoading}
        />
        <_EstimateCell
          label="Est. cost"
          value={
            estimate
              ? `$${nanoUsdToUsd(estimate.estimated_cost_nano_usd).toFixed(4)}`
              : '—'
          }
          loading={isEstimateLoading}
        />
      </div>

      {/* Action row */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => createMutation.mutate({})}
          disabled={
            !aiEnabled ||
            isEmpty ||
            isEstimateLoading ||
            createMutation.isPending
          }
          className={cn(
            'inline-flex items-center gap-1.5 rounded-sm border border-border/60',
            'bg-primary text-primary-foreground px-3 py-1.5 text-[11px] font-medium',
            'hover:bg-primary/90 transition-colors',
            'disabled:opacity-50 disabled:cursor-not-allowed',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          )}
        >
          {createMutation.isPending ? (
            <Loader2 size={11} className="animate-spin" aria-hidden="true" />
          ) : (
            <Play size={11} aria-hidden="true" />
          )}
          {createMutation.isPending ? 'Starting…' : 'Start Batch'}
        </button>

        {isEmpty && (
          <span className="text-[10px] text-muted-foreground">
            Nothing to extract — every session is already processed.
          </span>
        )}
        {aiLoading && (
          <span className="text-[10px] text-muted-foreground">
            Checking AI availability…
          </span>
        )}

        {createMutation.isError && (
          <span className="text-[10px] text-red-600 dark:text-red-400">
            {createMutation.error?.message ?? 'Failed to start batch'}
          </span>
        )}
      </div>
    </div>
  );
}

function _EstimateCell({
  label,
  value,
  loading,
}: {
  label: string;
  value: string | number;
  loading: boolean;
}) {
  return (
    <div className="rounded-sm border border-border/40 bg-muted/20 px-2 py-1.5">
      <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="font-mono text-[11px] font-semibold text-foreground">
        {loading ? (
          <span className="inline-block h-3 w-8 animate-pulse bg-muted rounded" />
        ) : (
          value
        )}
      </div>
    </div>
  );
}

function _BatchesList({
  query,
  selectedBatchId,
  onSelect,
}: {
  query: ReturnType<typeof useBatchesList>;
  selectedBatchId: string | null;
  onSelect: (id: string) => void;
}) {
  if (query.isPending) {
    return (
      <div className="space-y-2" aria-busy="true">
        {[0, 1].map((i) => (
          <div
            key={i}
            className="h-10 animate-pulse rounded-sm bg-muted/40"
          />
        ))}
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="rounded-sm border border-red-500/20 bg-red-500/5 p-3 text-[11px] text-red-700 dark:text-red-300">
        <AlertCircle size={11} className="inline mr-1" aria-hidden="true" />
        Failed to load batches: {query.error?.message ?? 'unknown error'}
      </div>
    );
  }

  const batches = query.data?.batches ?? [];
  if (batches.length === 0) {
    return (
      <div className="rounded-sm border border-dashed border-border/60 bg-muted/10 p-6 text-center">
        <p className="text-[11px] text-muted-foreground">
          No batches yet. Start one above to see it here.
        </p>
      </div>
    );
  }

  return (
    <ul className="space-y-1.5">
      {batches.map((batch) => (
        <li key={batch.batch_id}>
          <_BatchRow
            batch={batch}
            selected={batch.batch_id === selectedBatchId}
            onSelect={() => onSelect(batch.batch_id)}
          />
        </li>
      ))}
    </ul>
  );
}

function _BatchRow({
  batch,
  selected,
  onSelect,
}: {
  batch: MemoryBatch;
  selected: boolean;
  onSelect: () => void;
}) {
  const created = new Date(batch.created_at * 1000);
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        'w-full flex items-center justify-between gap-2 rounded-sm border px-3 py-2 text-left text-[11px] transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
        selected
          ? 'border-primary/60 bg-primary/5'
          : 'border-border/60 bg-card hover:bg-muted/40',
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[10px] text-foreground truncate">
          {batch.batch_id}
        </div>
        <div className="text-[10px] text-muted-foreground">
          {batchStatusLabel(batch.status)} • {batch.job_count} jobs •{' '}
          {created.toLocaleString()}
        </div>
      </div>
      <ChevronRight
        size={12}
        aria-hidden="true"
        className={cn(
          'shrink-0 transition-transform',
          selected ? 'rotate-90 text-primary' : 'text-muted-foreground',
        )}
      />
    </button>
  );
}
