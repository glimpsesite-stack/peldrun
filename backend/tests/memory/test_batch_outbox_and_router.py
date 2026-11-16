# backend/tests/memory/test_batch_outbox_and_router.py
"""
Phase 3 - M6a tests: outbox batch CRUD + trigger router batch_id.

Covers:

  1. ``MemoryOutbox.cancel_with_code`` -- allowed_statuses gate
     and error-code persistence.
  2. ``MemoryOutbox`` batch CRUD -- create / get / update /
     list / count / aggregate.
  3. ``TriggerRouter.route_batch(batch_id=...)`` -- the id is
     stamped on every enqueued job AND appears in the
     idempotency key.

The router tests use a mocked scanner (no filesystem) and a
patched snapshot function (no I/O), so they exercise the
routing logic in isolation.

Run with::

    pytest backend/tests/memory/test_batch_outbox_and_router.py -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import pytest_asyncio

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)

from omweb.memory.batch.models import (
    MemoryBatch,
    MemoryBatchStatus,
)
from omweb.memory.extraction.session_scanner import (
    SessionCandidate,
)
from omweb.memory.extraction.trigger_router import (
    ExtractionModelConfig,
    TriggerRouter,
)
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
)
from omweb.memory.jobs.outbox import (
    JobNotFoundError,
    MemoryOutbox,
)
from omweb.memory.storage.migrations import apply_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
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
    ob = MemoryOutbox(temp_db_path)
    await ob.open()
    try:
        yield ob
    finally:
        await ob.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    chat_id: str = "chat_1",
    idem: str = "k1",
    start: int = 0,
    end: int = 1,
    batch_id: Optional[str] = None,
) -> MemoryJob:
    return MemoryJob(
        chat_id=chat_id,
        scope_type=MemoryScope.CHAT,
        scope_id=chat_id,
        trigger_type=ExtractionTrigger.BATCH
        if batch_id else ExtractionTrigger.MANUAL,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash="deadbeef" * 8,
        model_provider_id="lmstudio",
        model_id="test-model",
        idempotency_key=idem,
        batch_id=batch_id,
    )


def _make_batch(
    *,
    batch_id: str = "batch_test001",
    status: MemoryBatchStatus = MemoryBatchStatus.PENDING,
    project_id: Optional[str] = None,
    session_count: int = 0,
    job_count: int = 0,
) -> MemoryBatch:
    return MemoryBatch(
        batch_id=batch_id,
        status=status,
        project_id=project_id,
        session_count=session_count,
        job_count=job_count,
        triggered_by="test",
    )


# ===========================================================================
# 1. cancel_with_code
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_with_code_on_pending_succeeds(outbox):
    """Default allowed_statuses includes 'pending'."""
    job = _make_job()
    await outbox.enqueue(job)

    cancelled = await outbox.cancel_with_code(
        job.job_id,
        error_code="BATCH_PAUSED",
        error_message="paused by user",
    )
    assert cancelled.status == MemoryJobStatus.CANCELLED
    assert cancelled.error_code == "BATCH_PAUSED"
    assert cancelled.error_message == "paused by user"


@pytest.mark.asyncio
async def test_cancel_with_code_on_running_succeeds(outbox):
    """Running jobs are also cancellable by default."""
    job = _make_job()
    await outbox.enqueue(job)
    claimed = await outbox.claim_one("worker_1")
    assert claimed is not None
    assert claimed.status == MemoryJobStatus.RUNNING

    cancelled = await outbox.cancel_with_code(
        job.job_id, error_code="BATCH_CANCELLED",
    )
    assert cancelled.status == MemoryJobStatus.CANCELLED
    assert cancelled.error_code == "BATCH_CANCELLED"


@pytest.mark.asyncio
async def test_cancel_with_code_rejects_running_when_not_allowed(outbox):
    """Pause uses allowed=('pending','retry_pending'); running refused."""
    job = _make_job()
    await outbox.enqueue(job)
    await outbox.claim_one("worker_1")

    with pytest.raises(ValueError, match="running"):
        await outbox.cancel_with_code(
            job.job_id,
            error_code="BATCH_PAUSED",
            allowed_statuses=("pending", "retry_pending"),
        )


@pytest.mark.asyncio
async def test_cancel_with_code_rejects_terminal_status(outbox):
    """Completed / failed jobs must not be cancelled."""
    job = _make_job()
    await outbox.enqueue(job)
    await outbox.fail(job.job_id, "ERR", "x")

    with pytest.raises(ValueError, match="failed"):
        await outbox.cancel_with_code(
            job.job_id, error_code="BATCH_PAUSED",
        )


@pytest.mark.asyncio
async def test_cancel_with_code_unknown_job_raises(outbox):
    with pytest.raises(JobNotFoundError):
        await outbox.cancel_with_code(
            "mjob_does_not_exist", error_code="X",
        )


@pytest.mark.asyncio
async def test_cancel_with_code_rejects_empty_allowed(outbox):
    job = _make_job()
    await outbox.enqueue(job)
    with pytest.raises(ValueError, match="allowed_statuses"):
        await outbox.cancel_with_code(
            job.job_id,
            error_code="X",
            allowed_statuses=(),
        )


# ===========================================================================
# 2. Batch CRUD
# ===========================================================================


@pytest.mark.asyncio
async def test_create_batch_persists_row(outbox):
    batch = _make_batch(batch_id="batch_abc", session_count=3)
    created = await outbox.create_batch(batch)
    assert created.batch_id == "batch_abc"

    loaded = await outbox.get_batch("batch_abc")
    assert loaded is not None
    assert loaded.batch_id == "batch_abc"
    assert loaded.session_count == 3
    assert loaded.status == MemoryBatchStatus.PENDING


@pytest.mark.asyncio
async def test_get_batch_unknown_returns_none(outbox):
    loaded = await outbox.get_batch("batch_does_not_exist")
    assert loaded is None


@pytest.mark.asyncio
async def test_update_batch_changes_fields(outbox):
    batch = _make_batch(batch_id="batch_upd")
    await outbox.create_batch(batch)

    updated = await outbox.update_batch(
        "batch_upd",
        status="running",
        started_at=12345.0,
        job_count=5,
    )
    assert updated.status == MemoryBatchStatus.RUNNING
    assert updated.started_at == 12345.0
    assert updated.job_count == 5


@pytest.mark.asyncio
async def test_update_batch_rejects_unknown_field(outbox):
    batch = _make_batch()
    await outbox.create_batch(batch)

    with pytest.raises(ValueError, match="unknown batch field"):
        await outbox.update_batch(
            batch.batch_id, nonexistent_field=1,
        )


@pytest.mark.asyncio
async def test_update_batch_rejects_batch_id_change(outbox):
    batch = _make_batch(batch_id="batch_immut")
    await outbox.create_batch(batch)

    with pytest.raises(ValueError, match="batch_id is immutable"):
        await outbox.update_batch(
            "batch_immut", batch_id="new_id",
        )


@pytest.mark.asyncio
async def test_update_batch_serializes_json_fields(outbox):
    batch = _make_batch()
    await outbox.create_batch(batch)

    await outbox.update_batch(
        batch.batch_id,
        chat_ids=["c1", "c2", "c3"],
        options={"foo": "bar", "n": 42},
    )
    reloaded = await outbox.get_batch(batch.batch_id)
    assert reloaded is not None
    assert reloaded.chat_ids == ["c1", "c2", "c3"]
    assert reloaded.options == {"foo": "bar", "n": 42}


@pytest.mark.asyncio
async def test_update_batch_unknown_id_raises(outbox):
    with pytest.raises(JobNotFoundError):
        await outbox.update_batch(
            "batch_unknown", status="running",
        )


@pytest.mark.asyncio
async def test_list_batches_returns_newest_first(outbox):
    for i in range(3):
        await outbox.create_batch(
            _make_batch(batch_id=f"batch_{i}")
        )

    batches = await outbox.list_batches()
    assert len(batches) == 3
    # created_at DESC -- timestamps from successive
    # _make_batch calls are monotonically non-decreasing.
    for prev, curr in zip(batches, batches[1:]):
        assert prev.created_at >= curr.created_at


@pytest.mark.asyncio
async def test_list_batches_filters_by_status(outbox):
    await outbox.create_batch(_make_batch(batch_id="b1"))
    b2 = _make_batch(batch_id="b2")
    await outbox.create_batch(b2)
    await outbox.update_batch("b2", status="completed")

    pending = await outbox.list_batches(
        status=MemoryBatchStatus.PENDING
    )
    completed = await outbox.list_batches(
        status="completed"
    )
    assert len(pending) == 1
    assert len(completed) == 1
    assert pending[0].batch_id == "b1"
    assert completed[0].batch_id == "b2"


@pytest.mark.asyncio
async def test_count_batches_matches_list(outbox):
    await outbox.create_batch(_make_batch(batch_id="b1"))
    await outbox.create_batch(_make_batch(batch_id="b2"))

    total = await outbox.count_batches()
    listed = await outbox.list_batches(limit=200)
    assert total == len(listed) == 2


# ===========================================================================
# 3. aggregate_batch_progress
# ===========================================================================


@pytest.mark.asyncio
async def test_aggregate_unknown_batch_returns_none(outbox):
    agg = await outbox.aggregate_batch_progress("batch_nope")
    assert agg is None


@pytest.mark.asyncio
async def test_aggregate_empty_batch(outbox):
    """A batch with no jobs yet -> all zeros, not None."""
    await outbox.create_batch(_make_batch(batch_id="batch_empty"))
    agg = await outbox.aggregate_batch_progress("batch_empty")
    assert agg is not None
    assert agg["total"] == 0
    assert agg["completed"] == 0
    assert agg["pending_work"] == 0
    assert agg["actual_cost_nano_usd"] == 0


@pytest.mark.asyncio
async def test_aggregate_mixed_statuses(outbox):
    """Two completed, one pending, one failed -> accurate counts."""
    await outbox.create_batch(_make_batch(batch_id="batch_agg"))

    # 2 completed
    for i in range(2):
        job = _make_job(
            chat_id=f"c{i}", idem=f"k_done_{i}",
            batch_id="batch_agg",
        )
        await outbox.enqueue(job)
        await outbox.complete(job.job_id, _job_result(5, 3, 100))
    # 1 pending
    job_p = _make_job(
        chat_id="c_p", idem="k_pending",
        batch_id="batch_agg",
    )
    await outbox.enqueue(job_p)
    # 1 failed
    job_f = _make_job(
        chat_id="c_f", idem="k_failed",
        batch_id="batch_agg",
    )
    await outbox.enqueue(job_f)
    await outbox.fail(job_f.job_id, "ERR", "boom")

    agg = await outbox.aggregate_batch_progress("batch_agg")
    assert agg is not None
    assert agg["total"] == 4
    assert agg["by_status"]["completed"] == 2
    assert agg["by_status"]["pending"] == 1
    assert agg["by_status"]["failed"] == 1
    assert agg["completed"] == 2
    assert agg["failed"] == 1
    assert agg["pending_work"] == 1
    # 2 completed jobs each with candidates=5, created=3, cost=100
    assert agg["total_candidates"] == 10
    assert agg["total_memories_created"] == 6
    assert agg["actual_cost_nano_usd"] == 200


# ===========================================================================
# 4. TriggerRouter.route_batch + batch_id
# ===========================================================================


class _FakeScanner:
    """In-memory scanner -- no filesystem, no project_manager."""

    def __init__(self, candidates: List[SessionCandidate]) -> None:
        self._candidates = list(candidates)

    async def scan_all_sessions(
        self, *, only_unprocessed: bool = True,
    ) -> List[SessionCandidate]:
        if only_unprocessed:
            return [c for c in self._candidates if c.pending_turns > 0]
        return list(self._candidates)

    async def get_candidate(
        self, chat_id: str
    ) -> Optional[SessionCandidate]:
        for c in self._candidates:
            if c.chat_id == chat_id:
                return c
        return None


def _make_candidate(
    chat_id: str,
    *,
    total_turns: int = 5,
    last_extracted: int = -1,
    project_id: Optional[str] = None,
) -> SessionCandidate:
    pending = (
        total_turns if last_extracted < 0
        else max(0, total_turns - last_extracted - 1)
    )
    return SessionCandidate(
        chat_id=chat_id,
        project_id=project_id,
        title=f"Chat {chat_id}",
        status="completed",
        total_turns=total_turns,
        last_extracted_turn_index=last_extracted,
        pending_turns=pending,
        source_session_path=f"/tmp/{chat_id}/session.json",
        updated_at=time.time(),
        has_tool_calls=False,
        recommended_evidence_source="session_json",
    )


def _model_config_provider() -> ExtractionModelConfig:
    return ExtractionModelConfig(
        provider_id="lmstudio", model_id="test-model",
    )


@pytest.fixture
def patch_snapshot(monkeypatch):
    """Patch the async snapshot helper so no filesystem is touched."""
    async def _fake(session_path: str) -> Optional[str]:
        # Deterministic pseudo-hash per path.
        return f"fake_{hash(session_path) & 0xFFFFFFFF:08x}"

    monkeypatch.setattr(
        "omweb.memory.extraction.trigger_router."
        "compute_snapshot_from_file_async",
        _fake,
    )
    return _fake


@pytest.mark.asyncio
async def test_route_batch_without_batch_id_leaves_jobs_unlinked(
    outbox, patch_snapshot,
):
    """Backward compatibility: no batch_id -> jobs carry batch_id=None."""
    scanner = _FakeScanner([_make_candidate("c1")])
    router = TriggerRouter(
        scanner, outbox,
        model_config_provider=_model_config_provider,
        after_turn_batching=True,
    )

    jobs = await router.route_batch()
    assert len(jobs) == 1
    assert jobs[0].batch_id is None


@pytest.mark.asyncio
async def test_route_batch_with_batch_id_stamps_jobs(
    outbox, patch_snapshot,
):
    """Every enqueued job carries the supplied batch_id."""
    scanner = _FakeScanner([
        _make_candidate("c1"),
        _make_candidate("c2"),
    ])
    router = TriggerRouter(
        scanner, outbox,
        model_config_provider=_model_config_provider,
        after_turn_batching=True,
    )

    jobs = await router.route_batch(batch_id="batch_zzz")
    assert len(jobs) == 2
    for job in jobs:
        assert job.batch_id == "batch_zzz"

    # Verify persistence.
    reloaded = await outbox.get_by_id(jobs[0].job_id)
    assert reloaded is not None
    assert reloaded.batch_id == "batch_zzz"


@pytest.mark.asyncio
async def test_route_batch_idempotency_differs_by_batch_id(
    outbox, patch_snapshot,
):
    """Two batches on the same sessions produce distinct keys."""
    scanner = _FakeScanner([_make_candidate("c1")])
    router = TriggerRouter(
        scanner, outbox,
        model_config_provider=_model_config_provider,
        after_turn_batching=True,
    )

    jobs_a = await router.route_batch(batch_id="batch_A")
    jobs_b = await router.route_batch(batch_id="batch_B")

    assert len(jobs_a) == 1
    assert len(jobs_b) == 1
    # Distinct jobs, distinct keys.
    assert jobs_a[0].job_id != jobs_b[0].job_id
    assert (
        jobs_a[0].idempotency_key
        != jobs_b[0].idempotency_key
    )


@pytest.mark.asyncio
async def test_route_batch_same_batch_id_is_idempotent(
    outbox, patch_snapshot,
):
    """Two calls with the SAME batch_id dedupe (UNIQUE constraint)."""
    scanner = _FakeScanner([_make_candidate("c1")])
    router = TriggerRouter(
        scanner, outbox,
        model_config_provider=_model_config_provider,
        after_turn_batching=True,
    )

    jobs_a = await router.route_batch(batch_id="batch_dup")
    # Same call again -- same key -> DuplicateJobError swallowed ->
    # empty list.
    jobs_b = await router.route_batch(batch_id="batch_dup")

    assert len(jobs_a) == 1
    assert jobs_b == []


# ---------------------------------------------------------------------------
# Helper used by aggregate test
# ---------------------------------------------------------------------------


def _job_result(candidates: int, created: int, cost: int):
    from omweb.memory.jobs.outbox import JobResult
    return JobResult(
        candidates_count=candidates,
        created_memories_count=created,
        cost_nano_usd=cost,
        decisions=[],
    )
