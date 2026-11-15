// src/features/memory/components/ExtractionTriggerSelector.tsx

/**
 * Radio group for choosing the extraction trigger pattern.
 *
 * The three triggers map 1:1 to the backend's
 * `ExtractionTrigger` type (`omweb/memory/extraction/trigger_router.py`):
 *
 *   * `after_turn`    -- fire on every completed turn (may be
 *                        batched via `after_turn_batching`).
 *   * `after_session` -- fire only when the session's status
 *                        becomes 'completed'.
 *   * `both`          -- fire on either.
 *
 * Visual pattern
 * --------------
 * Mirrors the mode radios in `SettingsForm`: card-as-input, one
 * `<label>` per option, selected state uses primary border and
 * tinted background, unselected uses neutral card. Icons on the
 * left keep the three options visually distinct even at a
 * glance -- without them, "after_turn" and "after_session" look
 * nearly identical in the dense settings layout.
 *
 * No validation
 * -------------
 * The trigger is a closed enum, always one of the three values
 * (the parent form initializes it from settings, which is
 * validated by Pydantic server-side). There is nothing to
 * validate client-side beyond the type itself.
 */

'use client';

import { Blend, Clock, MessageSquare } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { ExtractionTrigger } from '../types/ai';
import { ALL_EXTRACTION_TRIGGERS } from '../types/ai';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Trigger -> icon mapping
// ---------------------------------------------------------------------------

/**
 * One icon per trigger. The icon names are documented so a
 * future addition to the trigger enum forces a compile error
 * here -- `Record<ExtractionTrigger, LucideIcon>` guarantees
 * exhaustiveness.
 */
const TRIGGER_ICONS: Record<ExtractionTrigger, LucideIcon> = {
  after_turn: MessageSquare,
  after_session: Clock,
  both: Blend,
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface ExtractionTriggerSelectorProps {
  /** Currently selected trigger. */
  value: ExtractionTrigger;
  /** Called when the user picks a different trigger. */
  onChange: (value: ExtractionTrigger) => void;
  /** When true, disables every radio. */
  disabled?: boolean;
}

export function ExtractionTriggerSelector({
  value,
  onChange,
  disabled = false,
}: ExtractionTriggerSelectorProps) {
  const { t } = useTranslation('memory');

  return (
    <div
      className="grid grid-cols-1 sm:grid-cols-3 gap-2"
      role="radiogroup"
      aria-label={t('settings.ai.extractionTrigger')}
    >
      {ALL_EXTRACTION_TRIGGERS.map((trigger) => {
        const Icon = TRIGGER_ICONS[trigger];
        const selected = value === trigger;
        return (
          <label
            key={trigger}
            className={cn(
              'flex items-start gap-2 rounded-sm border p-2.5 text-xs transition-colors',
              selected
                ? 'border-primary/60 bg-primary/5'
                : 'border-border/60 bg-card hover:bg-muted/40',
              disabled
                ? 'cursor-not-allowed opacity-60'
                : 'cursor-pointer',
            )}
          >
            <input
              type="radio"
              name="extraction-trigger"
              value={trigger}
              checked={selected}
              onChange={() => onChange(trigger)}
              disabled={disabled}
              className="mt-0.5 h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
            />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <Icon
                  size={11}
                  aria-hidden="true"
                  className={cn(
                    'shrink-0',
                    selected ? 'text-primary' : 'text-muted-foreground',
                  )}
                />
                <span className="font-semibold text-foreground leading-tight">
                  {t(`settings.triggers.${trigger}`)}
                </span>
              </div>
              <p className="mt-0.5 text-[10px] leading-snug text-muted-foreground">
                {t(`settings.triggersHelp.${trigger}`)}
              </p>
            </div>
          </label>
        );
      })}
    </div>
  );
}
