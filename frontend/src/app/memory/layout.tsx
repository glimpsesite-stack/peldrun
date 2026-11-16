// src/app/memory/layout.tsx

/**
 * Layout for the /memory/* route group.
 *
 * Client-only rendering (via the `mounted` gate)
 * -------------------------------------------------
 *
 * i18n is initialized on the CLIENT by `I18nProvider`
 * (its `useEffect` runs after hydration). On the server,
 * `useTranslation('memory')` would return raw keys like
 * `"nav.title"` instead of the translated value, causing a
 * hydration mismatch the moment the client swaps to the real
 * translation. React then discards the server HTML and
 * re-renders the entire tree -- including the root layout,
 * whose `<script>` tag triggers a spurious warning.
 *
 * The fix is to render a **static skeleton** on the server
 * and during the first client paint, then swap to the full
 * layout after `useEffect` fires (which happens AFTER
 * hydration completes). Because the skeleton is identical on
 * both sides, hydration succeeds silently. The full layout
 * renders as a normal client-side update -- no hydration, no
 * warning, no mismatch.
 *
 * The trade-off: `/memory/*` pages are not server-rendered.
 * For an authenticated admin area with no SEO requirement,
 * this is acceptable. If SEO ever becomes a concern, the
 * proper fix is to initialize i18n on the server per request
 * (a much larger refactor involving `i18next` + App Router
 * RSC).
 */

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useTranslation } from 'react-i18next';
import { useEffect, useState } from 'react';
import {
  BarChart3,
  Brain,
  Download,
  FolderOpen,
  LayoutDashboard,
  Search,
  Settings,
  Upload,
  type LucideIcon,
} from 'lucide-react';

import { cn } from '../../features/memory/lib/cn';
import { ModeBadge } from '../../features/memory/components/ModeBadge';

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

interface NavItem {
  href: string;
  labelKey: string;
  icon: LucideIcon;
  exact: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { href: '/memory', labelKey: 'nav.dashboard', icon: LayoutDashboard, exact: true },
  { href: '/memory/browse', labelKey: 'nav.browse', icon: Search, exact: false },
  { href: '/memory/projects', labelKey: 'nav.projects', icon: FolderOpen, exact: false },
  { href: '/memory/import', labelKey: 'nav.import', icon: Upload, exact: false },
  { href: '/memory/export', labelKey: 'nav.export', icon: Download, exact: false },
  { href: '/memory/usage', labelKey: 'nav.usage', icon: BarChart3, exact: false },
  { href: '/memory/settings', labelKey: 'nav.settings', icon: Settings, exact: false },
];

function isActivePath(pathname: string, href: string, exact: boolean): boolean {
  if (exact) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MemoryLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { t } = useTranslation('memory');
  const pathname = usePathname() ?? '';
  const [mounted, setMounted] = useState(false);

  // Flip `mounted` after hydration completes. On the server
  // this effect never runs, so the skeleton is what ships in
  // the initial HTML -- matching what the client renders on
  // its first paint, and therefore never triggering a
  // hydration mismatch.
  useEffect(() => {
    setMounted(true);
  }, []);

  // ---- Server + first client paint: skeleton --------------------
  if (!mounted) {
    return <MemoryLayoutSkeleton />;
  }

  // ---- Post-mount: full layout ----------------------------------
  return (
    <div className="relative h-full w-full overflow-y-auto bg-setbg text-foreground font-sans flex flex-col">
      {/* ============ Ambient background ============ */}
      <div
        className="pointer-events-none absolute inset-0 overflow-hidden"
        aria-hidden="true"
      >
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-emerald-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-amber-500/5 blur-3xl" />
      </div>

      {/* ============ Sticky header ============ */}
      <header className="border-b border-darkest bg-darkest backdrop-blur-xl px-6 py-4 ">
        <div className="max-w-full mx-auto flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5">
            <div className="p-2 icon">
              <Brain size={16} className="text-slate-100" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-sm text-[#fff] font-semibold font-heading tracking-wide">
                {t('nav.title')}
              </h1>
              <p className="text-[10px] text-muted-foreground">
                PELDRUN Memory Platform
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <Link
              href="/memory/settings"
              className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              aria-label={t('nav.settings')}
            >
              <ModeBadge size="sm" />
            </Link>
          </div>
        </div>
      </header>

      {/* ============ Sub-navigation ============ */}
      <nav
        className="border-b border-dark bg-dark sticky top-0 z-20 backdrop-blur-sm"
        aria-label={t('a11y.mainNavigation')}
      >
        <div className="max-w-7xl mx-auto">
          <ul className="flex flex-wrap items-center gap-1 px-6 py-1.5">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = isActivePath(pathname, item.href, item.exact);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? 'page' : undefined}
                    className={cn(
                      'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium transition-colors',
                      active
                        ? 'bg-primary/10 text-amber-400'
                        : 'text-[#fff] hover:bg-darkest hover:text-amber-300',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-background',
                    )}
                  >
                    <Icon size={12} aria-hidden="true" />
                    {t(item.labelKey)}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      </nav>

      {/* ============ Page content ============ */}
      <main className="max-w-7xl w-full mx-auto p-6 flex-1 relative">
        {children}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------

/**
 * The skeleton rendered on the server and during the first
 * client paint.
 *
 * It intentionally mirrors the real layout's STRUCTURE (same
 * paddings, same section heights) so that the swap to the full
 * layout is visually seamless -- no layout shift, no flash.
 *
 * It contains NO translated text and NO data-dependent values,
 * so it renders identically on the server and the client,
 * which is precisely what makes hydration succeed.
 */
function MemoryLayoutSkeleton() {
  return (
    <div
      className="flex min-h-full flex-col bg-background text-foreground"
      aria-busy="true"
      aria-live="polite"
    >
      {/* Header */}
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-background/60 px-4 py-3 backdrop-blur">
        <div className="flex items-center gap-2">
          <div
            className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/10 text-primary"
            aria-hidden="true"
          >
            <Brain size={16} strokeWidth={1.8} />
          </div>
          <div className="min-w-0 space-y-1">
            <div className="h-3.5 w-24 animate-pulse rounded bg-muted" />
            <div className="h-2 w-32 animate-pulse rounded bg-muted" />
          </div>
        </div>
        <div className="h-5 w-16 animate-pulse rounded-full bg-muted" />
      </header>

      {/* Sub-navigation */}
      <nav className="border-b border-border bg-background/40">
        <div className="flex flex-wrap items-center gap-1 px-2 py-1">
          {[0, 1, 2, 3, 4, 5, 6].map((i) => (
            <div
              key={i}
              className="h-6 w-20 animate-pulse rounded-md bg-muted"
            />
          ))}
        </div>
      </nav>

      {/* Content area */}
      <main className="flex-1 p-4">
        <div className="mx-auto max-w-6xl space-y-3">
          <div className="h-5 w-40 animate-pulse rounded bg-muted" />
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-16 animate-pulse rounded-lg border border-border bg-muted/40"
              />
            ))}
          </div>
          <div className="h-32 animate-pulse rounded-lg border border-border bg-muted/40" />
        </div>
      </main>
    </div>
  );
}
