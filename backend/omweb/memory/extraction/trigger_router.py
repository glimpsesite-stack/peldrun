# backend/omweb/memory/extraction/trigger_router.py
"""
PELDRUN Memory Platform - Phase 1-C: Trigger Router.

Translates runtime events (turn completed, session completed,
batch request, on-demand click) into `MemoryJob` rows that the
worker will eventually pick up. This is the sole mechanism by
which extraction jobs enter the queue.

The four routes
---------------

    AFTER_TURN      -- fires after each chat turn (run_direct_chat)
    AFTER_SESSION   -- fires when session status -> "completed"
    BATCH           -- operator-initiated retroactive extraction
    MANUAL          -- one-shot "Extract from this chat" button

Each route returns a `List[MemoryJob]` of jobs it actually
enqueued. Callers may:

    * ignore the return value (dispatcher, status watcher),
    * inspect it for diagnostics or for the API response
      (batch, on-demand).

Empty list means one of:

    * extraction is not configured (no model),
    * the route has no work to do (no new turns, already
      extracted, session file unreadable),
    * the proposed job was a duplicate of an existing one
      (idempotency key collision).

Empty list is NOT an error. It is the normal, common result on
an idle queue.

Design invariants
-----------------

1. **Idempotency at the key level.**
   Every job's `idempotency_key` is derived deterministically
   from (chat_id, trigger_type, start_turn_index,
   end_turn_index, snapshot_hash[:16]). Two calls to the same
   route with the same inputs produce the same key; the second
   is rejected by `MemoryOutbox.enqueue()` and swallowed here
   as `DuplicateJobError`. There is NO read-then-write race.

2. **The cursor is READ, never WRITTEN, here.**
   The router consults `candidate.last_extracted_turn_index`
   (derived from the cursor row by the scanner) to decide the
   start of a job's range. Only `MemoryOutbox.complete()`
   advances the cursor. This preserves the §19.3 invariant:
   failed or cancelled jobs never lose work.

3. **After-Turn respects the batching flag.**
   When `after_turn_batching=True` (the default), a burst of
   unprocessed turns collapses into a single job whose range
   covers them all. When False, one job is created per turn.
   Batching is strongly preferred because each job triggers a
   separate LLM call; batching reduces cost without affecting
   extraction quality.

4. **After-Session skips already-extracted turns.**
   If a chat was live-extracted (After-Turn), its cursor is
   already past the last turn, so After-Session finds nothing
   to enqueue. That is the correct behavior: no double
   extraction, no double cost.

5. **Snapshot is computed BEFORE enqueue.**
   The job's `message_snapshot_hash` is the state of the
   session AT ENQUEUE TIME. The worker re-verifies it on every
   heartbeat; a mismatch triggers `SESSION_CHANGED` and the
   job fails. Computing the snapshot here (not at processing
   time) means the check is against the same state the trigger
   observed, which is the only sensible baseline.

6. **Scope is derived from project_id, not passed explicitly.**
   When `project_id` is non-empty, the job's scope is
   `(PROJECT, project_id)`. When it is None or empty, the
   scope is `(CHAT, chat_id)`. This keeps the caller from
   having to decide the scope for each route.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from omweb.memory.extraction.session_scanner import (
    MemorySessionScanner,
    SessionCandidate,
)
from omweb.memory.extraction.snapshot import (
    compute_snapshot_from_file,
)
from omweb.memory.jobs.models import (
    MemoryJob,
)
from omweb.memory.jobs.outbox import (
    DuplicateJobError,
    MemoryOutbox,
)
from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)


logger = logging.getLogger(
    "omweb.memory.extraction.trigger_router"
)


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionModelConfig:
    """Which LLM the extraction pipeline should call.

    Resolved by the caller (dispatcher, status watcher, API
    endpoint) from `MemoryAISettings.extraction`. Kept as a
    standalone value object rather than embedding the whole
    settings model so that:

      * the router stays decoupled from the settings schema,
      * tests can pass a config without building a full
        MemoryAISettings instance,
      * Phase 3 can add settings fields (temperature, etc.)
        without changing this contract.
    """

    provider_id: str
    model_id: str


# ---------------------------------------------------------------------------
# TriggerRouter
# ---------------------------------------------------------------------------


class TriggerRouter:
    """Enqueues `MemoryJob` rows in response to trigger events.

    Stateless except for the references it holds. A single
    instance can be shared across the process; concurrent calls
    are safe because every state-mutating operation is
    serialized by `MemoryOutbox`'s internal lock.
    """

    def __init__(
        self,
        scanner: MemorySessionScanner,
        outbox: MemoryOutbox,
        *,
        model_config_provider: Callable[
            [], Optional[ExtractionModelConfig]
        ],
        after_turn_batching: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        scanner
            Used read-only to inspect session state.
        outbox
            Used to enqueue jobs. Handles idempotency internally.
        model_config_provider
            Callable returning the current LLM configuration,
            or None when extraction is not configured (no model
            chosen in settings, or extraction disabled). When it
            returns None, every route short-circuits to an empty
            list WITHOUT inspecting the session -- a cheap
            fast-path that avoids disk I/O on every turn when
            extraction is off.
        after_turn_batching
            When True (default), After-Turn collapses multiple
            pending turns into a single job. When False, one job
            per turn. See invariant #3.
        """
        self._scanner = scanner
        self._outbox = outbox
        self._model_config_provider = model_config_provider
        self._after_turn_batching = bool(after_turn_batching)

    # ------------------------------------------------------------------
    # Route: After-Turn
    # ------------------------------------------------------------------

    async def route_after_turn(
        self,
        *,
        chat_id: str,
        project_id: Optional[str],
        current_turn_index: int,
    ) -> List[MemoryJob]:
        """Enqueue extraction for turns up to (and including) current_turn_index.

        Parameters
        ----------
        chat_id
            The chat that just produced a new turn.
        project_id
            The chat's project, or None/"" for standalone chats.
        current_turn_index
            0-based index of the just-completed turn (the new
            `turns[-1]`). Callers obtain it as
            ``len(project_manager.get_chat(cid)["turns"]) - 1``.

        Returns
        -------
        List[MemoryJob]
            Jobs actually enqueued. Usually zero (queue already
            ahead) or one (batching) or N (non-batching mode
            catching up on multiple pending turns).
        """
        config = self._model_config_provider()
        if config is None:
            return []

        if current_turn_index < 0:
            # Defensive: a chat with no turns cannot be extracted.
            logger.debug(
                "route_after_turn: chat=%r has current_turn_index=%d "
                "(negative); skipping.",
                chat_id, current_turn_index,
            )
            return []

        candidate = await self._scanner.get_candidate(chat_id)
        if candidate is None:
            logger.debug(
                "route_after_turn: no candidate for chat=%r "
                "(session missing or unreadable); skipping.",
                chat_id,
            )
            return []

        # Clamp current_turn_index against the actual turn count.
        # The caller's index might be stale if the session shrank
        # (a turn was deleted out-of-band). Better to under-extract
        # than to crash on an out-of-range index.
        effective_end = min(
            current_turn_index,
            candidate.total_turns - 1,
        )

        last_processed = candidate.last_extracted_turn_index
        pending_count = effective_end - last_processed
        if pending_count <= 0:
            logger.debug(
                "route_after_turn: chat=%r already up to turn %d "
                "(current=%d); nothing to do.",
                chat_id, last_processed, effective_end,
            )
            return []

        start = last_processed + 1
        end = effective_end

        snapshot = await self._compute_snapshot(candidate)
        if snapshot is None:
            return []

        if self._after_turn_batching:
            return await self._enqueue_one(
                chat_id=chat_id,
                project_id=project_id,
                candidate=candidate,
                trigger_type=ExtractionTrigger.AFTER_TURN,
                start=start,
                end=end,
                snapshot=snapshot,
                config=config,
            )

        # Non-batching mode: one job per turn.
        all_jobs: List[MemoryJob] = []
        for turn_idx in range(start, end + 1):
            jobs = await self._enqueue_one(
                chat_id=chat_id,
                project_id=project_id,
                candidate=candidate,
                trigger_type=ExtractionTrigger.AFTER_TURN,
                start=turn_idx,
                end=turn_idx,
                snapshot=snapshot,
                config=config,
            )
            all_jobs.extend(jobs)
        return all_jobs

    # ------------------------------------------------------------------
    # Route: After-Session
    # ------------------------------------------------------------------

    async def route_after_session(
        self,
        *,
        chat_id: str,
        project_id: Optional[str],
    ) -> List[MemoryJob]:
        """Enqueue extraction for turns not yet extracted (whole tail).

        Used by the `SessionStatusWatcher` when a session
        transitions to `completed`. In `both` mode (After-Turn +
        After-Session), After-Turn has already extracted up to
        the last turn, so this returns an empty list -- zero cost.

        Used standalone in `after_session` mode (the default),
        where it processes the entire session once at the end.
        """
        config = self._model_config_provider()
        if config is None:
            return []

        candidate = await self._scanner.get_candidate(chat_id)
        if candidate is None:
            return []

        last_processed = candidate.last_extracted_turn_index
        start = last_processed + 1
        end = candidate.total_turns - 1
        if start > end:
            logger.debug(
                "route_after_session: chat=%r has no pending turns "
                "(last_processed=%d, total=%d); nothing to do.",
                chat_id, last_processed, candidate.total_turns,
            )
            return []

        snapshot = await self._compute_snapshot(candidate)
        if snapshot is None:
            return []

        return await self._enqueue_one(
            chat_id=chat_id,
            project_id=project_id,
            candidate=candidate,
            trigger_type=ExtractionTrigger.AFTER_SESSION,
            start=start,
            end=end,
            snapshot=snapshot,
            config=config,
        )

    # ------------------------------------------------------------------
    # Route: Batch
    # ------------------------------------------------------------------

    async def route_batch(
        self,
        *,
        batch_id: Optional[str] = None,
    ) -> List[MemoryJob]:
        """Enqueue extraction for every chat with pending turns.

        Used by ``MemoryBatchService.create_batch``. One job per
        chat, covering the whole pending tail. Chats with no
        pending turns are skipped. Sessions whose snapshot
        cannot be read are skipped with a debug log.

        Parameters
        ----------
        batch_id
            Optional batch identifier to stamp on every enqueued
            job. When set, the id is:
              * stored in ``memory_jobs.batch_id`` (the SQL column
                added by Migration v3), so the batch service can
                aggregate progress with a single ``WHERE batch_id
                = ?`` query;
              * included in the job's ``idempotency_key`` (see
                ``MemoryJob.compute_idempotency_key``'s
                ``batch_id`` parameter) so two batches created
                back-to-back on the same unchanged sessions both
                run -- the second batch's jobs must NOT be
                silently deduped against the first.

        Returns
        -------
        List[MemoryJob]
            Every job enqueued, in the order the sessions were
            scanned. Ordering matters for the batch UI's progress
            bar; callers should not re-sort.
        """
        config = self._model_config_provider()
        if config is None:
            return []

        pending_sessions = await self._scanner.scan_all_sessions(
            only_unprocessed=True
        )
        logger.info(
            "route_batch: %d chat(s) have pending turns.",
            len(pending_sessions),
        )

        all_jobs: List[MemoryJob] = []
        for candidate in pending_sessions:
            if candidate.pending_turns <= 0:
                continue
            start = candidate.last_extracted_turn_index + 1
            end = candidate.total_turns - 1
            if start > end:
                continue

            snapshot = await self._compute_snapshot(candidate)
            if snapshot is None:
                logger.debug(
                    "route_batch: skipping chat=%r -- snapshot "
                    "unavailable (session file missing or corrupt).",
                    candidate.chat_id,
                )
                continue

            jobs = await self._enqueue_one(
                chat_id=candidate.chat_id,
                project_id=candidate.project_id,
                candidate=candidate,
                trigger_type=ExtractionTrigger.BATCH,
                start=start,
                end=end,
                snapshot=snapshot,
                config=config,
                batch_id=batch_id,
            )
            all_jobs.extend(jobs)

        logger.info(
            "route_batch: %d job(s) enqueued.", len(all_jobs)
        )
        return all_jobs

    # ------------------------------------------------------------------
    # Route: On-Demand
    # ------------------------------------------------------------------

    async def route_on_demand(
        self,
        *,
        chat_id: str,
        project_id: Optional[str] = None,
    ) -> List[MemoryJob]:
        """Enqueue extraction for one specific chat (whole session).

        Used by `POST /api/memory/extract/from-session` (the "Extract
        from this chat" button). Unlike After-Session, this is
        EXPLICIT user intent: process the whole session even if
        the cursor suggests some turns were already extracted.

        The idempotency key is derived from (chat_id, MANUAL,
        0, total-1, snapshot) so a user double-clicking the button
        does NOT create two jobs. A second click after the first
        completes re-processes the session (new job, different
        snapshot if content changed, or SESSION_CHANGED if the
        session was edited mid-processing).

        Parameters
        ----------
        chat_id
            The chat to extract from. Must exist on disk.
        project_id
            Optional override. When None, resolved from the
            session's own `project_id`. The override exists
            because the API endpoint knows the project from the
            request body/URL, and might want to enqueue a job
            for a chat that has moved projects.
        """
        config = self._model_config_provider()
        if config is None:
            return []

        candidate = await self._scanner.get_candidate(chat_id)
        if candidate is None:
            logger.debug(
                "route_on_demand: no candidate for chat=%r; "
                "skipping.",
                chat_id,
            )
            return []

        if candidate.total_turns == 0:
            logger.debug(
                "route_on_demand: chat=%r has zero turns; "
                "skipping.",
                chat_id,
            )
            return []

        # Prefer the caller's project_id (from request context)
        # over the one derived from the session file, because
        # the file's project_id may be stale after a move.
        effective_project_id = (
            project_id if project_id is not None
            else candidate.project_id
        )

        snapshot = await self._compute_snapshot(candidate)
        if snapshot is None:
            return []

        return await self._enqueue_one(
            chat_id=chat_id,
            project_id=effective_project_id,
            candidate=candidate,
            trigger_type=ExtractionTrigger.MANUAL,
            start=0,
            end=candidate.total_turns - 1,
            snapshot=snapshot,
            config=config,
        )

    # ------------------------------------------------------------------
    # Route: Explicit range (used by MemoryService.enqueue_extraction)
    # ------------------------------------------------------------------

    async def route_explicit(
        self,
        *,
        chat_id: str,
        project_id: Optional[str],
        trigger_type: ExtractionTrigger,
        start_turn_index: int,
        end_turn_index: int,
    ) -> List[MemoryJob]:
        """Enqueue a job with an EXPLICIT turn range.

        Unlike the four public routes above, this method does NOT
        compute the range from the cursor or from the session's
        total turn count. The caller has already decided the
        range -- typically because it is the dispatcher's
        After-Turn hook, which knows the current turn index, or
        an operator-initiated UI action that specifies a range.

        Idempotency is handled by `MemoryOutbox.enqueue()` (the
        UNIQUE constraint on ``idempotency_key``). A redundant
        call with the same inputs raises `DuplicateJobError`,
        which this method swallows and converts to an empty
        return list.

        Returns
        -------
        List[MemoryJob]
            Zero or one job. Zero when: extraction is not
            configured, the session is unreadable, the snapshot
            is unavailable, or the range duplicates an existing
            job.
        """
        config = self._model_config_provider()
        if config is None:
            return []

        if start_turn_index < 0 or end_turn_index < start_turn_index:
            logger.debug(
                "route_explicit: invalid range [%d..%d] for "
                "chat=%r; skipping.",
                start_turn_index, end_turn_index, chat_id,
            )
            return []

        candidate = await self._scanner.get_candidate(chat_id)
        if candidate is None:
            logger.debug(
                "route_explicit: no candidate for chat=%r "
                "(session missing or unreadable); skipping.",
                chat_id,
            )
            return []

        # Clamp the range against the actual turn count so a
        # stale end_turn_index from the caller cannot produce a
        # job whose snapshot cannot be computed.
        effective_end = min(
            end_turn_index, candidate.total_turns - 1
        )
        effective_start = max(0, start_turn_index)
        if effective_start > effective_end:
            logger.debug(
                "route_explicit: clamped range is empty "
                "[%d..%d] for chat=%r; skipping.",
                effective_start, effective_end, chat_id,
            )
            return []

        snapshot = await self._compute_snapshot(candidate)
        if snapshot is None:
            return []

        return await self._enqueue_one(
            chat_id=chat_id,
            project_id=project_id,
            candidate=candidate,
            trigger_type=trigger_type,
            start=effective_start,
            end=effective_end,
            snapshot=snapshot,
            config=config,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _compute_snapshot(
        self, candidate: SessionCandidate
    ) -> Optional[str]:
        """Compute the message snapshot hash, or None on failure."""
        snapshot = await compute_snapshot_from_file_async(
            candidate.source_session_path
        )
        if snapshot is None:
            logger.debug(
                "TriggerRouter: snapshot unavailable for chat=%r "
                "(path=%r).",
                candidate.chat_id,
                candidate.source_session_path,
            )
        return snapshot

    async def _enqueue_one(
        self,
        *,
        chat_id: str,
        project_id: Optional[str],
        candidate: SessionCandidate,
        trigger_type: ExtractionTrigger,
        start: int,
        end: int,
        snapshot: str,
        config: ExtractionModelConfig,
        batch_id: Optional[str] = None,
    ) -> List[MemoryJob]:
        """Build, hash, and enqueue one job. Swallow duplicates.

        ``batch_id`` is threaded through to the ``MemoryJob``
        constructor AND to ``compute_idempotency_key``. All
        non-batch routes (after_turn / after_session / on_demand
        / explicit) leave it at the default ``None`` -- their
        jobs behave exactly as before.
        """
        idempotency_key = MemoryJob.compute_idempotency_key(
            chat_id=chat_id,
            trigger_type=trigger_type,
            start_turn_index=start,
            end_turn_index=end,
            message_snapshot_hash=snapshot,
            batch_id=batch_id,
        )

        scope_type, scope_id = _derive_scope(
            chat_id=chat_id, project_id=project_id
        )

        job = MemoryJob(
            chat_id=chat_id,
            project_id=project_id,
            scope_type=scope_type,
            scope_id=scope_id,
            trigger_type=trigger_type,
            start_turn_index=start,
            end_turn_index=end,
            last_processed_turn_index_at_enqueue=(
                candidate.last_extracted_turn_index
            ),
            message_snapshot_hash=snapshot,
            source_session_path=candidate.source_session_path,
            model_provider_id=config.provider_id,
            model_id=config.model_id,
            idempotency_key=idempotency_key,
            batch_id=batch_id,
        )

        try:
            saved = await self._outbox.enqueue(job)
        except DuplicateJobError as dup:
            logger.debug(
                "route_%s: duplicate job for chat=%r range=[%d..%d] "
                "(existing=%r); skipping.",
                trigger_type.value,
                chat_id, start, end, dup.existing_job_id,
            )
            return []

        logger.info(
            "route_%s: enqueued job %s for chat=%r range=[%d..%d].",
            trigger_type.value,
            saved.job_id,
            chat_id, start, end,
        )
        return [saved]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _derive_scope(
    *,
    chat_id: str,
    project_id: Optional[str],
) -> tuple[MemoryScope, str]:
    """Return (scope_type, scope_id) for a job's target scope.

    * project_id present  -> (PROJECT, project_id)
    * project_id absent   -> (CHAT, chat_id)

    The chat-scope fallback is deliberate: standalone chats
    (default_project) should not have their memories leak into
    the shared default project scope, which would be visible to
    every standalone chat.
    """
    if project_id:
        return MemoryScope.PROJECT, project_id
    return MemoryScope.CHAT, chat_id


async def compute_snapshot_from_file_async(
    session_path: str,
) -> Optional[str]:
    """Async wrapper around `compute_snapshot_from_file`.

    Snapshot hashing is CPU-bound but fast (~microseconds for a
    typical session). Running it via `to_thread` keeps the event
    loop free during batch scanning, where the router hashes
    hundreds of sessions in a row.
    """
    import asyncio
    return await asyncio.to_thread(
        compute_snapshot_from_file, session_path
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ExtractionModelConfig",
    "TriggerRouter",
]
