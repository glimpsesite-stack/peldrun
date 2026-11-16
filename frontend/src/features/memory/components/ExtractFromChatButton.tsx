// src/features/memory/components/ExtractFromChatButton.tsx

/**
 * Button that triggers a MANUAL whole-session extraction.
 *
 * Drop-in for a chat view's header or action bar: given a
 * `chatId` and (optionally) a `projectId`, it enqueues a
 * `trigger_type='manual'` extraction job covering the whole
 * session and reports the outcome inline.
 *
 * Outcome semantics
 * -----------------
 *
 * The backend's response is a discriminated union on `job`:
 *
 *   * `job: MemoryJob` -> a job was enqueued. The button shows
 *                         "Queued" + calls `onQueued(job)`.
 *   * `job: null`     -> nothing to extract (0 turns, session
 *                         missing, or duplicate idempotency
 *                         key). The button shows the backend's
 *                         `message` + calls `onNoop(message)`.
 *
 * Both branches are HTTP 200 -- "nothing to do" is a
 * legitimate outcome. The button does NOT raise a toast for the
 * no-op path; the parent decides whether to surface it
 * (typically via a small inline note, not an intrusive error).
 *
 * AI gating
 * ---------
 *
 * The button is disabled when the AI extraction subsystem is
 * not `isAvailable` (enabled + extraction_enabled + a model
 * picked). The disabled state carries a `title` explaining WHY
 * -- "Checking AI settings…" while loading, or "AI extraction
 * not configured" once resolved-but-unavailable. This is the
 * ONLY difference between "cannot click" and "can click" from
 * the user's perspective; the backend would return 503 for the
 * same reason if we skipped the client gate.
 *
 * Error handling
 * --------------
 *
 * A `MemoryApiError` (network failure, 503, 500) is shown
 * inline as a small red badge. It does NOT throw to the parent
 * -- the button is a self-contained widget and the parent's
 * error UI is orthogonal. The `onQueued` callback is the ONLY
 * way the parent learns about a successful enqueue.
 */

'use client';

import { useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Sparkles,
} from 'lucide-react';

import { cn } from '../lib/cn';
import { useExtractFromSession } from '../api/mutations';
import { useAIEnabled } from '../hooks/useAIEnabled';
import type { MemoryJob } from '../types/jobs';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ExtractFromChatButtonProps {
  /** The chat to extract from. */
  chatId: string;

  /**
   * Optional project override. When omitted, the backend
   * resolves the project from the session file. Pass `null`
   * explicitly when the chat is known to be standalone (so the
   * backend does not fall back to the session's own project).
   */
  projectId?: string | null;

  /** Disables the button regardless of AI availability. */
  disabled?: boolean;

  /** Called after a successful enqueue with the new job. */
  onQueued?: (job: MemoryJob) => void;

  /**
   * Called when the backend responded 200 with `job: null`.
   * The message is the backend's own explanation ("no turns",
   * "session missing", or a duplicate key).
   */
  onNoop?: (message: string) => void;

  /** Extra classes for the wrapper. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Feedback state
// ---------------------------------------------------------------------------

type _Feedback =
  | { kind: 'idle' }
  | { kind: 'queued'; jobId: string }
  | { kind: 'noop'; message: string }
  | { kind: 'error'; message: string };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ExtractFromChatButton({
  chatId,
  projectId,
  disabled = false,
  onQueued,
  onNoop,
  className,
}: ExtractFromChatButtonProps) {
  const ai = useAIEnabled();
  const mutation = useExtractFromSession();
  const [feedback, setFeedback] = useState<_Feedback>({ kind: 'idle' });

  const isDisabled = disabled || mutation.isPending || !ai.isAvailable;

  const handleClick = async () => {
    setFeedback({ kind: 'idle' });
    try {
      const result = await mutation.mutateAsync({
        chat_id: chatId,
        project_id: projectId ?? null,
      });
      if (result.job) {
        setFeedback({ kind: 'queued', jobId: result.job.job_id });
        onQueued?.(result.job);
      } else {
        setFeedback({ kind: 'noop', message: result.message });
        onNoop?.(result.message);
      }
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Extraction request failed';
      setFeedback({ kind: 'error', message });
    }
  };

  const tooltip = !ai.isAvailable
    ? ai.isLoading
      ? 'Checking AI settings…'
      : 'AI extraction not configured'
    : undefined;

  return (
    <div className={cn('inline-flex items-center gap-2', className)}>
      <button
        type="button"
        onClick={handleClick}
        disabled={isDisabled}
        title={tooltip}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-sm border border-border/60',
          'bg-background px-2.5 py-1 text-[11px]',
          'hover:bg-muted/40 transition-colors',
          'disabled:opacity-50 disabled:cursor-not-allowed',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
        )}
      >
        {mutation.isPending ? (
          <Loader2 size={11} className="animate-spin" aria-hidden="true" />
        ) : (
          <Sparkles size={11} aria-hidden="true" />
        )}
        Extract from chat
      </button>

      {feedback.kind === 'queued' && (
        <span className="inline-flex items-center gap-1 text-[10px] text-emerald-600 dark:text-emerald-400">
          <CheckCircle2 size={10} aria-hidden="true" />
          Queued
        </span>
      )}

      {feedback.kind === 'noop' && (
        <span className="text-[10px] text-muted-foreground">
          {feedback.message}
        </span>
      )}

      {feedback.kind === 'error' && (
        <span className="inline-flex items-center gap-1 text-[10px] text-red-600 dark:text-red-400">
          <AlertCircle size={10} aria-hidden="true" />
          {feedback.message}
        </span>
      )}
    </div>
  );
}
