// frontend/src/app/projects/[projectId]/chat/[chatId]/page.tsx

/**
 * Project Active Chat Page
 *
 * Route: /projects/[projectId]/chat/[chatId]
 *
 * Responsibilities (intentionally minimal):
 *   - Extract `projectId` and `chatId` from the URL.
 *   - Render `<ChatContainer>` with those values forwarded as props.
 *   - Nothing else.
 *
 * Design Decision — why no wrapper / no right-side panel here:
 *
 *   `ChatContainer` is the single source of truth for the entire chat
 *   UI, including the right-hand workspace panel. It:
 *
 *     * owns the two-column layout (chat column + right panel),
 *     * renders `ProjectWorkspacePanel` automatically when
 *       `projectId` is present (same slot, same header controls as
 *       the legacy `WorkspacePanel`),
 *     * hydrates the chat session from `initialChatId` via
 *       `GET /api/chats` and feeds the resolved `job_id` into
 *       `fetchJobDetails`, so refreshing the page restores the full
 *       history, steps, tokens, and artifacts,
 *     * persists the user's right-panel visibility preference under
 *       `peldrun:right_panel_open` in localStorage,
 *     * toggles the panel through `ChatSubHeader`'s built-in button.
 *
 *   Adding a second wrapper panel here (as an earlier draft did)
 *   produced two visible bugs:
 *
 *     1. Two right-hand panels stacked side-by-side, each with its
 *        own independent show/hide state — the wrapper's close
 *        button did not close `ChatContainer`'s panel and vice
 *        versa.
 *     2. An infinite custom-event loop: the wrapper listened for
 *        `peldrun:artifact-created` and re-dispatched the same
 *        event under the same name, which `ChatContainer` also
 *        listened for. Every artifact triggered a chain of
 *        re-dispatches until React bailed out.
 *
 *   Both bugs disappear once the wrapper is removed. Keeping the
 *   page as a thin props-passing shell guarantees the layout is
 *   defined in exactly one place.
 *
 * Data flow (for reference):
 *
 *   URL: /projects/P/chat/C
 *        ↓
 *   <ChatContainer projectId="P" initialChatId="C" />
 *        ↓
 *   GET /api/chats?project_id=P  →  find chat with id === "C"
 *        ↓
 *   found:    fetchJobDetails(chat.job_id)  → hydrate full state
 *   not found: activeChatId = "C"           → fresh session, first
 *                                             run targets chat_id=C
 *                                             with project_id=P
 */

"use client";

import React from "react";
import { useParams } from "next/navigation";
import { ChatContainer } from "@/components/chat/chat-container";

export default function ProjectActiveChatPage() {
  const params = useParams();

  // `useParams()` returns a readonly record whose values are typed as
  // `string | string[]`. Both segments are defined by the route
  // pattern (`[projectId]/chat/[chatId]`), so a direct cast is safe.
  // The defensive `?? ""` keeps the component renderable during the
  // brief window before Next.js populates params on the client.
  const projectId = (params?.projectId as string) ?? "";
  const chatId = (params?.chatId as string) ?? "";

  // ChatContainer handles everything: layout, hydration, right panel,
  // run dispatch, file uploads, regeneration. This page exists purely
  // to bridge the URL segments into typed props.
  return (
    <div className="h-full w-full overflow-hidden bg-background">
      <ChatContainer
        projectId={projectId}
        initialChatId={chatId}
      />
    </div>
  );
}
