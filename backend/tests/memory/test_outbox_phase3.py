# backend/tests/memory/test_outbox_phase3.py
"""
Phase 3 outbox extensions - test suite.

Covers the four changes introduced for the batch subsystem:

  1. ``MemoryJob.batch_id`` field (Nullable) round-trips through
     the SQL layer without triggering Pydantic's ``extra="forbid"``
     rejection.
  2. ``MemoryOutbox.list_jobs`` / ``count_jobs`` filter and
     paginate correctly, and their results agree.
  3. ``MemoryOutbox.requeue`` transitions ``failed`` / ``cancelled``
     jobs back to ``pending`` with reset bookkeeping, and rejects
     every other state with ``ValueError``.
  4. Migration v3 creates ``memory_batches``, adds
     ``memory_jobs.batch_id``, and records ``CURRENT_VERSION = 3``.

Run with::

    pytest backend/tests/memory/test_outbox_phase3.py -v

Requires ``pytest`` and ``pytest-asyncio`` (already used by the
existing memory test suite in this repo).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)

from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
    MemoryJobType,
)
from omweb.memory.jobs.outbox import (
    JobNotFoundError,
    JobResult,
    MemoryOutbox,
)
from omweb.memory.storage.migrations import (
    CURRENT_VERSION,
    apply_schema,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    """Yield a fresh SQLite file path with the FULL schema applied.

    The outbox's ``open()`` deliberately does NOT call
    ``apply_schema`` -- that responsibility lives in the lifecycle
    manager (production) or in the test's own setup (here). See
    ``MemoryService._initialize_extraction`` for the production
    wiring.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "memory_test.db"
        conn = sqlite3.connect(str(db_path))
        try:
            apply_schema(conn)
        finally:
            conn.close()
        yield db_path


@pytest_asyncio.fixture
async def outbox(temp_db_path):
    """Opened outbox against the freshly-migrated database."""
    ob = MemoryOutbox(temp_db_path)
    await ob.open()
    try:
        yield ob
    finally:
        await ob.close()


def _make_job(
    *,
    chat_id: str = "chat_1",
    trigger: ExtractionTrigger = ExtractionTrigger.MANUAL,
    start: int = 0,
    end: int = 1,
    batch_id: str | None = None,
    idem: str | None = None,
    snapshot: str = "deadbeef" * 8,
) -> MemoryJob:
    """Build a minimal valid ``MemoryJob`` for tests."""
    return MemoryJob(
        chat_id=chat_id,
        scope_type=MemoryScope.CHAT,
        scope_id=chat_id,
        trigger_type=trigger,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash=snapshot,
        model_provider_id="lmstudio",
        model_id="test-model",
        idempotency_key=idem or f"key_{chat_id}_{start}_{end}",
        batch_id=batch_id,
    )


# ---------------------------------------------------------------------------
# 1. batch_id field + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_id_field_defaults_to_none():
    """New jobs without explicit batch_id default to None."""
    job = _make_job()
    assert job.batch_id is None


@pytest.mark.asyncio
async def test_batch_id_round_trips_through_sql(outbox):
    """A batch-scoped job persists and reloads with batch_id intact."""
    job = _make_job(batch_id="batch_abc123")
    await outbox.enqueue(job)

    loaded = await outbox.get_by_id(job.job_id)
    assert loaded is not None
    assert loaded.batch_id == "batch_abc123"


@pytest.mark.asyncio
async def test_batch_id_none_round_trips_through_sql(outbox):
    """A non-batch job persists and reloads with batch_id=None."""
    job = _make_job(batch_id=None)
    await outbox.enqueue(job)

    loaded = await outbox.get_by_id(job.job_id)
    assert loaded is not None
    assert loaded.batch_id is None


@pytest.mark.asyncio
async def test_job_type_remains_extraction_after_v3_migration(outbox):
    """Model still accepts job_type='extraction' post-migration."""
    job = _make_job()
    assert job.job_type == MemoryJobType.EXTRACTION
    await outbox.enqueue(job)
    loaded = await outbox.get_by_id(job.job_id)
    assert loaded is not None
    assert loaded.job_type == MemoryJobType.EXTRACTION


# ---------------------------------------------------------------------------
# 2. list_jobs / count_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_on_empty_table(outbox):
    """No rows -> empty list, not an error."""
    jobs = await outbox.list_jobs()
    assert jobs == []

    count = await outbox.count_jobs()
    assert count == 0


