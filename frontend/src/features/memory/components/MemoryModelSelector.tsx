// src/features/memory/components/MemoryModelSelector.tsx

/**
 * Dropdown for selecting an LLM model for a memory task
 * (extraction or consolidation).
 *
 * The list is NOT free-form: it shows ONLY the models the user
 * has marked as "memory" in Settings -> Models (see
 * `configClient.getMemoryModels`). This is deliberate -- the
 * memory subsystem must never use a model the user has not
 * explicitly authorized for memory work. Attempting to pick a
 * model from the general catalog would either fail at runtime
 * (no vault credentials) or silently leak conversation content
 * to a provider the user did not choose.
 *
 * Empty-state contract
 * --------------------
 * When the list is empty (no model marked), the component
 * renders a `MemoryBanner` with a deep link to
 * `/settings/setup?tab=models`. Showing an empty `<select>`
 * with a lone "— Select —" option would look like a UI bug; the
 * banner makes the required action explicit.
 *
 * When the CURRENT selection is no longer in the list (e.g. the
 * user unmarked the model after configuring it), the component
 * shows an empty `<select>` value and a warning. The persisted
 * `provider_id`/`model_id` are NOT cleared automatically --
 * clearing them is the user's decision (via "Clear selection"
 * in the parent form). This avoids silently undoing
 * configuration when a user temporarily unmarks a model.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import {
  configClient,
  type MemoryModelOption,
} from '../api/config-client';
import { cn } from '../lib/cn';
import { formatTokens } from '../lib/format';
import {
  MEMORY_FIELD_INPUT_CLASS,
  MemoryField,
} from './MemoryField';
import { MemoryBanner } from './MemoryBanner';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface MemoryModelSelectorProps {
  /** Field label (rendered uppercase via MemoryField). */
  label: string;
  /** Optional help text below the input. */
  help?: string;
  /** Current provider id; `null` means "not configured". */
  providerId: string | null;
  /** Current model id; `null` means "not configured". */
  modelId: string | null;
  /**
   * Called when the user picks a model (or clears the selection).
   *
   * The third argument carries the FULL model option object so
   * the parent form can pre-fill limits (max_output_tokens,
   * context_window) without a second lookup. `null` when the
   * selection is cleared.
   */
  onChange: (
    providerId: string | null,
    modelId: string | null,
    info: MemoryModelOption | null,
  ) => void;
  /** When true, disables the select and dims the card. */
  disabled?: boolean;
  /** Validation error to display instead of `help`. */
  error?: string;
  /**
   * When `true`, lists ONLY models the metadata flagged as
   * embeddings. When `false` (default), lists ONLY non-embedding
   * (chat-capable) models.
   *
   * Extraction and consolidation require chat models -- an
   * embedding model cannot serve `/chat/completions`. Phase 4's
   * semantic-search settings will use `embeddingOnly: true`.
   */
  embeddingOnly?: boolean;
}

