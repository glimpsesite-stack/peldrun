# backend/tests/memory/test_phase1a_jobs.py
"""
Phase 1-A acceptance tests: memory job queue mechanics.

Covers the five acceptance tests from the plan:

    T4   -- Duplicate enqueue -> DuplicateJobError
    T7   -- Kill worker -> restart -> lease recovered
    T8   -- 100 jobs across 3 chats, FIFO per chat, all complete
    T19  -- Rapid sends -> FIFO per chat (no cross-chat hijack)
    T20  -- Cursor invariant across 10 lifecycle steps

Every test uses a per-test SQLite file via `tmp_path`, so tests
are isolated and reproducible. No test touches the production
database. No test needs an LLM or a provider -- the worker's
`_process_job` is the Phase 1-A stub, which returns an empty
`JobResult` immediately.

The tests deliberately do NOT exercise `MemoryExtractionWorker`
beyond what is needed to validate queue mechanics. Real
extraction is Phase 1-B; its tests will live in a sibling file.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import pytest_asyncio

from omweb.memory.storage.migrations import apply_schema
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
    MemoryJobType,
)
from omweb.memory.jobs.outbox import (
    DuplicateJobError,
    JobNotFoundError,
    JobResult,
    MemoryOutbox,
)
from omweb.memory.jobs.worker import MemoryExtractionWorker
from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a fresh SQLite path with the memory schema applied."""
    path = tmp_path / "memory_test.db"
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return path


@pytest_asyncio.fixture
async def outbox(db_path: Path) -> MemoryOutbox:
    """Return an OPEN MemoryOutbox backed by `db_path`."""
    ob = MemoryOutbox(db_path)
    await ob.open()
    try:
        yield ob
    finally:
        await ob.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_job(
    *,
    chat_id: str = "chat_test",
    trigger: ExtractionTrigger = ExtractionTrigger.AFTER_SESSION,
    start: int | None = 0,
    end: int | None = 4,
    snapshot: str = "snap_aabbccdd11223344",
    project_id: str | None = "proj_test",
    scope_type: MemoryScope = MemoryScope.PROJECT,
    scope_id: str = "proj_test",
    chat_cursor_at_enqueue: int = 0,
    max_attempts: int = 3,
) -> MemoryJob:
    """Build a MemoryJob with deterministic inputs.

    The idempotency key is computed from the same fields the
    production callers will use, so the tests exercise the real
    key-collision surface rather than a simplified one.
    """
    key = MemoryJob.compute_idempotency_key(
        chat_id=chat_id,
        trigger_type=trigger,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash=snapshot,
    )
    return MemoryJob(
        chat_id=chat_id,
        project_id=project_id,
        scope_type=scope_type,
        scope_id=scope_id,
        trigger_type=trigger,
        start_turn_index=start,
        end_turn_index=end,
        last_processed_turn_index_at_enqueue=chat_cursor_at_enqueue,
        message_snapshot_hash=snapshot,
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key=key,
        max_attempts=max_attempts,
    )


class TrackingWorker(MemoryExtractionWorker):
    """Worker that records the completion order of jobs.

    Used by T8 and T19 to verify FIFO semantics at the level of
    "which job finished first". Recording happens INSIDE
    `_process_job` (after the super() stub returns), so the
    recorded order matches the order in which the loop saw each
    job transition to COMPLETED.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.completion_order: List[Tuple[str, int | None]] = []

    async def _process_job(self, job: MemoryJob) -> JobResult:
        result = await super()._process_job(job)
        self.completion_order.append(
            (job.chat_id, job.start_turn_index)
        )
        return result


async def drain_queue(
    outbox: MemoryOutbox,
    expected_completed: int,
    *,
    timeout: float = 30.0,
) -> None:
    """Poll until `expected_completed` jobs reach COMPLETED, or fail."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        completed = await outbox.count_by_status(
            MemoryJobStatus.COMPLETED
        )
        pending = await outbox.count_by_status(
            MemoryJobStatus.PENDING
        )
        running = await outbox.count_by_status(
            MemoryJobStatus.RUNNING
        )
        retry = await outbox.count_by_status(
            MemoryJobStatus.RETRY_PENDING
        )
        if (
            completed == expected_completed
            and pending == 0
            and running == 0
            and retry == 0
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Queue did not drain within {timeout}s "
        f"(expected {expected_completed} completed)"
    )


