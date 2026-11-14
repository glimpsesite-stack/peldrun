// src/app/memory/[memory_id]/page.tsx

/**
 * Entry detail page (`/memory/[memory_id]`).
 *
 * Layout (design aligned with SearchTab):
 *
 *   * Header: back-link + action row.
 *   * Badges row (type, scope, pinned, status).
 *   * Content section -- the entry body rendered as Markdown.
 *   * Metadata section -- importance bar, tags, confidence, timestamps.
 *   * Provenance section -- source type + source chat + import origin.
 *
 * Sections use `MemorySection` (bg-gigbg surface + accent line +
 * staggered FADE_UP). Values use the same uppercase-label /
 * mono-value typography as the settings form.
 *
 * Actions preserved from the pre-redesign version:
 *
 *   * Pin / unpin -- immediate; optimistic mutation.
 *   * Edit -- opens EntryEditor in edit mode.
 *   * Promote -- copies into user scope (source stays).
 *   * Archive -- soft delete; navigates back.
 *   * Delete -- hard delete with type-to-confirm.
 *
 * Keyboard shortcuts (from F9.8):
 *
 *   * E -- edit.
 *   * P -- pin / unpin.
 *   * B -- back to browse.
 *
 * Suspense wrapper is required by Next.js App Router because this
 * page uses `useSearchParams()`.
 */

'use client';

import { Suspense, useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useSearchParams, useRouter } from 'next/navigation';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  ArrowLeft,
  Archive,
  Check,
  Copy,
  Pencil,
  Pin,
  PinOff,
  Trash2,
  AlertTriangle,
  FileText,
  History,
  Info,
  type LucideIcon,
} from 'lucide-react';

import { importanceTier } from '../../../features/memory/components/ImportanceSlider';
import {
  useEntryDetail,
  useMemorySettings,
} from '../../../features/memory/api/queries';
import {
  useDeleteEntry,
  usePinEntry,
  usePromoteEntry,
} from '../../../features/memory/api/mutations';
import { HARD_DELETE_CONFIRM_TOKEN } from '../../../features/memory/lib/constants';
import { cn } from '../../../features/memory/lib/cn';
import { TypeBadge } from '../../../features/memory/components/TypeBadge';
import { ScopeBadge } from '../../../features/memory/components/ScopeBadge';
import { EntryEditor } from '../../../features/memory/components/EntryEditor';
import { useKeyboardShortcuts } from '../../../features/memory/hooks/useKeyboardShortcuts';
import { ConfirmDangerDialog } from '../../../features/memory/components/ConfirmDangerDialog';
import { EmptyState } from '../../../features/memory/components/EmptyState';
import { MemorySection } from '../../../features/memory/components/MemorySection';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';

// ---------------------------------------------------------------------------
// Suspense wrapper
// ---------------------------------------------------------------------------

export default function MemoryDetailPage() {
  return (
    <Suspense fallback={<DetailSkeleton />}>
      <DetailContent />
    </Suspense>
  );
}

// ---------------------------------------------------------------------------
// Main content
// ---------------------------------------------------------------------------

