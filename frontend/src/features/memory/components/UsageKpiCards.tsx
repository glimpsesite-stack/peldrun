// src/features/memory/components/UsageKpiCards.tsx

/**
 * The usage dashboard's four KPI cards.
 *
 * Redesigned to match SearchTab's top-level cards. Uses
 * `MemorySummaryCard` (gradient line + corner glow + hover lift)
 * so the dashboard, the usage page, and every future summary surface
 * share a single visual vocabulary.
 *
 * The tone mapping for `budget_utilization` and `drop_rate` is
 * preserved from the pre-redesign version:
 *
 *   * budget: green < 50%, amber 50-79%, red 80%+
 *   * drop:   green < 10%, amber 10-29%, red 30%+
 *
 * Number formatting delegates to `lib/format` (single source of
 * truth shared with the dashboard page).
 */

'use client';

import { useTranslation } from 'react-i18next';
import {
  Activity,
  Coins,
  Gauge,
  TrendingDown,
} from 'lucide-react';

import type { UsageSummaryResponse } from '../types/responses';
import {
  formatTokens,
  formatPercent,
  formatCount,
} from '../lib/format';
import { MemorySummaryCard } from './MemorySummaryCard';
import { MemoryBanner } from './MemoryBanner';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface UsageKpiCardsProps {
  summary: UsageSummaryResponse;

  /**
   * The fraction (0..1) of budget utilization above which the
   * budget warning banner appears. When `undefined`, the banner
   * is never shown (used by compact renderers like the dashboard
   * which handles its own warnings).
   */
  warnThreshold?: number;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function UsageKpiCards({
  summary,
  warnThreshold,
  className,
}: UsageKpiCardsProps) {
  const { t } = useTranslation('memory');

  const showWarning =
    warnThreshold !== undefined &&
    summary.budget_utilization >= warnThreshold;

  const budgetAccent =
    summary.budget_utilization >= 0.8
      ? '#f43f5e'
      : summary.budget_utilization >= 0.5
        ? '#f59e0b'
        : '#10b981';

  const dropAccent =
    summary.drop_rate >= 0.3
      ? '#f43f5e'
      : summary.drop_rate >= 0.1
        ? '#f59e0b'
        : '#10b981';

  return (
    <div className={className}>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <MemorySummaryCard
          label={t('usage.kpis.runs')}
          value={formatCount(summary.run_count)}
          icon={<Activity size={18} />}
          accent="#3b82f6"
          subtitle={t('usage.period.month')}
          delay={0}
        />
        <MemorySummaryCard
          label={t('usage.kpis.totalTokens')}
          value={formatTokens(summary.total_tokens, t)}
          icon={<Coins size={18} />}
          accent="#8b5cf6"
          subtitle={`${formatPercent(summary.budget_utilization)} ${t('usage.kpis.budgetUtil').toLowerCase()}`}
          delay={0.05}
        />
        <MemorySummaryCard
          label={t('usage.kpis.budgetUtil')}
          value={formatPercent(summary.budget_utilization)}
          icon={<Gauge size={18} />}
          accent={budgetAccent}
          subtitle={`${formatCount(summary.entries_used)} / ${formatCount(summary.entries_retrieved)}`}
          delay={0.1}
        />
        <MemorySummaryCard
          label={t('usage.kpis.dropRate')}
          value={formatPercent(summary.drop_rate)}
          icon={<TrendingDown size={18} />}
          accent={dropAccent}
          subtitle={`${formatCount(summary.entries_dropped)} ${t('usage.kpis.entriesDropped').toLowerCase()}`}
          delay={0.15}
        />
      </div>

      {showWarning && (
        <MemoryBanner
          tone="warning"
          message={t('usage.budgetWarning', {
            pct: formatPercent(summary.budget_utilization),
          })}
          className="mt-4"
        />
      )}
    </div>
  );
}
