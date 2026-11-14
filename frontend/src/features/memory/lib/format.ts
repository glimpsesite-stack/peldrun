// src/features/memory/lib/format.ts

/**
 * Shared formatting helpers for the memory feature.
 *
 * Extracted from inline duplicates in `UsageKpiCards` and
 * `ProjectCard`. Centralizing the three helpers used by the
 * Dashboard now prevents the same abbreviations from drifting
 * across pages ("15.2K" on the dashboard vs "15234" on the usage
 * page).
 *
 * All three helpers accept already-resolved i18n translations
 * (via the `t` parameter) so that Arabic / English / future
 * locales all format correctly without this module depending on
 * `react-i18next` directly.
 */

/**
 * Minimal `t` signature. Matches what `useTranslation()` returns
 * while keeping this module free of `i18next` type imports --
 * which avoids version-mismatch pain when the i18n stack
 * upgrades.
 */
type Translate = (key: string, opts?: Record<string, unknown>) => string;

/**
 * Abbreviate a token count: 984, 15.2K, 1.24M.
 *
 * Units come from `memory.json` (`format.tokensK` / `format.tokensM`)
 * so translators can localize "K"/"M" to Arabic conventions
 * (ألف / مليون) independently.
 *
 * Sub-1000 values are returned as integers -- decimals on a
 * count of 47 tokens would imply false precision.
 */
export function formatTokens(n: number, t: Translate): string {
  if (!Number.isFinite(n)) return '—';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) {
    return t('format.tokensK', { value: (n / 1000).toFixed(1) });
  }
  return t('format.tokensM', { value: (n / 1_000_000).toFixed(2) });
}

/**
 * Render a fraction (0..1) as a percentage string.
 *
 * Two display modes:
 *   * >= 10%  -- whole percent  ("32%", "99%")
 *   * <  10%  -- one decimal    ("3.4%", "0.2%")
 *
 * The cutoff exists because sub-10% values round to indistinguishable
 * integers ("3%" could be 2.5% or 3.4%), while above 10% the extra
 * precision is noise.
 *
 * Uses the ASCII `%` character (not `٪`) -- the app's CSS already
 * handles RTL for this string, and mixing percent glyph styles
 * across charts/labels looked inconsistent in the audit.
 */
export function formatPercent(fraction: number): string {
  if (!Number.isFinite(fraction)) return '—';
  const pct = fraction * 100;
  if (pct >= 10) return `${pct.toFixed(0)}%`;
  return `${pct.toFixed(1)}%`;
}

/**
 * Render an integer count with locale-appropriate grouping
 * separators (e.g. "1,234" in en-US, "١٬٢٣٤" in ar-EG).
 *
 * Uses `Intl.NumberFormat` without an explicit locale, which
 * resolves against the browser's language -- the same signal
 * that drives the app's i18n selection. No need to thread the
 * locale through manually.
 */
export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat().format(n);
}
