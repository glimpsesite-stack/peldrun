// src/app/memory/browse/page.tsx

/**
 * The browse page (`/memory/browse`) -- the primary entry list.
 *
 * Layout (desktop-only):
 *
 *   * Header: title + active scope badge + "New memory" button.
 *   * Two-column body: sticky sidebar (16rem) + main column.
 *   * Sidebar: Personal + Projects in two compact cards.
 *   * Main: filter bar + entry list + "Load more".
 *
 * The `md:` breakpoint at 768px is kept for narrow laptops, but
 * the app targets desktop so the mobile-only horizontal scope
 * chips have been removed (they never appeared at desktop widths
 * anyway, and they add code that no path uses).
 *
 * Design aligned with the app's SearchTab:
 *
 *   * Sidebar cards use `border-border/60 bg-card` -- the same
 *     chrome used inside MemorySection in other pages.
 *   * Section headers use the standard `text-[10px] uppercase
 *     tracking-wider` pattern.
 *   * Entries get the same hover treatment as MemorySummaryCard.
 *
 * Behaviour is unchanged from the pre-redesign version:
 *
 *   * URL `?scope=` → store on mount.
 *   * Store → URL on scope change.
 *   * Pagination via `Load more` (offset accumulation).
 *   * Filter / scope change resets the offset.
 */

'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Brain, Plus, User, FolderOpen } from 'lucide-react';

