// src/features/memory/hooks/useFocusTrap.ts

/**
 * Traps keyboard focus inside a container while it is active.
 *
 * Behavior
 * --------
 *
 *   * **Tab on the last focusable element** → wraps to the first.
 *   * **Shift+Tab on the first focusable element** → wraps to the last.
 *   * **Tab from outside the trap** → jumps to the first focusable
 *     element (handles the case where focus drifted out via mouse
 *     click on the backdrop).
 *   * **On unmount (or when `isActive` flips to false)** → restores
 *     focus to whatever had it when the trap activated. This is the
 *     "focus returns to trigger button" behavior that WAI-ARIA
 *     Authoring Practices recommends for modals.
 *
 * What it does NOT do
 * -------------------
 *
 *   * **It does NOT auto-focus the first element on mount.** The two
 *     memory dialogs (`EntryEditor`, `ConfirmDangerDialog`) already
 *     focus a specific element after mount (the content textarea,
 *     the confirm-token input). Overriding that would degrade UX.
 *     The hook only ensures focus STAYS inside once it is there.
 *
 *   * **It does NOT prevent clicks outside the container.** Backdrop
 *     clicks are handled by the dialogs themselves (they call
 *     `onOpenChange(false)` on backdrop click), which is the
 *     correct pattern: trap focus, but let the user escape by
 *     clicking outside.
 *
 *   * **It does NOT disable body scroll.** That is a separate
 *     concern -- a future `useScrollLock` hook can handle it. Not
 *     needed for these modal dialogs: the page underneath is
 *     covered by the backdrop, so scrolling it is invisible anyway.
 *
 * Focusable selector
 * ------------------
 *
 * Follows the WAI-ARIA list, minus elements that cannot be focused
 * programmatically (`tabindex="-1"`) and elements that are inside
 * another focus trap (nested dialogs would confuse the wrap logic —
 * a future enhancement, not needed today since the memory feature
 * never nests dialogs).
 *
 * Why a custom hook instead of a library
 * --------------------------------------
 *
 * `focus-trap-react` and similar libraries bring ~5KB and a lot of
 * configuration surface for what is, in practice, one ~60-line
 * effect. The memory feature has exactly two dialogs; both use the
 * same behavior. A custom hook keeps the dependency graph small and
 * the behavior explicit.
 */

'use client';

import { useEffect, type RefObject } from 'react';

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

/**
 * Elements that can receive focus via Tab, in DOM order.
 *
 * Excludes:
 *   * `[tabindex="-1"]` -- explicitly removed from the tab order.
 *   * disabled elements -- cannot be focused.
 *   * `[aria-hidden="true"]` -- hidden from assistive tech AND
 *     from keyboard users.
 *
 * `a[href]` (not `a`) so anchors without hrefs (rare, but possible
 * with custom routers) are correctly excluded.
 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Return the focusable descendants of `container`, in DOM order.
 *
 * Filters out elements that are hidden via `display: none` or
 * `visibility: hidden` — those still match `querySelectorAll` but
 * cannot actually receive focus, and including them would break
 * the wrap logic.
 */
function getFocusableElements(
  container: HTMLElement,
): HTMLElement[] {
  const all = Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  );
  return all.filter((el) => {
    if (!el.offsetParent && el.tagName !== 'BODY') {
      // offsetParent === null means the element or an ancestor is
      // display:none. But offsetParent is also null for position:fixed
      // elements, so we cannot rely on it alone. The `visibility`
      // check below catches the common case; a hidden element that
      // passes both checks would only fail if the app renders it
      // via `visibility: hidden` on an ancestor -- rare, and the
      // consequence is a focus attempt that silently does nothing,
      // which is strictly better than allowing focus to escape.
      const style = window.getComputedStyle(el);
      if (style.visibility === 'hidden') return false;
    }
    return true;
  });
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Trap focus inside `containerRef` while `isActive` is true.
 *
 * @param containerRef  Ref to the element that should contain focus
 *                      (typically the dialog panel, NOT the backdrop).
 * @param isActive      Whether the trap is currently active. Pass
 *                      `open` from the dialog component.
 */
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  isActive: boolean,
): void {
  useEffect(() => {
    if (!isActive) return undefined;
    const container = containerRef.current;
    if (!container) return undefined;

    // Snapshot the element that had focus BEFORE the trap opened,
    // so we can restore it on close. This is what makes "return
    // focus to the trigger button" work: the trigger button is
    // focused when the user clicks it, so it IS `previousFocus`.
    const previousFocus =
      (document.activeElement as HTMLElement | null) ?? null;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Tab') return;
      if (!container) return;

      const focusables = getFocusableElements(container);
      if (focusables.length === 0) {
        // Nothing focusable inside -- prevent Tab from escaping to
        // the browser chrome. This should never happen with these
        // dialogs (they always have at least a close button), but
        // the guard prevents a security-relevant edge case where
        // focus escapes to the page underneath.
        event.preventDefault();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const current = document.activeElement as HTMLElement | null;

      if (event.shiftKey) {
        // Shift+Tab: if we are AT the first element (or focus has
        // drifted outside entirely), wrap to the last.
        if (current === first || !container.contains(current)) {
          event.preventDefault();
          last.focus();
        }
      } else {
        // Tab: if we are AT the last element (or focus has drifted
        // outside entirely), wrap to the first.
        if (current === last || !container.contains(current)) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      // Restore focus, but only if the previous element still
      // exists in the DOM. If the trigger was a list item that
      // was just deleted (e.g. after "Delete" in the danger
      // dialog), focusing it would throw; skip silently.
      if (
        previousFocus &&
        document.body.contains(previousFocus)
      ) {
        try {
          previousFocus.focus();
        } catch {
          // Silently ignore -- the element may have become
          // unfocusable between the contains() check and the
          // focus() call.
        }
      }
    };
  }, [containerRef, isActive]);
}
