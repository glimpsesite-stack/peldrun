// src/features/memory/components/MemoryRetrievalSettingsForm.tsx

/**
 * Retrieval strategy settings form -- self-contained, with its
 * own draft state, dirty tracking, and sticky save bar.
 *
 * Covers every field on `MemoryRetrievalSettings`:
 *
 *   * Strategy:   mode (lexical / vector / hybrid / automatic)
 *   * Fusion:     vector_alpha, min_lexical_score,
 *                 min_vector_similarity, rrf_k
 *   * Rerank:     rerank_enabled, rerank_max_candidates,
 *                 rerank_timeout_seconds, rerank_batch_size
 *
 * Mirrors the pattern established by `AISettingsForm`: it owns
 * the full lifecycle (fetch, draft, diff, save) so the parent
 * page needs only `<MemoryRetrievalSettingsForm />`.
 *
 * Only changed leaf fields are PATCHed. Editing
 * `rerank_max_candidates` alone sends `{retrieval: {rerank_max_candidates: N}}`,
 * not the entire retrieval subtree.
 *
 * Failure isolation: a save error surfaces inline; the rest of
 * the settings page stays functional. No toast spawned.
 */

'use client';

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Loader2,
  RotateCcw,
  Save,
  Sparkles,
  X as XIcon,
  Zap,
} from 'lucide-react';

import type { MemoryRetrievalSettings } from '../types/embedding';
import { DEFAULT_MEMORY_RETRIEVAL_SETTINGS } from '../types/embedding';

import { useMemorySettings } from '../api/queries';
import { usePatchSettings } from '../api/mutations';
import { MemorySection } from './MemorySection';
import { MemoryField, MEMORY_FIELD_INPUT_CLASS } from './MemoryField';
import { MemoryBanner } from './MemoryBanner';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Bounds -- mirror backend Pydantic validators exactly
// ---------------------------------------------------------------------------