import type {
  MemoryEntry,
  MemoryScopeRef,
} from '../../../features/memory/types/entry';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import { MemoryScope } from '../../../features/memory/types/enums';
import { memoryClient } from '../../../features/memory/api/client';
import { memoryKeys } from '../../../features/memory/api/keys';
import {
  useProjectsList,
  useMemorySettings,
} from '../../../features/memory/api/queries';
import {
  useDeleteEntry,
  usePromoteEntry,
} from '../../../features/memory/api/mutations';
import {
  DEFAULT_PAGE_SIZE,
  HARD_DELETE_CONFIRM_TOKEN,
} from '../../../features/memory/lib/constants';
import { useMemoryUIStore } from '../../../features/memory/stores/memory-ui-store';
import { cn } from '../../../features/memory/lib/cn';
import { EntryFilters } from '../../../features/memory/components/EntryFilters';
import { EntryList } from '../../../features/memory/components/EntryList';
import { EntryEditor } from '../../../features/memory/components/EntryEditor';
import { ConfirmDangerDialog } from '../../../features/memory/components/ConfirmDangerDialog';
import { EmptyState } from '../../../features/memory/components/EmptyState';
import { ScopeBadge } from '../../../features/memory/components/ScopeBadge';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';
import { useKeyboardShortcuts } from '../../../features/memory/hooks/useKeyboardShortcuts';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MemoryBrowsePage() {
  const { t } = useTranslation('memory');
  const router = useRouter();
  const searchParams = useSearchParams();

  // ---- Zustand: filters + active scope ---------------------------
  const filters = useMemoryUIStore((s) => s.filters);
  const activeScope = useMemoryUIStore((s) => s.active_scope);
  const setActiveScope = useMemoryUIStore((s) => s.setActiveScope);

  // URL -> store on mount.
  useEffect(() => {
    const scopeParam = searchParams?.get('scope');
    if (scopeParam) {
      useMemoryUIStore.getState().setActiveScopeByString(scopeParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Queries ---------------------------------------------------
  const projectsQuery = useProjectsList();
  const settingsQuery = useMemorySettings();
  const canWrite = settingsQuery.data?.mode === 'read_write';
  const canRead = settingsQuery.data?.mode !== 'disabled';

  // ---- Pagination -----------------------------------------------
  const [offset, setOffset] = useState(0);
  const [entries, setEntries] = useState<MemoryEntry[]>([]);

  const scopeRef: MemoryScopeRef | null = activeScope;
  const scopeString = scopeRef
    ? `${scopeRef.scope_type}:${scopeRef.scope_id}`
    : '';

  // Reset pagination when scope or filters change.
  useEffect(() => {
    setOffset(0);
    setEntries([]);
  }, [
    scopeString,
    filters.status,
    filters.memory_type,
    filters.is_pinned,
  ]);

  // ---- The list query -------------------------------------------
  const listQuery = useQuery<
    Awaited<ReturnType<typeof memoryClient.entries.list>>,
    MemoryApiError
  >({
    queryKey: memoryKeys.entries.list({
      scope: scopeString,
      status: filters.status,
      memory_type: filters.memory_type,
      is_pinned: filters.is_pinned,
      limit: DEFAULT_PAGE_SIZE,
      offset,
    }),
    queryFn: async ({ signal }) => {
      const [scopeType, ...rest] = scopeString.split(':');
      const scopeId = rest.join(':');
      return memoryClient.entries.list(
        {
          scope_type: scopeType,
          scope_id: scopeId,
          limit: DEFAULT_PAGE_SIZE,
          offset,
          status: filters.status,
          memory_type: filters.memory_type,
          is_pinned: filters.is_pinned,
        },
        { signal },
      );
    },
    enabled: canRead && scopeRef !== null && scopeString.length > 0,
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });

  // ---- Accumulate pages -----------------------------------------
  useEffect(() => {
    const response = listQuery.data;
    if (!response) return;
    if (response.offset !== offset) return;

    if (offset === 0) {
      setEntries(response.memories);
      return;
    }

    setEntries((prev) => {
      const existing = new Set(prev.map((e) => e.memory_id));
      const fresh = response.memories.filter(
        (e) => !existing.has(e.memory_id),
      );
      return fresh.length === 0 ? prev : [...prev, ...fresh];
    });
  }, [listQuery.data, offset]);

  // ---- Derived flags --------------------------------------------
  const hasMore =
    (listQuery.data?.count ?? 0) === DEFAULT_PAGE_SIZE &&
    entries.length > 0 &&
    entries.length === offset + (listQuery.data?.count ?? 0);

  const isLoading = listQuery.isPending && !listQuery.data;
  const isLoadingMore = listQuery.isFetching && offset > 0;

  // ---- Dialogs ---------------------------------------------------
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingEntry, setEditingEntry] = useState<MemoryEntry | undefined>();
  const [deletingEntry, setDeletingEntry] = useState<MemoryEntry | null>(null);
  const [promotingEntry, setPromotingEntry] = useState<MemoryEntry | null>(
    null,
  );

  const deleteMutation = useDeleteEntry();
  const promoteMutation = usePromoteEntry();

  // ---- Scope switching ------------------------------------------
  function pickScope(ref: MemoryScopeRef) {
    setActiveScope(ref);
    const params = new URLSearchParams(searchParams?.toString() ?? '');
    params.set('scope', `${ref.scope_type}:${ref.scope_id}`);
    router.replace(`/memory/browse?${params.toString()}`);
  }

  // ---- Handlers --------------------------------------------------
  function handleEdit(entry: MemoryEntry) {
    setEditingEntry(entry);
    setEditorOpen(true);
  }

  function handleCreate() {
    setEditingEntry(undefined);
    setEditorOpen(true);
  }

  async function handleDeleteConfirm() {
    if (!deletingEntry) return;
    try {
      await deleteMutation.mutateAsync({
        memoryId: deletingEntry.memory_id,
        scope_type: deletingEntry.scope_type,
        scope_id: deletingEntry.scope_id,
        hard: true,
      });
      setEntries((prev) =>
        prev.filter((e) => e.memory_id !== deletingEntry.memory_id),
      );
      setDeletingEntry(null);
    } catch {
      // Error surfaced in the dialog
    }
  }

  async function handlePromoteConfirm() {
    if (!promotingEntry) return;
    try {
      await promoteMutation.mutateAsync({
        params: {
          memoryId: promotingEntry.memory_id,
          scope_type: promotingEntry.scope_type,
          scope_id: promotingEntry.scope_id,
        },
        body: { target_scope_type: 'user', target_scope_id: 'default' },
      });
      setPromotingEntry(null);
    } catch {
      // Error surfaced in the dialog
    }
  }

  // ---- Keyboard shortcuts ---------------------------------------
  useKeyboardShortcuts([
    {
      keys: 'n',
      handler: () => {
        if (canWrite && activeScope) handleCreate();
      },
    },
  ]);

  // ---- Derived: project scopes ----------------------------------
  const projectScopes: MemoryScopeRef[] = useMemo(
    () =>
      (projectsQuery.data?.projects ?? []).map((p) => ({
        scope_type: MemoryScope.PROJECT,
        scope_id: p.project_id,
      })),
    [projectsQuery.data?.projects],
  );

  // ---- Render ---------------------------------------------------
  return (
    <div className="space-y-6">
      {/* ============ Header ============ */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-lg font-semibold font-heading text-foreground">
            {t('browse.title')}
          </h2>
          {activeScope && (
            <ScopeBadge
              scope_type={activeScope.scope_type}
              scope_id={
                activeScope.scope_type === 'user'
                  ? undefined
                  : activeScope.scope_id
              }
              size="sm"
            />
          )}
        </div>
        <button
          type="button"
          onClick={handleCreate}
          disabled={!canWrite || !activeScope}
          title={
            !canWrite
              ? t('errors.mode_read_only')
              : `${t('browse.empty.action')} (N)`
          }
          aria-label={t('browse.empty.action')}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground transition-all',
            'hover:bg-primary/90 hover:scale-105',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:scale-100',
          )}
        >
          <Plus size={12} aria-hidden="true" />
          {t('browse.empty.action')}
        </button>
      </div>

      {/* ============ Disabled notice ============ */}
      {!canRead && (
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

      {/* ============ Two-column body ============ */}
      <div className="grid grid-cols-1 md:grid-cols-[16rem_1fr] gap-6">
        {/* ---- Sidebar ---- */}
        <aside className="md:sticky md:top-24 md:self-start space-y-3">
          {/* Personal */}
          <SidebarCard
            icon={User}
            title={t('browse.scopeSidebar.personal')}
          >
            <ScopeButton
              active={
                activeScope?.scope_type === MemoryScope.USER &&
                activeScope.scope_id === 'default'
              }
              onClick={() =>
                pickScope({
                  scope_type: MemoryScope.USER,
                  scope_id: 'default',
                })
              }
              icon={User}
              label={t('scope.userDefault')}
            />
          </SidebarCard>

          {/* Projects */}
          <SidebarCard
            icon={FolderOpen}
            title={t('browse.scopeSidebar.projects')}
          >
            {projectsQuery.isLoading ? (
              <div className="space-y-1" aria-busy="true">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="h-7 animate-pulse rounded-md bg-muted"
                  />
                ))}
              </div>
            ) : projectScopes.length === 0 ? (
              <p className="text-[11px] text-muted-foreground">
                {t('projects.empty.title')}
              </p>
            ) : (
              <div className="space-y-0.5">
                {projectScopes.map((ref) => (
                  <ScopeButton
                    key={ref.scope_id}
                    active={
                      activeScope?.scope_type === ref.scope_type &&
                      activeScope.scope_id === ref.scope_id
                    }
                    onClick={() => pickScope(ref)}
                    icon={FolderOpen}
                    label={ref.scope_id}
                  />
                ))}
              </div>
            )}
          </SidebarCard>
        </aside>

        {/* ---- Main column ---- */}
        <div className="space-y-4 min-w-0">
          {!activeScope ? (
            <EmptyState
              icon={Brain}
              title={t('browse.empty.title')}
              description={t('scope.selectScope')}
              size="spacious"
            />
          ) : (
            <>
              <EntryFilters orientation="horizontal" />
              <EntryList
                entries={entries}
                isLoading={isLoading}
                isLoadingMore={isLoadingMore}
                error={listQuery.error}
                hasMore={hasMore}
                onLoadMore={() =>
                  setOffset((o) => o + DEFAULT_PAGE_SIZE)
                }
                onRetry={() => listQuery.refetch()}
                onEdit={canWrite ? handleEdit : undefined}
                onDelete={
                  canWrite
                    ? (entry) => setDeletingEntry(entry)
                    : undefined
                }
                onPromote={
                  canWrite
                    ? (entry) => setPromotingEntry(entry)
                    : undefined
                }
              />
            </>
          )}
        </div>
      </div>

      {/* ============ Dialogs ============ */}
      {activeScope && (
        <EntryEditor
          open={editorOpen}
          onOpenChange={(open) => {
            setEditorOpen(open);
            if (!open) setEditingEntry(undefined);
          }}
          mode={editingEntry ? 'edit' : 'create'}
          entry={editingEntry}
          scope={
            editingEntry
              ? {
                  scope_type: editingEntry.scope_type,
                  scope_id: editingEntry.scope_id,
                }
              : activeScope
          }
          onSaved={() => {
            if (offset === 0) {
              listQuery.refetch();
            } else {
              setOffset(0);
            }
          }}
        />
      )}

      <ConfirmDangerDialog
        open={deletingEntry !== null}
        onOpenChange={(open) => {
          if (!open) setDeletingEntry(null);
        }}
        title={t('entry.delete.hardTitle')}
        description={t('entry.delete.hardDescription')}
        confirmToken={HARD_DELETE_CONFIRM_TOKEN}
        confirmPlaceholder={t('entry.delete.hardConfirmPlaceholder')}
        confirmLabel={t('common.delete')}
        isPending={deleteMutation.isPending}
        onConfirm={handleDeleteConfirm}
      />

      <ConfirmDangerDialog
        open={promotingEntry !== null}
        onOpenChange={(open) => {
          if (!open) setPromotingEntry(null);
        }}
        title={t('entry.promote.title')}
        description={t('entry.promote.description')}
        confirmToken="COPY"
        confirmLabel={t('entry.actions.promote')}
        isPending={promoteMutation.isPending}
        onConfirm={handlePromoteConfirm}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface SidebarCardProps {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  title: string;
  children: React.ReactNode;
}

function SidebarCard({
  icon: Icon,
  title,
  children,
}: SidebarCardProps) {
  return (
    <div className="rounded-md border border-border/60 bg-card p-3">
      <div className="flex items-center gap-1.5 mb-2">
        <Icon
          size={11}
          className="text-muted-foreground"
          aria-hidden="true"
        />
        <h3 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </h3>
      </div>
      {children}
    </div>
  );
}

interface ScopeButtonProps {
  active: boolean;
  onClick: () => void;
  icon: React.ComponentType<{ size?: number; className?: string }>;
  label: string;
}

function ScopeButton({
  active,
  onClick,
  icon: Icon,
  label,
}: ScopeButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-start text-[11px] transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
        active
          ? 'bg-primary/10 font-medium text-primary'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      <Icon size={11} aria-hidden="true" className="shrink-0" />
      <span className="truncate font-mono">{label}</span>
    </button>
  );
}
