// src/features/memory/hooks/useAutomationPolling.ts

/**
 * Compute the `refetchInterval` for an automation query based on
 * whether the underlying task is still active AND whether the
 * browser tab is visible.
 *
 * Why a helper (not a library)
 * ----------------------------
 *
 * TanStack Query's `refetchInterval` accepts `number | false`. A
 * caller who wants "poll while a batch is running" would
 * normally write:
 *
 *     refetchInterval: batchIsActive ? 3000 : false,
 *
 * That works -- but it does not pause polling when the tab is
 * in the background. Browsers throttle background timers, but
 * not consistently across engines, and a user who leaves the
 * tab open overnight with an active batch would burn CPU on
 * every throttled tick. The hook centralizes three rules:
 *
 *   1. **Active-task gating.** Callers pass `shouldPoll`; the
 *      hook returns `false` when it is `false`.
 *
 *   2. **Visibility pause.** When `document.hidden` is true,
 *      polling is suspended. It resumes on the `visibilitychange`
 *      event, so switching back to the tab gets an immediate
 *      refresh.
 *
 *   3. **Consistent default interval.** 3000ms for batch
 *      detail (a slow operation), 5000ms for job lists (jobs
 *      can flip faster but each list item is cheap). Callers
 *      can override `intervalMs`, but the defaults are tuned
 *      to balance liveness vs. load.
 *
 * Contract
 * --------
 *
 * The hook returns a value ready to spread into a query's
 * options:
 *
 *     const polling = useAutomationPolling({
 *       shouldPoll: hasActiveJobs,
 *       intervalMs: 5000,
 *     });
 *
 *     useJobsList(params, { ...polling });
 *
 * Do NOT compute `refetchInterval` at module scope -- it would
 * never see `document.hidden` changes. Always call the hook.
 */

'use client';

import { useEffect, useState } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseAutomationPollingParams {
  /**
   * Whether the underlying task is still active. `false`
   * disables polling regardless of tab visibility.
   *
   * Compute this from the DATA the query returned, not from the
   * mutation's pending state -- a `pending` job is still active
   * even if the last mutation that touched it has settled.
   */
  shouldPoll: boolean;

  /**
   * Interval in milliseconds when polling is active AND the
   * tab is visible. Defaults to 3000.
   *
   * The 3000ms default is tuned for the batch detail panel,
   * which is the most latency-sensitive surface in the feature.
   * Job lists can pass 5000 explicitly.
   */
  intervalMs?: number;
}

export interface UseAutomationPollingResult {
  /**
   * Ready to pass as `refetchInterval` in a `useQuery` options
   * object. `false` means "do not poll".
   */
  refetchInterval: number | false;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAutomationPolling(
  params: UseAutomationPollingParams,
): UseAutomationPollingResult {
  const intervalMs = params.intervalMs ?? 3000;

  // Track tab visibility. Start optimistically `true` so the
  // first render (which may run on the server or before
  // `visibilitychange` has ever fired) reflects the caller's
  // intent rather than defaulting to "paused".
  const [isVisible, setIsVisible] = useState<boolean>(() => {
    if (typeof document === 'undefined') return true;
    return !document.hidden;
  });

  useEffect(() => {
    if (typeof document === 'undefined') return undefined;

    function handleVisibility(): void {
      setIsVisible(!document.hidden);
    }

    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  if (!params.shouldPoll || !isVisible) {
    return { refetchInterval: false };
  }

  return { refetchInterval: intervalMs };
}
