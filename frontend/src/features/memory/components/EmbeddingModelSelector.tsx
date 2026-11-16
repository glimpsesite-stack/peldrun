// src/features/memory/components/EmbeddingModelSelector.tsx

/**
 * Embedding-model picker modeled after the chat `EngineSelector`.
 *
 * DATA SOURCE: backend/data/models_vault.json only.
 * -----------------------------------------------------------------
 * Reads `vault.embedding_models`:
 *
 *     {
 *       "<provider_id>": ["<model_id>", ...],
 *       "lmstudio":      ["text-embedding-nomic-embed-text-v1.5"],
 *       "gigachat":      ["GigaEmbeddings-3B-2025-09"],
 *       "cloudflare-ai": ["@cf/google/embeddinggemma-2"]
 *     }
 *
 * Provider metadata (display name + badge) is resolved from the
 * same vault, in priority order:
 *
 *   1. provider_connections[] (active + enabled) — carries the
 *      `badge` written by save_provider_connection() from the
 *      package manifest icon.
 *   2. cloud_vault[]                           — cp.id / cp.name / cp.badge
 *   3. custom_endpoints[]                      — ce.providerId / ce.name / ce.badge
 *   4. lmstudio_vault / ollama_vault           — hard-coded display names
 *   5. provider_package_models[id]             — bare id as name
 *   6. Otherwise                               — the raw id, generic icon.
 *
 * No `models_metadata.json` is consulted. No `/api/config/embedding-models`
 * call is made. One file. One source of truth.
 *
 * On-change contract
 * ------------------
 * The selected model is written as a COMPOSITE key
 * `"<provider_id>::<model_id>"` to `MemorySettings.embedding.model_key`.
 */

'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useTranslation } from 'react-i18next';
import Link from 'next/link';
import {
  Check,
  ChevronDown,
  Cloud,
  Package,
  Search,
  Server,
  Terminal,
} from 'lucide-react';

import { Icon } from '@/components/Icon/Icon';

import { MemoryField } from './MemoryField';
import { MemoryBanner } from './MemoryBanner';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ProviderKind =
  | 'lmstudio'
  | 'ollama'
  | 'cloud'
  | 'custom'
  | 'package'
  | 'unknown';

interface ProviderMeta {
  name: string;
  badge: string | null;
  kind: ProviderKind;
  /** Sort weight — lower numbers appear first. */
  order: number;
}

interface EmbeddingModelEntry {
  provider_id: string;
  provider_name: string;
  provider_badge: string | null;
  provider_kind: ProviderKind;
  provider_order: number;
  model_id: string;
  composite_key: string;
}

interface ProviderGroup {
  provider_id: string;
  name: string;
  badge: string | null;
  kind: ProviderKind;
  order: number;
  entries: EmbeddingModelEntry[];
}

// ---------------------------------------------------------------------------
// Vault → provider metadata
// ---------------------------------------------------------------------------

const _PROVIDER_ORDER: Record<ProviderKind, number> = {
  lmstudio: 10,
  ollama: 20,
  cloud: 30,
  custom: 40,
  package: 50,
  unknown: 90,
};

