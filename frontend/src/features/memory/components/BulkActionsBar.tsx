// src/features/memory/components/BulkActionsBar.tsx

/**
 * Destructive bulk operations for one project.
 *
 * Design aligned with SearchTab:
 *
 *   * Two action buttons side-by-side inside a compact card.
 *   * The "Purge" button keeps the destructive rose palette so
 *     the operation reads as dangerous at a glance.
 *   * Confirmation via `ConfirmDangerDialog` (type-to-confirm) --
 *     `window.confirm` is forbidden by spec §6.2.
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, Trash2 } from 'lucide-react';

import { useBulkArchive, useBulkPurge } from '../api/mutations';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ConfirmDangerDialog } from './ConfirmDangerDialog';

const ARCHIVE_CONFIRM_TOKEN = 'ARCHIVE';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface BulkActionsBarProps {
  projectId: string;
  total: number;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BulkActionsBar({
  projectId,
  total,
  className,
}: BulkActionsBarProps) {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const canWrite = settingsQuery.data?.mode === 'read_write';

  const archiveMutation = useBulkArchive();
  const purgeMutation = useBulkPurge();

  const [archiveOpen, setArchiveOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);

  const isEmpty = total === 0;
  const isPending = archiveMutation.isPending || purgeMutation.isPending;

  async function handleArchive() {
    try {
      await archiveMutation.mutateAsync({ projectId });
      setArchiveOpen(false);
    } catch {
      // Error surfaced via mutation state
    }
  }

  async function handlePurge() {
    try {
      await purgeMutation.mutateAsync({ projectId });
      setPurgeOpen(false);
    } catch {
      // Error surfaced via mutation state
    }
  }

  const error = archiveMutation.error ?? purgeMutation.error;

  return (
    <>
      <div
        className={cn(
          'flex flex-wrap items-center gap-2 rounded-md border border-border/60 bg-card p-3',
          className,
        )}
      >
        <button
          type="button"
          onClick={() => setArchiveOpen(true)}
          disabled={isEmpty || !canWrite || isPending}
          title={!canWrite ? t('errors.mode_read_only') : undefined}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground transition-all',
            'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
            'disabled:cursor-not-allowed disabled:opacity-50',
            !isEmpty && canWrite && !isPending && 'hover:scale-105',
          )}
        >
          <Archive size={12} aria-hidden="true" />
          {t('projects.bulk.archiveButton')}
        </button>

        <button
          type="button"
          onClick={() => setPurgeOpen(true)}
          disabled={isEmpty || !canWrite || isPending}
          title={!canWrite ? t('errors.mode_read_only') : undefined}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3 text-xs font-medium text-destructive transition-all',
            'hover:bg-destructive/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive focus-visible:ring-offset-1',
            'disabled:cursor-not-allowed disabled:opacity-50',
            !isEmpty && canWrite && !isPending && 'hover:scale-105',
          )}
        >
          <Trash2 size={12} aria-hidden="true" />
          {t('projects.bulk.purgeButton')}
        </button>

        {error && (
          <span
            className="text-[10px] font-mono text-destructive"
            role="alert"
          >
            {error.message}
          </span>
        )}
      </div>

      <ConfirmDangerDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={t('projects.bulk.archiveTitle')}
        description={t('projects.bulk.archiveDescription')}
        confirmToken={ARCHIVE_CONFIRM_TOKEN}
        confirmLabel={t('projects.bulk.archiveButton')}
        isPending={archiveMutation.isPending}
        onConfirm={handleArchive}
      />

      <ConfirmDangerDialog
        open={purgeOpen}
        onOpenChange={setPurgeOpen}
        title={t('projects.bulk.purgeTitle')}
        description={t('projects.bulk.purgeDescription')}
        confirmToken={projectId}
        confirmPlaceholder={projectId}
        confirmLabel={t('projects.bulk.purgeButton')}
        isPending={purgeMutation.isPending}
        onConfirm={handlePurge}
      />
    </>
  );
}
