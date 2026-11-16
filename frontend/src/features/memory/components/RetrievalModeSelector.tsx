// src/features/memory/components/RetrievalModeSelector.tsx

/**
 * Radio-card selector for the four retrieval strategies
 * (`lexical`, `vector`, `hybrid`, `automatic`).
 *
 * Design contract -- "ALLOW AND WARN", not "DISABLE"
 * ---------------------------------------------------
 * Per the Phase 4 plan (§5.3, closing note), this component
 * does NOT block the user from selecting a semantic mode
 * (`vector` / `hybrid` / `automatic`) while embeddings are
 * disabled or unconfigured. Choosing a semantic mode while
 * `embedding.enabled === false` or `embedding.model_key ===
 * null` is a VALID, STAGEABLE choice:
 *
 *   * The user may be pre-configuring their preferred
 *     strategy BEFORE flipping the master embedding toggle
 *     in Section A.
 *
 *   * The backend silently falls back to `lexical` at
 *     retrieval time until the prerequisites are met -- so
 *     the setting is harmless in the meantime.
 *
 * Blocking this with a disabled radio would prevent that
 * staging flow. Instead, the component surfaces the missing
 * prerequisites as:
 *
 *   1. A subtle warning icon on every semantic-mode card
 *      (`AlertTriangle`).
 *   2. A prominent `MemoryBanner` below the group when the
 *      CURRENTLY SELECTED mode is semantic but not fully
 *      configured.
 *   3. An optional "scroll to Section A" action via
 *      `onRequestEnable`, wired by the parent
 *      (`EmbeddingsSection`) to a scroll-into-view handler.
 *
 * Accessibility
 * -------------
 * Uses a native `<fieldset>` + `<input type="radio">` group
 * with `sr-only` inputs and `peer-*` styling on the card
 * content. This preserves:
 *
 *   * Native radio-group semantics for screen readers
 *     ("option 2 of 4, selected").
 *   * Native keyboard navigation (Tab into the group, arrow
 *     keys to change selection).
 *   * `peer-focus-visible` focus rings on the CARD so keyboard
 *     users see a clear focus target, even though the actual
 *     input is visually hidden.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';

import type {
  MemoryEmbeddingSettings,
  RetrievalMode,
} from '../types/embedding';
import {
  ALL_RETRIEVAL_MODES,
  isSemanticMode,
} from '../types/embedding';
import { MemoryBanner } from './MemoryBanner';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface RetrievalModeSelectorProps {
  /** The current `retrieval.mode` value from `MemorySettings`. */
  value: RetrievalMode;

  /**
   * Called when the user selects a mode. The parent persists
   * this immediately via the settings patch mutation.
   *
   * Not called for disabled-state changes (there is none --
   * this component never disables individual modes; see the
   * module JSDoc).
   */
  onChange: (mode: RetrievalMode) => void;

  /**
   * The current embedding sub-tree. Used ONLY to decide which
   * warning badges and banners to render -- the component does
   * NOT block any selection based on this. See the module
   * JSDoc for the reasoning.
   */
  embedding: MemoryEmbeddingSettings;

  /** When true, disables every radio in the group. */
  disabled?: boolean;

  /**
   * Field-level validation error key (i18n key or English
   * text) rendered below the group. Rare for this field --
   * `retrieval.mode` is a fixed enum with no numeric or
   * length constraints -- but kept for API symmetry with
   * sibling components.
   */
  error?: string;

  /**
   * When supplied, the warning banner renders a "go to
   * section above" action that invokes this callback.
   * Typically the parent (`EmbeddingsSection`) passes a
   * `scrollIntoView` handler targeting Section A. Omit to
   * render the warning without the action (text-only).
   */
  onRequestEnable?: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function RetrievalModeSelector({
  value,
  onChange,
  embedding,
  disabled = false,
  error,
  onRequestEnable,
}: RetrievalModeSelectorProps) {
  const { t } = useTranslation('memory');

  // Prerequisite flags -- used ONLY for the warning UI. They
  // never affect whether a radio is clickable.
  const embeddingEnabled = embedding.enabled === true;
  const modelSelected = embedding.model_key !== null;
  const fullyConfigured = embeddingEnabled && modelSelected;

  // The banner below the group appears ONLY when the CURRENT
  // mode is semantic AND at least one prerequisite is
  // missing. `lexical` is always safe, so it never triggers.
  const currentIsSemantic = isSemanticMode(value);
  const showWarningBanner = currentIsSemantic && !fullyConfigured;

  // Two variants of the warning text, chosen by WHICH
  // prerequisite is missing. Distinguishing them lets the
  // user act precisely (enable the toggle vs. pick a model).
  const warningMessage = !embeddingEnabled
    ? t('settings.retrieval.semanticDisabledWarning')
    : t('settings.embedding.modelNotSelected');

  return (
    <div className="space-y-3">
      <fieldset
        className="space-y-2"
        disabled={disabled}
      >
        <legend className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {t('settings.retrieval.modeLabel')}
        </legend>
        <p className="text-[10px] leading-snug text-muted-foreground">
          {t('settings.retrieval.modeHelp')}
        </p>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1">
          {ALL_RETRIEVAL_MODES.map((mode) => {
            const isSelected = mode === value;
            const semantic = isSemanticMode(mode);
            // Per-card warning icon appears ONLY on semantic
            // modes when prerequisites are missing. `lexical`
            // never shows a warning -- it needs nothing.
            const cardWarns = semantic && !fullyConfigured;

            return (
              <label
                key={mode}
                className={cn(
                  'cursor-pointer rounded-sm block',
                  disabled && 'cursor-not-allowed opacity-60',
                )}
              >
                {/*
                  The actual radio input is visually hidden
                  (sr-only) but remains in the tab order so
                  keyboard navigation works. The visible card
                  is styled via `peer-*` classes that react to
                  this input's :checked / :focus-visible /
                  :disabled states.
                */}
                <input
                  type="radio"
                  name="retrieval-mode"
                  value={mode}
                  checked={isSelected}
                  onChange={() => onChange(mode)}
                  disabled={disabled}
                  className="peer sr-only"
                />
                <div
                  className={cn(
                    'rounded-sm border p-3 transition-colors h-full',
                    'border-border/60 bg-card',
                    'peer-checked:border-primary peer-checked:bg-primary/5',
                    'peer-checked:shadow-sm',
                    'peer-focus-visible:ring-2 peer-focus-visible:ring-primary peer-focus-visible:ring-offset-0',
                    !disabled && 'hover:border-muted/30 hover:shadow-sm',
                  )}
                >
                  <div className="flex items-center gap-1.5 mb-1">
                    <span
                      className={cn(
                        'text-xs font-semibold',
                        isSelected
                          ? 'text-primary'
                          : 'text-foreground',
                      )}
                    >
                      {t(`settings.retrieval.modes.${mode}.label`)}
                    </span>
                    {cardWarns && (
                      <AlertTriangle
                        size={11}
                        aria-hidden="true"
                        className="text-amber-600 dark:text-amber-400 shrink-0"
                      />
                    )}
                  </div>
                  <p className="text-[10px] leading-snug text-muted-foreground">
                    {t(
                      `settings.retrieval.modes.${mode}.description`,
                    )}
                  </p>
                </div>
              </label>
            );
          })}
        </div>
      </fieldset>

      {/*
        Warning banner -- shown only when the CURRENTLY
        SELECTED mode is semantic AND prerequisites are
        missing. The banner uses `role="alert"` (from
        `MemoryBanner`) so screen readers announce it when
        the state changes (e.g. user selects `hybrid` while
        embeddings are off).
      */}
      {showWarningBanner && (
        <MemoryBanner
          tone="warning"
          message={warningMessage}
          action={
            onRequestEnable ? (
              <button
                type="button"
                onClick={onRequestEnable}
                className="text-[11px] font-medium underline underline-offset-2 whitespace-nowrap"
              >
                {t('settings.embedding.enabled')}
              </button>
            ) : undefined
          }
        />
      )}

      {/* Field-level validation error (rare). */}
      {error && (
        <p
          role="alert"
          className="text-[10px] font-medium text-rose-600 dark:text-rose-400"
        >
          {error}
        </p>
      )}
    </div>
  );
}
