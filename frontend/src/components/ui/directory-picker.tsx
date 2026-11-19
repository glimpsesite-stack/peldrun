// frontend/src/components/ui/directory-picker.tsx

"use client";

/**
 * DirectoryPicker — Cross-platform folder selection modal.
 *
 * Backed by GET /api/projects/browse-directories (projects.py).
 * Because PELDRUN runs locally (frontend + backend on the same
 * machine), this modal can display the real filesystem even
 * though browsers cannot expose absolute paths on their own.
 *
 * Features:
 * - Shortcuts sidebar (drives on Windows, roots on Unix)
 * - Editable path bar with Enter-to-navigate
 * - Up button to climb to the parent directory
 * - Click any folder row to descend
 * - Select button returns the current directory
 * - Escape closes; clicking the backdrop closes
 * - Graceful error states (permission denied, not found)
 * - Portals to document.body so parent overflow does not clip
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "motion/react";
import {
  Folder,
  FolderOpen,
  ChevronRight,
  HardDrive,
  Home,
  X,
  Loader2,
  AlertCircle,
  ArrowUp,
  RefreshCw,
  Check,
} from "lucide-react";
import { Button } from "@/components/ui/button";

/* ───────── Types ───────── */

interface DirEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

interface BrowseResponse {
  current: string;
  parent: string | null;
  entries: DirEntry[];
  roots: DirEntry[];
}

export interface DirectoryPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Starting directory. When omitted, the picker opens on shortcuts. */
  initialPath?: string;
  /** Called with the absolute path of the selected folder. */
  onSelect: (path: string) => void;
  title?: string;
}

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

/* ───────── Component ───────── */

