// frontend/src/app/projects/[projectId]/chat/new/page.tsx

/**
 * Project New Chat Page
 *
 * Route: /projects/[projectId]/chat/new
 *
 * Renders a fresh chat session scoped to the given project.
 *
 * Design Decision — why this route exists at all:
 *
 *   `ChatContainer` scopes *every* downstream operation (file uploads,
 *   `POST /api/run`, regeneration, URL updates, workspace panel) based
 *   on the `projectId` PROP — not on any URL query string.
 *
 *   The legacy "New Chat" button used to navigate to
 *   `/chat/new?projectId=X`. That route renders `<ChatContainer />`
 *   without a `projectId` prop, so the query string was silently
 *   ignored and the new session was created under the GLOBAL chat
 *   list instead of the project. This route closes that gap by
 *   forwarding the project segment as an explicit prop.
 *
 * Next.js routing precedence:
 *
 *   The App Router resolves static segments BEFORE dynamic segments.
 *   So `/projects/P/chat/new` matches this file, and
 *   `/projects/P/chat/chat_abc123` matches `[chatId]/page.tsx`. Since
 *   real chat ids always start with `chat_`, there is no realistic
 *   collision with the literal `"new"` segment.
 *
 * Lifecycle:
 *
 *   1. User lands on this route → `ChatContainer` mounts with
 *      `projectId=P` and no `initialChatId`.
 *   2. `isFreshSession === true` → `ChatLanding` renders.
 *   3. User types a message and sends:
 *        - `handleStartTask` builds `targetChatId` and forwards it as
 *          `chat_id` in the `POST /api/run` body.
 *        - `project_id=P` is included in the same body, so the
 *          backend creates the session under the project.
 *        - `window.history.replaceState` rewrites the URL to
 *          `/projects/P/chat/{returnedChatId}` WITHOUT a page reload,
 *          so the current component keeps its state and the timeline
 *          renders in place.
 *   4. Refreshing later lands on `[chatId]/page.tsx`, which forwards
 *      `initialChatId` → `ChatContainer` re-hydrates the session.
 */

"use client";

import React from "react";
import { useParams } from "next/navigation";
import { ChatContainer } from "@/components/chat/chat-container";

export default function NewProjectChatPage() {
  const params = useParams();

  // `projectId` is guaranteed by the route pattern
  // `[projectId]/chat/new`. The defensive `?? ""` keeps the render
  // alive during the brief window before Next.js populates params
  // on the client.
  const projectId = (params?.projectId as string) ?? "";

  return (
    <div className="h-screen w-full overflow-hidden bg-background">
      <ChatContainer projectId={projectId} />
    </div>
  );
}