# ---------------------------------------------------------------------------
# T4: Duplicate enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T4_duplicate_enqueue_raises_duplicate_error(
    outbox: MemoryOutbox,
) -> None:
    """A second enqueue with the same idempotency key MUST raise.

    A different trigger, range, chat, or snapshot MUST produce a
    distinct key and be accepted (verified by the second half of
    the test).
    """
    chat_id = "chat_t4"
    job1 = make_job(chat_id=chat_id)
    saved1 = await outbox.enqueue(job1)

    # Same inputs -> same key -> DuplicateJobError.
    job_dup = make_job(chat_id=chat_id)
    with pytest.raises(DuplicateJobError) as exc_info:
        await outbox.enqueue(job_dup)
    assert exc_info.value.existing_job_id == job1.job_id

    # Different trigger -> different key -> accepted.
    job_diff_trigger = make_job(
        chat_id=chat_id,
        trigger=ExtractionTrigger.AFTER_TURN,
    )
    await outbox.enqueue(job_diff_trigger)

    # Different snapshot -> different key -> accepted.
    job_diff_snap = make_job(chat_id=chat_id, snapshot="snap_other")
    await outbox.enqueue(job_diff_snap)

    # Different range -> different key -> accepted.
    job_diff_range = make_job(chat_id=chat_id, start=2, end=6)
    await outbox.enqueue(job_diff_range)

    # Verify all four coexist.
    all_jobs = await outbox.list_by_chat(chat_id, limit=100)
    assert len(all_jobs) == 4

    # The original row is unchanged.
    refetched = await outbox.get_by_id(job1.job_id)
    assert refetched is not None
    assert refetched.idempotency_key == job1.idempotency_key


# ---------------------------------------------------------------------------
# T7: Kill worker -> restart -> lease recovered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T7_stale_lease_recovered_on_restart(
    outbox: MemoryOutbox,
) -> None:
    """A crashed worker's claim MUST be reclaimable after restart.

    Simulation:
      1. Enqueue a job.
      2. Claim it as "dead_worker" (sets `running` + lease).
      3. Manually expire the lease to simulate a hard crash.
      4. Call `recover_stale_leases()` (what `start()` does).
      5. Verify the job is `retry_pending` with attempts=1.
      6. Claim again as "fresh_worker" -- MUST succeed.
    """
    chat_id = "chat_t7"
    job = make_job(chat_id=chat_id)
    await outbox.enqueue(job)

    # Step 2: claim as dead_worker.
    claimed = await outbox.claim_one(worker_id="dead_worker")
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == MemoryJobStatus.RUNNING
    assert claimed.lease_owner == "dead_worker"

    # Step 3: expire the lease (simulate a crash mid-run).
    conn = outbox._require_conn()  # type: ignore[attr-defined]
    with conn:
        conn.execute(
            "UPDATE memory_jobs "
            "SET lease_expires_at = ? WHERE job_id = ?",
            (time.time() - 3600, job.job_id),
        )

    # Step 4: recovery.
    recovered = await outbox.recover_stale_leases()
    assert recovered == 1

    # Step 5: verify retry_pending state.
    fetched = await outbox.get_by_id(job.job_id)
    assert fetched is not None
    assert fetched.status == MemoryJobStatus.RETRY_PENDING
    assert fetched.attempts == 1
    assert fetched.error_code == "LEASE_EXPIRED"
    assert fetched.lease_owner is None
    assert fetched.next_retry_at is not None
    assert fetched.next_retry_at <= time.time()

    # Step 6: re-claim works.
    reclaimed = await outbox.claim_one(worker_id="fresh_worker")
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id
    assert reclaimed.lease_owner == "fresh_worker"
    assert reclaimed.status == MemoryJobStatus.RUNNING


