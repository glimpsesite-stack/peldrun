// frontend/src/features/memory/components/AgenticMemorySearch.tsx
"use client";

/**
 * Agentic memory search -- a self-contained control, reusable anywhere.
 *
 * WHAT IT IS
 * ----------
 * One button, one result card, one state machine. Press the button and
 * the current text is answered from memory by
 * `POST /api/memory/search/agentic`; press again to search again.
 *
 * WHY IT IS EXPLICIT (no typing debounce)
 * ---------------------------------------
 * The first version fired on a 300ms debounce while the user typed. A
 * single agentic run is a rewrite, up to `agentic_max_hops` gap
 * analyses, several searches per hop, and a synthesis -- every one of
 * them a CHAT COMPLETION, serialized by the backend gate. On a local
 * model that is 15-30 seconds.
 *
 * Typing is faster than that. So the preview was always describing a
 * question that no longer existed, and a 4-pause sentence cost four
 * full runs (~24 completions). Worse, it fired on half-typed text:
 * the request
 *
 *     {"rewritten_queries": ["how can you t"]}
 *
 * was a real production run on an unfinished sentence. Its gap
 * analysis then invented coffee queries from the stale evidence and
 * the synthesizer answered a question nobody had asked.
 *
 * An explicit press fixes all of that by construction: one deliberate
 * question, one run, one result.
 *
 * THE TWO PIECES, AND WHY
 * -----------------------
 * The button lives in the composer's toolbar; the card lives ABOVE the
 * editor. Different DOM parents, so they cannot be one element. The
 * file therefore exports:
 *
 *   * `useAgenticMemorySearch`  -- the state machine (headless reuse)
 *   * `AgenticMemorySearchCard` -- the result card
 *   * `AgenticMemorySearchButton` -- the toolbar button
 *   * `AgenticMemorySearch`     -- button + card stacked, for pages
 *                                  where they are adjacent
 *
 * Reuse sites expected: `composer.tsx`, the memory browse page, the
 * entry editor ("is anything in memory close to this text?"), and the
 * command palette.
 *
 * RESULT IMMUTABILITY
 * -------------------
 * A result is frozen at the instant it was produced. Editing the draft
 * does NOT change it, does NOT re-run it, and does NOT clear it. That
 * is deliberate: the card prints the exact question it answered, so
 * the user can always see whether it still matches the draft. A card
 * that silently re-scoped itself would reintroduce the very
 * mislabelling this component exists to stop.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import {
  AlertCircle,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Loader2,
  Search,
  Settings2,
  Target,
} from "lucide-react";

import { useAgenticSearch } from "../api/mutations";
import { useAgenticStatus } from "../api/queries";
import {
  type AgenticSearchResponse,
  type AgenticStatusResponse,
  agenticErrorCopy,
  confidenceBucket,
  formatConfidence,
  hasAgenticAnswer,
} from "../types/agentic";

// ---------------------------------------------------------------------------
// Query eligibility
// ---------------------------------------------------------------------------

/**
 * Minimum length before a search is allowed.
 *
 * Higher than `SEARCH_MIN_QUERY_LENGTH` (3) on purpose: that constant
 * governs a single cheap FTS lookup, this one governs a multi-hop LLM
 * run. "thank you" is 9 characters and must not pay that cost.
 */
export const AGENTIC_MIN_QUERY_LENGTH = 12;

/**
 * Words that alone never justify an agentic run.
 *
 * A message made only of greetings/acknowledgements asks for no fact,
 * so there is nothing for memory retrieval to answer. Kept here rather
 * than imported from the backend: the frontend must not depend on a
 * backend module's internals, and a mismatch only costs one wasted
 * request, never wrong output.
 */
const CONVERSATIONAL_ONLY_WORDS = new Set([
  "hi", "hello", "hey", "yo", "thanks", "thank", "thx", "ty", "please",
  "ok", "okay", "k", "yes", "no", "yeah", "yep", "nope", "sure",
  "bye", "goodbye", "goodnight", "morning", "evening", "night",
  "great", "nice", "cool", "perfect", "awesome", "welcome", "sorry",
  "شكرا", "شكراً", "مرحبا", "اهلا", "أهلا", "السلام", "عليكم",
  "نعم", "لا", "اوكي", "أوكي", "تمام", "ماشي", "باي", "مع", "السلامة",
]);

