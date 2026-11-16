// src/app/memory/jobs/page.tsx

/**
 * The extraction jobs page (`/memory/jobs`).
 *
 * Lists every extraction job the system has created, with
 * filters and per-job retry/cancel actions. Reachable from the
 * memory settings page and from any chat's "Extract from chat"
 * button's follow-up link (once we add it in M9c).
 *
 * Self-contained: only needs the router and the
 * `<MemoryJobsList>` component. Everything else (polling,
 * mutations, filters) lives inside the list.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { ListChecks } from 'lucide-react';

import { MemoryJobsList } from '../../../features/memory/components/MemoryJobsList';
import { MemorySection } from '../../../features/memory/components/MemorySection';

export default function MemoryJobsPage() {
  const { t } = useTranslation('memory');

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('jobs.title', 'Extraction Jobs')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t(
            'jobs.description',
            'Every extraction job the system has created, with live status and per-job actions.',
          )}
        </p>
      </div>

      <MemorySection
        icon={ListChecks}
        title={t('jobs.sectionTitle', 'All Jobs')}
        subtitle={t(
          'jobs.sectionSubtitle',
          'Queued, running, completed, failed, and cancelled jobs',
        )}
        delay={0.15}
      >
        <MemoryJobsList />
      </MemorySection>
    </div>
  );
}
