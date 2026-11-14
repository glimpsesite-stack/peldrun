// src/features/memory/components/ExportForm.tsx

/**
 * The `/memory/export` form.
 *
 * Design aligned with SearchTab:
 *
 *   * Wrapped in `<MemorySection>` with a `Download` icon.
 *   * All fields use `<MemoryField>` (card-as-input pattern).
 *   * Format picker is a two-card radio group (zip / json),
 *     colored by the same accent vocabulary as everything else.
 *   * Download / Inspect buttons match the "primary / outline"
 *     pair used by every other page.
 *
 * Scope UX
 * --------
 *
 * The scope_id field is AUTO-MANAGED for the `user` scope:
 *
 *   * On mount, if the initial scope is `user`, scope_id is set
 *     to `'default'` (the only valid value in the single-user
 *     backend).
 *   * When the user switches the scope type to `user`, the id
 *     is set to `'default'` and the input becomes disabled.
 *   * When the user switches AWAY from `user`, `'default'` is
 *     cleared (it would be meaningless for project / chat /
 *     agent scopes).
 *
 * For the `project` scope, the input offers `<datalist>` autocomplete
 * from `useProjectsList()`, so the user picks a real project id
 * instead of typing from memory.
 *
 * An Info icon next to the label opens a hover tooltip that
 * explains what a scope is, with a table of all four types. This
 * removes the "what should I type here?" trap entirely.
 */

'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Download,
  Eye,
  FileArchive,
  FileJson,
  Info,
} from 'lucide-react';