/** True when the text contains at least one non-conversational word. */
export function hasContentWord(text: string): boolean {
  const words = text
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
  if (words.length === 0) return false;
  return words.some((word) => !CONVERSATIONAL_ONLY_WORDS.has(word));
}

// ---------------------------------------------------------------------------
// Hook -- the state machine
// ---------------------------------------------------------------------------

export interface AgenticMemoryScope {
  /** Current chat id, if any. */
  chatId?: string | null;
  /** Current project id, if any. */
  projectId?: string | null;
  /** Explicit user id; the backend defaults to `user:default`. */
  userId?: string;
}

export interface UseAgenticMemorySearchOptions {
  /** The text to search for. Never mutated by the hook. */
  query: string;
  /** Scope passed through to the request. */
  scope?: AgenticMemoryScope;
  /** Override the eligibility floor. Defaults to `AGENTIC_MIN_QUERY_LENGTH`. */
  minQueryLength?: number;
  /**
   * Change this value to clear the result. A KEY, not a callback, so
   * the caller cannot accidentally leave the component in a half-reset
   * state. Typical values: the chat id, or a counter bumped on send.
   */
  resetKey?: string | number;
  /** When false the status probe is skipped entirely. */
  enabled?: boolean;
  /** Notified when a run starts and when it settles. */
  onRunStateChange?: (running: boolean) => void;
}

export interface AgenticMemorySearchState {
  /** Backend capability snapshot. `undefined` until the probe resolves. */
  status: AgenticStatusResponse | undefined;
  /** True when the backend reports it can serve an agentic run. */
  available: boolean;
  /** True while a request is in flight. */
  running: boolean;
  /** Latest response, or `undefined` before the first run. */
  result: AgenticSearchResponse | undefined;
  /** Transport-level error message, if the request itself failed. */
  errorMessage: string | undefined;
  /**
   * The exact text the current result was produced for. `null` before
   * the first run. The card prints this so the user can compare it
   * with the draft.
   */
  searchedQuery: string | null;
  /** True when the draft no longer matches `searchedQuery`. */
  isStale: boolean;
  /** True when the current draft is long enough AND has a content word. */
  eligible: boolean;
  /** Why the button is disabled, if it is. `null` when pressable. */
  disabledReason: string | null;
  /** Fire a search for the current draft. No-op when not eligible. */
  run: () => void;
  /**
   * Clear the result and the "already asked" record.
   *
   * The host calls this to dismiss the answer and fall back to its
   * normal view. Distinct from `resetKey`: that one is a declarative
   * "this context changed" signal, this one is a user action.
   */
  reset: () => void;
  sourcesOpen: boolean;
  queriesOpen: boolean;
  toggleSources: () => void;
  toggleQueries: () => void;
}