function collectProviderMetadata(
  vault: any,
): Record<string, ProviderMeta> {
  const meta: Record<string, ProviderMeta> = {};

  // 1. Provider packages (highest priority for display).
  const conns = vault?.provider_connections;
  if (Array.isArray(conns)) {
    for (const c of conns) {
      if (!c || typeof c !== 'object') continue;
      if (!c.is_active || !c.enabled) continue;
      const pid = c.provider_package_id;
      if (typeof pid !== 'string' || !pid) continue;
      meta[pid] = {
        name: typeof c.name === 'string' && c.name ? c.name : pid,
        badge: typeof c.badge === 'string' && c.badge ? c.badge : null,
        kind: 'package',
        order: _PROVIDER_ORDER.package,
      };
    }
  }

  // 2. Cloud providers.
  const clouds = vault?.cloud_vault;
  if (Array.isArray(clouds)) {
    for (const cp of clouds) {
      if (!cp || typeof cp !== 'object') continue;
      const pid = cp.id;
      if (typeof pid !== 'string' || !pid) continue;
      // Don't overwrite package metadata.
      if (meta[pid]) continue;
      meta[pid] = {
        name: typeof cp.name === 'string' && cp.name ? cp.name : pid,
        badge: typeof cp.badge === 'string' && cp.badge ? cp.badge : null,
        kind: 'cloud',
        order: _PROVIDER_ORDER.cloud,
      };
    }
  }

  // 3. Custom endpoints.
  const customs = vault?.custom_endpoints;
  if (Array.isArray(customs)) {
    for (const ce of customs) {
      if (!ce || typeof ce !== 'object') continue;
      const pid = ce.providerId || ce.id;
      if (typeof pid !== 'string' || !pid) continue;
      if (meta[pid]) continue;
      meta[pid] = {
        name: typeof ce.name === 'string' && ce.name ? ce.name : pid,
        badge: typeof ce.badge === 'string' && ce.badge ? ce.badge : null,
        kind: 'custom',
        order: _PROVIDER_ORDER.custom,
      };
    }
  }

  // 4. Local engines.
  if (vault?.lmstudio_vault && !meta.lmstudio) {
    meta.lmstudio = {
      name: 'LM Studio',
      badge:
        typeof vault.lmstudio_vault.badge === 'string' &&
        vault.lmstudio_vault.badge
          ? vault.lmstudio_vault.badge
          : null,
      kind: 'lmstudio',
      order: _PROVIDER_ORDER.lmstudio,
    };
  }
  if (vault?.ollama_vault && !meta.ollama) {
    meta.ollama = {
      name: 'Ollama',
      badge:
        typeof vault.ollama_vault.badge === 'string' &&
        vault.ollama_vault.badge
          ? vault.ollama_vault.badge
          : null,
      kind: 'ollama',
      order: _PROVIDER_ORDER.ollama,
    };
  }

  return meta;
}

