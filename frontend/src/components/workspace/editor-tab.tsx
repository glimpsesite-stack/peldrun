//frontend\src\components\workspace\editor-tab.tsx
"use client";

import React, { useEffect, useState, useRef } from "react";
import { Highlight, themes } from "prism-react-renderer";
import { Save, FileCode, Check, Copy, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/layout/theme-provider";

export interface EditorTabProps {
  filePath?: string | null;
  initialContent?: string | null;
  activeJobId?: string | null;
  /**
   * When provided, file reads and writes target the project's
   * workspace (external or internal), not the chat session's
   * directory. This is required for the Editor to work correctly
   * inside `ProjectWorkspacePanel` — the agent writes deliverables
   * to the project workspace, and the editor must read/write from
   * the same location.
   */
  projectId?: string | null;
  onSave?: (filePath: string, content: string) => void;
}

const extToLangMap: Record<string, string> = {
  html: "markup",
  htm: "markup",
  svg: "markup",
  xml: "markup",
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  py: "python",
  css: "css",
  json: "json",
  sh: "bash",
  bash: "bash",
  md: "markdown",
  markdown: "markdown",
  sql: "sql",
  yaml: "yaml",
  yml: "yaml",
  rs: "rust",
  go: "go",
  c: "c",
  cpp: "cpp",
  txt: "text",
};

export function EditorTab({ filePath, initialContent, activeJobId, projectId, onSave }: EditorTabProps) {
  const { theme } = useTheme();
  const [content, setContent] = useState<string>(initialContent || "");

  // Determine if the current theme is dark to apply the correct syntax highlighting
  const isDark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);

  // SHA-256 version hash returned by the project workspace endpoint.
  // Used for Optimistic Concurrency Control (OCC) on save — if the
  // file changed since we last read it, the PUT is rejected with 409
  // instead of silently overwriting another writer's changes.
  const [version, setVersion] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const preRef = useRef<HTMLPreElement | null>(null);
  const gutterRef = useRef<HTMLDivElement | null>(null);

  const fileExt = filePath ? (filePath.split(".").pop()?.toLowerCase() || "") : "txt";
  const detectedLanguage = extToLangMap[fileExt] || "javascript";
  const displayLang = (fileExt || "TEXT").toUpperCase();

  useEffect(() => {
    if (initialContent !== undefined && initialContent !== null) {
      setContent(initialContent);
      return;
    }

    if (!filePath) {
      setContent("");
      return;
    }

    let cancelled = false;

    const loadFile = async () => {
      setLoading(true);
      setStatusMsg(null);
      setVersion("");
      try {
        let res: Response | null = null;

        // ── 1. Project-scoped read (preferred when projectId is set) ──
        //
        // Files inside a project live in the project workspace, not
        // the chat-scoped directory. Reading from the project endpoint
        // guarantees we always see the file the agent wrote, and it
        // returns a `version` hash that the save handler uses for OCC.
        if (projectId) {
          try {
            const projectRes = await fetch(
              `/api/projects/${encodeURIComponent(projectId)}/workspace/file?path=${encodeURIComponent(filePath)}&t=${Date.now()}`,
              { cache: "no-store" }
            );
            if (projectRes.ok) {
              const data = await projectRes.json();
              if (!cancelled) {
                setContent(data.content || "");
                setVersion(data.version || "");
                setLoading(false);
              }
              return;
            }
          } catch {
            /* fall through to chat-scoped / generic */
          }
        }

        // ── 2. Chat-scoped read (legacy chats) ──
        if (activeJobId) {
          try {
            const chatRes = await fetch(
              `/api/run/jobs/${activeJobId}/content?path=${encodeURIComponent(filePath)}&t=${Date.now()}`,
              { cache: "no-store" }
            );
            if (chatRes.ok) res = chatRes;
          } catch {
            /* fall through */
          }
        }

        // ── 3. Generic files endpoint (with project_id for correct resolution) ──
        if (!res || !res.ok) {
          const query = projectId
            ? `&project_id=${encodeURIComponent(projectId)}`
            : "";
          res = await fetch(
            `/api/files/content?path=${encodeURIComponent(filePath)}${query}&t=${Date.now()}`,
            { cache: "no-store" }
          );
        }

        if (res && res.ok) {
          const data = await res.json();
          if (!cancelled) {
            setContent(data.content || "");
            setVersion(data.version || "");
          }
        } else if (!cancelled) {
          setContent("// Error loading file or binary file.");
        }
      } catch {
        if (!cancelled) setContent("// Failed to fetch file content.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    loadFile();
    return () => {
      cancelled = true;
    };
  }, [filePath, initialContent, activeJobId, projectId]);

  const handleScroll = (e: React.UIEvent<HTMLTextAreaElement>) => {
    const target = e.currentTarget;
    if (preRef.current) {
      preRef.current.scrollTop = target.scrollTop;
      preRef.current.scrollLeft = target.scrollLeft;
    }
    if (gutterRef.current) {
      gutterRef.current.scrollTop = target.scrollTop;
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const textarea = e.currentTarget;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const newContent = content.substring(0, start) + "  " + content.substring(end);
      setContent(newContent);
      setTimeout(() => {
        textarea.selectionStart = textarea.selectionEnd = start + 2;
      }, 0);
    }
  };

  const handleCopy = async () => {
    if (!content) return;
    try {
      if (typeof window !== "undefined" && navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(content);
      } else {
        const textArea = document.createElement("textarea");
        textArea.value = content;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        textArea.style.top = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        textArea.remove();
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  };

  const handleSave = async () => {
    if (!filePath) return;
    setSaving(true);
    setStatusMsg(null);
    try {
      let res: Response | null = null;

      // ── 1. Project-scoped PUT (OCC-based) ──
      //
      // Uses the version hash captured at read time. If the file was
      // modified by the agent between our read and this save, the
      // backend rejects with 409 and we surface the conflict instead
      // of silently overwriting. This is the only path that preserves
      // concurrent edits — the POST fallbacks below always overwrite.
      if (projectId && version) {
        try {
          const projectRes = await fetch(
            `/api/projects/${encodeURIComponent(projectId)}/workspace/file`,
            {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                path: filePath,
                content,
                expected_version: version,
              }),
            }
          );
          if (projectRes.ok) {
            const data = await projectRes.json();
            setVersion(data.version || version);
            res = projectRes;
          } else if (projectRes.status === 409) {
            // The file was modified since we loaded it. Do NOT
            // overwrite — ask the user to reload.
            setStatusMsg(
              "Conflict: file was modified by another process. Reload to see latest."
            );
            return;
          }
          // Non-409 failure — fall through to POST fallbacks
        } catch {
          /* fall through */
        }
      }

      // ── 2. Chat-scoped POST (legacy) ──
      if (!res || !res.ok) {
        if (activeJobId) {
          try {
            const chatRes = await fetch(`/api/run/jobs/${activeJobId}/content`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: filePath, content }),
            });
            if (chatRes.ok) res = chatRes;
          } catch {
            /* fall through */
          }
        }
      }

      // ── 3. Generic POST (supports project_id for correct path resolution) ──
      if (!res || !res.ok) {
        res = await fetch("/api/files/content", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: filePath,
            content,
            project_id: projectId || null,
          }),
        });
      }

      if (res && res.ok) {
        setStatusMsg("Saved successfully");
        onSave?.(filePath, content);
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:file-saved", {
              detail: { path: filePath, content, projectId },
            })
          );
        }
        setTimeout(() => setStatusMsg(null), 3500);
      } else {
        const errData = res ? await res.json().catch(() => ({})) : {};
        setStatusMsg(errData.detail || "Failed to save");
      }
    } catch {
      setStatusMsg("Save error");
    } finally {
      setSaving(false);
    }
  };

  const lines = content.split("\n");
  const lineCount = Math.max(1, lines.length);
  const HighlightComponent = Highlight as any;

  return (
    <div className="flex flex-col h-full dark:bg-[#0d1117] bg-white dark:text-slate-100 text-slate-900 font-sans select-text">
      <div className="h-10 border-b dark:border-[#30363d] border-gray-200 dark:bg-[#161b22] bg-gray-50 px-3.5 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <FileCode size={14} className="text-[#305CDE] shrink-0" />
          <span className="font-mono text-xs font-semibold truncate max-w-[180px] dark:text-[#eee] text-slate-800">
            {filePath || "draft-snippet"}
          </span>
          <span className="px-1.5 py-0.5 rounded text-[10px] font-mono dark:bg-[#DEB230]/30 bg-yellow-100 border border-border/40 shrink-0 dark:text-slate-300 text-slate-700">
            {displayLang}
          </span>
          <span className="text-[11px] font-mono text-muted-foreground hidden sm:inline">
            ({lineCount} lines)
          </span>
          {initialContent && !filePath && (
            <span className="px-1.5 py-0.5 rounded text-[10px] bg-primary/10 text-primary border border-primary/20 shrink-0">
              Draft
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {saving && (
            <span className="flex items-center gap-1 text-[11px] font-mono text-muted-foreground">
              <Loader2 size={11} className="animate-spin text-primary" />
              <span>Saving...</span>
            </span>
          )}

          {!saving && statusMsg && (
            <span
              className={`flex items-center gap-1 text-[11px] font-mono ${
                statusMsg.includes("success") ? "text-peldrun-success" : "text-peldrun-error"
              }`}
            >
              {statusMsg.includes("success") && <Check size={11} />}
              <span>{statusMsg}</span>
            </span>
          )}

          <button
            type="button"
            onClick={handleCopy}
            disabled={!content || saving}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] font-mono
            text-muted-foreground hover:text-card hover:bg-black transition-all cursor-pointer disabled:opacity-30"
            title="Copy file content"
          >
            {copied ? (
              <>
                <Check size={12} className="text-peldrun-success" />
                <span className="text-peldrun-success">Copied</span>
              </>
            ) : (
              <>
                <Copy size={12} />
                <span className="hidden sm:inline">Copy</span>
              </>
            )}
          </button>

          <Button
            variant="save"
            size="sm"
            onClick={handleSave}
            disabled={saving || loading || !filePath}
            className="h-7 px-2.5 text-xs shadow-peldrun-xs cursor-pointer font-sans min-w-[70px]"
            title="Save changes to disk"
          >
            {saving ? (
              <>
                <Loader2 size={12} className="mr-1 animate-spin" />
                <span>Saving...</span>
              </>
            ) : (
              <>
                <Save size={12} className="mr-1" />
                <span>Save</span>
              </>
            )}
          </Button>
        </div>
      </div>

      <div className="relative flex-1 flex min-h-0 dark:bg-[#0d1117] bg-white overflow-hidden">
        <div
          ref={gutterRef}
          aria-hidden="true"
          className="select-none py-3 pl-3 pr-2.5 text-right font-mono text-[11px] leading-[20px] dark:text-slate-500 text-gray-500 dark:bg-[#161b22]/70 bg-gray-100 border-r dark:border-[#30363d]/60 border-gray-200 overflow-hidden w-11 shrink-0"
        >
          {Array.from({ length: lineCount }, (_, i) => (
            <div key={i}>{i + 1}</div>
          ))}
        </div>

        <div className="relative flex-1 min-w-0 h-full overflow-hidden">
          <pre
            ref={preRef}
            aria-hidden="true"
            className="absolute inset-0 m-0 p-3 font-mono text-xs leading-[20px] whitespace-pre overflow-hidden pointer-events-none bg-transparent"
            style={{ tabSize: 2 }}
          >
            <HighlightComponent
                theme={isDark ? themes.nightOwl : themes.vsLight}
              code={content || " "}
              language={detectedLanguage}
            >
              {({ tokens, getTokenProps }: any) => (
                <code>
                  {tokens.map((line: any, i: number) => (
                    <div key={i} className="leading-[20px]">
                      {line.map((token: any, key: number) => (
                        <span key={key} {...getTokenProps({ token })} />
                      ))}
                    </div>
                  ))}
                </code>
              )}
            </HighlightComponent>
          </pre>

          <textarea
            ref={textareaRef}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onScroll={handleScroll}
            onKeyDown={handleKeyDown}
            disabled={loading}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            autoCorrect="off"
            className="absolute inset-0 w-full h-full p-3 font-mono text-xs leading-[20px] whitespace-pre bg-transparent text-transparent dark:caret-white caret-black resize-none outline-none overflow-auto border-0 focus:ring-0 selection:bg-primary/35 shadow-none"
            style={{ tabSize: 2 }}
            placeholder={loading ? "Loading file..." : "Type or paste code here..."}
          />
        </div>
      </div>
    </div>
  );
}

export default EditorTab;
