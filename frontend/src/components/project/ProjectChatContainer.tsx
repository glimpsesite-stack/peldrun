// frontend/src/components/project/ProjectChatContainer.tsx

/**
 * Project Chat Container — DEPRECATED SHIM
 *
 * @deprecated This component is no longer needed. `ChatContainer`
 * now handles the project-scoped layout, right-hand
 * `ProjectWorkspacePanel`, session hydration via `initialChatId`,
 * and project-scoped file uploads natively. Import `ChatContainer`
 * directly and pass `projectId` + `initialChatId`:
 *
 *     import { ChatContainer } from "@/components/chat/chat-container";
 *
 *     <ChatContainer projectId={projectId} initialChatId={chatId} />
 *
 * This file is retained as a compatibility shim so existing imports
 * do not break while the codebase migrates. It will be removed in a
 * follow-up PR once `grep -rn "ProjectChatContainer" frontend/src`
 * returns no callers.
 *
 * History — why the previous implementation was replaced:
 *
 *   The original implementation added a *second* right-hand panel and
 *   a *second* `showRightPanel` state on top of the ones `ChatContainer`
 *   already owns. This produced two user-visible bugs:
 *
 *     1. Two panels stacked side-by-side on wide screens (the wrapper's
 *        panel plus ChatContainer's), each with an independent close
 *        button that did not affect the other.
 *
 *     2. An infinite custom-event loop: the wrapper listened for
 *        `peldrun:artifact-created` and re-dispatched the SAME event
 *        name, which `ChatContainer` also listens for. Every artifact
 *        triggered a re-dispatch chain that eventually hit React's
 *        update-depth bailout.
 *
 *   Both bugs are impossible in this shim: it renders a single
 *   `<ChatContainer>` and registers no event listeners.
 */

"use client";

import React from "react";
import { ChatContainer } from "@/components/chat/chat-container";

export interface ProjectChatContainerProps {
  /**
   * Project identifier. Forwarded to `ChatContainer` as `projectId`.
   * Required — a project chat without a project is semantically
   * impossible.
   */
  projectId: string;

  /**
   * Chat session identifier. Forwarded to `ChatContainer` as
   * `initialChatId`, which resolves it to a `job_id` internally via
   * `GET /api/chats?project_id=...` and feeds the result to
   * `fetchJobDetails` for full hydration (history, steps, tokens,
   * artifacts).
   */
  chatId: string;
}

export function ProjectChatContainer({
  projectId,
  chatId,
}: ProjectChatContainerProps) {
  // Single delegation point. ChatContainer owns:
  //   - the two-column layout,
  //   - the right-hand ProjectWorkspacePanel (mounted when
  //     `projectId` is set),
  //   - showRightPanel state (localStorage-backed),
  //   - session hydration from `initialChatId`,
  //   - project-scoped uploads (`/api/projects/{id}/workspace/upload`),
  //   - `project_id` on every `POST /api/run` and regeneration call,
  //   - URL updates (`/projects/{id}/chat/{chatId}`).
  return (
    <ChatContainer
      projectId={projectId}
      initialChatId={chatId}
    />
  );
}

export default ProjectChatContainer;
