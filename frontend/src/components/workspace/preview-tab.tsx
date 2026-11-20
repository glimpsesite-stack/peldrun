// frontend/src/components/workspace/preview-tab.tsx
"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Code, RefreshCw, ExternalLink, Code2, Sparkles, FileText, Image as ImageIcon, AlignLeft } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { Icon } from '@/components/Icon/Icon';

export interface PreviewTabProps {
  jobId?: string | null;
  activeJobId?: string | null;
  currentHtmlPath?: string | null;
  filePath?: string | null;
  overrideFile?: string | null;
  /**
   * When provided, content reads and raw asset URLs target the
   * project's workspace (external or internal) instead of the chat
   * session's directory. Required for correct previews inside
   * `ProjectWorkspacePanel` — the agent writes deliverables to the
   * project workspace, and the preview must read from the same place.
   */
  projectId?: string | null;
}

export function PreviewTab({
  jobId,
  activeJobId,
  currentHtmlPath,
  filePath,
  overrideFile,
  projectId,
}: PreviewTabProps) {
  const effectiveJobId = jobId || activeJobId || null;
  // Monotonic request counter. Polling + refresh events can overlap
  // fetches; only the newest response is allowed to touch state.
  const fetchSeqRef = useRef(0);
  const [autoFile, setAutoFile] = useState<string | null>(null);
  const explicitFile = overrideFile || currentHtmlPath || filePath || null;
  const effectiveFile = explicitFile || autoFile;

  // Normalize the selected path to a workspace-relative form.
  //
  // Two different shapes reach this component:
  //   * "my site/index.html" — artifact events and Files tab clicks,
  //     which come from the Workspace API listing.
  //   * "storage/projects/<id>/workspace/my site/index.html" — the
  //     selection hydrated after a page reload, which comes from the
  //     /api/files artifact walk and the job listings.
  //
  // Deriving <base href> from the second shape produced asset URLs the
  // workspace root could not resolve, so CSS/JS silently dropped out of
  // the preview after F5 while a Files tab click restored them. Every
  // project-scoped read and URL below uses this normalized value, so
  // both entry points are byte-identical.
  const workspaceFile = useMemo(() => {
    if (!effectiveFile) return null;
    const clean = effectiveFile.replace(/\\/g, "/").replace(/^\/+/, "");
    if (!projectId) return clean;
    const prefixes = [
      `storage/projects/${projectId}/workspace/`,
      `projects/${projectId}/workspace/`,
    ];
    const lower = clean.toLowerCase();
    for (const prefix of prefixes) {
      if (lower.startsWith(prefix.toLowerCase())) {
        return clean.slice(prefix.length);
      }
    }
    return clean;
  }, [effectiveFile, projectId]);

  const [rawContent, setRawContent] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [refreshKey, setRefreshKey] = useState<number>(0);

  const lowerFile = (effectiveFile || "").toLowerCase();
  const isMarkdown = lowerFile.endsWith(".md") || lowerFile.endsWith(".markdown");
  const isTxt = lowerFile.endsWith(".txt") || lowerFile.endsWith(".text");
  const isSvg = lowerFile.endsWith(".svg");
  const isHtml = lowerFile.endsWith(".html") || lowerFile.endsWith(".htm");
  const isPreviewable = isMarkdown || isTxt || isSvg || isHtml;

useEffect(() => {
    const handleSaved = (e: Event) => {
      const ce = e as CustomEvent<{ path?: string }>;
      const savedPath = ce.detail?.path?.toLowerCase() || "";
      // If current deliverable is an HTML application, any CSS, JS or workspace file modification must trigger live refresh
      if (
        !effectiveFile ||
        !savedPath ||
        isHtml ||
        savedPath.endsWith(effectiveFile.toLowerCase()) ||
        effectiveFile.toLowerCase().endsWith(savedPath)
      ) {
        setRefreshKey((k) => k + 1);
      }
    };

    const handleArtifact = () => {
      setRefreshKey((k) => k + 1);
    };

    window.addEventListener("peldrun:file-saved", handleSaved);
    window.addEventListener("peldrun:artifact-created", handleArtifact);
    window.addEventListener("peldrun:artifact-updated", handleArtifact);
    window.addEventListener("peldrun:run-completed", handleArtifact);

    return () => {
      window.removeEventListener("peldrun:file-saved", handleSaved);
      window.removeEventListener("peldrun:artifact-created", handleArtifact);
      window.removeEventListener("peldrun:artifact-updated", handleArtifact);
      window.removeEventListener("peldrun:run-completed", handleArtifact);
    };
  }, [effectiveFile, isHtml]);

  const scanForPreviewableDeliverables = useCallback(() => {
    if (!projectId && effectiveJobId && !explicitFile) {
      fetch(`/api/run/jobs/${effectiveJobId}/files?t=${Date.now()}`, { cache: "no-store" })
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data?.files && Array.isArray(data.files)) {
            const bestFile =
              data.files.find((f: any) => f.name.toLowerCase() === "index.html") ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".html") || l.endsWith(".htm");
              }) ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".md") || l.endsWith(".markdown");
              }) ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".txt") || l.endsWith(".text");
              }) ||
              data.files.find((f: any) => f.name.toLowerCase().endsWith(".svg"));

            if (bestFile) {
              const targetPath = bestFile.path || bestFile.name;
              if (targetPath !== autoFile) {
                setAutoFile(targetPath);
              }
            }
          }
        })
        .catch(() => {});
    }
  }, [projectId, effectiveJobId, explicitFile, autoFile]);

  useEffect(() => {
    scanForPreviewableDeliverables();
    const interval = setInterval(scanForPreviewableDeliverables, 3500);
    return () => clearInterval(interval);
  }, [scanForPreviewableDeliverables]);

  const fetchContent = useCallback(async () => {
    // Strictly prevent binary image files from being fetched as text
    if (!workspaceFile || !isPreviewable) {
      // Invalidate any in-flight fetch so it cannot repopulate content
      // after we switched to a non-previewable file.
      fetchSeqRef.current++;
      setRawContent("");
      return;
    }
    const seq = ++fetchSeqRef.current;
    setLoading(true);
    try {
      let res: Response | null = null;

      // ── 1. Project-scoped read (preferred when projectId is set) ──
      // Same endpoint the EditorTab reads from, so preview and editor
      // always show the exact same bytes the agent wrote. The path is
      // workspace-relative, so this now succeeds on a cold reload too
      // instead of falling through to the generic endpoint.
      if (projectId) {
        try {
          const projectRes = await fetch(
            `/api/projects/${encodeURIComponent(projectId)}/workspace/file?path=${encodeURIComponent(workspaceFile)}&t=${Date.now()}`,
            { cache: "no-store" }
          );
          if (projectRes.ok) {
            const data = await projectRes.json();
            if (seq === fetchSeqRef.current) {
              setRawContent(data.content || "");
            }
            return;
          }
        } catch {
          /* fall through to chat-scoped / generic */
        }
      }

      // ── 2. Chat-scoped read (legacy chats) ──
      if (effectiveJobId) {
        try {
          const chatRes = await fetch(
            `/api/run/jobs/${effectiveJobId}/content?path=${encodeURIComponent(workspaceFile)}&t=${Date.now()}`,
            { cache: "no-store" }
          );
          if (chatRes.ok) res = chatRes;
        } catch {
          /* fall through */
        }
      }

      // ── 3. Generic files endpoint (project_id keeps resolution correct) ──
      if (!res || !res.ok) {
        const query = projectId
          ? `&project_id=${encodeURIComponent(projectId)}`
          : "";
        res = await fetch(
          `/api/files/content?path=${encodeURIComponent(workspaceFile)}${query}&t=${Date.now()}`,
          { cache: "no-store" }
        );
      }

      if (seq !== fetchSeqRef.current) return; // superseded by a newer fetch
      if (res && res.ok) {
        const data = await res.json();
        if (seq === fetchSeqRef.current) {
          setRawContent(data.content || "");
        }
      } else {
        setRawContent("");
      }
    } catch (e) {
      if (seq === fetchSeqRef.current) {
        console.error("Failed to load preview content", e);
        setRawContent("");
      }
    } finally {
      if (seq === fetchSeqRef.current) setLoading(false);
    }
  }, [workspaceFile, effectiveJobId, projectId, isPreviewable, refreshKey]);

  useEffect(() => {
    fetchContent();
  }, [fetchContent]);

  const sandboxedHtml = useMemo(() => {
    if (!rawContent || !isHtml || !workspaceFile) return "";
    // Scope base href to the deliverable's parent folder inside the
    // workspace (e.g. "my site/"). workspaceFile is already normalized,
    // so a storage-prefixed selection hydrated after a page reload
    // yields exactly the same base as a Files tab click.
    const fileDir = workspaceFile.includes("/")
      ? workspaceFile.substring(0, workspaceFile.lastIndexOf("/") + 1)
      : "";
    const encodedDir = fileDir ? encodeURI(fileDir) : "";
    // Project scope MUST live in the path (not a query param): browsers
    // drop the base URL's query string when resolving relative
    // subresources, which would break every CSS/JS/image reference.
    const baseHref = projectId
      ? `/api/files/raw-project/${encodeURIComponent(projectId)}/${encodedDir}`
      : effectiveJobId
        ? `/api/run/jobs/${effectiveJobId}/raw/${encodedDir}`
        : `/api/files/raw/${encodedDir}`;
    const baseTag = `<base href="${baseHref}">`;

    // Cache-bust relative CSS and JS assets so modifications reload live in the iframe
    let processed = rawContent.replace(
      /(<link\s+[^>]*href=["'])(?!https?:\/\/|\/\/)([^"'>]+)(["'][^>]*>)/gi,
      (_match, p1, url, p3) => {
        const sep = url.includes("?") ? "&" : "?";
        return `${p1}${url}${sep}_t=${refreshKey}${p3}`;
      }
    );

    processed = processed.replace(
      /(<script\s+[^>]*src=["'])(?!https?:\/\/|\/\/)([^"'>]+)(["'][^>]*>)/gi,
      (_match, p1, url, p3) => {
        const sep = url.includes("?") ? "&" : "?";
        return `${p1}${url}${sep}_t=${refreshKey}${p3}`;
      }
    );

    if (processed.includes("<head>")) {
      return processed.replace("<head>", `<head>${baseTag}`);
    }
    return `${baseTag}${processed}`;
  }, [rawContent, isHtml, workspaceFile, effectiveJobId, projectId, refreshKey]);

  if (!effectiveFile || !isPreviewable || !rawContent) {
    return (
      <div className="flex flex-col items-center justify-center h-full w-full bg-background text-black p-8 select-none font-sans">
        <div className="flex flex-col items-center max-w-sm text-center space-y-4">
          <div className="p-4 rounded-md bg-card relative bg-opacity-5">
            <Code2 className="w-10 h-10 text-custom" />
            <Sparkles className="w-4 h-4 text-black/70 absolute top-2 right-2 animate-pulse" />
          </div>
          <div>
            <h3 className="text-sm font-semibold font-heading text-black">Sandbox Standby</h3>
            <p className="text-xs text-black/50 mt-1 leading-relaxed">
              Live web applications (.html, .svg), Markdown documents (.md), and plain text files (.txt) will render here in real time.
            </p>
          </div>
          {loading && (
            <div className="flex items-center gap-2 text-xs text-peldrun-accent font-medium">
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              <span>Loading deliverables...</span>
            </div>
          )}
        </div>
      </div>
    );
  }

  const rawUrl = projectId
    ? `/api/files/raw-project/${encodeURIComponent(projectId)}/${encodeURI(workspaceFile || "")}`
    : effectiveJobId
      ? `/api/run/jobs/${effectiveJobId}/raw/${encodeURI(effectiveFile || "")}`
      : `/api/files/raw/${encodeURI(effectiveFile || "")}`;

  const renderBadge = () => {
    if (isHtml) return <span className="iconname-html">
    <Icon name="html" size={15} /> </span>;
    if (isMarkdown) return <span className="iconname-markdown">
      <Icon name="markdown" size={15} /> </span>;
    if (isTxt) return <span className="px-1.5 py-0.5 rounded text-[10px] bg-peldrun-warning/15 text-peldrun-warning border border-peldrun-warning/30 font-mono">Plain Text</span>;
    if (isSvg) return <span className="iconname-svg">
    <Icon name="svg" size={15} /> </span>;
    return <span className="px-1.5 py-0.5 rounded text-[10px] bg-muted text-muted-foreground font-mono">Preview</span>;
  };

  return (
    <div className="flex flex-col h-full w-full bg-custom overflow-hidden font-sans">
      <div className="h-10 border-b border-custom bg-muted backdrop-blur-sm px-4 flex items-center justify-between shrink-0 text-xs">
        <div className="flex items-center gap-2 text-black min-w-0">
          {isMarkdown ? (
            <FileText className="w-4 h-4 text-primary shrink-0" />
          ) : isTxt ? (
            <AlignLeft className="w-4 h-4 text-peldrun-warning shrink-0" />
          ) : isSvg ? (
            <ImageIcon className="w-4 h-4 text-peldrun-info shrink-0" />
          ) : (
            <Code className="w-4 h-4 text-[#ccc] shrink-0" />
          )}
          {renderBadge()}
          <span className="font-mono text-xs font-medium truncate max-w-[200px]">{effectiveFile}</span>

        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={() => setRefreshKey((k) => k + 1)}
            disabled={loading}
            title="Refresh Live Preview"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>

          <a
            href={rawUrl}
            target="_blank"
            rel="noopener noreferrer"
            title="Open artifact in new window"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center gap-1 text-[11px] cursor-pointer"
          >
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </div>
      </div>

      <div className="flex-1 w-full h-full relative overflow-hidden bg-background">
        {loading && rawContent && (
          <div className="absolute top-3 right-3 z-20 flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-card/90 backdrop-blur-sm border border-border shadow-peldrun-sm text-[11px] text-muted-foreground pointer-events-none select-none">
            <RefreshCw className="w-3 h-3 animate-spin text-peldrun-accent" />
            <span>Refreshing...</span>
          </div>
        )}

        {isMarkdown && (
          <div className="w-full h-full overflow-y-auto p-4 bg-card/40">
            <div className="mx-auto rounded-sm border border-border/80 bg-card p-6 shadow-peldrun-sm">
              <MarkdownRenderer content={rawContent} />
            </div>
          </div>
        )}

        {isTxt && (
          <div className="w-full h-full overflow-y-auto p-2 bg-card/40">
            <div className="mx-auto rounded-sm border border-border/80 bg-card p-6 shadow-peldrun-sm">
              <pre className="whitespace-pre-wrap break-words font-mono text-xs md:text-sm leading-relaxed text-foreground/90 select-text">
                {rawContent}
              </pre>
            </div>
          </div>
        )}

        {isSvg && (
          <div className="w-full h-full flex items-center justify-center p-8 bg-slate-950/40 overflow-auto">
            <img
              src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(rawContent)}`}
              alt={effectiveFile || "SVG Preview"}
              className="max-w-full max-h-full object-contain rounded-sm border border-border/60 bg-card shadow-peldrun-md"
            />
          </div>
        )}

        {isHtml && (
          <div className="w-full h-full bg-white relative overflow-hidden">
            <iframe
              key={`${projectId || effectiveJobId}-${workspaceFile}-${refreshKey}`}
              title="Sandbox Preview"
              srcDoc={sandboxedHtml}
              sandbox="allow-scripts allow-modals allow-forms allow-same-origin"
              className="w-full h-full border-0"
            />
          </div>
        )}
      </div>
    </div>
  );
}

export default PreviewTab;
