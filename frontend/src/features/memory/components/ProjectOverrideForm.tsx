// src/features/memory/components/ProjectOverrideForm.tsx

/**
 * Editor for one project's settings override.
 *
 * Design aligned with SearchTab:
 *
 *   * Wrapped in `MemorySection` (bg-gigbg surface + accent line).
 *   * Every field uses `MemoryField` (card-as-input pattern) with
 *     the standard transparent input styling.
 *   * Each field shows an "inheriting / overridden" indicator
 *     underneath, matching the app's status-chip vocabulary.
 *
 * Behaviour preserved:
 *
 *   * PUT (full override replacement) -- HTTP PUT semantics.
 *   * DELETE via `ConfirmDangerDialog` with type-project-name.
 *   * Validation via `makeProjectOverrideSchema`.
 *   * Reset button restores the previously-loaded override.
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
import { RotateCcw, Save, Settings2, Trash2 } from 'lucide-react';

import {
  MemoryMode,
  ALL_MEMORY_MODES,
} from '../types/enums';
import type {
  MemoryProjectOverride,
  MemoryTokenBudget,
} from '../types/settings';
import {
  computeEffectiveOverride,
  MemorySettingsBounds,
} from '../types/settings';
import {
  makeProjectOverrideSchema,
  zodErrorToFieldMap,
} from '../lib/validators';
import {
  useDeleteProjectOverride,
  usePutProjectOverride,
} from '../api/mutations';
import { useProjectSettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ConfirmDangerDialog } from './ConfirmDangerDialog';
import { MemorySection } from './MemorySection';
import { MemoryBanner } from './MemoryBanner';
import {
  MemoryField,
  MEMORY_FIELD_INPUT_CLASS,
} from './MemoryField';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProjectOverrideFormProps {
  projectId: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProjectOverrideForm({
  projectId,
  className,
}: ProjectOverrideFormProps) {
  const { t } = useTranslation('memory');
  const formId = useId();

  const query = useProjectSettings(projectId);
  const putMutation = usePutProjectOverride();
  const deleteMutation = useDeleteProjectOverride();

  const [form, setForm] = useState<MemoryProjectOverride>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false);

  const globalBase = query.data?.effective ?? null;
  const initialOverride = query.data?.override ?? null;

  useEffect(() => {
    if (initialOverride === null || !globalBase) return;
    const effective = computeEffectiveOverride(globalBase, initialOverride);
    setForm(effective);
  }, [initialOverride, globalBase]);

  const patch = useMemo<MemoryProjectOverride>(() => {
    if (!globalBase || !initialOverride) return {};
    const effectiveOverride = computeEffectiveOverride(
      globalBase,
      initialOverride,
    );
    return computeEffectiveOverride(
      { ...globalBase, project_overrides: {} },
      {
        ...effectiveOverride,
        ...form,
        token_budget: {
          ...(effectiveOverride.token_budget ?? {}),
          ...(form.token_budget ?? {}),
        },
      },
    );
  }, [globalBase, initialOverride, form]);

  const isDirty = Object.keys(patch).length > 0 || Object.keys(form).length > 0;
  const hasExistingOverride =
    initialOverride !== null &&
    Object.keys(computeEffectiveOverride(globalBase!, initialOverride)).length >
      0;

  // ---- Loading / error ------------------------------------------
  if (query.isLoading || !globalBase) {
    return (
      <div
        className={cn('space-y-3', className)}
        aria-busy="true"
      >
        <div className="h-24 animate-pulse rounded-md bg-muted/40" />
        <div className="h-24 animate-pulse rounded-md bg-muted/40" />
      </div>
    );
  }

  if (query.error) {
    return (
      <MemoryBanner
        tone="error"
        message={query.error.message}
        className={className}
      />
    );
  }

  // ---- Handlers -------------------------------------------------
  function setField<K extends keyof MemoryProjectOverride>(
    key: K,
    value: MemoryProjectOverride[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function setBudgetField(key: keyof MemoryTokenBudget, value: number) {
    setForm((prev) => ({
      ...prev,
      token_budget: { ...(prev.token_budget ?? {}), [key]: value },
    }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!globalBase) return;
    const result = makeProjectOverrideSchema(globalBase).safeParse(patch);
    if (!result.success) {
      setErrors(zodErrorToFieldMap(result.error));
      return;
    }
    setErrors({});
    try {
      await putMutation.mutateAsync({
        projectId,
        override: result.data,
      });
    } catch {
      // Error surfaced via mutation state
    }
  }

  async function handleRemove() {
    try {
      await deleteMutation.mutateAsync({ projectId });
      setConfirmRemoveOpen(false);
    } catch {
      // Error surfaced via mutation state
    }
  }

  // ---- Derived visual -------------------------------------------
  const isPending = putMutation.isPending || deleteMutation.isPending;
  const mergedBudget: MemoryTokenBudget = {
    ...globalBase.token_budget,
    ...(form.token_budget ?? {}),
  };
  const budgetInvalid =
    mergedBudget.reserved_for_user_scope > mergedBudget.max_tokens;

  // ---- Render ---------------------------------------------------
  return (
    <>
      <MemorySection
        icon={Settings2}
        title={t('settings.sections.projectOverrides')}
        subtitle={t('settings.projectOverrideNote')}
        accent="purple"
        delay={0}
        className={className}
        action={
          hasExistingOverride ? (
            <button
              type="button"
              onClick={() => setConfirmRemoveOpen(true)}
              disabled={isPending}
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-destructive hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive disabled:cursor-not-allowed disabled:opacity-60 transition-colors"
            >
              <Trash2 size={11} aria-hidden="true" />
              {t('settings.removeProjectOverride')}
            </button>
          ) : undefined
        }
      >
        <form onSubmit={handleSubmit} className="space-y-4" aria-labelledby={`${formId}-title`}>
          {/* Mode */}
          <OverrideField
            id={`${formId}-mode`}
            label={t('settings.sections.mode')}
            inheriting={form.mode === undefined}
            inheritedValue={t(`mode.${globalBase.mode}.name`)}
          >
            <select
              id={`${formId}-mode`}
              value={form.mode ?? ''}
              onChange={(e) =>
                setField(
                  'mode',
                  e.target.value === ''
                    ? undefined
                    : (e.target.value as MemoryMode),
                )
              }
              disabled={isPending}
              className={cn(MEMORY_FIELD_INPUT_CLASS, 'cursor-pointer')}
            >
              <option value="">—</option>
              {ALL_MEMORY_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`mode.${m}.name`)}
                </option>
              ))}
            </select>
          </OverrideField>

          {/* Retrieval */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <OverrideField
              id={`${formId}-default-limit`}
              label={t('settings.fields.defaultLimit')}
              inheriting={form.default_limit === undefined}
              inheritedValue={String(globalBase.default_limit)}
              error={errors.default_limit}
            >
              <input
                id={`${formId}-default-limit`}
                type="number"
                value={form.default_limit ?? ''}
                min={MemorySettingsBounds.default_limit.min}
                max={MemorySettingsBounds.default_limit.max}
                step={1}
                onChange={(e) =>
                  setField(
                    'default_limit',
                    e.target.value === ''
                      ? undefined
                      : Number(e.target.value),
                  )
                }
                disabled={isPending}
                placeholder={String(globalBase.default_limit)}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </OverrideField>
            <OverrideField
              id={`${formId}-min-relevance`}
              label={t('settings.fields.minRelevance')}
              inheriting={form.min_relevance_score === undefined}
              inheritedValue={String(globalBase.min_relevance_score)}
              error={errors.min_relevance_score}
            >
              <input
                id={`${formId}-min-relevance`}
                type="number"
                value={form.min_relevance_score ?? ''}
                min={MemorySettingsBounds.min_relevance_score.min}
                max={MemorySettingsBounds.min_relevance_score.max}
                step={0.05}
                onChange={(e) =>
                  setField(
                    'min_relevance_score',
                    e.target.value === ''
                      ? undefined
                      : Number(e.target.value),
                  )
                }
                disabled={isPending}
                placeholder={String(globalBase.min_relevance_score)}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </OverrideField>
            <OverrideField
              id={`${formId}-boost-pinned`}
              label={t('settings.fields.boostPinned')}
              inheriting={form.boost_pinned === undefined}
              inheritedValue={String(globalBase.boost_pinned)}
              error={errors.boost_pinned}
            >
              <input
                id={`${formId}-boost-pinned`}
                type="number"
                value={form.boost_pinned ?? ''}
                min={MemorySettingsBounds.boost_pinned.min}
                max={MemorySettingsBounds.boost_pinned.max}
                step={0.1}
                onChange={(e) =>
                  setField(
                    'boost_pinned',
                    e.target.value === ''
                      ? undefined
                      : Number(e.target.value),
                  )
                }
                disabled={isPending}
                placeholder={String(globalBase.boost_pinned)}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </OverrideField>
          </div>

          {/* Token budget */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <OverrideField
              id={`${formId}-max-tokens`}
              label={t('settings.fields.maxTokens')}
              inheriting={form.token_budget?.max_tokens === undefined}
              inheritedValue={String(globalBase.token_budget.max_tokens)}
              error={errors['token_budget.max_tokens']}
            >
              <input
                id={`${formId}-max-tokens`}
                type="number"
                value={form.token_budget?.max_tokens ?? ''}
                min={MemorySettingsBounds.token_budget_max_tokens.min}
                max={MemorySettingsBounds.token_budget_max_tokens.max}
                step={64}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === '') {
                    setForm((prev) => {
                      const next = { ...prev };
                      if (next.token_budget) {
                        const { max_tokens: _m, ...rest } = next.token_budget;
                        void _m;
                        next.token_budget = rest;
                        if (Object.keys(rest).length === 0) {
                          delete next.token_budget;
                        }
                      }
                      return next;
                    });
                  } else {
                    setBudgetField('max_tokens', Number(v));
                  }
                }}
                disabled={isPending}
                placeholder={String(globalBase.token_budget.max_tokens)}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </OverrideField>
            <OverrideField
              id={`${formId}-reserved`}
              label={t('settings.fields.reservedForUserScope')}
              inheriting={
                form.token_budget?.reserved_for_user_scope === undefined
              }
              inheritedValue={String(
                globalBase.token_budget.reserved_for_user_scope,
              )}
              error={errors['token_budget.reserved_for_user_scope']}
            >
              <input
                id={`${formId}-reserved`}
                type="number"
                value={form.token_budget?.reserved_for_user_scope ?? ''}
                min={0}
                max={mergedBudget.max_tokens}
                step={64}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === '') {
                    setForm((prev) => {
                      const next = { ...prev };
                      if (next.token_budget) {
                        const { reserved_for_user_scope: _r, ...rest } =
                          next.token_budget;
                        void _r;
                        next.token_budget = rest;
                        if (Object.keys(rest).length === 0) {
                          delete next.token_budget;
                        }
                      }
                      return next;
                    });
                  } else {
                    setBudgetField(
                      'reserved_for_user_scope',
                      Number(v),
                    );
                  }
                }}
                disabled={isPending}
                placeholder={String(
                  globalBase.token_budget.reserved_for_user_scope,
                )}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </OverrideField>
          </div>

          {/* Merged-budget hint */}
          {budgetInvalid && (
            <MemoryBanner
              tone="error"
              message={t('validation.reservedExceedsMax')}
            />
          )}

          {/* Footer */}
          <div className="flex items-center justify-end gap-2 pt-2 border-t border-border/40">
            {!isDirty && (
              <span className="text-[10px] font-mono text-muted-foreground">
                {t('settings.noChanges')}
              </span>
            )}
            {isDirty && (
              <button
                type="button"
                onClick={() => {
                  setForm(
                    initialOverride
                      ? computeEffectiveOverride(
                          globalBase,
                          initialOverride,
                        )
                      : {},
                  );
                  setErrors({});
                }}
                disabled={isPending}
                className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2.5 text-[11px] font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60 transition-colors"
              >
                <RotateCcw size={10} aria-hidden="true" />
                {t('common.reset')}
              </button>
            )}
            <button
              type="submit"
              disabled={!isDirty || isPending}
              className={cn(
                'inline-flex h-7 items-center gap-1 rounded-md bg-primary px-2.5 text-[11px] font-medium text-primary-foreground transition-all',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
                isDirty && !isPending && 'hover:scale-105',
              )}
            >
              <Save size={10} aria-hidden="true" />
              {isPending ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </form>
      </MemorySection>

      <ConfirmDangerDialog
        open={confirmRemoveOpen}
        onOpenChange={setConfirmRemoveOpen}
        title={t('settings.removeProjectOverride')}
        description={t('settings.removeOverrideConfirm')}
        confirmToken={projectId}
        confirmLabel={t('settings.removeProjectOverride')}
        isPending={deleteMutation.isPending}
        onConfirm={handleRemove}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Field wrapper (adds inherit / override indicator)
// ---------------------------------------------------------------------------

interface OverrideFieldProps {
  id: string;
  label: string;
  inheriting: boolean;
  inheritedValue: string;
  error?: string;
  children: React.ReactNode;
}

function OverrideField({
  id,
  label,
  inheriting,
  inheritedValue,
  error,
  children,
}: OverrideFieldProps) {
  return (
    <div className="space-y-1">
      <MemoryField
        label={label}
        htmlFor={id}
        error={error}
      >
        {children}
      </MemoryField>
      <p
        className={cn(
          'text-[10px] font-mono px-1 transition-colors',
          inheriting
            ? 'text-muted-foreground'
            : 'text-primary font-semibold',
        )}
      >
        {inheriting ? (
          <>
            · inheriting{' '}
            <span className="text-foreground">{inheritedValue}</span>
          </>
        ) : (
          '· overridden'
        )}
      </p>
    </div>
  );
}