# ---------------------------------------------------------------------------
# T8: 100 jobs across 3 chats -- all complete, FIFO per chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T8_many_jobs_all_complete_fifo_per_chat(
    outbox: MemoryOutbox,
) -> None:
    """90 jobs (3 chats x 30) drain in FIFO order per chat.

    Assertions:
      * every job reaches COMPLETED;
      * within each chat, completion order == enqueue order
        (verified via TrackingWorker);
      * the per-chat cursor ends at the chat's maximum
        end_turn_index.
    """
    chat_count = 3
    jobs_per_chat = 30
    total = chat_count * jobs_per_chat

    # Enqueue -- all of chat_0 first, then chat_1, then chat_2.
    jobs_by_chat: Dict[str, List[MemoryJob]] = {}
    for chat_idx in range(chat_count):
        chat_id = f"chat_t8_{chat_idx}"
        jobs_by_chat[chat_id] = []
        for turn_idx in range(jobs_per_chat):
            job = make_job(
                chat_id=chat_id,
                start=turn_idx,
                end=turn_idx,
                snapshot=f"snap_{chat_idx}_{turn_idx}",
            )
            await outbox.enqueue(job)
            jobs_by_chat[chat_id].append(job)

    # Sanity: correct number enqueued.
    pending_before = await outbox.count_by_status(
        MemoryJobStatus.PENDING
    )
    assert pending_before == total

    # Drain with a single tracking worker (deterministic order).
    worker = TrackingWorker(
        outbox,
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=5.0,
    )
    await worker.start()
    try:
        await drain_queue(outbox, expected_completed=total)
    finally:
        await worker.stop(timeout_seconds=5)

    # All completed.
    completed = await outbox.count_by_status(
        MemoryJobStatus.COMPLETED
    )
    assert completed == total

    # FIFO per chat.
    for chat_id, chat_jobs in jobs_by_chat.items():
        chat_order = [
            idx for (cid, idx) in worker.completion_order
            if cid == chat_id
        ]
        expected_order = [j.start_turn_index for j in chat_jobs]
        assert chat_order == expected_order, (
            f"FIFO broken for {chat_id}: "
            f"{chat_order} != {expected_order}"
        )

    # Cursor at each chat = max end_turn_index.
    for chat_id, chat_jobs in jobs_by_chat.items():
        cursor = await outbox.get_cursor(chat_id)
        max_end = max(
            j.end_turn_index for j in chat_jobs
            if j.end_turn_index is not None
        )
        assert cursor == max_end, (
            f"cursor mismatch for {chat_id}: "
            f"{cursor} != {max_end}"
        )


# ---------------------------------------------------------------------------
# T19: Rapid sends -> FIFO per chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T19_rapid_sends_fifo_per_chat(
    outbox: MemoryOutbox,
) -> None:
    """Three rapid AFTER_TURN jobs for one chat complete FIFO.

    Mirrors what the After-Turn trigger produces when the operator
    sends three messages in quick succession AND batching is
    disabled (one job per turn). This is one of the two valid
    outcomes of T19; the other (batching into a single job) is
    exercised in Phase 1-C's TriggerRouter tests.
    """
    chat_id = "chat_t19"
    snapshot = "snap_t19_aaaaaaaa"

    # Enqueue three single-turn jobs rapidly.
    for turn_idx in range(3):
        job = make_job(
            chat_id=chat_id,
            trigger=ExtractionTrigger.AFTER_TURN,
            start=turn_idx,
            end=turn_idx,
            snapshot=snapshot,
        )
        await outbox.enqueue(job)

    # Drain.
    worker = TrackingWorker(
        outbox,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        await drain_queue(outbox, expected_completed=3)
    finally:
        await worker.stop(timeout_seconds=3)

    # Completion order matches enqueue order.
    assert worker.completion_order == [
        (chat_id, 0),
        (chat_id, 1),
        (chat_id, 2),
    ]

    # Cursor advanced to the LAST completed turn.
    assert await outbox.get_cursor(chat_id) == 2


# ---------------------------------------------------------------------------
# T20: Cursor invariant (10 steps per §13 of the plan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T20_cursor_invariant_ten_steps(
    outbox: MemoryOutbox,
) -> None:
    """Verify the cursor NEVER advances except on `complete()`.

    The full 10-step sequence from §13:

      1. enqueue(start=0, end=4, S1)       -> cursor == 0
      2. claim -> running                  -> cursor == 0
      3. fail                              -> cursor == 0
      4. enqueue(start=0, end=4, S2)       -> cursor == 0
      5. claim -> fail                     -> cursor == 0
      6. session changes (new snapshot S3)
      7. enqueue(start=0, end=6, S3)       -> pending
      8. claim -> complete                 -> cursor == 6
      9. monotonic: cursor never decreased
     10. enqueue(start=0, end=4, S3)       -> new key, but
                                             cursor stays == 6
    """
    chat_id = "chat_t20"

    # Step 0: initial cursor.
    assert await outbox.get_cursor(chat_id) == 0

    # Step 1: first enqueue.
    job1 = make_job(chat_id=chat_id, start=0, end=4, snapshot="S1")
    await outbox.enqueue(job1)
    assert await outbox.get_cursor(chat_id) == 0

    # Step 2: claim -> running.
    claimed1 = await outbox.claim_one(worker_id="w1")
    assert claimed1 is not None
    assert claimed1.job_id == job1.job_id
    assert claimed1.status == MemoryJobStatus.RUNNING
    assert await outbox.get_cursor(chat_id) == 0

    # Step 3: fail (simulated LLM error).
    await outbox.fail(job1.job_id, "LLM_ERROR", "Simulated")
    assert await outbox.get_cursor(chat_id) == 0
    fetched1 = await outbox.get_by_id(job1.job_id)
    assert fetched1 is not None
    assert fetched1.status == MemoryJobStatus.FAILED

    # Step 4: user retries with same range but new snapshot.
    job2 = make_job(chat_id=chat_id, start=0, end=4, snapshot="S2")
    await outbox.enqueue(job2)
    assert await outbox.get_cursor(chat_id) == 0

    # Step 5: claim -> fail again.
    claimed2 = await outbox.claim_one(worker_id="w1")
    assert claimed2 is not None
    assert claimed2.job_id == job2.job_id
    await outbox.fail(job2.job_id, "LLM_ERROR", "Simulated again")
    assert await outbox.get_cursor(chat_id) == 0

    # Step 6: user sends 2 new messages -> snapshot becomes S3.
    # (Simulated by using a new snapshot string.)

    # Step 7: new job3 covering the full 7-turn range (0..6).
    job3 = make_job(chat_id=chat_id, start=0, end=6, snapshot="S3")
    await outbox.enqueue(job3)
    # Still 0: job3 is pending, not complete.
    assert await outbox.get_cursor(chat_id) == 0

    # Step 8: claim -> complete. Cursor MUST become 6.
    claimed3 = await outbox.claim_one(worker_id="w1")
    assert claimed3 is not None
    assert claimed3.job_id == job3.job_id
    await outbox.complete(job3.job_id, JobResult())
    assert await outbox.get_cursor(chat_id) == 6

    # Step 9: monotonic verification -- cursor never decreased.
    # (Already asserted inline above; final check.)
    final_cursor = await outbox.get_cursor(chat_id)
    assert final_cursor == 6

    # Step 10: enqueue a range that was already processed.
    # Different range (0..4) than job3 (0..6), so this is NOT a
    # duplicate. It enqueues successfully. But the cursor MUST
    # NOT move backwards -- it stays at 6.
    job4 = make_job(chat_id=chat_id, start=0, end=4, snapshot="S3")
    await outbox.enqueue(job4)
    assert await outbox.get_cursor(chat_id) == 6

    # Even if job4 completes, the cursor can only advance:
    # `complete()` uses `MAX(existing, new_end)`, and new_end=4
    # is less than existing=6, so it stays 6.
    claimed4 = await outbox.claim_one(worker_id="w1")
    assert claimed4 is not None
    assert claimed4.job_id == job4.job_id
    await outbox.complete(job4.job_id, JobResult())
    assert await outbox.get_cursor(chat_id) == 6


