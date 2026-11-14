// src/features/memory/components/MemoryField.tsx

/**
 * A card-wrapped form field.
 *
 * Mirrors SearchTab's `CompactField` / `CompactTextField` visual
 * pattern: the ENTIRE card is the input surface, not a label above
 * an input. The input itself is transparent with no borders -- the
 * card's own border acts as the focus target's container.
 *
 * Why the card-as-input approach
 * ------------------------------
 *
 * It was chosen by SearchTab for a specific reason that applies
 * equally well to the memory settings page: the settings page
 * renders many fields in a dense grid. Drawing a border around
 * every small input creates visual noise that fragments the
 * section. The card-as-input pattern gives each field a single
 * "chip" that groups label + value + help into a discrete unit.
 *
 * Input styling
 * -------------
 *
 * Callers pass their own `<input>` / `<select>` as `children`.
 * The `inputClassName` export below provides the standard
 * transparent styling that all consumers use, ensuring
 * consistency without forcing a specific element type.
 */

'use client';

import { AlertCircle } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';

/**
 * Standard input styling shared by all fields in the memory
 * settings forms. Kept as an exported constant so a select or a
 * custom masked input can use the exact same classes without
 * duplicating the token soup in every file.
 */
export const MEMORY_FIELD_INPUT_CLASS =
  'w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono ' +
  'focus:ring-0 p-1 hover:bg-muted/50 rounded transition-colors ' +
  'disabled:cursor-not-allowed disabled:opacity-60';

export interface MemoryFieldProps {
  /** Field label (uppercase, tracking-wider). */
  label: string;
  /** The `htmlFor` target -- should match the child input's id. */
  htmlFor?: string;
  /** Optional help text below the input (hidden when `error` is set). */
  help?: string;
  /** Validation error -- when present, tints the card red and shows the message. */
  error?: string;
  /** When true, replaces the card border with a subtle "disabled" tint. */
  disabled?: boolean;
  /** The input element. */
  children: ReactNode;
  /** Extra classes on the outer card. */
  className?: string;
}

export function MemoryField({
  label,
  htmlFor,
  help,
  error,
  disabled = false,
  children,
  className,
}: MemoryFieldProps) {
  return (
    <div
      className={cn(
        'p-3 rounded-sm border flex flex-col gap-1 transition-colors',
        error
          ? 'border-rose-500/60 bg-rose-500/5'
          : disabled
            ? 'border-border/40 bg-card opacity-70'
            : 'border-border/60 bg-card hover:border-muted/30 hover:shadow-sm',
        className,
      )}
    >
      <label
        htmlFor={htmlFor}
        className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
      >
        {label}
      </label>
      {children}
      {help && !error && (
        <p className="text-[10px] leading-snug text-muted-foreground">
          {help}
        </p>
      )}
      {error && (
        <p
          className="text-[10px] font-medium text-rose-600 dark:text-rose-400 flex items-center gap-1"
          role="alert"
        >
          <AlertCircle size={10} aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}
    </div>
  );
}
