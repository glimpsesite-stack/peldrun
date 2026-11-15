// src/features/memory/components/MemoryAISettingsSection.tsx

/**
 * The AI extraction settings section.
 *
 * Container that groups every knob governing the LLM-powered
 * extraction pipeline:
 *
 *   1. Master switch (`enabled`)
 *   2. Extraction block (trigger, model, per-task limits,
 *      global bounds, batching)
 *   3. Consolidation block (Phase 5 preview)
 *   4. Batch block (Phase 3 preview)
 *
 * Layout
 * ------
 * Follows the same card-as-input pattern as the rest of the
 * settings page. The master toggle is always visible; the three
 * sub-blocks only render when `enabled` is true, keeping the
 * form short for users who have not opted in.
 *
 * Design rationale
 * ----------------
 *   * NO "Test model" button in this iteration. The backend
 *     does not expose a dedicated test endpoint for memory
 *     models -- the existing `/api/config/test-llm` is for
 *     provider connectivity, not memory-model dispatch. Users
 *     verify the model by running a chat; the worker's status
 *     shows up on `/memory/jobs` once Phase 3 lands. When the
 *     dedicated endpoint arrives, a `Test model` button can be
 *     added here without touching sibling components.
 *
 *   * NO explicit "no model selected" warning banner. The
 *     `MemoryModelSelector` shows:
 *       - an inline banner with a link to Settings -> Models
 *         when the user has not marked ANY model; OR
 *       - an empty dropdown with a clear placeholder when
 *         models exist but the user has not selected one.
 *     Duplicating the warning here would be noise.
 *
 *   * Error paths flow through `errors` prop. The parent
 *     `SettingsForm` runs `makeSettingsPatchSchema` and
 *     `zodErrorToFieldMap`, which produces dot-joined paths
 *     like `"ai.min_confidence"`. Each field reads its own
 *     error via `errors?.['ai.<field>']`.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { ChevronRight, Sparkles } from 'lucide-react';
import Link from 'next/link';

import type { MemoryAISettings, MemoryTaskModel } from '../types/ai';
import { MemoryAIBounds } from '../types/ai';
import { MemorySection } from './MemorySection';
import {
  MEMORY_FIELD_INPUT_CLASS,
  MemoryField,
} from './MemoryField';
import { MemoryBanner } from './MemoryBanner';
import { ExtractionTriggerSelector } from './ExtractionTriggerSelector';
import { MemoryModelSelector } from './MemoryModelSelector';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface MemoryAISettingsSectionProps {
  /** Current AI settings object (from the parent form). */
  value: MemoryAISettings;
  /** Called with a FULLY-REPLACED settings object on any change. */
  onChange: (next: MemoryAISettings) => void;
  /** When true, disables every input in the section. */
  disabled?: boolean;
  /**
   * Field errors produced by `zodErrorToFieldMap`, keyed by
   * dot-joined path (e.g. `"ai.min_confidence"`). Only errors
   * for fields the user has typed into will be present.
   */
  errors?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryAISettingsSection({
  value,
  onChange,
  disabled = false,
  errors,
}: MemoryAISettingsSectionProps) {
  const { t } = useTranslation('memory');

  // ---- Update helpers -------------------------------------------
  // Two helpers keep the call sites short and prevent accidental
  // mutation of the nested objects.

  function update<K extends keyof MemoryAISettings>(
    key: K,
    next: MemoryAISettings[K],
  ): void {
    onChange({ ...value, [key]: next });
  }

  function updateTask<K extends 'extraction' | 'consolidation'>(
    which: K,
    patch: Partial<MemoryAISettings[K]>,
  ): void {
    onChange({
      ...value,
      [which]: { ...value[which], ...patch },
    });
  }

  function err(path: string): string | undefined {
    return errors?.[path];
  }

  /**
   * Pre-fill a `MemoryTaskModel` from the model catalog facts.
   *
   * Used when the user selects a model in the extraction or
   * consolidation dropdown. The catalog knows the model's
   * `max_output_tokens`; filling it here spares the user from
   * having to remember (or look up) each model's limit.
   *
   * The current value is ALWAYS overwritten with the catalog
   * value when the catalog is non-null. Rationale: the user
   * just picked a model, so the prior max_output_tokens
   * belonged to the previous model and is no longer meaningful.
   * Users who want a custom limit can edit the field AFTER
   * selecting the model -- the auto-fill only fires on
   * selection, not on every render.
   */
  function prefillTaskModel(
    catalogMaxOutput: number | null | undefined,
  ): Partial<MemoryTaskModel> {
    if (catalogMaxOutput == null) return {};
    const clamped = Math.max(
      MemoryAIBounds.max_output_tokens.min,
      Math.min(catalogMaxOutput, MemoryAIBounds.max_output_tokens.max),
    );
    return { max_output_tokens: clamped };
  }

  /**
   * Compute the recommended `max_input_tokens` from a model's
   * context window.
   *
   * Reserves ~20% of the window for the extraction prompt
   * itself (system instructions + user message + wrapper the
   * gateway builds). 80% is the maximum amount of session
   * content we can safely send in one call.
   *
   * Returns `null` when the model's context window is unknown,
   * so the caller leaves the current value untouched.
   */
  function suggestMaxInputTokens(
    contextWindow: number | null | undefined,
  ): number | null {
    if (contextWindow == null || contextWindow <= 0) return null;
    const suggested = Math.floor(contextWindow * 0.8);
    return Math.max(
      MemoryAIBounds.max_input_tokens.min,
      Math.min(suggested, MemoryAIBounds.max_input_tokens.max),
    );
  }

  // ---- Render ---------------------------------------------------
  return (
    <MemorySection
      icon={Sparkles}
      title={t('settings.sections.autoMemory')}
      subtitle={t('settings.ai.enabledHelp')}
      accent="amber"
      delay={0.2}
    >
      <div className="space-y-4">
        {/* ============ Master toggle ============ */}
        <label
          className={cn(
            'inline-flex items-center gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-xs cursor-pointer',
            'hover:bg-muted/40 transition-colors',
            disabled && 'cursor-not-allowed opacity-60',
          )}
        >
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(e) => update('enabled', e.target.checked)}
            disabled={disabled}
            className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
          />
          <span className="font-medium text-foreground">
            {t('settings.ai.enabled')}
          </span>
        </label>

        {/* ============ Disabled hint ============ */}
        {!value.enabled && (
          <MemoryBanner
            tone="info"
            message={t('settings.ai.disabledHint')}
          />
        )}

        {/* ============ Enabled content ============ */}
        {value.enabled && (
          <div className="space-y-4">
            {/* -------- Extraction block -------- */}
            <div className="rounded-md border border-border/60 p-3 space-y-3">
              <h4 className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
                {t('settings.ai.extractionTitle')}
              </h4>

              <label
                className={cn(
                  'inline-flex items-center gap-2 text-xs cursor-pointer',
                  disabled && 'cursor-not-allowed opacity-60',
                )}
              >
                <input
                  type="checkbox"
                  checked={value.extraction_enabled}
                  onChange={(e) =>
                    update('extraction_enabled', e.target.checked)
                  }
                  disabled={disabled}
                  className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                />
                <span className="font-medium text-foreground">
                  {t('settings.ai.extractionEnabled')}
                </span>
              </label>

              {value.extraction_enabled && (
                <div className="space-y-3">
                  {/* Trigger */}
                  <ExtractionTriggerSelector
                    value={value.extraction_trigger}
                    onChange={(v) => update('extraction_trigger', v)}
                    disabled={disabled}
                  />

                  {/* Model */}
                  <MemoryModelSelector
                    label={t('settings.ai.extractionModel')}
                    providerId={value.extraction.provider_id}
                    modelId={value.extraction.model_id}
                    onChange={(pid, mid, info) => {
                      // Pre-fill per-task limit from the catalog.
                      const taskPatch: Partial<MemoryTaskModel> = {
                        provider_id: pid,
                        model_id: mid,
                        ...prefillTaskModel(info?.maxOutputTokens),
                      };
                      // Pre-fill the shared max_input_tokens from
                      // the model's context window (80% heuristic).
                      // Only for EXTRACTION -- consolidation uses the
                      // same shared max_input_tokens, so a smaller
                      // consolidation model must NOT silently shrink
                      // the extraction budget. If consolidation ends
                      // up on a smaller model, the user lowers
                      // max_input_tokens manually.
                      const sharedMaxInput = suggestMaxInputTokens(
                        info?.contextWindow,
                      );
                      onChange({
                        ...value,
                        extraction: { ...value.extraction, ...taskPatch },
                        ...(sharedMaxInput !== null
                          ? { max_input_tokens: sharedMaxInput }
                          : {}),
                      });
                    }}
                    disabled={disabled}
                    error={
                      err('ai.extraction.provider_id') ??
                      err('ai.extraction.model_id')
                    }
                  />
                  {/*
                    Embedding-model guardrail.

                    The backend's `get_memory_models()` returns
                    every model the user marked as "memory",
                    WITHOUT distinguishing chat-completion models
                    from embedding models. In Phase 4 (semantic
                    search), users are expected to mark BOTH kinds
                    -- so the dropdown here can legitimately
                    contain embedding models that WOULD fail on
                    the first extraction job (an embedding model
                    cannot serve `/chat/completions`).

                    Until the backend enriches `memory-models`
                    with an `is_embedding` flag (Phase 4 work),
                    surface a clear warning so the user does not
                    select the wrong kind by mistake.
                  */}
                  <p className="text-[10px] leading-snug text-amber-600 dark:text-amber-400">
                    {t('settings.ai.embeddingWarning')}
                  </p>

                  {/* Per-task limits */}
                  <div className="grid grid-cols-2 gap-2">
                    <MemoryField
                      label={t('settings.ai.maxOutputTokens')}
                      help={t('settings.ai.maxOutputTokensHelp')}
                      error={err('ai.extraction.max_output_tokens')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.extraction.max_output_tokens}
                        min={MemoryAIBounds.max_output_tokens.min}
                        max={MemoryAIBounds.max_output_tokens.max}
                        step={1}
                        onChange={(e) =>
                          updateTask('extraction', {
                            max_output_tokens: Number(e.target.value),
                          })
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.timeoutSeconds')}
                      help={t('settings.ai.timeoutSecondsHelp')}
                      error={err('ai.extraction.timeout_seconds')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.extraction.timeout_seconds}
                        min={MemoryAIBounds.timeout_seconds.min}
                        max={MemoryAIBounds.timeout_seconds.max}
                        step={5}
                        onChange={(e) =>
                          updateTask('extraction', {
                            timeout_seconds: Number(e.target.value),
                          })
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                  </div>

                  {/* Global bounds */}
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                    <MemoryField
                      label={t('settings.ai.maxMessagesPerJob')}
                      help={t('settings.ai.maxMessagesPerJobHelp')}
                      error={err('ai.max_messages_per_job')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.max_messages_per_job}
                        min={MemoryAIBounds.max_messages_per_job.min}
                        max={MemoryAIBounds.max_messages_per_job.max}
                        step={1}
                        onChange={(e) =>
                          update(
                            'max_messages_per_job',
                            Number(e.target.value),
                          )
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.maxInputTokens')}
                      help={t('settings.ai.maxInputTokensHelp')}
                      error={err('ai.max_input_tokens')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.max_input_tokens}
                        min={MemoryAIBounds.max_input_tokens.min}
                        max={MemoryAIBounds.max_input_tokens.max}
                        step={1}
                        onChange={(e) =>
                          update(
                            'max_input_tokens',
                            Number(e.target.value),
                          )
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.maxCandidatesPerJob')}
                      help={t('settings.ai.maxCandidatesPerJobHelp')}
                      error={err('ai.max_candidates_per_job')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.max_candidates_per_job}
                        min={MemoryAIBounds.max_candidates_per_job.min}
                        max={MemoryAIBounds.max_candidates_per_job.max}
                        step={1}
                        onChange={(e) =>
                          update(
                            'max_candidates_per_job',
                            Number(e.target.value),
                          )
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.minConfidence')}
                      help={t('settings.ai.minConfidenceHelp')}
                      error={err('ai.min_confidence')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.min_confidence}
                        min={MemoryAIBounds.min_confidence.min}
                        max={MemoryAIBounds.min_confidence.max}
                        step="any"
                        onChange={(e) =>
                          update('min_confidence', Number(e.target.value))
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.maxCostPerDay')}
                      help={t('settings.ai.maxCostPerDayHelp')}
                      error={err('ai.max_cost_nano_usd_per_day')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.max_cost_nano_usd_per_day}
                        min={MemoryAIBounds.max_cost_nano_usd_per_day.min}
                        max={MemoryAIBounds.max_cost_nano_usd_per_day.max}
                        step={1_000_000}
                        onChange={(e) =>
                          update(
                            'max_cost_nano_usd_per_day',
                            Number(e.target.value),
                          )
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                  </div>

                  {/* After-turn batching */}
                  <label
                    className={cn(
                      'inline-flex items-center gap-2 text-xs cursor-pointer',
                      disabled && 'cursor-not-allowed opacity-60',
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={value.after_turn_batching}
                      onChange={(e) =>
                        update(
                          'after_turn_batching',
                          e.target.checked,
                        )
                      }
                      disabled={disabled}
                      className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                    />
                    <span className="font-medium text-foreground">
                      {t('settings.ai.afterTurnBatching')}
                    </span>
                  </label>
                  <p className="text-[10px] leading-snug text-muted-foreground">
                    {t('settings.ai.afterTurnBatchingHelp')}
                  </p>
                </div>
              )}
            </div>

            {/* -------- Consolidation block -------- */}
            <div className="rounded-md border border-border/60 p-3 space-y-3">
              <h4 className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
                {t('settings.ai.consolidationTitle')}
              </h4>

              <label
                className={cn(
                  'inline-flex items-center gap-2 text-xs cursor-pointer',
                  disabled && 'cursor-not-allowed opacity-60',
                )}
              >
                <input
                  type="checkbox"
                  checked={value.consolidation_enabled}
                  onChange={(e) =>
                    update(
                      'consolidation_enabled',
                      e.target.checked,
                    )
                  }
                  disabled={disabled}
                  className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                />
                <span className="font-medium text-foreground">
                  {t('settings.ai.consolidationEnabled')}
                </span>
              </label>

              {value.consolidation_enabled && (
                <MemoryModelSelector
                  label={t('settings.ai.consolidationModel')}
                  providerId={value.consolidation.provider_id}
                  modelId={value.consolidation.model_id}
                  onChange={(pid, mid, info) => {
                    // Same auto-fill logic as extraction for the
                    // per-task max_output_tokens. The SHARED
                    // max_input_tokens is deliberately NOT touched
                    // here -- see the extraction onChange above.
                    updateTask('consolidation', {
                      provider_id: pid,
                      model_id: mid,
                      ...prefillTaskModel(info?.maxOutputTokens),
                    });
                  }}
                  disabled={disabled}
                  error={
                    err('ai.consolidation.provider_id') ??
                    err('ai.consolidation.model_id')
                  }
                />
              )}
            </div>

            {/* -------- Batch block -------- */}
            <div className="rounded-md border border-border/60 p-3 space-y-3">
              <h4 className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
                {t('settings.ai.batchTitle')}
              </h4>

              <label
                className={cn(
                  'inline-flex items-center gap-2 text-xs cursor-pointer',
                  disabled && 'cursor-not-allowed opacity-60',
                )}
              >
                <input
                  type="checkbox"
                  checked={value.batch_enabled}
                  onChange={(e) =>
                    update('batch_enabled', e.target.checked)
                  }
                  disabled={disabled}
                  className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                />
                <span className="font-medium text-foreground">
                  {t('settings.ai.batchEnabled')}
                </span>
              </label>

              {value.batch_enabled && (
                <>
                  <div className="grid grid-cols-2 gap-2">
                    <MemoryField
                      label={t('settings.ai.batchMaxConcurrentJobs')}
                      help={t('settings.ai.batchMaxConcurrentJobsHelp')}
                      error={err('ai.batch_max_concurrent_jobs')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.batch_max_concurrent_jobs}
                        min={MemoryAIBounds.batch_max_concurrent_jobs.min}
                        max={MemoryAIBounds.batch_max_concurrent_jobs.max}
                        step={1}
                        onChange={(e) =>
                          update(
                            'batch_max_concurrent_jobs',
                            Number(e.target.value),
                          )
                        }
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                    <MemoryField
                      label={t('settings.ai.batchAutoScheduleHours')}
                      help={t('settings.ai.batchAutoScheduleHoursHelp')}
                      error={err('ai.batch_auto_schedule_hours')}
                      disabled={disabled}
                    >
                      <input
                        type="number"
                        value={value.batch_auto_schedule_hours ?? ''}
                        min={MemoryAIBounds.batch_auto_schedule_hours.min}
                        max={MemoryAIBounds.batch_auto_schedule_hours.max}
                        step={1}
                        placeholder={t('settings.ai.batchManualOnly')}
                        onChange={(e) => {
                          const raw = e.target.value.trim();
                          if (raw === '') {
                            update('batch_auto_schedule_hours', null);
                            return;
                          }
                          const n = Number(raw);
                          if (Number.isFinite(n)) {
                            update('batch_auto_schedule_hours', n);
                          }
                        }}
                        disabled={disabled}
                        className={MEMORY_FIELD_INPUT_CLASS}
                      />
                    </MemoryField>
                  </div>
                  <Link
                    href="/memory/batch"
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                  >
                    {t('settings.ai.batchTitle')}
                    <ChevronRight size={11} aria-hidden="true" />
                  </Link>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </MemorySection>
  );
}
