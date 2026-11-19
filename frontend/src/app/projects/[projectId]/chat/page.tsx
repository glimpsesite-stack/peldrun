// frontend/src/app/projects/[projectId]/chat/page.tsx

/**
 * Project Chat List Page
 *
 * Displays all chat sessions belonging to a specific project.
 * Provides a "New Chat" button to start a conversation within the project context.
 *
 * Design:
 * - Reuses the session store's project cache for consistency with the sidebar.
 * - Provides a clear, focused view of project-specific conversations.
 * - Navigates to the specific chat page while maintaining the project layout.
 */

"use client";

import React, { useEffect, useState, useMemo } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  MessageSquare,
  Plus,
  Loader2,
  Calendar,
  Clock,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSessionStore } from "@/stores/session-store";
import { parseChatDate } from "@/lib/chatsApi";

export default function ProjectChatListPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  const {
    projectSessionsCache,
    projectSessionsLoading,
    projectSessionsError,
    loadProjectSessions,
    loadMoreProjectSessions,
    projectSessionsHasMore,
  } = useSessionStore();

  const [visibleLimit, setVisibleLimit] = useState(10);

  useEffect(() => {
    loadProjectSessions(projectId);
  }, [projectId, loadProjectSessions]);

  useEffect(() => {
    setVisibleLimit(10);
  }, [projectId]);

  const sessions = projectSessionsCache[projectId] || [];
  const isLoading = projectSessionsLoading[projectId] || false;
  const error = projectSessionsError[projectId];
  const hasMore = projectSessionsHasMore[projectId] || false;

  const sortedSessions = useMemo(() => {
    return [...sessions].sort((a, b) => {
      const timeA = parseChatDate(a.updated_at || a.created_at).getTime();
      const timeB = parseChatDate(b.updated_at || b.created_at).getTime();
      return timeB - timeA;
    });
  }, [sessions]);

  const visibleSessions = sortedSessions.slice(0, visibleLimit);
  const canLoadMoreLocally = visibleLimit < sortedSessions.length;
  const canLoadMoreFromServer = hasMore;

  const handleLoadMore = async () => {
    if (canLoadMoreLocally) {
      setVisibleLimit((n) => n + 10);
      return;
    }
    if (canLoadMoreFromServer) {
      await loadMoreProjectSessions(projectId);
    }
  };

  const handleNewChat = () => {
    // Route to the project-scoped "new chat" page.
    //
    // The legacy target `/chat/new?projectId=X` was broken: ChatContainer
    // scopes its behaviour from the `projectId` PROP, not from any query
    // string, so the `?projectId=X` segment was silently discarded and
    // the new session was created under the global chat list.
    //
    // `/projects/[projectId]/chat/new/page.tsx` forwards the project
    // segment as an explicit prop, so every downstream operation
    // (POST /api/run body, workspace uploads, workspace panel
    // rendering, URL updates) is correctly scoped to `projectId`.
    router.push(`/projects/${projectId}/chat/new`);
  };

  const formatDate = (dateString: string | number) => {
    const date = parseChatDate(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) return "Today";
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays} days ago`;

    return date.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: date.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
    });
  };

  return (
    <div className="container mx-auto px-4 py-8 max-w-4xl h-full flex flex-col">
      <div className="flex items-center justify-between mb-8 shrink-0">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <MessageSquare className="text-primary" size={24} />
            Project Conversations
          </h1>
          <p className="text-muted-foreground mt-1">
            Manage and view all chat sessions within this project.
          </p>
        </div>
        <Button onClick={handleNewChat} className="gap-2">
          <Plus size={16} />
          New Chat
        </Button>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-destructive/10 border border-destructive/30 rounded-lg flex items-start gap-3 shrink-0">
          <AlertCircle className="text-destructive shrink-0 mt-0.5" size={18} />
          <div>
            <h3 className="font-semibold text-destructive mb-1">Failed to load conversations</h3>
            <p className="text-sm text-muted-foreground">{error}</p>
          </div>
        </div>
      )}

      {isLoading && sortedSessions.length === 0 && (
        <div className="flex items-center justify-center flex-1">
          <div className="flex flex-col items-center gap-3">
            <Loader2 size={32} className="animate-spin text-primary" />
            <p className="text-sm text-muted-foreground">Loading conversations...</p>
          </div>
        </div>
      )}

      {!isLoading && sortedSessions.length === 0 && !error && (
        <div className="text-center py-16 bg-muted/30 border border-border rounded-lg flex-1 flex flex-col items-center justify-center">
          <MessageSquare size={48} className="mx-auto text-muted-foreground mb-4 opacity-50" />
          <h3 className="text-lg font-semibold mb-2">No conversations yet</h3>
          <p className="text-muted-foreground mb-6 max-w-md mx-auto">
            Start a new chat to begin working with AI agents within this project workspace.
          </p>
          <Button onClick={handleNewChat} className="gap-2">
            <Plus size={16} />
            Start Your First Chat
          </Button>
        </div>
      )}

      {sortedSessions.length > 0 && (
        <div className="space-y-3 flex-1 overflow-y-auto">
          {visibleSessions.map((session) => (
            <div
              key={session.id}
              // Route by the CHAT id — NOT the job id.
              //
              // The target route (`[chatId]/page.tsx`) forwards this
              // segment to `ChatContainer` as `initialChatId`, and
              // ChatContainer resolves it against `GET /api/chats`
              // by matching `chat.id === initialChatId`. Passing the
              // job_id here would never match, so the container
              // would fall into the "session not found" branch and
              // silently spawn a fresh session on the next dispatch.
              onClick={() =>
                router.push(`/projects/${projectId}/chat/${session.id}`)
              }
              className="group flex items-center gap-4 p-4 bg-card border border-border rounded-lg
                         hover:shadow-md hover:border-primary/50 transition-all cursor-pointer"
            >
              <div
                className={`w-3 h-3 rounded-full shrink-0 ${
                  session.status === "completed"
                    ? "bg-emerald-500"
                    : session.status === "running"
                    ? "bg-amber-500 animate-pulse"
                    : session.status === "failed"
                    ? "bg-rose-500"
                    : session.status === "cancelled"
                    ? "bg-slate-400"
                    : session.status === "interrupted"
                    ? "bg-orange-400"
                    : "bg-muted-foreground/40"
                }`}
                title={`Status: ${session.status || "unknown"}`}
              />

              <div className="flex-1 min-w-0">
                <h3 className="font-semibold truncate mb-1 group-hover:text-primary transition-colors">
                  {session.title || session.prompt || "New Session"}
                </h3>
                <p className="text-sm text-muted-foreground truncate">
                  {session.prompt || "No prompt provided"}
                </p>
              </div>

              <div className="flex items-center gap-4 text-xs text-muted-foreground shrink-0">
                <div className="flex items-center gap-1.5">
                  <Calendar size={12} />
                  <span>{formatDate(session.created_at)}</span>
                </div>
                {session.updated_at && session.updated_at !== session.created_at && (
                  <div className="flex items-center gap-1.5">
                    <Clock size={12} />
                    <span>Updated {formatDate(session.updated_at)}</span>
                  </div>
                )}
              </div>
            </div>
          ))}

          {(canLoadMoreLocally || canLoadMoreFromServer) && (
            <div className="pt-4 text-center">
              <Button
                variant="outline"
                onClick={handleLoadMore}
                disabled={isLoading}
                className="gap-2"
              >
                {isLoading ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Plus size={16} />
                )}
                {canLoadMoreLocally
                  ? `Load More (${Math.min(sortedSessions.length - visibleLimit, 10)} remaining)`
                  : "Load More from Server"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