function buildEntries(vault: any): EmbeddingModelEntry[] {
  const byProvider = vault?.embedding_models;
  if (!byProvider || typeof byProvider !== 'object') return [];

  const meta = collectProviderMetadata(vault);
  const out: EmbeddingModelEntry[] = [];

  for (const [pid, models] of Object.entries(byProvider)) {
    if (typeof pid !== 'string' || !pid) continue;
    if (!Array.isArray(models)) continue;

    const info: ProviderMeta =
      meta[pid] || {
        name: pid,
        badge: null,
        kind: 'unknown',
        order: _PROVIDER_ORDER.unknown,
      };

    for (const mid of models) {
      if (typeof mid !== 'string' || !mid.trim()) continue;
      out.push({
        provider_id: pid,
        provider_name: info.name,
        provider_badge: info.badge,
        provider_kind: info.kind,
        provider_order: info.order,
        model_id: mid,
        composite_key: `${pid}::${mid}`,
      });
    }
  }

  out.sort((a, b) => {
    if (a.provider_order !== b.provider_order)
      return a.provider_order - b.provider_order;
    if (a.provider_id !== b.provider_id)
      return a.provider_id.localeCompare(b.provider_id);
    return a.model_id.localeCompare(b.model_id);
  });

  return out;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EmbeddingModelSelectorProps {
  value: string | null;
  onChange: (key: string | null) => void;
  disabled?: boolean;
  error?: string;
  label?: string;
  help?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EmbeddingModelSelector({
  value,
  onChange,
  disabled = false,
  error,
  label,
  help,
}: EmbeddingModelSelectorProps) {
  const { t } = useTranslation('memory');
  const [isOpen, setIsOpen] = useState(false);
  const [vault, setVault] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const containerRef = useRef<HTMLDivElement>(null);

  // ---- Load vault ------------------------------------------------
  const load = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const res = await fetch('/api/config/vault', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setVault(await res.json());
    } catch (e) {
      setFetchError(
        e instanceof Error
          ? e.message
          : t('settings.embedding.loadFailed'),
      );
      setVault(null);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleToggle = () => {
    if (!isOpen) void load();
    setIsOpen(!isOpen);
  };

  useEffect(() => {
    if (!isOpen) return;
    const onDown = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [isOpen]);

  const entries = useMemo(
    () => (vault ? buildEntries(vault) : []),
    [vault],
  );

  // ---- Group by provider ----------------------------------------
  const grouped: ProviderGroup[] = useMemo(() => {
    const map = new Map<string, ProviderGroup>();
    for (const e of entries) {
      const existing = map.get(e.provider_id);
      if (existing) {
        existing.entries.push(e);
      } else {
        map.set(e.provider_id, {
          provider_id: e.provider_id,
          name: e.provider_name,
          badge: e.provider_badge,
          kind: e.provider_kind,
          order: e.provider_order,
          entries: [e],
        });
      }
    }
    return [...map.values()].sort((a, b) => {
      if (a.order !== b.order) return a.order - b.order;
      return a.name.localeCompare(b.name);
    });
  }, [entries]);

  // ---- Search filter --------------------------------------------
  const filtered: ProviderGroup[] = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return grouped;
    return grouped
      .map((g) => ({
        ...g,
        entries: g.entries.filter(
          (e) =>
            e.model_id.toLowerCase().includes(q) ||
            e.provider_name.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.entries.length > 0);
  }, [grouped, search]);

  const selected = useMemo(
    () => entries.find((e) => e.composite_key === value) || null,
    [entries, value],
  );
  const selectedIsVisible = useMemo(() => {
    if (!selected) return false;
    return filtered.some((g) =>
      g.entries.some((e) => e.composite_key === value),
    );
  }, [filtered, selected, value]);

  const showSearch = entries.length > 10;

  // ---- Provider icon --------------------------------------------
  const renderProviderIcon = (
    kind: ProviderKind,
    badge: string | null,
    size: number = 14,
  ): ReactNode => {
    if (badge && badge.trim()) {
      return <Icon name={badge.trim()} size={size} />;
    }
    switch (kind) {
      case 'lmstudio':
      case 'ollama':
        return <Server size={size} className="text-emerald-500" />;
      case 'cloud':
        return <Cloud size={size} className="text-sky-500" />;
      case 'custom':
        return <Terminal size={size} className="text-amber-500" />;
      case 'package':
        return <Package size={size} className="text-purple-500" />;
      default:
        return <Server size={size} className="text-muted-foreground" />;
    }
  };

  // ---- Loading --------------------------------------------------
  if (loading && !vault) {
    return (
      <MemoryField
        label={label ?? t('settings.embedding.modelLabel')}
        help={help}
        disabled
      >
        <div className="h-8 w-full animate-pulse rounded-sm bg-muted/40" />
      </MemoryField>
    );
  }

  // ---- Fetch error ---------------------------------------------
  if (fetchError && !vault) {
    return (
      <MemoryBanner
        tone="error"
        title={label ?? t('settings.embedding.modelLabel')}
        message={fetchError}
        action={
          <button
            type="button"
            onClick={() => void load()}
            className="text-[11px] font-medium underline underline-offset-2"
          >
            {t('common.retry')}
          </button>
        }
      />
    );
  }

  // ---- Empty vault ---------------------------------------------
  if (entries.length === 0) {
    return (
      <MemoryBanner
        tone="warning"
        title={label ?? t('settings.embedding.modelLabel')}
        message={
          <div className="space-y-1.5">
            <p>{t('settings.embedding.noModelsAvailable')}</p>
            <Link
              href="/settings/setup?tab=models"
              className="inline-flex items-center gap-1 font-medium underline underline-offset-2"
            >
              {t('settings.embedding.markModelAsEmbedding')}
              <span aria-hidden="true">&rarr;</span>
            </Link>
          </div>
        }
      />
    );
  }

  // ---- Normal render ------------------------------------------
  return (
    <MemoryField
      label={label ?? t('settings.embedding.modelLabel')}
      help={help ?? t('settings.embedding.modelHelp')}
      error={error}
      disabled={disabled}
    >
      <div className="relative" ref={containerRef}>
        {/* Trigger */}
        <button
          type="button"
          onClick={handleToggle}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          className={cn(
            'w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded-sm border border-border bg-card text-left text-xs transition-colors',
            disabled
              ? 'cursor-not-allowed opacity-60'
              : 'hover:bg-muted/30 cursor-pointer',
          )}
        >
          <span className="truncate flex-1 min-w-0 flex items-center gap-1.5">
            {selected ? (
              <>
                {renderProviderIcon(
                  selected.provider_kind,
                  selected.provider_badge,
                )}
                <span className="font-mono text-[11px] truncate">
                  {selected.model_id}
                </span>
                <span className="text-muted-foreground text-[10px]">
                  · {selected.provider_name}
                </span>
              </>
            ) : (
              <span className="text-muted-foreground italic">
                {t('settings.embedding.selectModelPlaceholder')}
              </span>
            )}
          </span>
          <ChevronDown
            size={12}
            className={cn(
              'shrink-0 text-muted-foreground transition-transform',
              isOpen && 'rotate-180',
            )}
          />
        </button>

        {/* Popover */}
        {isOpen && (
          <div
            role="listbox"
            className="absolute z-50 mt-1 w-full rounded-md border border-card bg-card shadow-lg max-h-96 overflow-y-auto p-1.5 space-y-1 animate-in fade-in"
          >
            {showSearch && (
              <div className="relative p-1 sticky top-0 bg-card z-10">
                <Search
                  size={11}
                  aria-hidden="true"
                  className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"
                />
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('settings.embedding.searchPlaceholder')}
                  aria-label={t('settings.embedding.searchLabel')}
                  className="w-full pl-6 pr-2 py-1 text-xs rounded-sm border border-border bg-background"
                  autoFocus
                />
              </div>
            )}

            {/* Pinned current selection (when hidden by search) */}
            {selected && !selectedIsVisible && (
              <div className="border-b border-border/40 pb-1 mb-1">
                <div className="px-2 py-1 text-[9px] font-semibold text-muted-foreground uppercase tracking-wider">
                  {t('settings.embedding.currentSelection')}
                </div>
                <button
                  type="button"
                  role="option"
                  aria-selected={true}
                  onClick={() => {
                    onChange(selected.composite_key);
                    setIsOpen(false);
                  }}
                  className="w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded-sm bg-primary/10 border border-primary/30 text-left text-xs"
                >
                  <span className="flex items-center gap-1.5 min-w-0 flex-1">
                    {renderProviderIcon(
                      selected.provider_kind,
                      selected.provider_badge,
                    )}
                    <span className="font-mono truncate">
                      {selected.model_id}
                    </span>
                    <span className="text-muted-foreground text-[10px] shrink-0">
                      · {selected.provider_name}
                    </span>
                  </span>
                  <Check size={12} className="text-primary shrink-0" />
                </button>
              </div>
            )}

            {/* Empty search result */}
            {filtered.length === 0 && search.trim() !== '' && (
              <p className="px-2 py-3 text-[10px] text-muted-foreground italic text-center">
                {t('settings.embedding.noSearchResults')}
              </p>
            )}

            {/* Provider groups */}
            {filtered.map((group) => (
              <div key={`${group.provider_id}`}>
                {/* Group header — matches EngineSelector's style */}
                <div className="px-2 py-1 text-[11px] font-semibold text-foreground flex items-center justify-between">
                  <span className="flex items-center gap-1.5 min-w-0">
                    {renderProviderIcon(group.kind, group.badge, 12)}
                    <span className="truncate">{group.name}</span>
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono shrink-0 ml-2">
                    {group.entries.length}{' '}
                    {group.entries.length === 1 ? 'model' : 'models'}
                  </span>
                </div>

                {/* Model rows */}
                <div className="space-y-0.5 pl-3">
                  {group.entries.map((e) => {
                    const isSelected = e.composite_key === value;
                    return (
                      <button
                        key={e.composite_key}
                        type="button"
                        role="option"
                        aria-selected={isSelected}
                        onClick={() => {
                          onChange(e.composite_key);
                          setIsOpen(false);
                        }}
                        className={cn(
                          'w-full flex items-center justify-between gap-2 px-2 py-1 rounded-sm text-xs text-left transition-colors',
                          isSelected
                            ? 'bg-primary/10 text-primary font-medium border border-primary/30'
                            : 'hover:bg-muted/50 text-foreground cursor-pointer',
                        )}
                      >
                        <span className="font-mono truncate flex-1 min-w-0">
                          {e.model_id}
                        </span>
                        {isSelected && (
                          <Check
                            size={12}
                            className="text-primary shrink-0 ml-1"
                          />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}

            {/* Clear selection footer */}
            {value !== null && (
              <div className="border-t border-border/40 pt-1 mt-1">
                <button
                  type="button"
                  onClick={() => {
                    onChange(null);
                    setIsOpen(false);
                  }}
                  className="w-full px-2 py-1 text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted/30 rounded-sm text-left cursor-pointer"
                >
                  {t('settings.embedding.clearSelection')}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </MemoryField>
  );
}