export function DirectoryPicker({
  open,
  onOpenChange,
  initialPath,
  onSelect,
  title = "Select Workspace Folder",
}: DirectoryPickerProps) {
  const [mounted, setMounted] = useState(false);
  const [data, setData] = useState<BrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pathInput, setPathInput] = useState("");
  const pathInputRef = useRef<HTMLInputElement>(null);

  // SSR guard — createPortal requires document.body.
  useEffect(() => setMounted(true), []);

  /* ── Fetch a directory ── */
  const fetchDir = useCallback(async (path: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const url = path
        ? `/api/projects/browse-directories?path=${encodeURIComponent(path)}`
        : `/api/projects/browse-directories`;
      const res = await fetch(url, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const json: BrowseResponse = await res.json();
      setData(json);
      setPathInput(json.current || "");
    } catch (err: any) {
      setError(err?.message || "Failed to load directory");
    } finally {
      setLoading(false);
    }
  }, []);

  /* ── Initial load on open ── */
  useEffect(() => {
    if (!open) return;
    setData(null);
    setError(null);
    setPathInput(initialPath || "");
    fetchDir(initialPath || null);
  }, [open, initialPath, fetchDir]);

  /* ── Escape to close ── */
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onOpenChange]);

  /* ── Path form submit ── */
  const handlePathSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = pathInput.trim();
    if (trimmed) fetchDir(trimmed);
  };

  /* ── Selection confirm ── */
  const handleSelect = () => {
    if (!data?.current) return;
    onSelect(data.current);
    onOpenChange(false);
  };

  if (!mounted) return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          key="dir-picker-backdrop"
          className="fixed inset-0 z-[100] flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => onOpenChange(false)}
            aria-hidden="true"
          />

          {/* Modal */}
          <motion.div
            className="relative z-10 w-full max-w-3xl bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
            style={{ maxHeight: "min(80vh, 640px)" }}
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.97 }}
            transition={{ duration: 0.25, ease: EASE_OUT }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="dir-picker-title"
          >
            {/* ── Header ── */}
            <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-border bg-card shrink-0">
              <div className="flex items-start gap-3">
                <div className="p-2 bg-accent/10 rounded-lg shrink-0 mt-0.5">
                  <FolderOpen size={18} className="text-accent" />
                </div>
                <div>
                  <h2
                    id="dir-picker-title"
                    className="text-base font-semibold leading-tight"
                  >
                    {title}
                  </h2>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Browse and select a directory on this machine.
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>

            {/* ── Body ── */}
            <div className="flex flex-1 min-h-0 overflow-hidden">
              {/* Shortcuts sidebar */}
              <aside className="w-56 shrink-0 border-r border-border bg-muted/20 overflow-y-auto">
                <div className="px-3 py-2 text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">
                  Shortcuts
                </div>
                <ul className="pb-2">
                  {(data?.roots || []).map((root) => {
                    const isActive = data?.current === root.path;
                    const isHome = /^\/home\/|\/Users\//.test(root.path);
                    const Icon = isHome ? Home : HardDrive;
                    return (
                      <li key={root.path}>
                        <button
                          type="button"
                          onClick={() => fetchDir(root.path)}
                          className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left truncate cursor-pointer transition-colors ${
                            isActive
                              ? "bg-accent/10 text-accent font-medium"
                              : "text-foreground/80 hover:bg-muted hover:text-foreground"
                          }`}
                          title={root.path}
                        >
                          <Icon
                            size={12}
                            className={
                              isActive
                                ? "text-accent shrink-0"
                                : "text-muted-foreground shrink-0"
                            }
                          />
                          <span className="truncate">{root.name}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </aside>

              {/* Main listing */}
              <div className="flex-1 flex flex-col min-w-0">
                {/* Path bar */}
                <div className="px-4 py-3 border-b border-border bg-card shrink-0">
                  <form
                    onSubmit={handlePathSubmit}
                    className="flex items-center gap-2"
                  >
                    <button
                      type="button"
                      onClick={() =>
                        data?.parent && fetchDir(data.parent)
                      }
                      disabled={!data?.parent}
                      className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                      title="Parent directory"
                      aria-label="Parent directory"
                    >
                      <ArrowUp size={14} />
                    </button>
                    <div className="flex-1 relative">
                      <input
                        ref={pathInputRef}
                        type="text"
                        value={pathInput}
                        onChange={(e) => setPathInput(e.target.value)}
                        placeholder="Type a path and press Enter…"
                        className="w-full px-3 py-1.5 bg-background border border-border rounded-md font-mono text-xs
                                   focus:outline-none focus:ring-2 focus:ring-accent/40 focus:border-accent
                                   transition-shadow"
                        spellCheck={false}
                        autoComplete="off"
                      />
                    </div>
                    <button
                      type="submit"
                      className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
                      title="Go"
                      aria-label="Go"
                    >
                      <ChevronRight size={14} />
                    </button>
                  </form>
                </div>

                {/* Entries */}
                <div className="flex-1 overflow-y-auto">
                  {loading && (
                    <div className="flex flex-col items-center justify-center py-16 gap-3">
                      <Loader2
                        size={24}
                        className="animate-spin text-accent"
                      />
                      <p className="text-xs text-muted-foreground">
                        Loading…
                      </p>
                    </div>
                  )}

                  {!loading && error && (
                    <div className="flex flex-col items-center justify-center py-12 px-6 gap-3 text-center">
                      <div className="p-2.5 bg-destructive/10 rounded-full">
                        <AlertCircle
                          size={20}
                          className="text-destructive"
                        />
                      </div>
                      <p className="text-sm text-muted-foreground max-w-md break-words">
                        {error}
                      </p>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          fetchDir(data?.current || initialPath || null)
                        }
                        className="gap-1.5"
                      >
                        <RefreshCw size={13} />
                        Retry
                      </Button>
                    </div>
                  )}

                  {!loading &&
                    !error &&
                    data &&
                    data.entries.length === 0 && (
                      <div className="flex flex-col items-center justify-center py-16 gap-2 text-center">
                        <Folder
                          size={32}
                          className="text-muted-foreground/50"
                        />
                        <p className="text-xs text-muted-foreground">
                          This folder has no subfolders.
                        </p>
                      </div>
                    )}

                  {!loading &&
                    !error &&
                    data &&
                    data.entries.length > 0 && (
                      <ul>
                        {data.entries.map((entry) => (
                          <li key={entry.path}>
                            <button
                              type="button"
                              onClick={() => fetchDir(entry.path)}
                              className="w-full flex items-center gap-2.5 px-4 py-2 text-sm text-left hover:bg-muted transition-colors cursor-pointer group"
                              title={entry.path}
                            >
                              <Folder
                                size={14}
                                className="text-amber-500 shrink-0"
                              />
                              <span className="truncate flex-1">
                                {entry.name}
                              </span>
                              <ChevronRight
                                size={13}
                                className="text-muted-foreground opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all shrink-0"
                              />
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                </div>
              </div>
            </div>

            {/* ── Footer ── */}
            <div className="px-6 py-3 border-t border-border bg-card flex items-center gap-3 shrink-0">
              <div className="flex-1 min-w-0">
                <div className="text-[10px] uppercase tracking-wider font-medium text-muted-foreground mb-0.5">
                  Selected folder
                </div>
                <div className="text-xs font-mono truncate text-foreground">
                  {data?.current || "—"}
                </div>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleSelect}
                disabled={!data?.current || loading}
                className="gap-1.5"
              >
                <Check size={14} />
                Select This Folder
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
  );
}
