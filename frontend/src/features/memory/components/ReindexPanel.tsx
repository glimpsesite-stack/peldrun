// src/features/memory/components/ReindexPanel.tsx

/**
 * Section D of the Phase 4 settings page -- the "Rebuild
 * Embedding Index" control plus a live coverage summary.
 *
 * Responsibilities
 * ----------------
 *
 *   1. **Status summary.** "12 of 15 memories have embeddings
 *      under this model." Plus pending count and last-reindex
 *      timestamp -- from `useEmbeddingStats`.
 *
 *   2. **Action.** A single "Rebuild Embedding Index" button,
 *      gated by `isEmbeddingConfigured` (both `enabled` AND
 *      `model_key` present). When gated, a reason banner
 *      tells the user exactly what is missing and (when the
 *      parent supplies `onRequestEnable`) offers a scroll-
 *      to-Section-A action.
 *
 *   3. **Confirmation.** An INLINE warning banner (not a
 *      modal) explains what will happen; Cancel/Confirm
 *      buttons dismiss or fire the mutation.
 *
 *   4. **Feedback.** Success banner shows the exact
 *      `enqueued` / `skipped` counts from the response, then
 *      auto-dismisses after 5 seconds. Error banner persists
 *      until the user retries or navigates away.
 *
 *   5. **Progress.** When `pending_reindex > 0`, polls the
 *      stats endpoint every 2 seconds so `with_embeddings`
 *      and `pending_reindex` stay close-to-live without the
 *      user having to refresh.
 *
 * Graceful degradation (plan §9)
 * ------------------------------
 * `useEmbeddingStats` returns `null` when the endpoint is
 * NOT IMPLEMENTED on this deployment (HTTP 404). In that case
 * the panel:
 *
 *   * Renders a small "stats unavailable" info banner
 *     INSTEAD of the coverage summary.
 *   * Still allows the reindex button to work -- the mutation
 *     is independent of the stats endpoint.
 *   * Shows the success counts from the MUTATION RESPONSE
 *     after a reindex, since the stats endpoint cannot
 *     confirm them.
 *
 * No polling is attempted in the degraded case (there is
 * nothing to poll).
 *
 * Error contract for `POST /api/memory/reindex`
 * ---------------------------------------------
 * Four failure branches surface as `MemoryApiError` (see
 * `useReindexEmbeddings`'s JSDoc in `mutations.ts` for the
 * full mapping). The panel renders ALL of them the same way:
 * an error banner with the backend-provided message
 * (already user-facing) and a "Go to settings" action when
 * `uxHintFor(error).suggestSettings` is true. The backend's
 * `message` field is preferred over hardcoded strings
 * because it distinguishes the four "config not ready"
 * sub-cases precisely (enable vs. select model vs. re-
 * select model).
 */

'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { Database, Loader2 } from 'lucide-react';

import type { MemoryEmbeddingSettings } from '../types/embedding';
import { isEmbeddingConfigured } from '../types/embedding';
import { useEmbeddingStats } from '../api/queries';
import { useReindexEmbeddings } from '../api/mutations';
import { memoryKeys } from '../api/keys';
import { uxHintFor } from '../types/errors';
import { MemoryBanner } from './MemoryBanner';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ReindexPanelProps {
  /**
   * Current embedding sub-tree. Read-only -- the panel does
   * NOT modify settings; it only reads `enabled` and
   * `model_key` to decide whether the button is ready.
   */
  embedding: MemoryEmbeddingSettings;

  /**
   * Section-level disabled flag from the parent. When true,
   * every interactive element in this panel is disabled
   * (e.g. because the overall memory mode forbids writes).
   * Distinct from the config-not-ready case: a disabled
   * section shows NO reason banner (the user did not cause
   * it) -- only the button is greyed out.
   */
  disabled?: boolean;

  /**
   * Optional scroll-to-Section-A handler, supplied by the
   * parent. Rendered as an action on the "select a model
   * first" reason banner and on `model_key_not_set` errors.
   */
  onRequestEnable?: () => void;
}

// ---------------------------------------------------------------------------
// Feedback state
// ---------------------------------------------------------------------------

/**
 * The two feedback messages the panel can display.
 *
 * `success` auto-dismisses after 5 seconds.
 * `error` persists until the user retries or clears it.
 */