export function MemoryModelSelector({
  label,
  help,
  providerId,
  modelId,
  onChange,
  disabled = false,
  error,
  embeddingOnly = false,
}: MemoryModelSelectorProps) {
  const { t } = useTranslation('memory');

  // Fetch the list of marked memory models.
  //
  // `staleTime: 30_000` -- the list only changes when the user
  // toggles a capability in Settings -> Models. 30s is the same
  // cadence as the global memory settings query, so the two
  // stay consistent without a separate polling strategy.
  //
  // The query key uses a `['config', ...]` namespace so it does
  // not collide with the memory-client keys defined in
  // `api/keys.ts` (which are scoped to `/api/memory/*`).
  const modelsQuery = useQuery({
    queryKey: ['config', 'memory-models'] as const,
    queryFn: ({ signal }) => configClient.getMemoryModels(signal),
    staleTime: 30_000,
  });

  // Filter by model kind. The backend returns every model marked
  // as "memory", including embedding models. Extraction and
  // consolidation can only use chat models, so we filter them
  // out here. Phase 4's semantic-search settings will pass
  // `embeddingOnly: true` to filter in the opposite direction.
  const models = (modelsQuery.data ?? []).filter((m) =>
    embeddingOnly ? m.isEmbedding : !m.isEmbedding,
  );

  // Build the "currently selected" composite key for the
  // <select value>. If the backend stored a plain key
  // (`providerId === modelId` or `providerId === null`), we
  // still need a stable value to match against the option list.
  const selectedKey: string = (() => {
    if (!modelId) return '';
    if (!providerId) return modelId;
    return `${providerId}::${modelId}`;
  })();

  // If the current selection is no longer in the list (user
  // unmarked the model), show it as "not selected" in the
  // dropdown -- but do NOT call onChange to clear it. The
  // caller is responsible for deciding whether to clear a
  // stale selection (e.g. after showing a warning).
  const selectionExists =
    selectedKey.length > 0 &&
    models.some((m) => m.key === selectedKey);

  function handleChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const key = event.target.value;
    if (!key) {
      onChange(null, null, null);
      return;
    }
    const found = models.find((m) => m.key === key);
    if (!found) {
      // Should never happen -- the option's value came from
      // the same list we are searching. Defensive only.
      return;
    }
    onChange(found.providerId, found.modelId, found);
  }

  // ---- Loading ---------------------------------------------------
  if (modelsQuery.isLoading) {
    return (
      <MemoryField label={label} disabled>
        <div className="h-5 w-full animate-pulse rounded-sm bg-muted/40" />
      </MemoryField>
    );
  }

  // ---- Fetch error ----------------------------------------------
  if (modelsQuery.error) {
    return (
      <MemoryBanner
        tone="error"
        title={label}
        message={
          modelsQuery.error instanceof Error
            ? modelsQuery.error.message
            : t('errors.generic')
        }
      />
    );
  }

  // ---- No models marked -----------------------------------------
  if (models.length === 0) {
    return (
      <MemoryBanner
        tone="warning"
        title={label}
        message={
          <div className="space-y-1.5">
            <p>{t('settings.ai.noMemoryModels')}</p>
            <Link
              href="/settings/setup?tab=models"
              className="inline-flex items-center gap-1 font-medium underline underline-offset-2"
            >
              {t('settings.ai.markModelAsMemory')}
              <span aria-hidden="true">&rarr;</span>
            </Link>
          </div>
        }
      />
    );
  }

  // ---- Normal render --------------------------------------------
  //
  // The current selection (if any) shows the model's known
  // limits beneath the dropdown, so the user can see what the
  // auto-fill in MemoryAISettingsSection was derived from.
  const selectedInfo = models.find((m) => m.key === selectedKey);
  const selectedContextWindow = selectedInfo?.contextWindow ?? null;
  const selectedMaxOutput = selectedInfo?.maxOutputTokens ?? null;

  return (
    <MemoryField
      label={label}
      help={help}
      error={error}
      disabled={disabled}
    >
      <select
        value={selectionExists ? selectedKey : ''}
        onChange={handleChange}
        disabled={disabled}
        className={cn(MEMORY_FIELD_INPUT_CLASS, 'cursor-pointer')}
      >
        <option value="">{t('settings.ai.selectModel')}</option>
        {models.map((m) => (
          <option key={m.key} value={m.key}>
            {m.providerId ? `${m.providerId} / ${m.modelId}` : m.modelId}
          </option>
        ))}
      </select>
      {!selectionExists && selectedKey.length > 0 && (
        <p className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">
          {t('settings.ai.selectionNoLongerMarked')}
        </p>
      )}
      {(selectedContextWindow !== null || selectedMaxOutput !== null) && (
        <p className="mt-1 text-[10px] text-muted-foreground">
          {selectedContextWindow !== null && (
            <>
              {t('settings.ai.contextWindowLabel')}:{' '}
              {formatTokens(selectedContextWindow, t)}
            </>
          )}
          {selectedContextWindow !== null && selectedMaxOutput !== null && ' · '}
          {selectedMaxOutput !== null && (
            <>
              {t('settings.ai.maxOutputLabel')}:{' '}
              {formatTokens(selectedMaxOutput, t)}
            </>
          )}
        </p>
      )}
    </MemoryField>
  );
}
