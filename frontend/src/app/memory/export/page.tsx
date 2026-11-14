// src/app/memory/export/page.tsx

'use client';

import { useTranslation } from 'react-i18next';

import { ExportForm } from '../../../features/memory/components/ExportForm';

export default function MemoryExportPage() {
  const { t } = useTranslation('memory');

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('export.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('export.description')}
        </p>
      </div>

      <ExportForm />
    </div>
  );
}