type Feedback =
  | { type: 'success'; enqueued: number; skipped: number }
  | { type: 'error'; message: string };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ReindexPanel({
  embedding,
  disabled = false,
  onRequestEnable,
}: ReindexPanelProps) {
  const { t } = useTranslation('memory');
  const queryClient = useQueryClient();

  const [confirming, setConfirming] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  // ---- Fetch the coverage snapshot ------------------------------
  //
  // `data` can be three things after loading completes:
  //
  //   * `null`      -- endpoint returned 404 (plan §9 optional
  //                    endpoint not implemented).
  //   * an object   -- normal snapshot.
  //
  // `error` (non-404) also possible. See `useEmbeddingStats`.
  const statsQuery = useEmbeddingStats({
    enabled: !disabled,
  });
  const stats = statsQuery.data ?? null;

  // ---- Reindex mutation ----------------------------------------
  const reindexMutation = useReindexEmbeddings({
    onSuccess: (data) => {
      setConfirming(false);
      setFeedback({
        type: 'success',
        enqueued: data.enqueued,
        skipped: data.skipped,
      });
    },
    onError: (err) => {
      setConfirming(false);
      setFeedback({ type: 'error', message: err.message });
    },
  });

  // ---- Polling while jobs are pending --------------------------
  //
  // `pending_reindex > 0` means the worker has work queued. A
  // 2-second refetch interval is short enough to feel live and
  // long enough not to hammer the endpoint during a long
  // reindex. The effect cleans up when pending drops to 0 or
  // the component unmounts.
  //
  // We use `queryClient.invalidateQueries` (not a custom
  // `refetchInterval` on the hook) because `useEmbeddingStats`
  // does not expose that option -- see `queries.ts`'s
  // `HookOptions` interface for why the wrapper hooks keep a
  // narrow surface.
  const pendingCount = stats?.pending_reindex ?? 0;
  useEffect(() => {
    if (pendingCount <= 0) return;
    const id = setInterval(() => {
      void queryClient.invalidateQueries({
        queryKey: memoryKeys.embedding.stats(),
      });
    }, 2000);
    return () => clearInterval(id);
  }, [pendingCount, queryClient]);

  // ---- Auto-dismiss success feedback after 5s ------------------
  useEffect(() => {
    if (!feedback || feedback.type !== 'success') return;
    const id = setTimeout(() => setFeedback(null), 5000);
    return () => clearTimeout(id);
  }, [feedback]);

  // ---- Readiness & disabled reason -----------------------------
  const ready = isEmbeddingConfigured(embedding);

  /**
   * Why is the button disabled? `null` when the button is
   * ACTUALLY usable (config wise). Distinct from `disabled`
   * (section-level) -- that one is enforced separately so the
   * user does not see a reason banner for a state they did
   * not cause.
   */
  const disabledReason: string | null = !embedding.enabled
    ? t('settings.embedding.reindex.disabledNotEnabled')
    : !embedding.model_key
      ? t('settings.embedding.reindex.disabledNoModel')
      : null;

  const isButtonDisabled =
    disabled || !ready || reindexMutation.isPending;

  // ---- Date formatting -----------------------------------------
  //
  // The backend stores `last_reindex_at` as Unix epoch SECONDS
  // (int or float, treated as seconds by the client). We
  // multiply by 1000 for JS's Date constructor.
  function formatDate(epochSeconds: number): string {
    const d = new Date(epochSeconds * 1000);
    return d.toLocaleString(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }

  // ---- Handlers ------------------------------------------------
  function handleStartConfirm() {
    setConfirming(true);
    setFeedback(null); // Clear any stale error when the user retries.
  }

  function handleCancelConfirm() {
    setConfirming(false);
  }

  function handleConfirm() {
    reindexMutation.mutate({});
  }

  // ---- Render --------------------------------------------------
  return (
    <div className="space-y-3">
      {/* ============ Status summary ============ */}
      {statsQuery.isLoading ? (
        <div
          className="h-10 animate-pulse rounded-sm bg-muted/40"
          aria-busy="true"
        />
      ) : statsQuery.isError ? (
        // Non-404 error from the stats endpoint -- unexpected.
        // Show it, but the reindex button below still works
        // (the mutation is independent of stats).
        <MemoryBanner
          tone="error"
          message={
            statsQuery.error instanceof Error
              ? statsQuery.error.message
              : t('errors.generic')
          }
          action={
            <button
              type="button"
              onClick={() => void statsQuery.refetch()}
              className="text-[11px] font-medium underline underline-offset-2"
            >
              {t('common.retry')}
            </button>
          }
        />
      ) : stats === null ? (
        // 404 -- the optional stats endpoint is not deployed.
        // Show a small info line, keep the button working.
        <MemoryBanner
          tone="info"
          message={t('settings.embedding.reindex.statsUnavailable')}
        />
      ) : (
        // Normal path -- we have a coverage snapshot.
        <div className="space-y-1">
          <p className="text-[11px] text-foreground">
            {t('settings.embedding.reindex.summary', {
              withEmbeddings: stats.with_embeddings,
              total: stats.total_memories,
            })}
          </p>
          {stats.pending_reindex > 0 && (
            <p className="text-[10px] text-amber-600 dark:text-amber-400">
              {t('settings.embedding.reindex.pending', {
                count: stats.pending_reindex,
              })}
            </p>
          )}
          <p className="text-[10px] text-muted-foreground">
            {stats.last_reindex_at !== null
              ? t('settings.embedding.reindex.lastReindex', {
                  date: formatDate(stats.last_reindex_at),
                })
              : t('settings.embedding.reindex.never')}
          </p>
        </div>
      )}

      {/* ============ Disabled reason (config not ready) ============ */}
      {disabledReason && (
        <MemoryBanner
          tone="info"
          message={disabledReason}
          action={
            onRequestEnable && !embedding.enabled ? (
              <button
                type="button"
                onClick={onRequestEnable}
                className="text-[11px] font-medium underline underline-offset-2 whitespace-nowrap"
              >
                {t('settings.embedding.enabled')}
              </button>
            ) : undefined
          }
        />
      )}

      {/* ============ Feedback (success / error) ============ */}
      {feedback?.type === 'success' && (
        <MemoryBanner
          tone="success"
          message={t('settings.embedding.reindex.success', {
            enqueued: feedback.enqueued,
            skipped: feedback.skipped,
          })}
        />
      )}
      {feedback?.type === 'error' && (
        <MemoryBanner
          tone="error"
          title={t('settings.embedding.reindex.errorPrefix')}
          message={feedback.message}
          action={
            onRequestEnable &&
            reindexMutation.error &&
            uxHintFor(reindexMutation.error).suggestSettings ? (
              <button
                type="button"
                onClick={onRequestEnable}
                className="text-[11px] font-medium underline underline-offset-2 whitespace-nowrap"
              >
                {t('errors.withSettings')}
              </button>
            ) : undefined
          }
        />
      )}

      {/* ============ Action ============ */}
      {!confirming ? (
        <button
          type="button"
          onClick={handleStartConfirm}
          disabled={isButtonDisabled}
          className={cn(
            'inline-flex items-center gap-2 rounded-sm border border-border/60 bg-card',
            'px-3 py-2 text-xs font-medium text-foreground',
            'transition-colors',
            'hover:border-primary/40 hover:bg-muted/40',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
            isButtonDisabled &&
              'cursor-not-allowed opacity-60 hover:border-border/60 hover:bg-card',
          )}
        >
          {reindexMutation.isPending ? (
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          ) : (
            <Database size={12} aria-hidden="true" />
          )}
          <span>{t('settings.embedding.reindex.button')}</span>
        </button>
      ) : (
        <MemoryBanner
          tone="warning"
          title={t('settings.embedding.reindex.confirmTitle')}
          message={t('settings.embedding.reindex.confirmMessage')}
          action={
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleCancelConfirm}
                disabled={reindexMutation.isPending}
                className="rounded-sm border border-border/60 bg-transparent px-2 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-muted/40 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                onClick={handleConfirm}
                disabled={reindexMutation.isPending}
                className="inline-flex items-center gap-1 rounded-sm border border-primary/60 bg-primary/10 px-2 py-1 text-[11px] font-semibold text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {reindexMutation.isPending && (
                  <Loader2
                    size={10}
                    className="animate-spin"
                    aria-hidden="true"
                  />
                )}
                <span>
                  {t('settings.embedding.reindex.confirmAction')}
                </span>
              </button>
            </div>
          }
        />
      )}
    </div>
  );
}
