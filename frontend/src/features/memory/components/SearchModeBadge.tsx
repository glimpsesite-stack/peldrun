// src/features/memory/components/SearchModeBadge.tsx

/**
 * A small chip that reports WHICH retrieval strategy produced
 * a given `/api/memory/search` result set.
 *
 * Values come directly from `SearchMemoryResponse.search_mode`:
 *
 *   * `"fts"`    -> "Keyword"  (gray)
 *   * `"vector"` -> "Semantic" (blue)
 *   * `"hybrid"` -> "Hybrid"   (green)
 *
 * Purpose
 * -------
 *
 * A single visual indicator that the operator is looking at
 * semantic recall rather than pure lexical search. Two real
 * use cases drove its creation:
 *
 *   1. **Configuration verification.** After enabling embeddings
 *      in Settings -> Memory, the user needs to SEE that the
 *      search now uses vector cosine (or hybrid) rather than
 *      silently falling back to FTS because the pipeline
 *      failed to initialize. A "Keyword" badge after enabling
 *      embeddings is an immediate, actionable signal.
 *
 *   2. **Debugging "why is this query empty?".** When FTS
 *      returns nothing and vector was supposed to cover the
 *      semantic gap, seeing `"fts"` on the empty-result panel
 *      tells the operator "the vector path did not
 *      participate" -- which narrows the investigation to
 *      embedding configuration rather than "the memory DB has
 *      no relevant entries".
 *
 * Layout contract
 * ---------------
 *
 * Compact: fits on the same line as the panel header, with
 * height matched to the existing icon+label chip (see the
 * "Similar memories" header). No wrapping on narrow layouts;
 * the label collapses to icon-only below the `sm:` breakpoint
 * so the panel keeps its single-line header at narrow widths.
 *
 * Accessibility
 * -------------
 *
 * `title` carries a longer human-readable description of what
 * the mode MEANS (not just its label), so a sighted user who
 * hovers learns the difference without reading the docs. The
 * `aria-label` includes the mode word so a screen reader
 * announces "Semantic search mode" instead of just reading
 * "chip".
 */

'use client';

import { Layers, Search, Sparkles } from 'lucide-react';

import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SearchModeBadgeProps {
  /**
   * The strategy reported by the search endpoint.
   *
   * Typed as the exact union from `SearchMemoryResponse` -- an
   * `undefined` here means "the response did not carry the
   * field" (older backend, partial rollout), in which case the
   * badge renders nothing rather than guessing. A badge with a
   * fabricated mode would be worse than no badge at all: it
   * could show "Hybrid" on a deployment that is FTS-only.
   */
  mode: 'fts' | 'vector' | 'hybrid' | undefined;

  /** Extra classes on the wrapping `<span>`. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Mode metadata
// ---------------------------------------------------------------------------

/**
 * Visual + textual configuration per mode.
 *
 * Colors follow the app's existing chip palette:
 *   - gray   = neutral / fallback (FTS is the safe default)
 *   - blue   = informational / semantic (matches the
 *              embedding-related accents in the settings page)
 *   - green  = "both signals agree" (hybrid is the best case)
 *
 * `title` text is deliberately explanatory, not just a label.
 * The user hover target is small (a chip), so the tooltip is
 * where the actual meaning lives.
 */
const MODE_CONFIG = {
  fts: {
    label: 'Keyword',
    title:
      'Keyword search: exact word matches (BM25). Semantic recall did not contribute to these results.',
    Icon: Search,
    classes:
      'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  },
  vector: {
    label: 'Semantic',
    title:
      'Semantic search: meaning-based matching via embeddings. No keyword matches were found, but entries with similar meaning were.',
    Icon: Sparkles,
    classes:
      'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  },
  hybrid: {
    label: 'Hybrid',
    title:
      'Hybrid search: keyword matches AND semantic similarity, combined via Reciprocal Rank Fusion. The highest-quality mode.',
    Icon: Layers,
    classes:
      'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  },
} as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SearchModeBadge({
  mode,
  className,
}: SearchModeBadgeProps) {
  // Absent field -> render nothing. See the docstring on `mode`.
  if (mode === undefined || mode === null) {
    return "No Mode";
  }

  const config = MODE_CONFIG[mode];

  return (
    <span
      title={config.title}
      aria-label={`${config.label} search mode`}
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium',
        config.classes,
        className,
      )}
    >
      <config.Icon size={9} aria-hidden="true" />
      <span className="hidden sm:inline">{config.label}</span>
    </span>
  );
}
