// Path: frontend/src/components/chat/chat-container/types.ts

/**
 * Shared type definitions for the chat container and its extracted hooks.
 *
 * Centralising these types keeps the hook interfaces small and stable,
 * and prevents accidental divergence between producer/consumer signatures.
 */

/** Live state of a child (delegated) run spawned by a parent agent. */
export interface ChildRunState {
  childRunId: string;
  parentRunId: string;
  taskId: string;
  targetAgentId: string;
  instruction: string;
  status: "running" | "completed" | "failed" | "cancelled";
  startedAt: number;
  steps: any[];
  deliverables: any[];
  tokensUsed?: any;
  durationSeconds?: number;
  error?: string;
}


/**
 * Pending delegation approval request awaiting a human decision.
 * Resolved requests are removed from the pending map by the SSE handler.
 */
export interface DelegationApprovalRequest {
  approvalRequestId: string;
  taskId: string;
  parentRunId: string;
  targetAgentId: string;
  instruction: string;
  status: "pending" | "resolved";
  timeoutSeconds: number;
  createdAt: number;
}

/** Props accepted by the top-level ChatContainer component. */
export interface ChatContainerProps {
  initialJobId?: string | null;

  /**
   * Optional chat session identifier.
   *
   * When provided (typically from the URL route
   * `/projects/[projectId]/chat/[chatId]`), ChatContainer fetches the
   * chat list, resolves the matching session, and hydrates the full
   * state (history, steps, tokens, artifacts) from its `job_id`.
   *
   * Resolution precedence:
   *   1. `initialJobId` — if set, used directly (legacy behaviour).
   *   2. `initialChatId` — if set and no `initialJobId`, resolved
   *      via `GET /api/chats` (project-scoped when `projectId` is
   *      present) and its `job_id` fed to `fetchJobDetails`.
   *
   * Passing both is allowed; `initialJobId` wins. This lets legacy
   * routes (which carry a job id in the URL) keep working while the
   * new project routes use the chat id.
   */
  initialChatId?: string;

  /**
   * Optional project identifier.
   *
   * When provided, every chat operation — file uploads, artifact
   * generation, memory retrieval, and run dispatch — is scoped to
   * that project's persistent workspace instead of the legacy
   * per-chat session directory.
   *
   * Scope of the change (backward-compatible):
   *   - File uploads:    POST /api/projects/{projectId}/workspace/upload
   *                      instead of POST /api/files/upload.
   *   - Run dispatch:    `project_id` is included in the POST /api/run
   *                      body so the backend scopes memory + tools.
   *   - Regeneration:    Same `project_id` is forwarded on both the
   *                      "latest" and "historical turn" code paths.
   *   - Right panel:     ProjectWorkspacePanel replaces WorkspacePanel
   *                      inside the same slot (same layout, same
   *                      header controls, zero UI difference beyond
   *                      the scoping).
   *   - URL updates:     New turns write `/projects/{projectId}/chat/{chatId}`
   *                      instead of `/chat/{chatId}`.
   *
   * When omitted (legacy chat route), all endpoints, payloads, and
   * UI rendering remain byte-for-byte identical to the pre-project
   * behaviour.
   */
  projectId?: string;
}

/**
 * Lifecycle status of the current chat run.
 *
 * Mirrors `SessionStatus` from `session-store.ts` (and
 * `docs/state-contracts.md` §3), plus a local-only `"idle"` sentinel
 * used before any run has been dispatched. Keeping the union complete
 * lets ChatContainer pass status values through without `as any`
 * casts, and lets ChatSubHeader's exhaustive badge switch stay
 * statically verifiable.
 */
export type ChatStatus =
  | "idle"
  | "pending"
  | "queued"
  | "starting"
  | "running"
  | "waiting_for_input"
  | "retrying"
  // NOTE: "paused" is intentionally omitted. Pause was removed in
  // Phase H because no LLM provider supports true streaming pause —
  // the feature was misleading. See LiveStatusIndicator.tsx for the
  // full rationale. Backend endpoints still exist for backward
  // compatibility but return 501 Not Implemented.
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted";

/** Aggregated token usage counters for the current or completed run. */
export interface TokenUsage {
  input: number;
  output: number;
  total: number;
}

/** Optional cost/latency metrics emitted alongside terminal SSE events. */
export interface UsageMetrics {
  cost_usd?: number;
  latency_ms?: number;
  tokens_per_second?: number;
}

/** A file produced by an agent run and shown in the workspace panel. */
export interface ProducedFile {
  name: string;
  path: string;
}

// ===========================================================================
// Image Search Results (Gallery)
// ===========================================================================
//
// Structured image records emitted by the backend via the "image_results"
// SSE event. Populated by:
//   - omweb/agent_bridge_parts/dispatcher.py  (chat mode)
//   - omweb/engines/peldrun_engine.py          (agent mode)
//
// URLs are already pre-processed by the backend to route through
// /api/search/image-proxy, so the frontend can render them directly
// without additional transformation.

/** A single image result from the backend. */
export interface ImageResultItem {
  /** Human-readable title or alt-text. */
  title: string;
  /** Proxied URL to the actual image (via /api/search/image-proxy). */
  image_url: string;
  /** Proxied URL to a thumbnail, when provided by the upstream. */
  thumbnail_url?: string | null;
  /** Original page where the image was found (external link). */
  source_url?: string | null;
  /** Image width in pixels, when known. */
  width?: number | null;
  /** Image height in pixels, when known. */
  height?: number | null;
  /** Normalized hostname of the original image source. */
  source?: string | null;
}

/** Payload carried by the "image_results" SSE event. */
export interface ImageResultsPayload {
  /** Search query that produced these images. */
  query: string;
  /** Provider that handled the search (DuckDuckGo, Brave, SearXNG, Google). */
  provider: string;
  /** Ordered list of image results. */
  images: ImageResultItem[];
  /** Count of items in `images`. */
  count: number;
  /** End-to-end search latency in milliseconds. */
  latency_ms: number;
  /** Estimated USD cost of the search. */
  estimated_cost_usd: number;
}