const BOUNDS = {
  vector_alpha: { min: 0.0, max: 1.0, step: 0.05 },
  min_lexical_score: { min: 0.0, max: 1.0, step: 0.05 },
  min_vector_similarity: { min: 0.0, max: 1.0, step: 0.05 },
  rrf_k: { min: 1, max: 100, step: 1 },
  rerank_max_candidates: { min: 1, max: 100, step: 1 },
  rerank_timeout_seconds: { min: 1.0, max: 30.0, step: 0.5 },
  rerank_batch_size: { min: 1, max: 20, step: 1 },
} as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryRetrievalSettingsForm() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();
  const patchSettings = usePatchSettings();

  const serverRetrieval = settingsQuery.data?.retrieval as
    | MemoryRetrievalSettings
    | undefined;

  const [draft, setDraft] = useState<MemoryRetrievalSettings | null>(
    null,
  );
  const [saveSucceeded, setSaveSucceeded] = useState(false);

  // ---- Hydrate draft from server ------------------------------
  useEffect(() => {
    if (serverRetrieval && (draft === null || saveSucceeded)) {
      // Merge the server response with our frontend defaults.
      // This ensures that if the backend hasn't been patched yet
      // to return the Phase 5A fields, the UI still renders the
      // default values instead of empty inputs.
      const hydrated: MemoryRetrievalSettings = {
        ...DEFAULT_MEMORY_RETRIEVAL_SETTINGS,
        ...serverRetrieval,
      };
      setDraft(deepClone(hydrated));
      setSaveSucceeded(false);
    }
  }, [serverRetrieval, draft, saveSucceeded]);

  // ---- Diff: only changed leaf fields -------------------------
  const diff = useMemo<Partial<MemoryRetrievalSettings> | null>(() => {
    if (!draft || !serverRetrieval) return null;
    const d = computeDiff(serverRetrieval, draft);
    return Object.keys(d).length > 0 ? d : null;
  }, [draft, serverRetrieval]);

  const isDirty = diff !== null;
  const isSaving = patchSettings.isPending;

  // ---- Actions ------------------------------------------------
  async function handleSave() {
    if (!diff) return;
    try {
      await patchSettings.mutateAsync({
        retrieval: diff,
      } as Parameters<typeof patchSettings.mutate>[0]);
      setSaveSucceeded(true);
    } catch {
      // surfacing via patchSettings.error below
    }
  }

  function handleReset() {
    if (serverRetrieval) setDraft(deepClone(serverRetrieval));
  }

  function update<K extends keyof MemoryRetrievalSettings>(
    key: K,
    value: MemoryRetrievalSettings[K],
  ) {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  // ---- Loading state ------------------------------------------
  if (!draft || !serverRetrieval) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 size={12} className="animate-spin" />
          <span className="text-[11px]">
            {t('settings.retrieval.loading', 'Loading retrieval settings...')}
          </span>
        </div>
      </div>
    );
  }

  const showFusionTuning =
    draft.mode === 'hybrid' || draft.mode === 'automatic';

  // ---- Render -------------------------------------------------
  return (
    <div className="space-y-4">
      <MemorySection
        icon={Sparkles}
        title={t('settings.retrieval.title', 'Retrieval Strategy')}
        subtitle={t(
          'settings.retrieval.subtitle',
          'How the memory subsystem searches: lexical, semantic, or fused',
        )}
        accent="cyan"
      >
        <div className="space-y-4">
          {/* ---------- Strategy ---------- */}
          <MemoryField
            label={t('settings.retrieval.mode', 'Search mode')}
            help={t(
              'settings.retrieval.modeHelp',
              'lexical = FTS only; vector = embeddings only; hybrid = both + RRF; automatic = short queries use lexical, long use hybrid.',
            )}
          >
            <select
              value={draft.mode}
              onChange={(e) =>
                update(
                  'mode',
                  e.target.value as MemoryRetrievalSettings['mode'],
                )
              }
              className={MEMORY_FIELD_INPUT_CLASS}
            >
              <option value="lexical">
                {t('settings.retrieval.modeOptions.lexical', 'Lexical (BM25 only)')}
              </option>
              <option value="vector">
                {t('settings.retrieval.modeOptions.vector', 'Vector (cosine similarity only)')}
              </option>
              <option value="hybrid">
                {t('settings.retrieval.modeOptions.hybrid', 'Hybrid (FTS + Vector, fused via RRF)')}
              </option>
              <option value="automatic">
                {t('settings.retrieval.modeOptions.automatic', 'Automatic (adaptive by query length)')}
              </option>
            </select>
          </MemoryField>

          {draft.mode === 'lexical' && (
            <MemoryBanner
              tone="info"
              message={t(
                'settings.retrieval.lexicalHint',
                'Lexical mode does not use embeddings or reranking. Switch to hybrid or vector to enable semantic recall.',
              )}
            />
          )}

          {/* ---------- Fusion tuning ---------- */}
          {showFusionTuning && (
            <div className="rounded-md border border-border/60 p-3 space-y-3">
              <h4 className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
                {t('settings.retrieval.fusionTitle', 'Fusion Tuning')}
              </h4>

              <MemoryField
                label={t('settings.retrieval.vectorAlpha', 'Vector weight (α)')}
                help={t(
                  'settings.retrieval.vectorAlphaHelp',
                  '0.0 = pure lexical, 1.0 = pure vector. 0.5 = balanced. Only consulted when mode is hybrid or automatic.',
                )}
              >
                <div className="flex items-center gap-2">
                  <input
                    type="range"
                    value={draft.vector_alpha}
                    min={BOUNDS.vector_alpha.min}
                    max={BOUNDS.vector_alpha.max}
                    step={BOUNDS.vector_alpha.step}
                    onChange={(e) =>
                      update('vector_alpha', Number(e.target.value))
                    }
                    className="flex-1 accent-primary"
                  />
                  <span className="text-[11px] font-mono tabular-nums min-w-[3rem] text-end">
                    {draft.vector_alpha.toFixed(2)}
                  </span>
                </div>
              </MemoryField>

              <div className="grid grid-cols-2 gap-2">
                <MemoryField
                  label={t('settings.retrieval.rrfK', 'RRF k')}
                  help={t(
                    'settings.retrieval.rrfKHelp',
                    'Smoothing constant for Reciprocal Rank Fusion. 60 is standard.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.rrf_k}
                    min={BOUNDS.rrf_k.min}
                    max={BOUNDS.rrf_k.max}
                    step={BOUNDS.rrf_k.step}
                    onChange={(e) =>
                      update('rrf_k', Number(e.target.value))
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
                <MemoryField
                  label={t(
                    'settings.retrieval.minLexical',
                    'Min lexical score',
                  )}
                  help={t(
                    'settings.retrieval.minLexicalHelp',
                    'Below this normalized BM25 threshold, hits are dropped before fusion.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.min_lexical_score}
                    min={BOUNDS.min_lexical_score.min}
                    max={BOUNDS.min_lexical_score.max}
                    step={BOUNDS.min_lexical_score.step}
                    onChange={(e) =>
                      update('min_lexical_score', Number(e.target.value))
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
                <MemoryField
                  label={t(
                    'settings.retrieval.minVector',
                    'Min vector similarity',
                  )}
                  help={t(
                    'settings.retrieval.minVectorHelp',
                    'Below this cosine similarity, hits are dropped before fusion.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.min_vector_similarity}
                    min={BOUNDS.min_vector_similarity.min}
                    max={BOUNDS.min_vector_similarity.max}
                    step={BOUNDS.min_vector_similarity.step}
                    onChange={(e) =>
                      update(
                        'min_vector_similarity',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
              </div>
            </div>
          )}

          {/* ---------- Reranking (Phase 5A) ---------- */}
          <div
            className={cn(
              'rounded-md border p-3 space-y-3',
              draft.rerank_enabled
                ? 'border-emerald-300/40 dark:border-emerald-700/30'
                : 'border-border/60',
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <Zap
                  size={12}
                  className={cn(
                    draft.rerank_enabled
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : 'text-muted-foreground',
                  )}
                />
                <h4 className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
                  {t(
                    'settings.retrieval.rerankTitle',
                    'LLM Reranking',
                  )}
                </h4>
                <span className="text-[9px] font-medium text-muted-foreground rounded-sm bg-muted px-1 py-0.5 uppercase tracking-wider">
                  {t('common.phase5', 'Phase 5')}
                </span>
              </div>
            </div>

            <label
              className={cn(
                'inline-flex items-center gap-2 text-xs cursor-pointer',
              )}
            >
              <input
                type="checkbox"
                checked={draft.rerank_enabled}
                onChange={(e) =>
                  update('rerank_enabled', e.target.checked)
                }
                disabled={draft.mode === 'lexical'}
                className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
              />
              <span className="font-medium text-foreground">
                {t(
                  'settings.retrieval.rerankEnabled',
                  'Enable LLM reranking',
                )}
              </span>
            </label>

            <p className="text-[10px] leading-snug text-muted-foreground">
              {t(
                'settings.retrieval.rerankHelp',
                'When on, the fused top-K candidates are re-scored by the extraction model for query relevance. Requires a model in Automation settings. Any failure (timeout, unreachable) silently falls back to RRF ordering.',
              )}
            </p>

            {draft.mode === 'lexical' && draft.rerank_enabled && (
              <MemoryBanner
                tone="warning"
                message={t(
                  'settings.retrieval.rerankLexicalConflict',
                  'Reranking only runs in hybrid or vector modes. Switch mode above to activate.',
                )}
              />
            )}

            {draft.rerank_enabled && draft.mode !== 'lexical' && (
              <div className="grid grid-cols-3 gap-2">
                <MemoryField
                  label={t(
                    'settings.retrieval.rerankMaxCandidates',
                    'Max candidates',
                  )}
                  help={t(
                    'settings.retrieval.rerankMaxCandidatesHelp',
                    'Top-K sent to the LLM. Remaining candidates keep RRF order.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.rerank_max_candidates}
                    min={BOUNDS.rerank_max_candidates.min}
                    max={BOUNDS.rerank_max_candidates.max}
                    step={BOUNDS.rerank_max_candidates.step}
                    onChange={(e) =>
                      update(
                        'rerank_max_candidates',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
                <MemoryField
                  label={t(
                    'settings.retrieval.rerankTimeout',
                    'Timeout (s)',
                  )}
                  help={t(
                    'settings.retrieval.rerankTimeoutHelp',
                    'Per-batch. 3s is healthy latency + headroom. Do not exceed 10s.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.rerank_timeout_seconds}
                    min={BOUNDS.rerank_timeout_seconds.min}
                    max={BOUNDS.rerank_timeout_seconds.max}
                    step={BOUNDS.rerank_timeout_seconds.step}
                    onChange={(e) =>
                      update(
                        'rerank_timeout_seconds',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
                <MemoryField
                  label={t(
                    'settings.retrieval.rerankBatchSize',
                    'Batch size',
                  )}
                  help={t(
                    'settings.retrieval.rerankBatchSizeHelp',
                    'Candidates per LLM call. Set = max_candidates for single-call.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.rerank_batch_size}
                    min={BOUNDS.rerank_batch_size.min}
                    max={BOUNDS.rerank_batch_size.max}
                    step={BOUNDS.rerank_batch_size.step}
                    onChange={(e) =>
                      update(
                        'rerank_batch_size',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
              </div>
            )}
          </div>
        </div>
      </MemorySection>

      {/* ---------- Error surface ---------- */}
      {patchSettings.error && !isSaving && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px] text-destructive">
          <XIcon size={14} className="mt-0.5 shrink-0" />
          <div>
            <strong className="font-semibold">
              {t('settings.retrieval.saveFailed', 'Save failed')}
            </strong>
            <p className="mt-0.5">{patchSettings.error.message}</p>
          </div>
        </div>
      )}

      {/* ---------- Sticky save bar ---------- */}
      <div className="sticky bottom-4 z-10 flex items-center justify-between gap-2 rounded-md border border-border bg-card/95 px-3 py-2 shadow-lg backdrop-blur">
        <span className="text-[10px] text-muted-foreground">
          {isDirty
            ? t(
                'settings.retrieval.dirtyHint',
                'You have unsaved changes',
              )
            : t(
                'settings.retrieval.cleanHint',
                'No unsaved changes',
              )}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReset}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm border border-border bg-card px-2.5 text-[11px] font-medium text-foreground transition-colors',
              'hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            <RotateCcw size={11} />
            {t('settings.retrieval.reset', 'Reset')}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm bg-primary px-2.5 text-[11px] font-medium text-primary-foreground transition-colors',
              'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {isSaving ? (
              <>
                <Loader2 size={11} className="animate-spin" />
                {t('settings.retrieval.saving', 'Saving...')}
              </>
            ) : (
              <>
                <Save size={11} />
                {t('settings.retrieval.save', 'Save changes')}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function deepClone<T>(v: T): T {
  if (v === null || typeof v !== 'object') return v;
  if (Array.isArray(v)) return v.map(deepClone) as T;
  const out: Record<string, unknown> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    out[k] = deepClone(val);
  }
  return out as T;
}

function computeDiff(
  base: unknown,
  draft: unknown,
): Record<string, unknown> {
  if (base === draft) return {};
  if (
    base === null ||
    draft === null ||
    typeof base !== 'object' ||
    typeof draft !== 'object' ||
    Array.isArray(base) ||
    Array.isArray(draft)
  ) {
    return { __scalar: draft };
  }
  const diff: Record<string, unknown> = {};
  const keys = new Set([
    ...Object.keys(base as object),
    ...Object.keys(draft as object),
  ]);
  for (const key of keys) {
    const b = (base as Record<string, unknown>)[key];
    const d = (draft as Record<string, unknown>)[key];
    const sub = computeDiff(b, d);
    if (Object.keys(sub).length === 0) continue;
    if (Object.keys(sub).length === 1 && '__scalar' in sub) {
      diff[key] = sub.__scalar;
    } else {
      diff[key] = sub;
    }
  }
  return diff;
}