import type { ExportFormat, MemoryStatusFilter } from '../types/enums';
import { MemoryScope } from '../types/enums';
import {
  EXPORT_DEFAULT_ENTRIES,
  EXPORT_MAX_ENTRIES,
} from '../types/import-export';
import { useExportJson, useExportZip } from '../api/mutations';
import {
  useMemorySettings,
  useProjectsList,
} from '../api/queries';
import { cn } from '../lib/cn';
import { ScopeBadge } from './ScopeBadge';
import { MemorySection } from './MemorySection';
import { MemoryBanner } from './MemoryBanner';
import {
  MemoryField,
  MEMORY_FIELD_INPUT_CLASS,
} from './MemoryField';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ExportFormProps {
  initialScope?: { scope_type: MemoryScope; scope_id: string };
  allowScopeChange?: boolean;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ExportForm({
  initialScope = { scope_type: MemoryScope.PROJECT, scope_id: '' },
  allowScopeChange = true,
  className,
}: ExportFormProps) {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const projectsQuery = useProjectsList();
  const isDisabled = settingsQuery.data?.mode === 'disabled';

  const exportZip = useExportZip();
  const exportJson = useExportJson();

  const [scopeType, setScopeType] = useState<MemoryScope>(
    initialScope.scope_type,
  );
  const [scopeId, setScopeId] = useState<string>(
    // Auto-fill for user scope at construction time -- handles the
    // case where the caller passed `initialScope = {type: user}`
    // without an id (or with a stale id).
    initialScope.scope_type === MemoryScope.USER
      ? 'default'
      : initialScope.scope_id,
  );
  const [format, setFormat] = useState<ExportFormat>('zip');
  const [status, setStatus] = useState<MemoryStatusFilter>('all');
  const [limit, setLimit] = useState<number>(EXPORT_DEFAULT_ENTRIES);
  const [preview, setPreview] = useState<{
    manifest: Record<string, unknown>;
    memories: unknown[];
    truncated: boolean;
    totalMatched: number;
  } | null>(null);

  // Keep `default` in sync when the initial scope arrives
  // asynchronously (e.g. from a parent query). The check is
  // narrow: only reset if the initial scope is user AND the
  // current id is empty.
  useEffect(() => {
    if (
      initialScope.scope_type === MemoryScope.USER &&
      !scopeId
    ) {
      setScopeId('default');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialScope.scope_type]);

  const trimmedScopeId =
    scopeType === MemoryScope.USER ? 'default' : scopeId.trim();
  const formValid = trimmedScopeId.length > 0;
  const isPending = exportZip.isPending || exportJson.isPending;

  // ---- Handlers ---------------------------------------------------
  function handleScopeTypeChange(nextType: MemoryScope) {
    setScopeType(nextType);
    if (nextType === MemoryScope.USER) {
      // User scope always uses "default". Set it and let the
      // disabled input reflect the locked value.
      setScopeId('default');
    } else if (scopeId === 'default') {
      // Leaving user scope -- clear the now-invalid id so the
      // placeholder reappears and the user knows to type.
      setScopeId('');
    }
  }

  async function handleDownload() {
    if (!formValid) return;
    try {
      const result = await exportZip.mutateAsync({
        scope_type: scopeType,
        scope_id: trimmedScopeId,
        status,
        limit,
      });
      const url = URL.createObjectURL(result.blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = result.filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        URL.revokeObjectURL(url);
        a.remove();
      }, 500);
    } catch {
      /* error surfaced below */
    }
  }

  async function handleInspect() {
    if (!formValid) return;
    try {
      const result = await exportJson.mutateAsync({
        scope_type: scopeType,
        scope_id: trimmedScopeId,
        status,
        limit,
      });
      const entries = result.memories ?? [];
      setPreview({
        manifest: result.manifest ?? {},
        memories: entries,
        truncated: entries.length >= limit,
        totalMatched: entries.length,
      });
    } catch {
      /* error surfaced below */
    }
  }

  const error = exportZip.error ?? exportJson.error;

  // ---- Render -----------------------------------------------------
  return (
    <div className={className}>
      {isDisabled && (
        <MemoryBanner
          tone="warning"
          message={t('errors.mode_disabled')}
          className="mb-4"
        />
      )}

      <MemorySection
        icon={Download}
        title={t('export.title')}
        subtitle={t('export.description')}
        accent="primary"
        delay={0}
      >
        <div className="space-y-4">
          {/* ============ Scope ============ */}
          <div>
            {/* Label + Info tooltip */}
            <div className="mb-1.5 flex items-center gap-1.5">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {t('export.fields.scope')}
              </p>
              <ScopeTooltip />
            </div>

            {allowScopeChange ? (
              <div className="space-y-2">
                <div className="grid grid-cols-1 sm:grid-cols-[10rem_1fr] gap-2">
                  <select
                    value={scopeType}
                    onChange={(e) =>
                      handleScopeTypeChange(e.target.value as MemoryScope)
                    }
                    disabled={isPending || isDisabled}
                    className={cn(
                      MEMORY_FIELD_INPUT_CLASS,
                      'cursor-pointer border border-border/60 px-2 py-1.5',
                    )}
                  >
                    {Object.values(MemoryScope).map((s) => (
                      <option key={s} value={s}>
                        {t(`scope.${s}`)}
                      </option>
                    ))}
                  </select>
                  <input
                    type="text"
                    value={trimmedScopeId}
                    onChange={(e) => setScopeId(e.target.value)}
                    placeholder={getScopeIdPlaceholder(scopeType)}
                    list={
                      scopeType === MemoryScope.PROJECT
                        ? 'export-project-ids'
                        : undefined
                    }
                    disabled={
                      isPending ||
                      isDisabled ||
                      // user scope is auto-managed and read-only.
                      scopeType === MemoryScope.USER
                    }
                    className={cn(
                      MEMORY_FIELD_INPUT_CLASS,
                      'border border-border/60 px-2 py-1.5',
                      scopeType === MemoryScope.USER &&
                        'opacity-60 cursor-not-allowed',
                    )}
                    aria-describedby="export-scope-id-help"
                  />
                  {/* Autocomplete datalist for project ids */}
                  {scopeType === MemoryScope.PROJECT && (
                    <datalist id="export-project-ids">
                      {(projectsQuery.data?.projects ?? []).map((p) => (
                        <option key={p.project_id} value={p.project_id} />
                      ))}
                    </datalist>
                  )}
                </div>

                {/* Contextual hint under the input */}
                <p
                  id="export-scope-id-help"
                  className="text-[10px] leading-snug text-muted-foreground px-0.5"
                >
                  {getScopeIdHelp(
                    scopeType,
                    projectsQuery.data?.projects,
                  )}
                </p>
              </div>
            ) : (
              <ScopeBadge
                scope_type={scopeType}
                scope_id={
                  scopeType === MemoryScope.USER
                    ? undefined
                    : trimmedScopeId
                }
              />
            )}
          </div>

          {/* ============ Format ============ */}
          <div>
            <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {t('export.fields.format')}
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {(['zip', 'json'] as const).map((f) => {
                const selected = format === f;
                const Icon = f === 'zip' ? FileArchive : FileJson;
                const accentClass =
                  f === 'zip'
                    ? 'border-primary/60 bg-primary/5 text-primary'
                    : 'border-cyan-500/60 bg-cyan-500/5 text-cyan-600 dark:text-cyan-400';
                return (
                  <label
                    key={f}
                    className={cn(
                      'flex items-center gap-2 rounded-sm border p-3 text-xs cursor-pointer transition-colors',
                      selected
                        ? accentClass
                        : 'border-border/60 bg-card hover:bg-muted/40',
                      (isPending || isDisabled) &&
                        'cursor-not-allowed opacity-60',
                    )}
                  >
                    <input
                      type="radio"
                      name="export-format"
                      value={f}
                      checked={selected}
                      onChange={() => setFormat(f)}
                      disabled={isPending || isDisabled}
                      className="h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
                    />
                    <Icon size={14} aria-hidden="true" />
                    <span className="font-medium text-foreground">
                      {t(`export.formats.${f}`)}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>

          {/* ============ Status + limit ============ */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <MemoryField
              label={t('export.fields.status')}
              htmlFor="export-status"
            >
              <select
                id="export-status"
                value={status}
                onChange={(e) =>
                  setStatus(e.target.value as MemoryStatusFilter)
                }
                disabled={isPending || isDisabled}
                className={cn(MEMORY_FIELD_INPUT_CLASS, 'cursor-pointer')}
              >
                <option value="all">
                  {t('export.statusOptions.all')}
                </option>
                <option value="active">
                  {t('export.statusOptions.active')}
                </option>
              </select>
            </MemoryField>
            <MemoryField
              label={t('export.fields.limit')}
              htmlFor="export-limit"
              help={`Max ${EXPORT_MAX_ENTRIES.toLocaleString()} entries per export.`}
            >
              <input
                id="export-limit"
                type="number"
                value={limit}
                min={1}
                max={EXPORT_MAX_ENTRIES}
                step={100}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  if (
                    Number.isFinite(v) &&
                    v >= 1 &&
                    v <= EXPORT_MAX_ENTRIES
                  ) {
                    setLimit(v);
                  }
                }}
                disabled={isPending || isDisabled}
                className={MEMORY_FIELD_INPUT_CLASS}
              />
            </MemoryField>
          </div>

          {/* ============ Actions ============ */}
          <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-border/40">
            <button
              type="button"
              onClick={handleDownload}
              disabled={!formValid || isPending || isDisabled}
              className={cn(
                'inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground transition-all',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
                formValid && !isPending && !isDisabled && 'hover:scale-105',
              )}
            >
              <Download size={12} aria-hidden="true" />
              {exportZip.isPending
                ? t('common.loading')
                : t('export.actions.download')}
            </button>
            <button
              type="button"
              onClick={handleInspect}
              disabled={!formValid || isPending || isDisabled}
              className={cn(
                'inline-flex h-9 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground transition-all',
                'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            >
              <Eye size={12} aria-hidden="true" />
              {exportJson.isPending
                ? t('common.loading')
                : t('export.actions.inspect')}
            </button>
          </div>

          {error && (
            <MemoryBanner tone="error" message={error.message} />
          )}
        </div>
      </MemorySection>

      {/* ============ Preview panel ============ */}
      {preview && (
        <MemorySection
          icon={Eye}
          title={t('export.inspectPanel.title')}
          subtitle={t('export.inspectPanel.firstN', {
            shown: Math.min(preview.memories.length, 10),
            total: preview.totalMatched,
          })}
          accent="cyan"
          delay={0.1}
          className="mt-6"
        >
          {preview.truncated && (
            <MemoryBanner
              tone="warning"
              message={t('export.truncated', {
                limit,
                total: preview.totalMatched,
              })}
              className="mb-3"
            />
          )}
          <ul className="space-y-1">
            {preview.memories.slice(0, 10).map((m, i) => {
              const e = m as { memory_id: string; content: string };
              return (
                <li
                  key={e.memory_id ?? i}
                  className="truncate text-[11px] text-foreground/80 font-mono"
                  title={e.content}
                >
                  · {e.content?.slice(0, 120)}
                </li>
              );
            })}
          </ul>
        </MemorySection>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scope tooltip
// ---------------------------------------------------------------------------

/**
 * Hover tooltip attached to the "Scope" label.
 *
 * Pure CSS (`group` + `group-hover:opacity-100`): no JS state,
 * no portal, no layout shift. Positioned above the trigger with
 * a small arrow pointing down. `pointer-events-none` on the
 * tooltip prevents the hover state from flickering as the user
 * moves the mouse over the tooltip's own surface.
 *
 * The text is intentionally thorough: a user landing on the
 * export page for the first time does not know what "scope"
 * means, and the distinction between user / project / chat /
 * agent is not obvious from the four labels alone.
 */
function ScopeTooltip() {
  const { t } = useTranslation('memory');
  return (
    <div className="group relative flex items-center">
      <Info
        size={12}
        className="text-muted-foreground cursor-help hover:text-foreground transition-colors"
        aria-label={t('export.scope.tooltip.title')}
        role="img"
      />
      <div
        role="tooltip"
        className={cn(
          'pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 w-72 -translate-x-1/2',
          'rounded-md border border-border bg-popover p-3 text-[11px] leading-relaxed text-popover-foreground shadow-lg',
          'opacity-0 transition-opacity duration-200 group-hover:opacity-100',
        )}
      >
        <div className="font-semibold text-foreground mb-1.5">
          {t('export.scope.tooltip.title')}
        </div>
        <p className="text-muted-foreground mb-2">
          {t('export.scope.tooltip.intro')}
        </p>
        <ul className="space-y-1">
          <li>
            <strong className="text-foreground">
              {t('export.scope.tooltip.user_label')}
            </strong>{' '}
            — {t('export.scope.tooltip.user_desc')}
          </li>
          <li>
            <strong className="text-foreground">
              {t('export.scope.tooltip.project_label')}
            </strong>{' '}
            — {t('export.scope.tooltip.project_desc')}
          </li>
          <li>
            <strong className="text-foreground">
              {t('export.scope.tooltip.chat_label')}
            </strong>{' '}
            — {t('export.scope.tooltip.chat_desc')}
          </li>
          <li>
            <strong className="text-foreground">
              {t('export.scope.tooltip.agent_label')}
            </strong>{' '}
            — {t('export.scope.tooltip.agent_desc')}
          </li>
        </ul>
        <div
          className="absolute top-full left-1/2 -mt-1 h-2 w-2 -translate-x-1/2 rotate-45 border-b border-r border-border bg-popover"
          aria-hidden="true"
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scope helpers
// ---------------------------------------------------------------------------

function getScopeIdPlaceholder(scopeType: MemoryScope): string {
  switch (scopeType) {
    case MemoryScope.USER:
      return 'default';
    case MemoryScope.PROJECT:
      return 'acme';
    case MemoryScope.CHAT:
      return 'chat_9b66e9b2faa5';
    case MemoryScope.AGENT:
      return 'peldrun';
    default:
      return 'identifier';
  }
}

function getScopeIdHelp(
  scopeType: MemoryScope,
  projects?: Array<{ project_id: string }>,
): string {
  switch (scopeType) {
    case MemoryScope.USER:
      return 'Personal scope always uses "default" (single-user backend). The field is locked automatically — no input needed.';
    case MemoryScope.PROJECT: {
      const ids = (projects ?? []).slice(0, 3).map((p) => p.project_id);
      if (ids.length === 0) {
        return 'The project id to export. Projects with memories appear as autocomplete suggestions as you type.';
      }
      return `The project id to export. Available: ${ids
        .map((id) => `"${id}"`)
        .join(', ')}${
        (projects?.length ?? 0) > 3
          ? ` (+${(projects?.length ?? 0) - 3} more)`
          : ''
      }.`;
    }
    case MemoryScope.CHAT:
      return 'The chat id (e.g. "chat_9b66e9b2faa5"). Find it in the chat URL or session.json.';
    case MemoryScope.AGENT:
      return 'The agent id (e.g. "peldrun", "researcher"). Find it in the agents list.';
    default:
      return 'The identifier of the scope to export.';
  }
}
