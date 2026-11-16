// src/app/memory/diagnostics/page.tsx

'use client';

import { useTranslation } from 'react-i18next';
import { Activity } from 'lucide-react';

import { MemoryDiagnosticsPanel } from '../../../features/memory/components/MemoryDiagnosticsPanel';
import { MemorySection } from '../../../features/memory/components/MemorySection';

export default function MemoryDiagnosticsPage() {
  const { t } = useTranslation('memory');

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('diagnostics.title', 'Diagnostics')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t(
            'diagnostics.description',
            'Health check, model probe, and quality metrics for the AI extraction subsystem.',
          )}
        </p>
      </div>

      <MemorySection
        icon={Activity}
        title={t('diagnostics.sectionTitle', 'System Health')}
        subtitle={t(
          'diagnostics.sectionSubtitle',
          'Live status and diagnostic tools.',
        )}
        delay={0.1}
      >
        <MemoryDiagnosticsPanel />
      </MemorySection>
    </div>
  );
}