export function useAgenticMemorySearch({
  query,
  scope,
  minQueryLength = AGENTIC_MIN_QUERY_LENGTH,
  resetKey,
  enabled = true,
  onRunStateChange,
}: UseAgenticMemorySearchOptions): AgenticMemorySearchState {
  const statusQuery = useAgenticStatus({ enabled });
  const search = useAgenticSearch();

  const [searchedQuery, setSearchedQuery] = useState<string | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [queriesOpen, setQueriesOpen] = useState(false);
  const inFlight = useRef(false);

  const trimmed = query.trim();
  const eligible =
    trimmed.length >= minQueryLength && hasContentWord(trimmed);
  const available = statusQuery.data?.available ?? false;
  const running = search.isPending;

  // Reset when the caller's key changes (new chat, message sent).
  // `reset` is stable from react-query; listing `search` would re-run
  // this effect on every render.
  useEffect(() => {
    setSearchedQuery(null);
    setSourcesOpen(false);
    setQueriesOpen(false);
    inFlight.current = false;
    search.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  // Report transitions upward, once each.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current === running) return;
    wasRunning.current = running;
    onRunStateChange?.(running);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  const chatId = scope?.chatId ?? null;
  const projectId = scope?.projectId ?? null;
  const userId = scope?.userId ?? "default";

  const run = useCallback(() => {
    if (!eligible || running || !available) return;

    // Synchronous double-fire guard.
    //
    // `running` comes from react-query and only flips on the next
    // render. Two calls in the SAME tick -- a button that both toggles
    // and searches, a double click before paint -- would both see
    // `running === false` and issue two requests. Against a
    // single-slot local runtime that is not a duplicate search, it is
    // interleaved token streams and unparseable output. The ref is set
    // before any await, so the second call cannot pass this gate.
    if (inFlight.current) return;
    inFlight.current = true;

    // Record the query BEFORE the call so an immediate re-render
    // cannot show a result without its question.
    setSearchedQuery(trimmed);
    setSourcesOpen(false);
    setQueriesOpen(false);
    search.mutate(
      {
        query: trimmed,
        ...(chatId ? { chat_id: chatId } : {}),
        ...(projectId ? { project_id: projectId } : {}),
        user_id: userId,
      },
      {
        // Reset on BOTH outcomes: a failed run must be retryable.
        onSettled: () => {
          inFlight.current = false;
        },
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eligible, running, available, trimmed, chatId, projectId, userId]);

  const toggleSources = useCallback(() => {
    setSourcesOpen((value) => !value);
  }, []);
  const toggleQueries = useCallback(() => {
    setQueriesOpen((value) => !value);
  }, []);

  const reset = useCallback(() => {
    inFlight.current = false;
    setSearchedQuery(null);
    setSourcesOpen(false);
    setQueriesOpen(false);
    search.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const isStale =
    searchedQuery !== null &&
    searchedQuery !== trimmed &&
    trimmed.length > 0;

  const disabledReason = useMemo(() => {
    if (!available) {
      return statusQuery.data?.enabled === false
        ? "Agentic search is disabled in Settings → Memory → Agentic & Graph."
        : "Agentic search needs a model. Pick one in Settings → Memory → Agentic & Graph.";
    }
    if (running) return "A search is already running.";
    if (trimmed.length < minQueryLength)
      return `Write at least ${minQueryLength} characters first.`;
    if (!hasContentWord(trimmed))
      return "This message contains no question to search memory for.";
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [available, running, trimmed, minQueryLength, statusQuery.data]);

  return {
    status: statusQuery.data,
    available,
    running,
    result: search.data,
    errorMessage: search.error ? search.error.message : undefined,
    searchedQuery,
    isStale,
    eligible,
    disabledReason,
    run,
    reset,
    sourcesOpen,
    queriesOpen,
    toggleSources,
    toggleQueries,
  };
}

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------

export interface AgenticMemorySearchButtonProps
  extends AgenticMemorySearchState {
  /**
   * Presentation of the control.
   *
   *  * `"toggle"` -- a persistent mode switch. The composer uses this:
   *    the button carries ON/OFF state, and pressing it also searches.
   *  * `"action"` -- a plain one-shot button. Pages that have no
   *    "mode" concept (browse, entry editor) use this.
   */
  mode?: "toggle" | "action";
  /** True when the button renders as ON. Defaults to true in `action` mode. */
  pressed?: boolean;
  /**
   * Own the click entirely: flip whatever state the host wants AND
   * start the search. When provided, the button does NOT call `run`
   * itself -- doing both is how a single press became two requests.
   */
  onToggle?: () => void;
  /** Extra disable reason from the host (e.g. a message is being sent). */
  hostDisabled?: boolean;
  className?: string;
}

export function AgenticMemorySearchButton({
  mode = "action",
  pressed = true,
  onToggle,
  hostDisabled = false,
  running,
  disabledReason,
  run,
  className,
}: AgenticMemorySearchButtonProps) {
  const isOn = mode === "action" ? true : pressed;
  const disabled = hostDisabled || disabledReason !== null;

  const handleClick = () => {
    if (disabled) return;
    // TWO CONTRACTS, and mixing them is a double-fire.
    //
    //  * `onToggle` provided  -> the HOST owns the whole action. It
    //    flips the mode and starts the search itself. The button must
    //    NOT also call `run`, or the first press issues two requests.
    //  * `onToggle` absent    -> the button is a plain action: it
    //    searches and nothing else.
    if (onToggle) {
      onToggle();
      return;
    }
    run();
  };

  const title = disabled
    ? (disabledReason ?? "Unavailable right now.")
    : running
      ? "Searching memory…"
      : mode === "toggle"
        ? pressed
          ? "An agentic answer is shown above — press to search again. The similar-memories panel keeps working either way."
          : "Answer this from memory: one synthesized, cited answer from your memories. Slower than the panel, and it does not replace it."
        : "Answer this from memory";

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={disabled}
      aria-pressed={mode === "toggle" ? isOn : undefined}
      aria-busy={running || undefined}
      title={title}
      className={
        className ??
        `flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium border transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 ${
          isOn
            ? "bg-purple-500/15 border-purple-500/40 text-purple-600 dark:text-purple-300 shadow-xs"
            : "bg-card hover:bg-muted border-border text-muted-foreground"
        }`
      }
    >
      {/* The icon follows the MODE, not availability: a persisted ON
          preference that is temporarily unavailable must still read as
          "on" (dimmed), or the user sees their setting silently revert. */}
      {mode === "toggle" ? (
        isOn ? (
          <BrainCircuit size={13} className="text-purple-500" aria-hidden="true" />
        ) : (
          <Target size={13} aria-hidden="true" />
        )
      ) : (
        <Search size={13} aria-hidden="true" />
      )}

      {running ? (
        <>
          <Loader2 size={11} className="animate-spin" aria-hidden="true" />
          <span>Searching…</span>
        </>
      ) : (
        <span>Agentic</span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export interface AgenticMemorySearchCardProps
  extends AgenticMemorySearchState {
  /** Current draft, used only to decide whether to show the stale line. */
  query: string;
  /** Hide entirely (e.g. the toggle is off). */
  hidden?: boolean;
  /**
   * Show a dismiss control.
   *
   * WHY IT EXISTS
   * -------------
   * The button latches ON on the first press and thereafter always
   * RE-SEARCHES (never toggles off), because that is how a Send button
   * behaves: press, wait, press again to send again. Without a
   * separate dismiss there would be no way back to the standard
   * similar-memories panel. The dismiss is therefore the ONLY "off"
   * affordance, and it lives on the card -- next to the thing it
   * closes -- rather than competing with the search button for the
   * same click.
   */
  onDismiss?: () => void;
  className?: string;
}

/**
 * The result card.
 *
 * The three states it must never confuse
 * --------------------------------------
 *   1. **Pending** -- a request is running. Distinct copy
 *      ("Reasoning…") because the wait is seconds, not the
 *      milliseconds the standard panel shows.
 *   2. **Degraded** -- the backend answered 200 with
 *      `is_available: false`. This is NOT an error: the standard
 *      retrieval result came back under `fallback`. It renders as a
 *      quiet notice, never a red failure.
 *   3. **Answered** -- a non-empty synthesized answer exists.
 *
 * A fourth case -- 200 with `is_available: true` but an empty answer --
 * is legitimate (the question had no supportable answer) and renders as
 * the "no answer" line rather than an empty card.
 */
export function AgenticMemorySearchCard({
  status,
  running,
  result,
  errorMessage,
  searchedQuery,
  isStale,
  sourcesOpen,
  queriesOpen,
  toggleSources,
  toggleQueries,
  hidden = false,
  onDismiss,
  className = "",
}: AgenticMemorySearchCardProps) {
  if (hidden) return null;

  // Nothing has been searched yet. The card does not exist -- the
  // button is the affordance, an empty card would just add noise
  // above the editor.
  if (!running && !result && !errorMessage) return null;

  const bucket = result ? confidenceBucket(result.confidence) : "low";
  const badgeClass =
    bucket === "high"
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30"
      : bucket === "medium"
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30"
        : "bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30";
  const bucketLabel =
    bucket === "high"
      ? "High confidence"
      : bucket === "medium"
        ? "Medium confidence"
        : "Low — verify manually";

  const answered = hasAgenticAnswer(result);
  const degraded = result !== undefined && !result.is_available;

  return (
    <div
      className={`rounded-md border border-purple-500/30 bg-purple-500/[0.04] px-2.5 py-2 text-[11px] ${className}`}
      aria-label="Agentic memory preview"
    >
      {/* ---------- Header ---------- */}
      <div className="mb-1 flex items-center gap-1 text-muted-foreground">
        <BrainCircuit size={10} className="text-purple-500" aria-hidden="true" />
        <span className="font-medium text-foreground">Agentic answer</span>

        {running && (
          <span className="ms-1 inline-flex items-center gap-1 opacity-80">
            <span
              aria-hidden="true"
              className="h-2 w-2 animate-spin rounded-full border border-purple-500 border-t-transparent"
            />
            Reasoning… (this may take a few seconds)
          </span>
        )}

        {!running && result && (
          <span className="ms-auto flex items-center gap-1">
            <span
              className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${badgeClass}`}
              title={
                result.confidence_threshold_met
                  ? "Reached the configured confidence threshold"
                  : "Below the configured confidence threshold — treat as indicative"
              }
            >
              {formatConfidence(result.confidence)} · {bucketLabel}
            </span>
            {result.hops_used > 0 && (
              <span className="text-[9px] text-muted-foreground">
                {result.hops_used} hop{result.hops_used === 1 ? "" : "s"}
              </span>
            )}
          </span>
        )}

        {onDismiss && !running && (
          <button
            type="button"
            onClick={onDismiss}
            className={`${!running && result ? "" : "ms-auto"} shrink-0 rounded-sm px-1 text-[11px] leading-none text-muted-foreground hover:text-foreground`}
            title="Close the agentic answer and go back to similar memories"
            aria-label="Dismiss agentic answer"
          >
            ×
          </button>
        )}
      </div>

      {/* ---------- What was actually asked ----------
          Printed BEFORE the answer, not as a footnote. This is the
          whole contract of a frozen result: the card states the
          question it answers, so a draft that has moved on cannot be
          mistaken for a current answer. */}
      {!running && searchedQuery && (
        <p
          className="mb-1 truncate text-[10px] text-muted-foreground"
          title={searchedQuery}
        >
          Searched for: “{searchedQuery}”
        </p>
      )}

      {/* ---------- Stale hint ---------- */}
      {!running && isStale && (
        <p className="mb-1 text-[10px] text-amber-700 dark:text-amber-400">
          The draft has changed since this answer — press the button again to
          refresh it.
        </p>
      )}

      {/* ---------- Transport failure ---------- */}
      {errorMessage && !running && (
        <p className="text-destructive" role="alert">
          {errorMessage}
        </p>
      )}

      {/* ---------- Degraded (200 + is_available: false) ---------- */}
      {!running && degraded && result && (
        <div className="space-y-1">
          <p className="flex items-start gap-1 text-amber-700 dark:text-amber-400">
            <AlertCircle size={10} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{agenticErrorCopy(result.error)}</span>
          </p>
          {result.fallback && result.fallback.memories.length > 0 && (
            <ul className="space-y-0.5">
              {result.fallback.memories.slice(0, 3).map((m) => (
                <li
                  key={m.memory_id}
                  className="truncate text-foreground/80"
                  title={m.content}
                >
                  · {m.content.slice(0, 120)}
                </li>
              ))}
            </ul>
          )}
          {result.fallback?.is_empty && (
            <p className="text-muted-foreground">
              No related memories were found either.
            </p>
          )}
          {status !== undefined && status.model_resolved === false && (
            <UnavailableNotice status={status} />
          )}
        </div>
      )}

      {/* ---------- Answered ---------- */}
      {!running && !degraded && answered && result && (
        <div className="space-y-1">
          <p className="whitespace-pre-wrap leading-snug text-foreground/90">
            {result.synthesized_answer}
          </p>

          {/* Sources */}
          {result.evidence.length > 0 && (
            <div>
              <button
                type="button"
                onClick={toggleSources}
                className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground hover:text-foreground"
                aria-expanded={sourcesOpen}
              >
                {sourcesOpen ? (
                  <ChevronDown size={10} aria-hidden="true" />
                ) : (
                  <ChevronRight size={10} aria-hidden="true" />
                )}
                Sources ({result.evidence.length}{" "}
                {result.evidence.length === 1 ? "memory" : "memories"})
              </button>
              {sourcesOpen && (
                <ul className="mt-1 space-y-0.5 ps-3">
                  {result.evidence.map((e, index) => {
                    const cited = result.citations.includes(index + 1);
                    return (
                      <li
                        key={e.memory_id}
                        className="flex items-start gap-1 text-foreground/80"
                      >
                        <span
                          className={`shrink-0 font-mono text-[9px] ${
                            cited
                              ? "font-bold text-purple-600 dark:text-purple-300"
                              : "text-muted-foreground"
                          }`}
                          title={cited ? "Cited in the answer" : "Not cited"}
                        >
                          [{index + 1}]
                        </span>
                        <span className="min-w-0 flex-1 truncate" title={e.content}>
                          {e.content.slice(0, 140)}
                        </span>
                        {typeof e.score === "number" && (
                          <span className="shrink-0 font-mono text-[9px] text-muted-foreground">
                            {e.score.toFixed(2)}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}

          {/* Sub-queries */}
          {result.sub_queries_used.length > 0 && (
            <div>
              <button
                type="button"
                onClick={toggleQueries}
                className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground hover:text-foreground"
                aria-expanded={queriesOpen}
              >
                {queriesOpen ? (
                  <ChevronDown size={10} aria-hidden="true" />
                ) : (
                  <ChevronRight size={10} aria-hidden="true" />
                )}
                Searched for ({result.sub_queries_used.length})
              </button>
              {queriesOpen && (
                <ul className="mt-1 space-y-0.5 ps-3">
                  {result.sub_queries_used.map((q) => (
                    <li key={q} className="truncate text-muted-foreground" title={q}>
                      • {q}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {result.elapsed_ms > 0 && (
            <p className="text-[9px] text-muted-foreground/70">
              {(result.elapsed_ms / 1000).toFixed(1)}s
            </p>
          )}
        </div>
      )}

      {/* ---------- Answered, but nothing to say ---------- */}
      {!running && !degraded && !answered && result && (
        <p className="text-muted-foreground">
          No supported answer in memory for this question.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unavailable notice
// ---------------------------------------------------------------------------

/**
 * Shown when agentic is enabled but the backend cannot serve it.
 *
 * `enabled === false`        -> the operator must turn the feature on.
 * `ai_enabled === false`     -> the whole AI subsystem is off.
 * `model_resolved === false` -> it IS on but no model resolved.
 * otherwise                  -> enabled + model, but no pipeline yet.
 *
 * Telling the user the wrong one sends them to the wrong screen, so the
 * copy branches on the raw flags rather than on `available`.
 */
function UnavailableNotice({ status }: { status: AgenticStatusResponse }) {
  const disabledByOperator = status.enabled === false;
  const aiOff = status.ai_enabled === false;
  const modelMissing = !aiOff && status.model_resolved === false;

  const message = disabledByOperator
    ? "Agentic search is disabled on the server."
    : aiOff
      ? "Agentic search needs the AI subsystem, which is off."
      : modelMissing
        ? "Agentic search is enabled but no model is configured."
        : "Agentic search is enabled but the server has not built the pipeline yet. Open settings and save once.";

  return (
    <div className="mt-1 rounded-md border border-amber-500/40 bg-amber-500/[0.06] px-2 py-1.5">
      <p className="flex items-start gap-1 text-[10px] text-amber-700 dark:text-amber-400">
        <AlertCircle size={10} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>{message}</span>
      </p>
      <Link
        href="/memory/settings"
        className="mt-1 inline-flex items-center gap-1 text-[10px] font-medium text-primary hover:underline"
      >
        <Settings2 size={10} aria-hidden="true" />
        {disabledByOperator || aiOff
          ? "Open Settings → Agentic & Graph"
          : "Choose an agentic model in Settings"}
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Convenience wrapper -- button + card stacked
// ---------------------------------------------------------------------------

export interface AgenticMemorySearchProps
  extends Omit<UseAgenticMemorySearchOptions, "enabled"> {
  /** Hide the whole control (e.g. a host toggle is off). */
  hidden?: boolean;
  /** Passed through to the button. */
  hostDisabled?: boolean;
  className?: string;
}

/**
 * Button above card, for hosts where the two are adjacent.
 *
 * The composer does NOT use this: its button belongs in the toolbar
 * while the card sits above the editor, so it composes the hook with
 * the two pieces separately. Every other consumer can use this.
 */
export default function AgenticMemorySearch({
  hidden = false,
  hostDisabled = false,
  className = "",
  ...options
}: AgenticMemorySearchProps) {
  const state = useAgenticMemorySearch({
    ...options,
    enabled: !hidden,
  });

  if (hidden) return null;

  return (
    <div className={`space-y-1 ${className}`}>
      <AgenticMemorySearchButton
        {...state}
        mode="action"
        hostDisabled={hostDisabled}
      />
      <AgenticMemorySearchCard {...state} query={options.query} />
    </div>
  );
}
