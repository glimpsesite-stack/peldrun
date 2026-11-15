// src/features/memory/components/SettingsForm.tsx

/**
 * The global memory settings form.
 *
 * Design aligned with the app's SearchTab:
 *
 *   * Each logical group (mode, provider, retrieval, budget,
 *     extraction) is its own `<MemorySection>` with a distinct
 *     accent color and entry animation delay. The staggered
 *     reveal matches SearchTab's sections.
 *
 *   * Fields are wrapped in `<MemoryField>` -- the card-as-input
 *     pattern used by SearchTab. Labels are uppercase,
 *     tracking-wider, and the input itself is borderless.
 *
 *   * The mode section's accent color tracks the current mode:
 *     emerald for read_write, amber for read_only, rose for
 *     disabled. This makes the state visible without reading
 *     the labels.
 *
 * Behaviour preserved from the pre-redesign version:
 *
 *   * Diff-based PATCH submission (only changed fields).
 *   * `isNoOpPatch` guard against "changed then changed back".
 *   * `makeSettingsPatchSchema` cross-checks `reserved <= max`.
 *   * Form remains editable in `disabled` mode (spec §6.1).
 */

'use client';

import {
  type FormEvent,
  useEffect,
  useId,
  useMemo,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import {
  Check,
  Database,
  Power,
  Save,
  SlidersHorizontal,
  Wallet,
} from 'lucide-react';

import {
  MemoryMode,
  ALL_MEMORY_MODES,
} from '../types/enums';
import type {
  MemorySettings,
  MemorySettingsPatch,
  MemoryTokenBudget,
} from '../types/settings';
import {
  computeEffectiveOverride,
  isNoOpPatch,
  MemorySettingsBounds,
} from '../types/settings';
import { makeSettingsPatchSchema, zodErrorToFieldMap } from '../lib/validators';
import { usePatchSettings } from '../api/mutations';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ModeBadge } from './ModeBadge';
import { MemorySection } from './MemorySection';
import { MemoryBanner } from './MemoryBanner';
import {
  MemoryField,
  MEMORY_FIELD_INPUT_CLASS,
} from './MemoryField';
import { MemoryAISettingsSection } from './MemoryAISettingsSection';
import { DEFAULT_MEMORY_AI_SETTINGS } from '../types/ai';
import type { MemorySectionAccent } from './MemorySection';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SettingsForm({ className }: { className?: string }) {
  const { t } = useTranslation('memory');
  const formId = useId();

  const settingsQuery = useMemorySettings();
  const patchMutation = usePatchSettings();

  const [form, setForm] = useState<MemorySettings | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [justSaved, setJustSaved] = useState(false);

  // Initialize / sync form state.
  useEffect(() => {
    if (settingsQuery.data && form === null) {
      setForm(structuredClone(settingsQuery.data));
    }
  }, [settingsQuery.data, form]);

  // Sync form after successful save (server may normalize values).
  useEffect(() => {
    if (patchMutation.isSuccess && patchMutation.data) {
      setForm(structuredClone(patchMutation.data.settings));
      setJustSaved(true);
      const handle = setTimeout(() => setJustSaved(false), 1200);
      return () => clearTimeout(handle);
    }
    return undefined;
  }, [patchMutation.isSuccess, patchMutation.data]);

  const current = settingsQuery.data ?? null;

  const patch = useMemo<MemorySettingsPatch>(() => {
    if (!current || !form) return {};
    return computeEffectiveOverride(current, form) as MemorySettingsPatch;
  }, [current, form]);

  const isDirty = !current ? false : !isNoOpPatch(current, patch);

  // ---- Loading / error states ------------------------------------
  if (settingsQuery.isLoading) {
    return (
      <div className={cn('space-y-4', className)} aria-busy="true">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-32 w-full animate-pulse rounded-md bg-muted/40"
          />
        ))}
      </div>
    );
  }

  if (settingsQuery.error || !form || !current) {
    return (
      <MemoryBanner
        tone="error"
        message={settingsQuery.error?.message ?? t('errors.generic')}
        className={className}
      />
    );
  }

  // ---- Handlers --------------------------------------------------
  function updateField<K extends keyof MemorySettings>(
    key: K,
    value: MemorySettings[K],
  ) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  function updateBudgetField(
    key: keyof MemoryTokenBudget,
    value: number,
  ) {
    setForm((prev) =>
      prev
        ? { ...prev, token_budget: { ...prev.token_budget, [key]: value } }
        : prev,
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!current) return;
    const result = makeSettingsPatchSchema(current).safeParse(patch);
    if (!result.success) {
      setErrors(zodErrorToFieldMap(result.error));
      return;
    }
    setErrors({});
    try {
      await patchMutation.mutateAsync(result.data);
    } catch {
      // Error surfaced via mutation state
    }
  }

  // ---- Derived visual state --------------------------------------
  const isPending = patchMutation.isPending;
  const canSave = isDirty && !isPending;

  const modeAccent: MemorySectionAccent =
    form.mode === 'read_write'
      ? 'emerald'
      : form.mode === 'read_only'
        ? 'amber'
        : 'rose';

  // ---- Render ----------------------------------------------------
  return (
    <form
      onSubmit={handleSubmit}
      className={cn('space-y-6', className)}
      aria-labelledby={`${formId}-title`}
    >
      <h2 id={`${formId}-title`} className="sr-only">
        {t('settings.title')}
      </h2>

      {/* ============ Mode ============ */}
      <MemorySection
        icon={Power}
        title={t('settings.sections.mode')}
        subtitle={t('mode.label')}
        accent={modeAccent}
        delay={0}
      >
        <div className="space-y-2">
          {ALL_MEMORY_MODES.map((mode) => {
            const selected = form.mode === mode;
            return (
              <label
                key={mode}
                className={cn(
                  'flex items-start gap-2.5 rounded-sm border p-3 text-xs transition-colors cursor-pointer',
                  selected
                    ? 'border-primary/60 bg-primary/5'
                    : 'border-border/60 bg-card hover:bg-muted/40',
                  isPending && 'cursor-not-allowed opacity-60',
                )}
              >
                <input
                  type="radio"
                  name="memory-mode"
                  value={mode}
                  checked={selected}
                  onChange={() => updateField('mode', mode)}
                  disabled={isPending}
                  className="mt-0.5 h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="font-semibold text-foreground">
                      {t(`mode.${mode}.name`)}
                    </span>
                    {selected && <ModeBadge mode={mode} size="xs" />}
                  </div>
                  <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                    {t(`mode.${mode}.description`)}
                  </p>
                </div>
              </label>
            );
          })}
        </div>
        {form.mode === 'disabled' && (
          <MemoryBanner
            tone="warning"
            message={t('settings.modeWarningDisabled')}
          />
        )}
      </MemorySection>

      {/* ============ Provider ============ */}
      <MemorySection
        icon={Database}
        title={t('settings.sections.provider')}
        subtitle={t('settings.fields.provider')}
        accent="purple"
        delay={0.05}
      >
        <MemoryField
          label={t('settings.fields.provider')}
          htmlFor={`${formId}-provider`}
          help="Storage backend used by the memory subsystem."
        >
          <select
            id={`${formId}-provider`}
            value={form.provider_id}
            onChange={(e) => updateField('provider_id', e.target.value)}
            disabled={isPending}
            className={cn(MEMORY_FIELD_INPUT_CLASS, 'cursor-pointer')}
          >
            <option value="sqlite_fts">sqlite_fts</option>
            {form.provider_id !== 'sqlite_fts' && (
              <option value={form.provider_id}>{form.provider_id}</option>
            )}
          </select>
        </MemoryField>
      </MemorySection>

      {/* ============ Retrieval tuning ============ */}
      <MemorySection
        icon={SlidersHorizontal}
        title={t('settings.sections.retrieval')}
        subtitle={t('settings.help.minRelevance')}
        accent="primary"
        delay={0.1}
      >
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <MemoryField
            label={t('settings.fields.defaultLimit')}
            htmlFor={`${formId}-default-limit`}
            help={t('settings.help.defaultLimit')}
            error={errors.default_limit}
          >
            <input
              id={`${formId}-default-limit`}
              type="number"
              value={form.default_limit}
              min={MemorySettingsBounds.default_limit.min}
              max={MemorySettingsBounds.default_limit.max}
              step={1}
              onChange={(e) =>
                updateField('default_limit', Number(e.target.value))
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
          <MemoryField
            label={t('settings.fields.minRelevance')}
            htmlFor={`${formId}-min-relevance`}
            help={t('settings.help.minRelevance')}
            error={errors.min_relevance_score}
          >
            <input
              id={`${formId}-min-relevance`}
              type="number"
              value={form.min_relevance_score}
              min={MemorySettingsBounds.min_relevance_score.min}
              max={MemorySettingsBounds.min_relevance_score.max}
              step="any"
              onChange={(e) =>
                updateField('min_relevance_score', Number(e.target.value))
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
          <MemoryField
            label={t('settings.fields.boostPinned')}
            htmlFor={`${formId}-boost-pinned`}
            help={t('settings.help.boostPinned')}
            error={errors.boost_pinned}
          >
            <input
              id={`${formId}-boost-pinned`}
              type="number"
              value={form.boost_pinned}
              min={MemorySettingsBounds.boost_pinned.min}
              max={MemorySettingsBounds.boost_pinned.max}
              step="any"
              onChange={(e) =>
                updateField('boost_pinned', Number(e.target.value))
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
        </div>
      </MemorySection>

      {/* ============ Token budget ============ */}
      <MemorySection
        icon={Wallet}
        title={t('settings.sections.tokenBudget')}
        subtitle={t('settings.help.maxTokens')}
        accent="cyan"
        delay={0.15}
      >
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <MemoryField
            label={t('settings.fields.maxTokens')}
            htmlFor={`${formId}-max-tokens`}
            help={t('settings.help.maxTokens')}
            error={errors['token_budget.max_tokens']}
          >
            <input
              id={`${formId}-max-tokens`}
              type="number"
              value={form.token_budget.max_tokens}
              min={MemorySettingsBounds.token_budget_max_tokens.min}
              max={MemorySettingsBounds.token_budget_max_tokens.max}
              step={1}
              onChange={(e) =>
                updateBudgetField('max_tokens', Number(e.target.value))
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
          <MemoryField
            label={t('settings.fields.reservedForUserScope')}
            htmlFor={`${formId}-reserved`}
            help={t('settings.help.reservedForUserScope')}
            error={errors['token_budget.reserved_for_user_scope']}
          >
            <input
              id={`${formId}-reserved`}
              type="number"
              value={form.token_budget.reserved_for_user_scope}
              min={0}
              max={form.token_budget.max_tokens}
              step={1}
              onChange={(e) =>
                updateBudgetField(
                  'reserved_for_user_scope',
                  Number(e.target.value),
                )
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
          <MemoryField
            label={t('settings.fields.warnThreshold')}
            htmlFor={`${formId}-warn-threshold`}
            help={t('settings.help.warnThreshold')}
            error={errors['token_budget.warn_threshold']}
          >
            <input
              id={`${formId}-warn-threshold`}
              type="number"
              value={form.token_budget.warn_threshold}
              min={MemorySettingsBounds.token_budget_warn_threshold.min}
              max={MemorySettingsBounds.token_budget_warn_threshold.max}
              step="any"
              onChange={(e) =>
                updateBudgetField('warn_threshold', Number(e.target.value))
              }
              disabled={isPending}
              className={MEMORY_FIELD_INPUT_CLASS}
            />
          </MemoryField>
        </div>
      </MemorySection>

      {/* ============ Automatic memory (Phase 3) ============ */}
      {/*
        Replaces the pre-Phase-1 "Auto extraction" block. The
        legacy fields (``auto_extraction_enabled``,
        ``auto_extraction_min_turns``) remain on ``MemorySettings``
        for backward compatibility with older settings files, but
        are no longer surfaced in the UI. The AI extraction
        pipeline is now governed entirely by ``settings.ai``,
        which this section edits.
      */}
      <MemoryAISettingsSection
        value={form.ai ?? DEFAULT_MEMORY_AI_SETTINGS}
        onChange={(ai) => updateField('ai', ai)}
        disabled={isPending}
        errors={errors}
      />

      {/* ============ Footer ============ */}
      <div className="flex items-center justify-end gap-3 pt-2">
        {justSaved && (
          <span className="inline-flex items-center gap-1 text-[11px] font-mono text-emerald-600 dark:text-emerald-400">
            <Check size={11} aria-hidden="true" />
            {t('settings.savedToast')}
          </span>
        )}
        {!isDirty && !justSaved && (
          <span className="text-[11px] font-mono text-muted-foreground">
            {t('settings.noChanges')}
          </span>
        )}
        <button
          type="submit"
          disabled={!canSave}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3.5 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-60',
            canSave && 'hover:scale-105 transition-transform',
          )}
        >
          <Save size={12} aria-hidden="true" />
          {isPending ? t('common.saving') : t('common.saveChanges')}
        </button>
      </div>
    </form>
  );
}