# ---------------------------------------------------------------------------
# Additional mechanics tests (not numbered in the plan but critical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_stub_completes_jobs_with_empty_result(
    outbox: MemoryOutbox,
) -> None:
    """The Phase 1-A worker stub fills `status=completed` cleanly."""
    chat_id = "chat_stub"
    job = make_job(chat_id=chat_id)
    await outbox.enqueue(job)

    worker = MemoryExtractionWorker(
        outbox, poll_interval_seconds=0.05
    )
    await worker.start()
    try:
        await drain_queue(outbox, expected_completed=1)
    finally:
        await worker.stop(timeout_seconds=3)

    completed = await outbox.get_by_id(job.job_id)
    assert completed is not None
    assert completed.status == MemoryJobStatus.COMPLETED
    # Stub returns an empty JobResult, so all counters stay 0.
    assert completed.candidates_count == 0
    assert completed.created_memories_count == 0
    assert completed.cost_nano_usd == 0
    assert completed.decisions == []


@pytest.mark.asyncio
async def test_worker_stop_is_idempotent(
    outbox: MemoryOutbox,
) -> None:
    """Calling `stop()` twice MUST not raise."""
    worker = MemoryExtractionWorker(
        outbox, poll_interval_seconds=0.05
    )
    await worker.start()
    await worker.stop(timeout_seconds=2)
    await worker.stop(timeout_seconds=2)  # no-op, no exception
    assert not worker.is_running


@pytest.mark.asyncio
async def test_worker_start_is_idempotent(
    outbox: MemoryOutbox,
) -> None:
    """Calling `start()` twice MUST not spawn a second loop."""
    worker = MemoryExtractionWorker(
        outbox, poll_interval_seconds=0.05
    )
    await worker.start()
    first_task = worker._run_task  # type: ignore[attr-defined]
    await worker.start()  # no-op
    second_task = worker._run_task  # type: ignore[attr-defined]
    assert first_task is second_task
    await worker.stop(timeout_seconds=2)


@pytest.mark.asyncio
async def test_not_found_on_complete_raises(
    outbox: MemoryOutbox,
) -> None:
    """`complete()` on an unknown job MUST raise `JobNotFoundError`."""
    with pytest.raises(JobNotFoundError):
        await outbox.complete("nonexistent_job_id", JobResult())