@pytest.mark.asyncio
async def test_list_jobs_returns_all_by_default(outbox):
    """No filter -> all rows, newest first."""
    for i in range(3):
        await outbox.enqueue(_make_job(chat_id=f"c{i}", idem=f"k{i}"))

    jobs = await outbox.list_jobs()
    assert len(jobs) == 3
    # Newest first (created_at DESC).
    for prev, curr in zip(jobs, jobs[1:]):
        assert prev.created_at >= curr.created_at


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status(outbox):
    """Status filter accepts enum member AND string form."""
    job_a = _make_job(chat_id="c1", idem="k1")
    job_b = _make_job(chat_id="c2", idem="k2")
    await outbox.enqueue(job_a)
    await outbox.enqueue(job_b)
    await outbox.fail(job_a.job_id, "ERR", "boom")

    pending_enum = await outbox.list_jobs(status=MemoryJobStatus.PENDING)
    pending_str = await outbox.list_jobs(status="pending")
    failed_enum = await outbox.list_jobs(status=MemoryJobStatus.FAILED)

    assert len(pending_enum) == 1
    assert len(pending_str) == 1
    assert len(failed_enum) == 1
    assert pending_enum[0].job_id == job_b.job_id
    assert failed_enum[0].job_id == job_a.job_id


@pytest.mark.asyncio
async def test_list_jobs_filters_by_chat_id(outbox):
    """chat_id filter is AND-combined with other filters."""
    await outbox.enqueue(_make_job(chat_id="c1", idem="k1"))
    await outbox.enqueue(_make_job(chat_id="c2", idem="k2"))

    jobs = await outbox.list_jobs(chat_id="c1")
    assert len(jobs) == 1
    assert jobs[0].chat_id == "c1"


@pytest.mark.asyncio
async def test_list_jobs_blank_chat_id_means_no_filter(outbox):
    """Whitespace-only chat_id is treated as None (no filter)."""
    await outbox.enqueue(_make_job(chat_id="c1", idem="k1"))
    await outbox.enqueue(_make_job(chat_id="c2", idem="k2"))

    jobs = await outbox.list_jobs(chat_id="   ")
    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_list_jobs_filters_by_trigger_type(outbox):
    """trigger_type filter works with enum and string forms."""
    await outbox.enqueue(_make_job(
        chat_id="c1", idem="k1", trigger=ExtractionTrigger.MANUAL,
    ))
    await outbox.enqueue(_make_job(
        chat_id="c2", idem="k2", trigger=ExtractionTrigger.AFTER_TURN,
    ))

    manual = await outbox.list_jobs(
        trigger_type=ExtractionTrigger.MANUAL
    )
    after_turn = await outbox.list_jobs(
        trigger_type="after_turn"
    )

    assert len(manual) == 1
    assert len(after_turn) == 1


@pytest.mark.asyncio
async def test_list_jobs_pagination(outbox):
    """limit/offset partition the result set with no overlap."""
    for i in range(5):
        await outbox.enqueue(_make_job(chat_id=f"c{i}", idem=f"k{i}"))

    page1 = await outbox.list_jobs(limit=2, offset=0)
    page2 = await outbox.list_jobs(limit=2, offset=2)
    page3 = await outbox.list_jobs(limit=2, offset=4)

    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1

    ids = {j.job_id for j in page1} | {j.job_id for j in page2} | {
        j.job_id for j in page3
    }
    assert len(ids) == 5  # no duplicates across pages


@pytest.mark.asyncio
async def test_list_jobs_clamps_limit(outbox):
    """limit is clamped to [1, 200] regardless of input."""
    for i in range(3):
        await outbox.enqueue(_make_job(chat_id=f"c{i}", idem=f"k{i}"))

    # limit=0 -> clamped to 1.
    jobs = await outbox.list_jobs(limit=0)
    assert len(jobs) == 1

    # limit=10000 -> clamped to 200 (result set only has 3 rows).
    jobs = await outbox.list_jobs(limit=10_000)
    assert len(jobs) == 3


@pytest.mark.asyncio
async def test_count_jobs_matches_list_jobs(outbox):
    """count_jobs() and len(list_jobs()) must agree on every filter."""
    await outbox.enqueue(_make_job(chat_id="c1", idem="k1"))
    await outbox.enqueue(_make_job(chat_id="c2", idem="k2"))
    await outbox.enqueue(_make_job(chat_id="c1", idem="k3"))

    # Unfiltered.
    total_count = await outbox.count_jobs()
    total_list = await outbox.list_jobs(limit=200)
    assert total_count == len(total_list) == 3

    # Filtered by chat_id.
    c1_count = await outbox.count_jobs(chat_id="c1")
    c1_list = await outbox.list_jobs(chat_id="c1", limit=200)
    assert c1_count == len(c1_list) == 2

    # Filtered by status.
    pending_count = await outbox.count_jobs(
        status=MemoryJobStatus.PENDING
    )
    pending_list = await outbox.list_jobs(
        status=MemoryJobStatus.PENDING, limit=200
    )
    assert pending_count == len(pending_list) == 3


