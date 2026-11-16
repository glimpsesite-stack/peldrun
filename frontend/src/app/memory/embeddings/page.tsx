// src/app/memory/settings/embeddings/page.tsx

/**
 * The Phase 4 semantic-search settings page
 * (`/memory/settings/embeddings`).
 *
 * Reached from a link card on `/memory/settings`. Kept as a
 * SEPARATE route (not embedded in `SettingsForm`) because:
 *
 *   * Phase 4 settings AUTO-SAVE on every change (toggle, model
 *     selection, retrieval mode, advanced sliders). They do not
 *     participate in the parent form's explicit save button.
 *     Mixing both models in one page would be confusing
 *     ("why did this change save instantly but that one
 *     didn't?").
 *
 *   * The section is large enough (four sub-panels) that
 *     embedding it inside the already-long settings form would
 *     bury the mode/provider/budget controls.
 *
 *   * The URL is bookmarkable -- useful for power users who
 *     frequently retune retrieval.
 *
 * The page delegates ALL interaction to `<EmbeddingsSection>`,
 * which owns the PATCH mutations, debounce logic, reindex
 * flow, and confirmation UX. This component's job is:
 *
 *   * Fetch the current settings (`useMemorySettings`).
 *   * Render loading / error states consistently with
 *     `/memory/settings`.
 *   * Pass the `embedding` and `retrieval` sub-trees to
 *     `EmbeddingsSection`.
 *   * Provide a back link to the parent settings page.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Brain } from 'lucide-react';

import { useMemorySettings } from '../../../features/memory/api/queries';
import { EmbeddingsSection } from '../../../features/memory/components/EmbeddingsSection';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MemoryEmbeddingsSettingsPage() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();

  // ---- Loading -------------------------------------------------
  //
  // Mirrors the skeleton of `/memory/settings` for visual
  // consistency: three tall-ish placeholder cards. The page
  // renders the header even during loading so a direct URL hit
  // shows something immediately (rather than a blank screen
  // with a spinner floating in the void).
  if (settingsQuery.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <div className="h-10 w-2/3 animate-pulse rounded-md bg-muted/40" />
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-32 w-full animate-pulse rounded-md bg-muted/40"
          />
        ))}
      </div>
    );
  }

  // ---- Error ---------------------------------------------------
  if (settingsQuery.error || !settingsQuery.data) {
    return (
      <MemoryBanner
        tone="error"
        message={
          settingsQuery.error?.message ?? t('errors.generic')
        }
      />
    );
  }

  // ---- Normal render ------------------------------------------
  const settings = settingsQuery.data;

  // Guard against old-backend deployments. The Phase 4 fields
  // (`embedding`, `retrieval`) are guaranteed by the contract
  // but not by every running instance. See `EmbeddingsSection`'s
  // JSDoc for the reasoning -- this page-level guard is the
  // primary UX (clear message + retry button), and the
  // component-level guard is defense-in-depth.
  const phase4Available =
    settings.embedding !== undefined &&
    settings.embedding !== null &&
    settings.retrieval !== undefined &&
    settings.retrieval !== null;

  if (!phase4Available) {
    return (
      <div className="space-y-6">
        <div>
          <Link
            href="/memory/settings"
            className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <ArrowLeft size={11} aria-hidden="true" />
            {t('settings.title')}
          </Link>
          <div className="mt-2 flex items-center gap-2">
            <Brain
              size={16}
              className="text-cyan-500"
              aria-hidden="true"
            />
            <h2 className="text-lg font-semibold font-heading text-foreground">
              {t('settings.sections.semanticSearch')}
            </h2>
          </div>
        </div>
        <MemoryBanner
          tone="warning"
          title={t('settings.embedding.unavailableTitle')}
          message={t('settings.embedding.unavailableMessage')}
          action={
            <button
              type="button"
              onClick={() => void settingsQuery.refetch()}
              className="text-[11px] font-medium underline underline-offset-2 whitespace-nowrap"
            >
              {t('common.retry')}
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* ============ Header ============ */}
      <div>
        <Link
          href="/memory/settings"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft size={11} aria-hidden="true" />
          {t('settings.title')}
        </Link>
        <div className="mt-2 flex items-center gap-2">
          <Brain
            size={16}
            className="text-cyan-500"
            aria-hidden="true"
          />
          <h2 className="text-lg font-semibold font-heading text-foreground">
            {t('settings.sections.semanticSearch')}
          </h2>
        </div>
        <p className="text-[11px] text-muted-foreground">
          {t('settings.embedding.sectionSubtitle')}
        </p>
      </div>

      {/* ============ Section (A–D) ============ */}
      <EmbeddingsSection
        embedding={settings.embedding}
        retrieval={settings.retrieval}
      />
    </div>
  );
}
