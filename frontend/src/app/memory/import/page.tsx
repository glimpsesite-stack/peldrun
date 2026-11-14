// src/app/memory/import/page.tsx

/**
 * Import wizard (`/memory/import`).
 *
 * Design aligned with SearchTab:
 *
 *   * No outer padding wrapper -- the parent `MemoryLayout`'s
 *     `<main>` already constrains width (`max-w-7xl`) and padding
 *     (`p-6`). The page only renders its content column.
 *   * Sections use `MemorySection` with distinct accent colors
 *     for each step (primary → cyan → emerald as the flow
 *     progresses).
 *   * The step indicator is a horizontal progress bar with
 *     numbered circles -- mirrors the "Sections have numbered
 *     indicators" pattern from SearchTab.
 *   * Errors use `MemoryBanner` for tone consistency.
 *
 * Behaviour unchanged:
 *
 *   * 3-step state machine (select → preview → result).
 *   * File preserved across select→preview→apply.
 *   * `<datalist>` for scope autocomplete.
 *   * Back from preview preserves inputs.
 */

'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileText,
  FolderOpen,
  Settings2,
  Upload,
  User as UserIcon,
} from 'lucide-react';

import type { ImportFileInfo } from '../../../features/memory/types/import-export';
import type {
  ImportAdapter,
  ImportPreview,
} from '../../../features/memory/types/responses';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import { MemoryScope } from '../../../features/memory/types/enums';
import {
  useImportAdapters,
  useMemorySettings,
  useProjectsList,
} from '../../../features/memory/api/queries';
import {
  useImportApply,
  useImportPreview,
  type ImportMutationVariables,
} from '../../../features/memory/api/mutations';
import { cn } from '../../../features/memory/lib/cn';
import { ImportDropZone } from '../../../features/memory/components/ImportDropZone';
import { ImportPreviewTable } from '../../../features/memory/components/ImportPreviewTable';
import { MemorySection } from '../../../features/memory/components/MemorySection';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';
import {
  MemoryField,
  MEMORY_FIELD_INPUT_CLASS,
} from '../../../features/memory/components/MemoryField';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type WizardStep = 'select' | 'preview' | 'result';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ImportPage() {
  const { t } = useTranslation('memory');

  // ---- Queries --------------------------------------------------
  const settingsQuery = useMemorySettings();
  const projectsQuery = useProjectsList();
  const adaptersQuery = useImportAdapters();

  const mode = settingsQuery.data?.mode;
  const canImport = mode !== undefined && mode !== 'disabled';

  // ---- Mutations ------------------------------------------------
  const previewMutation = useImportPreview();
  const applyMutation = useImportApply();

  // ---- Wizard state ---------------------------------------------
  const [step, setStep] = useState<WizardStep>('select');
  const [file, setFile] = useState<File | null>(null);
  const [fileInfo, setFileInfo] = useState<ImportFileInfo | null>(null);
  const [scopeType, setScopeType] = useState<MemoryScope>(
    MemoryScope.PROJECT,
  );
  const [scopeId, setScopeId] = useState('');
  const [adapterId, setAdapterId] = useState('');
  const [rejectionError, setRejectionError] = useState<string | null>(null);

  // ---- Derived --------------------------------------------------
  const effectiveScopeId =
    scopeType === MemoryScope.USER ? 'default' : scopeId.trim();

  const canSubmit =
    canImport &&
    file !== null &&
    effectiveScopeId.length > 0 &&
    !previewMutation.isPending;

  const projectIds = useMemo(
    () => (projectsQuery.data?.projects ?? []).map((p) => p.project_id),
    [projectsQuery.data?.projects],
  );

  const preview: ImportPreview | null = previewMutation.data?.preview ?? null;
  const applyResult = applyMutation.data?.result ?? null;

  // ---- Handlers --------------------------------------------------
  function buildVariables(): ImportMutationVariables | null {
    if (!file) return null;
    if (effectiveScopeId.length === 0) return null;
    return {
      kind: 'file',
      request: {
        file,
        target: { scope_type: scopeType, scope_id: effectiveScopeId },
        adapter_id: adapterId === '' ? undefined : adapterId,
      },
    };
  }

  async function handlePreview() {
    const vars = buildVariables();
    if (!vars) return;
    try {
      await previewMutation.mutateAsync(vars);
      setStep('preview');
    } catch {
      /* error surfaces via mutation state */
    }
  }

  async function handleApply() {
    const vars = buildVariables();
    if (!vars) return;
    try {
      await applyMutation.mutateAsync(vars);
      setStep('result');
    } catch {
      /* error surfaces via mutation state */
    }
  }

  function handleImportAnother() {
    setStep('select');
    setFile(null);
    setFileInfo(null);
    setRejectionError(null);
    previewMutation.reset();
    applyMutation.reset();
  }

  // ---- Render ---------------------------------------------------
  return (
    <div className="space-y-6">
      {/* ============ Title ============ */}
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('import.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('import.description')}
        </p>
      </div>

      {/* ============ Step indicator ============ */}
      <StepIndicator current={step} />

      {/* ============ Disabled banner ============ */}
      {!canImport && (
        <MemoryBanner
          tone="warning"
          message={t('errors.mode_disabled')}
          action={
            <Link href="/memory/settings" className="underline">
              {t('errors.withSettings')}
            </Link>
          }
        />
      )}

      {/* ============ Step 1: Select ============ */}
      {step === 'select' && (
        <>
          <MemorySection
            icon={Settings2}
            title={t('import.upload.scopeLabel')}
            subtitle={t('scope.scopeType')}
            accent="primary"
            delay={0}
          >
            <div className="space-y-4">
              {/* Scope type radios */}
              <div className="flex gap-3">
                {(
                  [
                    {
                      value: MemoryScope.PROJECT,
                      icon: FolderOpen,
                      label: t('scope.project'),
                    },
                    {
                      value: MemoryScope.USER,
                      icon: UserIcon,
                      label: t('scope.user'),
                    },
                  ] as const
                ).map(({ value, icon: Icon, label }) => (
                  <label
                    key={value}
                    className={cn(
                      'flex flex-1 cursor-pointer items-center gap-2 rounded-sm border p-3 text-xs transition-colors',
                      scopeType === value
                        ? 'border-primary/60 bg-primary/5'
                        : 'border-border/60 bg-card hover:bg-muted/40',
                      !canImport && 'cursor-not-allowed opacity-60',
                    )}
                  >
                    <input
                      type="radio"
                      name="import-scope-type"
                      value={value}
                      checked={scopeType === value}
                      onChange={() => setScopeType(value)}
                      disabled={!canImport}
                      className="h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
                    />
                    <Icon size={11} aria-hidden="true" />
                    <span className="font-medium text-foreground">
                      {label}
                    </span>
                  </label>
                ))}
              </div>

              {/* Scope id */}
              {scopeType === MemoryScope.PROJECT && (
                <MemoryField
                  label={t('scope.scopeId')}
                  htmlFor="import-scope-id"
                  help="Type a project id or pick from existing projects."
                >
                  <input
                    id="import-scope-id"
                    type="text"
                    list="import-project-ids"
                    value={scopeId}
                    onChange={(e) => setScopeId(e.target.value)}
                    placeholder="acme"
                    disabled={!canImport || previewMutation.isPending}
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                  <datalist id="import-project-ids">
                    {projectIds.map((id) => (
                      <option key={id} value={id} />
                    ))}
                  </datalist>
                </MemoryField>
              )}
            </div>
          </MemorySection>

          <MemorySection
            icon={Upload}
            title={t('import.upload.selectFile')}
            subtitle={t('import.upload.acceptedFormats', {
              formats: '.zip .json .md .db',
            })}
            accent="cyan"
            delay={0.05}
          >
            <div className="space-y-3">
              {!file || !fileInfo ? (
                <ImportDropZone
                  onFileSelected={(f, info) => {
                    setFile(f);
                    setFileInfo(info);
                    setRejectionError(null);
                  }}
                  onRejected={(_reason, message) => {
                    setRejectionError(message);
                    setFile(null);
                    setFileInfo(null);
                  }}
                  disabled={!canImport || previewMutation.isPending}
                />
              ) : (
                <div className="flex items-center justify-between gap-2 rounded-md border border-border/60 bg-card p-3">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <div
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary"
                      aria-hidden="true"
                    >
                      <FileText size={14} strokeWidth={1.8} />
                    </div>
                    <div className="min-w-0">
                      <p className="truncate font-mono text-[11px] font-semibold text-foreground">
                        {fileInfo.filename}
                      </p>
                      <p className="text-[10px] text-muted-foreground">
                        {(fileInfo.sizeBytes / 1024).toFixed(1)} KB
                        {fileInfo.detectedAdapter && (
                          <> · {fileInfo.detectedAdapter}</>
                        )}
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      setFile(null);
                      setFileInfo(null);
                    }}
                    disabled={previewMutation.isPending}
                    className="shrink-0 rounded-md border border-border bg-card px-2.5 py-1 text-[10px] font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60 transition-colors"
                  >
                    {t('common.reset')}
                  </button>
                </div>
              )}

              {rejectionError && (
                <MemoryBanner tone="error" message={rejectionError} />
              )}
            </div>
          </MemorySection>

          <MemorySection
            icon={Settings2}
            title={t('import.upload.adapterLabel')}
            subtitle="Auto-detect picks the right parser from the file's magic bytes."
            accent="amber"
            delay={0.1}
          >
            <MemoryField
              label={t('import.upload.adapterLabel')}
              htmlFor="import-adapter"
            >
              <select
                id="import-adapter"
                value={adapterId}
                onChange={(e) => setAdapterId(e.target.value)}
                disabled={
                  !canImport ||
                  previewMutation.isPending ||
                  adaptersQuery.isLoading
                }
                className={cn(MEMORY_FIELD_INPUT_CLASS, 'cursor-pointer')}
              >
                <option value="">{t('import.upload.adapterAuto')}</option>
                {(adaptersQuery.data?.adapters ?? []).map(
                  (a: ImportAdapter) => (
                    <option key={a.adapter_id} value={a.adapter_id}>
                      {a.adapter_id} — {a.description}
                    </option>
                  ),
                )}
              </select>
            </MemoryField>
          </MemorySection>

          {previewMutation.error && (
            <MemoryBanner
              tone="error"
              message={previewMutation.error.message}
            />
          )}

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={handlePreview}
              disabled={!canSubmit}
              className={cn(
                'inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground transition-all',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
                canSubmit && 'hover:scale-105',
              )}
            >
              <ArrowRight size={12} aria-hidden="true" />
              {previewMutation.isPending
                ? t('common.loading')
                : t('import.preview.title')}
            </button>
          </div>
        </>
      )}

      {/* ============ Step 2: Preview ============ */}
      {step === 'preview' && preview && (
        <>
          <MemorySection
            icon={FileText}
            title={t('import.preview.title')}
            subtitle={t('import.preview.candidates')}
            accent="cyan"
            delay={0}
          >
            <ImportPreviewTable preview={preview} />
          </MemorySection>

          {applyMutation.error && (
            <MemoryBanner
              tone="error"
              message={applyMutation.error.message}
            />
          )}

          <div className="flex items-center justify-between gap-2 pt-2">
            <button
              type="button"
              onClick={() => setStep('select')}
              disabled={applyMutation.isPending}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 transition-colors"
            >
              <ArrowLeft size={12} aria-hidden="true" />
              {t('import.preview.back')}
            </button>
            <button
              type="button"
              onClick={handleApply}
              disabled={applyMutation.isPending}
              className={cn(
                'inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground transition-all',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
                !applyMutation.isPending && 'hover:scale-105',
              )}
            >
              <Upload size={12} aria-hidden="true" />
              {applyMutation.isPending
                ? t('common.loading')
                : t('import.preview.confirm')}
            </button>
          </div>
        </>
      )}

      {/* ============ Step 3: Result ============ */}
      {step === 'result' && applyResult && (
        <>
          <MemorySection
            icon={CheckCircle2}
            title={t('import.result.title')}
            subtitle={t('import.result.summary', {
              created: applyResult.created,
              skipped: applyResult.skipped,
              errors: applyResult.errors.length,
            })}
            accent="emerald"
            delay={0}
          >
            <div className="grid grid-cols-3 gap-3">
              <ResultStat
                label="Created"
                value={applyResult.created}
                accent="emerald"
              />
              <ResultStat
                label="Skipped"
                value={applyResult.skipped}
                accent="slate"
              />
              <ResultStat
                label="Errors"
                value={applyResult.errors.length}
                accent={
                  applyResult.errors.length > 0 ? 'rose' : 'slate'
                }
              />
            </div>

            {applyResult.errors.length > 0 && (
              <div className="mt-4 rounded-sm border border-destructive/40 bg-destructive/10 p-3">
                <p className="mb-2 text-[11px] font-semibold text-destructive">
                  {t('import.result.errors', {
                    count: applyResult.errors.length,
                  })}
                </p>
                <ul className="space-y-1">
                  {applyResult.errors.slice(0, 5).map((e, i) => (
                    <li
                      key={i}
                      className="text-[10px] leading-snug text-destructive/90 font-mono"
                    >
                      · {e}
                    </li>
                  ))}
                  {applyResult.errors.length > 5 && (
                    <li className="text-[10px] italic text-destructive/70">
                      · ... {applyResult.errors.length - 5} more
                    </li>
                  )}
                </ul>
              </div>
            )}
          </MemorySection>

          <div className="flex items-center justify-end gap-2 pt-2">
            {scopeType === MemoryScope.PROJECT && (
              <Link
                href={`/memory/projects/${encodeURIComponent(effectiveScopeId)}`}
                className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 transition-colors"
              >
                <FolderOpen size={12} aria-hidden="true" />
                {t('import.result.viewProject')}
              </Link>
            )}
            <button
              type="button"
              onClick={handleImportAnother}
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground hover:bg-primary/90 hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 transition-all"
            >
              <Upload size={12} aria-hidden="true" />
              {t('import.result.importAnother')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step indicator
// ---------------------------------------------------------------------------

interface StepIndicatorProps {
  current: WizardStep;
}

function StepIndicator({ current }: StepIndicatorProps) {
  const { t } = useTranslation('memory');
  const steps: Array<{ key: WizardStep; labelKey: string }> = [
    { key: 'select', labelKey: 'import.steps.select' },
    { key: 'preview', labelKey: 'import.steps.preview' },
    { key: 'result', labelKey: 'import.steps.result' },
  ];
  const currentIndex = steps.findIndex((s) => s.key === current);

  return (
    <ol
      className="flex items-center gap-2 rounded-md border border-border/60 bg-card px-4 py-3"
      aria-label={t('import.title')}
    >
      {steps.map((s, i) => {
        const active = i === currentIndex;
        const done = i < currentIndex;
        return (
          <li key={s.key} className="flex items-center gap-2">
            <span
              className={cn(
                'flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-semibold transition-colors',
                active && 'bg-primary text-primary-foreground shadow-sm',
                done && 'bg-emerald-600 text-white',
                !active && !done && 'bg-muted text-muted-foreground',
              )}
              aria-current={active ? 'step' : undefined}
            >
              {done ? <CheckCircle2 size={13} /> : i + 1}
            </span>
            <span
              className={cn(
                'text-[11px] font-medium',
                active ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              {t(s.labelKey)}
            </span>
            {i < steps.length - 1 && (
              <span
                aria-hidden="true"
                className={cn(
                  'mx-1 h-px w-6',
                  done ? 'bg-emerald-500' : 'bg-border',
                )}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Result stat pill
// ---------------------------------------------------------------------------

interface ResultStatProps {
  label: string;
  value: number;
  accent: 'emerald' | 'slate' | 'rose';
}

function ResultStat({ label, value, accent }: ResultStatProps) {
  const styles =
    accent === 'emerald'
      ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400'
      : accent === 'rose'
        ? 'border-rose-500/30 bg-rose-500/5 text-rose-600 dark:text-rose-400'
        : 'border-border/60 bg-card text-foreground';

  return (
    <div className={cn('rounded-sm border p-3', styles)}>
      <p className="text-[10px] font-semibold uppercase tracking-wider opacity-80">
        {label}
      </p>
      <p className="mt-1 text-xl font-bold font-mono tabular-nums">
        {value}
      </p>
    </div>
  );
}
