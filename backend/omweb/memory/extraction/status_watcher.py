# backend/omweb/memory/extraction/status_watcher.py
"""
PELDRUN Memory Platform - Phase 1-C: Session Status Watcher.

Bridges the "session just completed" event to the After-Session
extraction trigger. Two entry points:

  * ``notify_session_updated()`` -- called by the dispatcher after
    every turn (or by any code that observes a session change).
    Idempotent: a redundant call on an unchanged session is a
    no-op.

  * ``recover()`` -- called once at worker startup. Scans all
    sessions for completed-but-unprocessed ones and re-enqueues
    them. This is how the system survives a crash between the
    "session completed" transition and the enqueue of the
    AFTER_SESSION job.

Why the watcher exists
----------------------

The session status transition is written by
``project_manager.save_chat_session``, which is synchronous and
has no way to call an async enqueue. The watcher is the async
bridge: the dispatcher calls it AFTER the save, on the same
event loop, so the enqueue happens promptly but never blocks
the write.

``recover()`` closes the durability gap:

    User clicks "End Chat"
       |
       v
    save_chat_session(status="completed")  <- synchronous, fast
       |
       v  (CRASH HERE = no enqueue)
       |
       v
    notify_session_updated(chat_id)        <- async, creates job
       |
       v
    worker picks up job, extracts

A crash between the two calls means the session is marked
completed but no job was enqueued. Without `recover()`, that
session would never be extracted. With `recover()`, the next
worker startup catches it.

Design invariants
-----------------

1. **Failures are logged, never raised.**
   A status-watcher failure must NOT abort the turn that just
   completed. The user's chat is already saved; the extraction
   is best-effort. ``notify_session_updated`` returns ``None``
   on every path and logs at WARNING level on unexpected
   errors.

2. **Idempotent by construction.**
   The watcher calls ``trigger_router.route_after_session``,
   which computes an idempotency key from the current state.
   Calling the watcher twice in a row produces the same key
   twice; the second enqueue raises ``DuplicateJobError``,
   which the router swallows. No job duplication is possible.

3. **The enabled check is caller-controlled.**
   The watcher accepts ``settings_provider: Callable[[], MemoryAISettings]``.
   Every entry point consults it FIRST, before any I/O. When
   extraction is disabled, the watcher returns immediately --
   zero cost for deployments that never enable the feature.

4. **`both` and `after_session` are honored; `after_turn` skips.**
   The trigger mode is read from settings. When the mode is
   ``"after_turn"``, the After-Session watcher does nothing
   (the dispatcher's After-Turn hook handles extraction).
   When it is ``"after_session"`` or ``"both"``, the watcher
   enqueues as appropriate.

5. **`recover()` is bounded and logged.**
   It scans all sessions once, processes only those with
   ``status == completed`` AND pending turns, and logs a
   summary. It does NOT retry on failure -- the next restart
   will try again.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from omweb.memory.extraction.session_scanner import (
    MemorySessionScanner,
    SessionCandidate,
)
from omweb.memory.extraction.trigger_router import (
    TriggerRouter,
)


logger = logging.getLogger(
    "omweb.memory.extraction.status_watcher"
)


# ---------------------------------------------------------------------------
# Settings protocol (duck-typed)
# ---------------------------------------------------------------------------


# The watcher only needs to inspect three fields of the settings
# object. Importing `MemoryAISettings` here would create a
# module-level dependency on the settings schema, which couples
# the extraction layer to P1.7's settings package. Instead, the
# watcher declares the minimal shape it needs via a Protocol-like
# duck-typing convention:
#
#     settings.enabled                     -- bool
#     settings.extraction_enabled          -- bool
#     settings.extraction_trigger          -- "after_turn" | "after_session" | "both"
#
# Any object with these attributes (Pydantic model, dataclass,
# namespace) satisfies the contract. Phase 1-C's caller passes the
# real MemoryAISettings; tests pass a lightweight SimpleNamespace.
# This keeps the coupling strictly structural.

_APPLICABLE_TRIGGERS = frozenset({"after_session", "both"})
"""Extraction trigger modes for which the watcher does work."""


# ---------------------------------------------------------------------------
# SessionStatusWatcher
# ---------------------------------------------------------------------------


class SessionStatusWatcher:
    """Enqueues After-Session extraction when a session completes.

    See the module docstring for the rationale and design
    invariants.
    """

    def __init__(
        self,
        scanner: MemorySessionScanner,
        trigger_router: TriggerRouter,
        *,
        settings_provider: Callable[[], Optional[object]],
    ) -> None:
        """
        Parameters
        ----------
        scanner
            Used read-only to inspect session state.
        trigger_router
            Used to enqueue jobs. Handles idempotency and the
            enabled check internally (double-checked here).
        settings_provider
            Callable returning the current MemoryAISettings
            instance (or any object exposing ``enabled``,
            ``extraction_enabled``, and ``extraction_trigger``).
            Returns None when settings are not yet available
            (e.g. very early startup). In that case the watcher
            treats it as "disabled" and returns immediately.
        """
        self._scanner = scanner
        self._router = trigger_router
        self._settings_provider = settings_provider

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def notify_session_updated(
        self,
        *,
        chat_id: str,
        project_id: Optional[str] = None,
    ) -> None:
        """React to a session update, enqueuing After-Session if due.

        Called by the dispatcher after each successful turn.
        Idempotent. Never raises.

        Parameters
        ----------
        chat_id
            The chat whose session just changed.
        project_id
            The project the chat belongs to. Optional --
            resolved from the session file when absent.
        """
        try:
            settings = self._settings_provider()
            if not self._is_after_session_applicable(settings):
                return

            candidate = await self._scanner.get_candidate(chat_id)
            if candidate is None:
                logger.debug(
                    "notify_session_updated: no candidate for %r; "
                    "skipping.", chat_id,
                )
                return

            if candidate.status != "completed":
                logger.debug(
                    "notify_session_updated: chat=%r status=%r "
                    "(not completed); skipping.",
                    chat_id, candidate.status,
                )
                return

            if candidate.pending_turns <= 0:
                logger.debug(
                    "notify_session_updated: chat=%r has no "
                    "pending turns; skipping.", chat_id,
                )
                return

            effective_project_id = (
                project_id
                if project_id is not None
                else candidate.project_id
            )

            jobs = await self._router.route_after_session(
                chat_id=chat_id,
                project_id=effective_project_id,
            )
            if jobs:
                logger.info(
                    "notify_session_updated: enqueued %d After-"
                    "Session job(s) for chat=%r.",
                    len(jobs), chat_id,
                )
            # If jobs is empty, the router's own logging already
            # explains why (duplicate, no work, etc.). No need to
            # duplicate here.
        except Exception as exc:
            # Never raise: the caller (dispatcher) has just
            # completed a user turn. A status-watcher failure
            # must not abort or fail that turn.
            logger.warning(
                "notify_session_updated failed for chat=%r "
                "(non-fatal): %s",
                chat_id, exc, exc_info=True,
            )

    async def recover(self) -> int:
        """Scan all sessions and re-enqueue completed-but-unprocessed.

        Called once at worker startup (see ``main.py`` lifespan).
        Returns the number of jobs enqueued (0 in the common
        case). Never raises.

        What "unprocessed" means
        ------------------------
        A session is a recovery candidate when:

          * its healed status is ``"completed"``,
          * its ``pending_turns > 0``,
          * its trigger mode allows After-Session.

        Sessions whose ``status`` is still ``"running"`` are
        skipped: they may be live and currently being written by
        a dispatcher. If they were abandoned mid-turn, their
        healing will convert them to ``"completed"`` on the next
        read, and the NEXT restart will pick them up.
        """
        try:
            settings = self._settings_provider()
            if not self._is_after_session_applicable(settings):
                logger.debug(
                    "recover: After-Session trigger is not active; "
                    "skipping recovery scan."
                )
                return 0

            # only_unprocessed=True gives us an already-filtered list.
            candidates = await self._scanner.scan_all_sessions(
                only_unprocessed=True
            )

            completed = [
                c for c in candidates
                if c.status == "completed"
            ]
            if not completed:
                logger.info(
                    "recover: no completed sessions with pending "
                    "turns to re-enqueue."
                )
                return 0

            logger.info(
                "recover: found %d completed session(s) with "
                "pending turns; re-enqueueing.",
                len(completed),
            )

            total_enqueued = 0
            for candidate in completed:
                try:
                    jobs = await self._router.route_after_session(
                        chat_id=candidate.chat_id,
                        project_id=candidate.project_id,
                    )
                    total_enqueued += len(jobs)
                except Exception as exc:
                    # Per-session failures do not abort recovery.
                    # A malformed session is skipped; the next
                    # restart will try again.
                    logger.warning(
                        "recover: failed to enqueue chat=%r "
                        "(continuing): %s",
                        candidate.chat_id, exc,
                    )

            logger.info(
                "recover: re-enqueued %d job(s) across %d "
                "candidate session(s).",
                total_enqueued, len(completed),
            )
            return total_enqueued

        except Exception as exc:
            logger.warning(
                "recover: scan failed (non-fatal): %s",
                exc, exc_info=True,
            )
            return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_after_session_applicable(
        self, settings: Optional[object]
    ) -> bool:
        """Return True when After-Session should run.

        Three checks, in order:

          * ``settings`` is not None (early-startup guard),
          * ``settings.enabled`` AND ``settings.extraction_enabled``
            are both True,
          * ``settings.extraction_trigger`` is one of
            ``"after_session"`` / ``"both"``.

        Any failed check returns False -- the caller then does
        nothing, which is correct: no work is due.
        """
        if settings is None:
            return False
        if not getattr(settings, "enabled", False):
            return False
        if not getattr(settings, "extraction_enabled", False):
            return False
        trigger = getattr(settings, "extraction_trigger", None)
        return trigger in _APPLICABLE_TRIGGERS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["SessionStatusWatcher"]