# ---------------------------------------------------------------------------
# 3. requeue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_failed_job_resets_bookkeeping(outbox):
    """failed -> pending, with all bookkeeping fields cleared."""
    job = _make_job()
    await outbox.enqueue(job)
    await outbox.fail(job.job_id, "ERR_CODE", "boom")

    pre = await outbox.get_by_id(job.job_id)
    assert pre.status == MemoryJobStatus.FAILED
    assert pre.attempts == 1
    assert pre.error_code == "ERR_CODE"
    assert pre.completed_at is not None

    requeued = await outbox.requeue(job.job_id)
    assert requeued.status == MemoryJobStatus.PENDING
    assert requeued.attempts == 0
    assert requeued.error_code is None
    assert requeued.error_message is None
    assert requeued.next_retry_at is None
    assert requeued.started_at is None
    assert requeued.completed_at is None
    assert requeued.heartbeat_at is None
    assert requeued.lease_owner is None
    assert requeued.lease_expires_at is None

    # Idempotency + creation metadata preserved.
    assert requeued.idempotency_key == job.idempotency_key
    assert requeued.created_at == job.created_at


@pytest.mark.asyncio
async def test_requeue_cancelled_job(outbox):
    """cancelled -> pending with the same reset behavior."""
    job = _make_job()
    await outbox.enqueue(job)
    await outbox.cancel(job.job_id)

    requeued = await outbox.requeue(job.job_id)
    assert requeued.status == MemoryJobStatus.PENDING
    assert requeued.attempts == 0


@pytest.mark.asyncio
async def test_requeue_rejects_pending_job(outbox):
    """A pending job is not in a terminal state; requeue must refuse."""
    job = _make_job()
    await outbox.enqueue(job)

    with pytest.raises(ValueError, match="only 'failed' and 'cancelled'"):
        await outbox.requeue(job.job_id)


@pytest.mark.asyncio
async def test_requeue_rejects_completed_job(outbox):
    """A completed job must not be silently re-executed (would double-extract)."""
    job = _make_job()
    await outbox.enqueue(job)
    await outbox.complete(job.job_id, JobResult())

    with pytest.raises(ValueError, match="only 'failed' and 'cancelled'"):
        await outbox.requeue(job.job_id)


@pytest.mark.asyncio
async def test_requeue_unknown_job_raises(outbox):
    """Unknown job_id -> JobNotFoundError (not ValueError)."""
    with pytest.raises(JobNotFoundError):
        await outbox.requeue("mjob_does_not_exist")


@pytest.mark.asyncio
async def test_requeue_preserves_snapshot_hash(outbox):
    """Snapshot hash is preserved so worker can detect session drift."""
    job = _make_job(snapshot="cafebabe" * 8)
    await outbox.enqueue(job)
    await outbox.fail(job.job_id, "ERR", "x")

    requeued = await outbox.requeue(job.job_id)
    assert requeued.message_snapshot_hash == job.message_snapshot_hash


# ---------------------------------------------------------------------------
# 4. Migration v3 verification
# ---------------------------------------------------------------------------


def test_migration_v3_is_current_version():
    """The module advertises v3 as the current schema version."""
    assert CURRENT_VERSION == 3


def test_migration_v3_created_batch_table(temp_db_path):
    """``memory_batches`` table exists after apply_schema."""
    conn = sqlite3.connect(str(temp_db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='memory_batches'"
        ).fetchone()
        assert row is not None, "memory_batches table was not created"
    finally:
        conn.close()


def test_migration_v3_added_batch_id_column(temp_db_path):
    """``memory_jobs.batch_id`` column exists after apply_schema."""
    conn = sqlite3.connect(str(temp_db_path))
    try:
        cols = conn.execute("PRAGMA table_info(memory_jobs)").fetchall()
        col_names = [c[1] for c in cols]
        assert "batch_id" in col_names, (
            f"batch_id missing from memory_jobs; columns: {col_names}"
        )
    finally:
        conn.close()


def test_migration_v3_recorded_version(temp_db_path):
    """The highest recorded version in ``memory_schema_version`` is 3."""
    conn = sqlite3.connect(str(temp_db_path))
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM memory_schema_version"
        ).fetchone()[0]
        assert version == 3
    finally:
        conn.close()


def test_migration_v3_batch_index_exists(temp_db_path):
    """The partial index on memory_jobs(batch_id, status) is created."""
    conn = sqlite3.connect(str(temp_db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_memory_jobs_batch'"
        ).fetchone()
        assert row is not None, "idx_memory_jobs_batch was not created"
    finally:
        conn.close()


def test_migration_v3_batch_table_has_expected_columns(temp_db_path):
    """Sanity-check that the batch table has the fields the API will use."""
    conn = sqlite3.connect(str(temp_db_path))
    try:
        cols = conn.execute("PRAGMA table_info(memory_batches)").fetchall()
        col_names = {c[1] for c in cols}
        # These are the columns the batch API (M6) will read/write.
        expected = {
            "batch_id",
            "status",
            "triggered_by",
            "project_id",
            "session_count",
            "job_count",
            "completed_jobs",
            "failed_jobs",
            "cancelled_jobs",
            "total_candidates",
            "total_memories_created",
            "estimated_cost_nano_usd",
            "actual_cost_nano_usd",
            "created_at",
            "started_at",
            "completed_at",
        }
        missing = expected - col_names
        assert not missing, (
            f"memory_batches missing columns: {sorted(missing)}"
        )
    finally:
        conn.close()