function DetailContent() {
  const { t, i18n } = useTranslation('memory');
  const router = useRouter();
  const params = useParams<{ memory_id?: string | string[] }>();
  const searchParams = useSearchParams();

  const memoryId = Array.isArray(params?.memory_id)
    ? params.memory_id[0]
    : params?.memory_id;
  const scopeString = searchParams?.get('scope') ?? '';

  const settingsQuery = useMemorySettings();
  const canWrite = settingsQuery.data?.mode === 'read_write';

  const detailQuery = useEntryDetail(memoryId, scopeString);
  const deleteMutation = useDeleteEntry();
  const pinMutation = usePinEntry();
  const promoteMutation = usePromoteEntry();

  const [editorOpen, setEditorOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [flash, setFlash] = useState<'promoted' | null>(null);

  // Auto-clear the success flash.
  useEffect(() => {
    if (!flash) return undefined;
    const handle = setTimeout(() => setFlash(null), 3000);
    return () => clearTimeout(handle);
  }, [flash]);

  const entry = detailQuery.data?.memory;

  // ---- Keyboard shortcuts ----------------------------------------
  useKeyboardShortcuts([
    {
      keys: 'e',
      handler: () => {
        if (canWrite && entry) setEditorOpen(true);
      },
    },
    {
      keys: 'p',
      handler: () => {
        if (!canWrite || !entry || pinMutation.isPending) return;
        void pinMutation
          .mutateAsync({
            memoryId: entry.memory_id,
            scope_type: entry.scope_type,
            scope_id: entry.scope_id,
            pinned: !entry.is_pinned,
          })
          .catch(() => {
            // Error surfaces via mutation state.
          });
      },
    },
    {
      keys: 'b',
      handler: () => {
        if (!scopeString) return;
        router.push(
          `/memory/browse?scope=${encodeURIComponent(scopeString)}`,
        );
      },
    },
  ]);

  // ---- Missing scope ----------------------------------------------
  if (!scopeString) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title={t('errors.scope_required')}
        description={t('scope.selectScope')}
        size="spacious"
        action={
          <Link
            href="/memory/browse"
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            <ArrowLeft size={12} aria-hidden="true" />
            {t('entry.detail.backToList')}
          </Link>
        }
      />
    );
  }

  // ---- Loading ----------------------------------------------------
  if (detailQuery.isLoading) {
    return <DetailSkeleton />;
  }

  // ---- Error / not found -----------------------------------------
  if (detailQuery.error || !entry) {
    const isNotFound = detailQuery.error?.errorType === 'not_found';
    return (
      <EmptyState
        icon={AlertTriangle}
        title={
          isNotFound ? t('entry.detail.notFound') : t('errors.generic')
        }
        description={detailQuery.error?.message}
        size="spacious"
        action={
          <Link
            href={`/memory/browse?scope=${encodeURIComponent(scopeString)}`}
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            <ArrowLeft size={12} aria-hidden="true" />
            {t('entry.detail.backToList')}
          </Link>
        }
      />
    );
  }

  // ---- Handlers ---------------------------------------------------
  async function handlePinToggle() {
    if (!entry) return;
    try {
      await pinMutation.mutateAsync({
        memoryId: entry.memory_id,
        scope_type: entry.scope_type,
        scope_id: entry.scope_id,
        pinned: !entry.is_pinned,
      });
    } catch {
      // error surfaced via mutation state
    }
  }

  async function handleArchive() {
    if (!entry) return;
    try {
      await deleteMutation.mutateAsync({
        memoryId: entry.memory_id,
        scope_type: entry.scope_type,
        scope_id: entry.scope_id,
        hard: false,
      });
      router.push(
        `/memory/browse?scope=${encodeURIComponent(scopeString)}`,
      );
    } catch {
      // error surfaced in the dialog
    }
  }

  async function handleDelete() {
    if (!entry) return;
    try {
      await deleteMutation.mutateAsync({
        memoryId: entry.memory_id,
        scope_type: entry.scope_type,
        scope_id: entry.scope_id,
        hard: true,
      });
      router.push(
        `/memory/browse?scope=${encodeURIComponent(scopeString)}`,
      );
    } catch {
      // error surfaced in the dialog
    }
  }

  async function handlePromote() {
    if (!entry) return;
    try {
      await promoteMutation.mutateAsync({
        params: {
          memoryId: entry.memory_id,
          scope_type: entry.scope_type,
          scope_id: entry.scope_id,
        },
        body: { target_scope_type: 'user', target_scope_id: 'default' },
      });
      setPromoteOpen(false);
      setFlash('promoted');
    } catch {
      // error surfaced in the dialog
    }
  }

  // ---- Render -----------------------------------------------------
  const tier = importanceTier(entry.importance);
  const backHref = `/memory/browse?scope=${encodeURIComponent(scopeString)}`;

  return (
    <div className="space-y-6">
      {/* ============ Flash banner ============ */}
      {flash === 'promoted' && (
        <MemoryBanner
          tone="success"
          message={t('entry.promote.success')}
        />
      )}

      {/* ============ Header ============ */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          href={backHref}
          className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          <ArrowLeft size={11} aria-hidden="true" />
          {t('entry.detail.backToList')}
        </Link>

        <div className="flex flex-wrap items-center gap-1">
          <ActionButton
            icon={entry.is_pinned ? PinOff : Pin}
            label={
              entry.is_pinned
                ? `${t('entry.actions.unpin')} (P)`
                : `${t('entry.actions.pin')} (P)`
            }
            onClick={handlePinToggle}
            disabled={!canWrite || pinMutation.isPending}
            active={entry.is_pinned}
          />
          <ActionButton
            icon={Pencil}
            label={`${t('entry.actions.edit')} (E)`}
            onClick={() => setEditorOpen(true)}
            disabled={!canWrite}
          />
          <ActionButton
            icon={Copy}
            label={t('entry.actions.promote')}
            onClick={() => setPromoteOpen(true)}
            disabled={!canWrite}
          />
          <ActionButton
            icon={Archive}
            label={t('entry.actions.archive')}
            onClick={() => setArchiveOpen(true)}
            disabled={!canWrite || deleteMutation.isPending}
          />
          <ActionButton
            icon={Trash2}
            label={t('entry.actions.delete')}
            onClick={() => setDeleteOpen(true)}
            disabled={!canWrite || deleteMutation.isPending}
            danger
          />
        </div>
      </div>

      {/* ============ Badges row ============ */}
      <div className="flex flex-wrap items-center gap-1.5">
        <TypeBadge memory_type={entry.memory_type} size="md" />
        <ScopeBadge
          scope_type={entry.scope_type}
          scope_id={
            entry.scope_type === 'user' ? undefined : entry.scope_id
          }
          size="md"
        />
        {entry.is_pinned && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-1 text-[11px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
            <Pin size={10} strokeWidth={2.5} aria-hidden="true" />
            {t('entry.fields.pinned')}
          </span>
        )}
        {entry.status !== 'active' && (
          <span className="inline-flex items-center rounded-full bg-muted px-2.5 py-1 text-[11px] font-medium text-muted-foreground">
            {t(`status.${entry.status}`)}
          </span>
        )}
      </div>

      {/* ============ Content ============ */}
      <MemorySection
        icon={FileText}
        title={t('entry.detail.title')}
        subtitle={`${entry.content.length} chars`}
        accent="primary"
        delay={0}
      >
        <article className="prose-memory text-sm leading-relaxed text-foreground">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {entry.content}
          </ReactMarkdown>
        </article>
      </MemorySection>

      {/* ============ Metadata ============ */}
      <MemorySection
        icon={Info}
        title={t('entry.fields.importance')}
        subtitle={t(`entry.importanceLevels.${tier}`)}
        accent="cyan"
        delay={0.05}
      >
        <div className="space-y-4">
          {/* Importance bar */}
          <ImportanceBar value={entry.importance} tier={tier} />

          {/* Two-column metadata grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-border/40">
            {entry.tags.length > 0 && (
              <MetadataRow label={t('entry.fields.tags')} span={2}>
                <div className="flex flex-wrap gap-1">
                  {entry.tags.map((tag) => (
                    <span
                      key={tag}
                      className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground font-mono"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </MetadataRow>
            )}

            <MetadataRow label={t('entry.fields.confidence')}>
              <span className="tabular-nums text-foreground font-mono">
                {Math.round(entry.confidence * 100)}%
              </span>
            </MetadataRow>

            <MetadataRow label={t('entry.fields.updatedAt')}>
              <span className="text-foreground text-[11px]">
                {formatDateTime(entry.updated_at, t, i18n.language)}
              </span>
            </MetadataRow>

            <MetadataRow label={t('entry.fields.createdAt')}>
              <span className="text-foreground text-[11px]">
                {formatDateTime(entry.created_at, t, i18n.language)}
              </span>
            </MetadataRow>

            <MetadataRow label={t('entry.fields.lastUsedAt')}>
              <span className="text-foreground text-[11px]">
                {entry.last_used_at
                  ? formatDateTime(entry.last_used_at, t, i18n.language)
                  : t('dates.never')}
              </span>
            </MetadataRow>

            {entry.expires_at && (
              <MetadataRow label={t('entry.fields.expiresAt')}>
                <span className="text-foreground text-[11px]">
                  {formatDateTime(entry.expires_at, t, i18n.language)}
                </span>
              </MetadataRow>
            )}
          </div>
        </div>
      </MemorySection>

      {/* ============ Provenance ============ */}
      <MemorySection
        icon={History}
        title={t('entry.fields.provenance')}
        subtitle={t(`sourceType.${entry.provenance.source_type}`, {
          defaultValue: entry.provenance.source_type,
        })}
        accent="purple"
        delay={0.1}
      >
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <MetadataRow label={t('sourceType.label')}>
            <span className="text-foreground text-[11px]">
              {t(`sourceType.${entry.provenance.source_type}`, {
                defaultValue: entry.provenance.source_type,
              })}
            </span>
          </MetadataRow>

          {entry.provenance.source_chat_id && (
            <MetadataRow label={t('scope.chat')}>
              <span className="font-mono text-foreground text-[11px]">
                {entry.provenance.source_chat_id}
              </span>
            </MetadataRow>
          )}

          {entry.provenance.imported_from && (
            <MetadataRow label="Imported from" span={2}>
              <span className="font-mono text-foreground text-[11px]">
                {entry.provenance.imported_from}
              </span>
            </MetadataRow>
          )}
        </div>
      </MemorySection>

      {/* ============ Dialogs ============ */}
      <EntryEditor
        open={editorOpen}
        onOpenChange={setEditorOpen}
        mode="edit"
        entry={entry}
        scope={{
          scope_type: entry.scope_type,
          scope_id: entry.scope_id,
        }}
      />

      <ConfirmDangerDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={t('entry.delete.softTitle')}
        description={t('entry.delete.softDescription')}
        confirmToken={entry.memory_id.slice(-6)}
        confirmLabel={t('entry.actions.archive')}
        isPending={deleteMutation.isPending}
        onConfirm={handleArchive}
      />

      <ConfirmDangerDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={t('entry.delete.hardTitle')}
        description={t('entry.delete.hardDescription')}
        confirmToken={HARD_DELETE_CONFIRM_TOKEN}
        confirmPlaceholder={t('entry.delete.hardConfirmPlaceholder')}
        confirmLabel={t('common.delete')}
        isPending={deleteMutation.isPending}
        onConfirm={handleDelete}
      />

      <ConfirmDangerDialog
        open={promoteOpen}
        onOpenChange={setPromoteOpen}
        title={t('entry.promote.title')}
        description={t('entry.promote.description')}
        confirmToken="COPY"
        confirmLabel={t('entry.actions.promote')}
        isPending={promoteMutation.isPending}
        onConfirm={handlePromote}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface ActionButtonProps {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  danger?: boolean;
}

function ActionButton({
  icon: Icon,
  label,
  onClick,
  disabled = false,
  active = false,
  danger = false,
}: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={active || undefined}
      title={label}
      className={cn(
        'inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-[11px] font-medium transition-all',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
        'disabled:cursor-not-allowed disabled:opacity-50',
        !disabled && 'hover:scale-105',
        danger
          ? 'text-destructive hover:bg-destructive/10'
          : active
            ? 'text-amber-700 hover:bg-amber-100/60 dark:text-amber-400 dark:hover:bg-amber-900/20'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      <Icon size={11} strokeWidth={2.2} aria-hidden="true" />
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}

interface MetadataRowProps {
  label: string;
  span?: 1 | 2;
  children: React.ReactNode;
}

function MetadataRow({ label, span = 1, children }: MetadataRowProps) {
  return (
    <div
      className={cn(
        'grid grid-cols-[7rem_1fr] items-baseline gap-2 text-[11px]',
        span === 2 && 'sm:col-span-2',
      )}
    >
      <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <div className="min-w-0" suppressHydrationWarning>
        {children}
      </div>
    </div>
  );
}

interface ImportanceBarProps {
  value: number;
  tier: 'low' | 'medium' | 'high';
}

function ImportanceBar({ value, tier }: ImportanceBarProps) {
  const { t } = useTranslation('memory');
  const colorClass =
    tier === 'high'
      ? 'bg-red-500'
      : tier === 'medium'
        ? 'bg-amber-500'
        : 'bg-slate-500';
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between text-[11px]">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {t('entry.fields.importance')}
        </span>
        <span className="tabular-nums text-foreground font-mono font-semibold">
          {value} / 100 · {t(`entry.importanceLevels.${tier}`)}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-[width]', colorClass)}
          style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------

function DetailSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <div className="flex justify-between">
        <div className="h-5 w-24 animate-pulse rounded bg-muted" />
        <div className="flex gap-1">
          {[0, 1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className="h-7 w-16 animate-pulse rounded-md bg-muted"
            />
          ))}
        </div>
      </div>
      <div className="flex gap-2">
        <div className="h-5 w-20 animate-pulse rounded-full bg-muted" />
        <div className="h-5 w-20 animate-pulse rounded-full bg-muted" />
      </div>
      <div className="space-y-3 rounded-md bg-gigbg p-5">
        <div className="h-4 w-1/4 animate-pulse rounded bg-muted" />
        <div className="h-3 w-full animate-pulse rounded bg-muted" />
        <div className="h-3 w-11/12 animate-pulse rounded bg-muted" />
        <div className="h-3 w-3/4 animate-pulse rounded bg-muted" />
      </div>
      <div className="space-y-3 rounded-md bg-gigbg p-5">
        <div className="h-4 w-1/3 animate-pulse rounded bg-muted" />
        <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
        <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Date formatting
// ---------------------------------------------------------------------------

function formatDateTime(
  iso: string | null,
  t: (key: string, opts?: Record<string, unknown>) => string,
  locale: string,
): string {
  if (!iso) return t('dates.never');
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diff = Math.max(0, Date.now() - date.getTime()) / 1000;
  if (diff < 60) return t('dates.justNow');
  if (diff < 3600) {
    return t('dates.minutesAgo', { count: Math.floor(diff / 60) });
  }
  if (diff < 86400) {
    return t('dates.hoursAgo', { count: Math.floor(diff / 3600) });
  }
  if (diff < 604800) {
    return t('dates.daysAgo', { count: Math.floor(diff / 86400) });
  }
  if (diff < 2592000) {
    return t('dates.weeksAgo', { count: Math.floor(diff / 604800) });
  }
  return date.toLocaleString(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
