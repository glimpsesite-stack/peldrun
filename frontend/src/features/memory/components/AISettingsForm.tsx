// src/features/memory/components/AISettingsForm.tsx

/**
 * Wrapper for the "Automatic memory" settings tab.
 *
 * Loads MemoryAISettings from the server, manages a local draft,
 * computes a minimal diff (only changed leaf fields), and PATCHes
 * it on save. All UI is delegated to MemoryAISettingsSection --
 * the same component that will power any future embedding of the
 * automatic-memory settings (onboarding wizard, mobile view, ...).
 *
 * Responsibilities (and ONLY these):
 *
 *   1. Server -> draft hydration.
 *   2. Draft -> server diff computation.
 *   3. Save/reset actions.
 *   4. Sticky save bar with dirty-state hint.
 *   5. Inline error surfacing.
 *
 * Everything else -- field layout, help text, model selectors,
 * validation -- belongs to MemoryAISettingsSection.
 */

'use client';

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Loader2,
  RotateCcw,
  Save,
  X as XIcon,
} from 'lucide-react';

import type {
  MemoryAISettings,
  MemoryAISettingsOverlay,
} from '../types/ai';
import { useMemorySettings } from '../api/queries';
import { usePatchAISettings } from '../api/mutations';
import { MemoryAISettingsSection } from './MemoryAISettingsSection';
import { cn } from '../lib/cn';

export function AISettingsForm() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();
  const patchAI = usePatchAISettings();

  const serverAi = settingsQuery.data?.ai as
    | MemoryAISettings
    | undefined;

  const [draft, setDraft] = useState<MemoryAISettings | null>(null);
  const [saveSucceeded, setSaveSucceeded] = useState(false);

  // ---- Server -> draft hydration ------------------------------
  //
  // Triggers on:
  //   * first load (draft === null)
  //   * after a successful save (saveSucceeded flag flipped)
  //
  // Does NOT trigger on every serverAi change -- if the user
  // is mid-edit and something else invalidates the settings
  // query (unlikely but possible), we do NOT want to silently
  // discard the draft. The user sees "dirty" state and can
  // choose Save or Reset explicitly.
  useEffect(() => {
    if (serverAi && (draft === null || saveSucceeded)) {
      setDraft(deepClone(serverAi));
      setSaveSucceeded(false);
    }
  }, [serverAi, draft, saveSucceeded]);

  // ---- Draft -> server diff -----------------------------------
  //
  // Only changed leaf fields are included. E.g. editing
  // `extraction.model_id` produces `{ extraction: { model_id: ... } }`,
  // NOT the entire extraction object. The backend's deep-merge
  // preserves every unlisted field, so PATCH only applies what
  // the user actually changed.
  const diff: MemoryAISettingsOverlay = useMemo(() => {
    if (!draft || !serverAi) return {};
    return computeDiff(serverAi, draft) as MemoryAISettingsOverlay;
  }, [draft, serverAi]);

  const isDirty = Object.keys(diff).length > 0;
  const isSaving = patchAI.isPending;

  // ---- Actions ------------------------------------------------
  async function handleSave() {
    if (!isDirty || isSaving) return;
    try {
      await patchAI.mutateAsync(diff);
      setSaveSucceeded(true);
    } catch {
      // Error surfaces via `patchAI.error` at the bottom.
    }
  }

  function handleReset() {
    if (serverAi) setDraft(deepClone(serverAi));
  }

  // ---- Loading state -----------------------------------------
  if (!draft || !serverAi) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 size={12} className="animate-spin" />
          <span className="text-[11px]">
            {t('settings.ai.loading', 'Loading AI settings...')}
          </span>
        </div>
      </div>
    );
  }

  // ---- Render ------------------------------------------------
  return (
    <div className="space-y-4">
      <MemoryAISettingsSection
        value={draft}
        onChange={setDraft}
      />

      {patchAI.error && !isSaving && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px] text-destructive">
          <XIcon size={14} className="mt-0.5 shrink-0" />
          <div>
            <strong className="font-semibold">
              {t('settings.ai.saveFailed', 'Save failed')}
            </strong>
            <p className="mt-0.5">{patchAI.error.message}</p>
          </div>
        </div>
      )}

      <div className="sticky bottom-4 z-10 flex items-center justify-between gap-2 rounded-md border border-border bg-card/95 px-3 py-2 shadow-lg backdrop-blur">
        <span className="text-[10px] text-muted-foreground">
          {isDirty
            ? t('settings.ai.dirtyHint', 'You have unsaved changes')
            : t('settings.ai.cleanHint', 'No unsaved changes')}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReset}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm border border-border bg-card px-2.5 text-[11px] font-medium text-foreground transition-colors',
              'hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            <RotateCcw size={11} />
            {t('settings.ai.reset', 'Reset')}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm bg-primary px-2.5 text-[11px] font-medium text-primary-foreground transition-colors',
              'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {isSaving ? (
              <>
                <Loader2 size={11} className="animate-spin" />
                {t('settings.ai.saving', 'Saving...')}
              </>
            ) : (
              <>
                <Save size={11} />
                {t('settings.ai.save', 'Save changes')}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers (unchanged from the previous version)
// ---------------------------------------------------------------------------

function deepClone<T>(v: T): T {
  if (v === null || typeof v !== 'object') return v;
  if (Array.isArray(v)) return v.map(deepClone) as T;
  const out: Record<string, unknown> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    out[k] = deepClone(val);
  }
  return out as T;
}

function computeDiff(base: unknown, draft: unknown): Record<string, unknown> {
  if (base === draft) return {};
  if (
    base === null ||
    draft === null ||
    typeof base !== 'object' ||
    typeof draft !== 'object' ||
    Array.isArray(base) ||
    Array.isArray(draft)
  ) {
    return { __scalar: draft };
  }
  const diff: Record<string, unknown> = {};
  const keys = new Set([
    ...Object.keys(base as object),
    ...Object.keys(draft as object),
  ]);
  for (const key of keys) {
    const b = (base as Record<string, unknown>)[key];
    const d = (draft as Record<string, unknown>)[key];
    const sub = computeDiff(b, d);
    if (Object.keys(sub).length === 0) continue;
    if (Object.keys(sub).length === 1 && '__scalar' in sub) {
      diff[key] = sub.__scalar;
    } else {
      diff[key] = sub;
    }
  }
  return diff;
}
