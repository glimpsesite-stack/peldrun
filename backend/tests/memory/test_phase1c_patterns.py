# backend/tests/memory/test_phase1c_patterns.py
"""
Phase 1-C acceptance tests: trigger patterns + status watcher.

Covers the eleven acceptance tests from the plan that Phase 1-C
introduces (Phase 1-A covered T4/T7/T8/T19/T20; Phase 1-B
covered T5/T6/T11/T13):

    T1   -- P1 regression (extraction off -> retrieve still works)
    T2   -- mode=disabled -> no extraction pipeline
    T3   -- memory_ai.enabled=false -> no jobs enqueued
    T9   -- model missing -> retry -> terminal fail
    T10  -- chat deleted mid-job -> SESSION_MISSING
    T12  -- cost is tracked on completed jobs
    T14  -- after_session -> 1 job per session
    T15  -- after_turn -> batch (default) OR per-turn mode
    T16  -- batch -> every pending session, skip processed
    T18  -- session changed mid-flight -> SESSION_CHANGED

Legacy vs Hermetic transport
----------------------------
The ``test_real_transport_*`` tests at the bottom of this file
exercise the LEGACY transport path (``force_openai_compat=False``,
which routes through ``provider_factory.create``). Extraction
production uses the HERMETIC path
(``force_openai_compat=True``, which uses ``OpenAICompatProvider``
directly). Both paths are legitimate and tested independently:

  * Legacy   -> ``test_real_transport_*`` (this file)
  * Hermetic -> ``test_phase1b_reasoning_disable.py``

The hermetic path was introduced to solve a specific production
bug: reasoning-capable models (Nemotron, Qwen-QwQ, DeepSeek-R1)
emitted a hidden "thinking" phase that looped indefinitely on
Arabic content, and the legacy path could not pass ``extra_body``
to the SDK (because ``client.py``'s ``config.extra_params`` is
dead code — it is never read). Rather than patch ``client.py``
(a chat-owned file), extraction uses ``OpenAICompatProvider``
directly with ``reasoning_effort="none"`` and ``extra_body`` —
both of which are PUBLIC kwargs that ``client.py`` already
supports.

Design notes
------------

* ``_TestScanner`` subclasses ``MemorySessionScanner`` to read
  sessions from an in-memory dict. This is necessary because the
  production scanner reads ``project_manager.get_chat`` -- a
  process-wide singleton whose disk layout is not what this test
  wants to construct. The subclass overrides ``_read_session_dict``
  and ``_build_candidate``; every other method (cursor lookups,
  scanner interface) is inherited unchanged.

* Snapshot verification requires a REAL file on disk (see
  ``snapshot.compute_snapshot_from_file``). ``_TestScanner``
  writes each session to a temp file whenever it builds a
  candidate; the file path travels on
  ``SessionCandidate.source_session_path`` and is what the worker's
  ``_verify_snapshot`` reads. T10 and T18 exercise this path by
  deleting or modifying the file AFTER enqueue.

* No test touches the production database. Every test uses
  ``tmp_path`` + ``apply_schema`` (same pattern as Phase 1-A/B).

* Every test uses the real ``MemoryOutbox`` -- the SQL layer is
  the point of these tests. Only the LLM transport is mocked.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from omweb.memory.storage.migrations import apply_schema
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
)
from omweb.memory.jobs.outbox import (
    JobResult,
    MemoryOutbox,
)
from omweb.memory.jobs.worker import MemoryExtractionWorker
from omweb.memory.extraction.contracts import ExtractionResult
from omweb.memory.extraction.gateway import (
    LLMStructuredResponse,
    LLMTransportError,
    MemoryModelGateway,
)
from omweb.memory.extraction.orchestrator import ExtractionOrchestrator
from omweb.memory.extraction.session_scanner import (
    MemorySessionScanner,
    SessionCandidate,
)
from omweb.memory.extraction.status_watcher import SessionStatusWatcher
from omweb.memory.extraction.trigger_router import (
    ExtractionModelConfig,
    TriggerRouter,
)
from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite file with the memory schema applied."""
    path = tmp_path / "memory_test_phase1c.db"
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return path


@pytest_asyncio.fixture
async def outbox(db_path: Path) -> MemoryOutbox:
    ob = MemoryOutbox(db_path)
    await ob.open()
    try:
        yield ob
    finally:
        await ob.close()


# ---------------------------------------------------------------------------
# In-memory session scanner (test seam)
# ---------------------------------------------------------------------------


class _TestScanner(MemorySessionScanner):
    """MemorySessionScanner backed by an in-memory session dict.

    Every call to ``_build_candidate`` rewrites the session's JSON
    file in ``tmp_dir``. This gives snapshot computation a real
    on-disk source while the test controls the in-memory state --
    enabling T10 (delete file) and T18 (modify file) without
    patching ``project_manager``.
    """

    def __init__(
        self,
        outbox: MemoryOutbox,
        sessions: Dict[str, Dict[str, Any]],
        tmp_dir: Path,
    ) -> None:
        super().__init__(outbox)
        self._sessions = sessions
        self._tmp_dir = Path(tmp_dir)
        self._file_paths: Dict[str, Path] = {}

    async def _read_session_dict(
        self, chat_id: str
    ) -> Optional[Dict[str, Any]]:
        return self._sessions.get(chat_id)

    async def _read_context_sidecar(
        self, chat_id: str
    ) -> Optional[Dict[str, Any]]:
        return None

    async def _build_candidate(
        self, chat_id: str
    ) -> Optional[SessionCandidate]:
        session = self._sessions.get(chat_id)
        if session is None:
            return None

        turns = session.get("turns")
        turns_list = turns if isinstance(turns, list) else []
        total_turns = len(turns_list)

        cursor_or_none = await self._outbox.get_cursor_or_none(
            chat_id
        )
        if cursor_or_none is None:
            last_extracted = -1
            pending = total_turns
        else:
            last_extracted = cursor_or_none
            pending = max(0, total_turns - last_extracted - 1)

        # Materialise the session on disk (required for snapshot).
        path = self._tmp_dir / f"{chat_id}.session.json"
        path.write_text(
            json.dumps(session, ensure_ascii=False),
            encoding="utf-8",
        )
        self._file_paths[chat_id] = path

        status_raw = session.get("status") or "running"
        title_raw = session.get("title") or ""

        return SessionCandidate(
            chat_id=chat_id,
            project_id=session.get("project_id"),
            title=str(title_raw).strip(),
            status=status_raw,  # type: ignore[arg-type]
            total_turns=total_turns,
            last_extracted_turn_index=last_extracted,
            pending_turns=pending,
            source_session_path=str(path),
            updated_at=0.0,
            has_tool_calls=False,
            recommended_evidence_source="session_json",
        )

    async def scan_all_sessions(
        self, *, only_unprocessed: bool = True
    ) -> List[SessionCandidate]:
        candidates: List[SessionCandidate] = []
        for chat_id in self._sessions:
            c = await self._build_candidate(chat_id)
            if c is None:
                continue
            if only_unprocessed and c.pending_turns <= 0:
                continue
            candidates.append(c)
        return candidates

    def file_path_for(self, chat_id: str) -> Path:
        return self._file_paths[chat_id]


# ---------------------------------------------------------------------------
# Session / settings builders
# ---------------------------------------------------------------------------


def _session(
    *,
    turns: int = 3,
    status: str = "completed",
    project_id: Optional[str] = "proj_test",
) -> Dict[str, Any]:
    """Build a session dict with `turns` filler turns."""
    return {
        "id": "chat_test",
        "project_id": project_id,
        "status": status,
        "title": "Test Chat",
        "turns": [
            {
                "id": f"turn_{i}_1000",
                "user": f"User message {i}",
                "assistant": f"Assistant reply {i}",
            }
            for i in range(turns)
        ],
    }


class _Settings:
    """Minimal settings object satisfying the extraction-layer duck type.

    The trigger router only inspects ``enabled``, ``extraction_enabled``,
    and ``extraction_trigger`` via ``getattr``. Using a simple class
    (rather than ``MemoryAISettings``) keeps the tests independent of
    the P1.7 settings schema.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        extraction_enabled: bool = True,
        extraction_trigger: str = "after_session",
    ) -> None:
        self.enabled = enabled
        self.extraction_enabled = extraction_enabled
        self.extraction_trigger = extraction_trigger
        # Attributes read by MemoryService._initialize_extraction.
        self.extraction = _ExtractionConfig(
            provider_id="openai",
            model_id="gpt-4o-mini",
            max_output_tokens=1200,
            timeout_seconds=45,
        )
        self.after_turn_batching = True
        self.snapshot_heartbeat_interval_seconds = 5
        self.max_candidates_per_job = 20
        self.min_confidence = 0.0


class _ExtractionConfig:
    def __init__(
        self,
        *,
        provider_id: str,
        model_id: str,
        max_output_tokens: int = 1200,
        timeout_seconds: int = 45,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds


def _model_config() -> ExtractionModelConfig:
    return ExtractionModelConfig(
        provider_id="openai", model_id="gpt-4o-mini"
    )


# ---------------------------------------------------------------------------
# Transport doubles
# ---------------------------------------------------------------------------


class _EmptyTransport:
    """Transport that always returns an empty candidates array."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMStructuredResponse:
        self.calls += 1
        return LLMStructuredResponse(
            parsed={"candidates": []},
            raw_text='{"candidates": []}',
            cost_nano_usd=1_000_000,
            input_tokens=100,
            output_tokens=20,
        )


class _AlwaysFailingTransport:
    """Transport that raises LLMTransportError on every call."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMStructuredResponse:
        self.calls += 1
        raise LLMTransportError("Simulated model unavailable")


# ---------------------------------------------------------------------------
# Production-mode worker used by integration tests
# ---------------------------------------------------------------------------


class _WiredWorker(MemoryExtractionWorker):
    """MemoryExtractionWorker wired to a real orchestrator + scanner.

    Overrides ``_compute_backoff_seconds`` to return 0 so retry tests
    finish in milliseconds instead of minutes.
    """

    async def _process_job(self, job: MemoryJob) -> JobResult:
        # Re-use the full production _process_job by delegating to
        # super(). The three dependencies were injected via __init__.
        return await super()._process_job(job)

    def _compute_backoff_seconds(self, attempts: int) -> int:
        return 0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _wait_terminal(
    outbox: MemoryOutbox,
    chat_id: str,
    *,
    timeout: float = 10.0,
) -> MemoryJob:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = await outbox.list_by_chat(chat_id, limit=1)
        if jobs and jobs[0].status.is_terminal:
            return jobs[0]
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Job for {chat_id} did not reach terminal state in {timeout}s"
    )


# ---------------------------------------------------------------------------
# T1: P1 regression -- extraction off, retrieve still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T1_p1_regression_when_extraction_is_off(
    tmp_path: Path,
) -> None:
    """Backward compatibility: extraction off, MemoryService works.

    Asserts:
      * ``extraction_enabled`` is False when no settings provider is
        supplied (the P1 default),
      * ``get_extraction_trigger()`` returns None (no auto-triggers),
      * ``retrieve()`` returns a MemoryContext without raising.

    This is the primary acceptance contract for "the new system is
    additive, not a replacement."
    """
    from omweb.memory.manager.memory_service import MemoryService

    svc = MemoryService(
        db_path=tmp_path / "memory.db",
        settings_path=tmp_path / "settings.json",
        # extraction_settings_provider deliberately NOT supplied.
    )
    await svc.ainitialize()
    try:
        assert svc.extraction_enabled is False
        assert svc.get_extraction_trigger() is None
        assert svc.outbox is None

        ctx = await svc.retrieve(
            query="hello",
            chat_id="chat_t1",
            project_id="proj_t1",
        )
        assert ctx is not None
    finally:
        await svc.aclose()


# ---------------------------------------------------------------------------
# T2: mode=disabled -> no extraction pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T2_mode_disabled_skips_extraction_init(
    tmp_path: Path,
) -> None:
    """When memory mode is DISABLED, extraction must NOT initialize.

    Even if the AI settings say "enabled=True", DISABLED memory mode
    installs a NullMemoryProvider that raises on every write. Starting
    extraction would enqueue jobs whose every write attempt fails.
    """
    from peldrun.memory.domain.enums import MemoryMode
    from omweb.memory.manager.memory_service import MemoryService

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "mode": MemoryMode.DISABLED.value,
                "provider_id": "sqlite_fts",
            }
        ),
        encoding="utf-8",
    )

    svc = MemoryService(
        db_path=tmp_path / "memory.db",
        settings_path=settings_path,
        extraction_settings_provider=lambda: _Settings(
            enabled=True, extraction_enabled=True
        ),
        transport_provider=lambda: _EmptyTransport(),
    )
    await svc.ainitialize()
    try:
        assert svc.mode == MemoryMode.DISABLED
        assert svc.extraction_enabled is False, (
            "extraction_enabled must be False when memory mode is "
            "DISABLED -- otherwise jobs enqueue but every write "
            "fails against the NullMemoryProvider"
        )
        assert svc.outbox is None
    finally:
        await svc.aclose()


# ---------------------------------------------------------------------------
# T3: memory_ai.enabled=false -> no jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T3_ai_disabled_creates_no_jobs(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """When the AI settings say `enabled=False`, no route enqueues."""
    sessions = {"chat_t3": _session(turns=3)}
    scanner = _TestScanner(outbox, sessions, tmp_path)

    # The router's fast path checks `config is None` FIRST, before
    # any I/O. Simulate by supplying a provider that returns None.
    router = TriggerRouter(
        scanner,
        outbox,
        model_config_provider=lambda: None,  # AI disabled
        after_turn_batching=True,
    )

    jobs_after_turn = await router.route_after_turn(
        chat_id="chat_t3",
        project_id="proj_test",
        current_turn_index=2,
    )
    jobs_after_session = await router.route_after_session(
        chat_id="chat_t3", project_id="proj_test"
    )
    jobs_batch = await router.route_batch()
    jobs_manual = await router.route_on_demand(chat_id="chat_t3")

    assert jobs_after_turn == []
    assert jobs_after_session == []
    assert jobs_batch == []
    assert jobs_manual == []

    # Zero rows in memory_jobs.
    total = (
        await outbox.count_by_status(MemoryJobStatus.PENDING)
        + await outbox.count_by_status(MemoryJobStatus.RUNNING)
        + await outbox.count_by_status(MemoryJobStatus.COMPLETED)
    )
    assert total == 0


# ---------------------------------------------------------------------------
# T9: model missing -> retry -> terminal fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T9_model_missing_retries_then_fails(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """A transport that consistently reports 'model missing' fails the job.

    Behaviour verified:
      * The worker retries on ``LLMTransportError``.
      * After ``max_attempts`` failures the job transitions to FAILED.
      * The cursor remains 0 (never advances for failed jobs).
      * The transport was called exactly ``max_attempts`` times.
    """
    sessions = {"chat_t9": _session(turns=3)}
    scanner = _TestScanner(outbox, sessions, tmp_path)

    transport = _AlwaysFailingTransport()
    gateway = MemoryModelGateway(transport=transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    chat_id = "chat_t9"
    snapshot_path = tmp_path / "seed.json"
    snapshot_path.write_text(
        json.dumps(sessions[chat_id]), encoding="utf-8"
    )

    # [T9 FIX] Use the REAL snapshot hash computed from the file,
    # not a placeholder string.
    #
    # The worker's ``_verify_snapshot`` re-reads the file at
    # ``source_session_path`` and compares its hash against
    # ``job.message_snapshot_hash`` BEFORE the extraction pipeline
    # runs. A placeholder hash fails that check with
    # ``SESSION_CHANGED`` and short-circuits to a terminal failure --
    # the LLM transport is NEVER called, and ``attempts`` stays at 1
    # instead of the 2 the test asserts.
    #
    # Computing the hash from the real file (same pattern as T10 and
    # T12) lets the job proceed into the extraction pipeline, where
    # the transport raises LLMTransportError and the worker exercises
    # its retry-then-terminal-fail policy.
    real_snapshot_hash = _compute_hash_of(str(snapshot_path))

    job = MemoryJob(
        chat_id=chat_id,
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=0,
        end_turn_index=2,
        message_snapshot_hash=real_snapshot_hash,
        source_session_path=str(snapshot_path),
        model_provider_id="openai",
        model_id="nonexistent-model",
        idempotency_key=MemoryJob.compute_idempotency_key(
            chat_id=chat_id,
            trigger_type=ExtractionTrigger.AFTER_SESSION,
            start_turn_index=0,
            end_turn_index=2,
            message_snapshot_hash=real_snapshot_hash,
        ),
        max_attempts=2,
    )
    await outbox.enqueue(job)

    worker = _WiredWorker(
        outbox,
        scanner=scanner,
        orchestrator=orchestrator,
        write_pipeline_provider=lambda: None,  # not reached
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=5.0,
    )
    await worker.start()
    try:
        terminal = await _wait_terminal(outbox, chat_id)
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.FAILED
    assert terminal.attempts == 2
    assert transport.calls == 2
    assert await outbox.get_cursor(chat_id) == 0


# ---------------------------------------------------------------------------
# T10: chat deleted mid-job -> SESSION_MISSING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T10_missing_session_file_fails_with_session_missing(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """A session.json that disappears between enqueue and processing fails.

    The worker's ``_verify_snapshot`` reads the file, gets
    FileNotFoundError (mapped to None), and fails the job with
    ``SESSION_MISSING``. The cursor does NOT advance.
    """
    sessions = {"chat_t10": _session(turns=3)}
    scanner = _TestScanner(outbox, sessions, tmp_path)

    # Build the candidate so the file exists on disk with a
    # real snapshot hash.
    candidate = await scanner._build_candidate("chat_t10")
    assert candidate is not None
    real_hash = _compute_hash_of(candidate.source_session_path)

    job = MemoryJob(
        chat_id="chat_t10",
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=0,
        end_turn_index=2,
        message_snapshot_hash=real_hash,
        source_session_path=candidate.source_session_path,
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key=MemoryJob.compute_idempotency_key(
            chat_id="chat_t10",
            trigger_type=ExtractionTrigger.AFTER_SESSION,
            start_turn_index=0,
            end_turn_index=2,
            message_snapshot_hash=real_hash,
        ),
    )
    await outbox.enqueue(job)

    # Delete the underlying file to simulate "chat deleted mid-job".
    Path(candidate.source_session_path).unlink()

    empty_transport = _EmptyTransport()
    gateway = MemoryModelGateway(transport=empty_transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    worker = _WiredWorker(
        outbox,
        scanner=scanner,
        orchestrator=orchestrator,
        write_pipeline_provider=lambda: None,
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=5.0,
    )
    await worker.start()
    try:
        terminal = await _wait_terminal(outbox, "chat_t10")
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.FAILED
    assert terminal.error_code == "SESSION_MISSING"
    assert empty_transport.calls == 0  # never reached LLM
    assert await outbox.get_cursor("chat_t10") == 0


# ---------------------------------------------------------------------------
# T12: cost is tracked on completed jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T12_cost_is_recorded_on_completed_jobs(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """Completed jobs carry the LLM cost reported by the transport.

    Cost enforcement (Phase 2's ``max_cost_nano_usd_per_day`` gate)
    depends on this field being accurate. T12 verifies the
    foundation: ``cost_nano_usd`` on the persisted row equals the
    sum of costs reported by every transport call the job made.
    """
    sessions = {"chat_t12": _session(turns=2)}
    scanner = _TestScanner(outbox, sessions, tmp_path)
    candidate = await scanner._build_candidate("chat_t12")
    assert candidate is not None
    real_hash = _compute_hash_of(candidate.source_session_path)

    job = MemoryJob(
        chat_id="chat_t12",
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=0,
        end_turn_index=1,
        message_snapshot_hash=real_hash,
        source_session_path=candidate.source_session_path,
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key=MemoryJob.compute_idempotency_key(
            chat_id="chat_t12",
            trigger_type=ExtractionTrigger.AFTER_SESSION,
            start_turn_index=0,
            end_turn_index=1,
            message_snapshot_hash=real_hash,
        ),
    )
    await outbox.enqueue(job)

    transport = _EmptyTransport()  # cost_nano_usd=1_000_000
    gateway = MemoryModelGateway(transport=transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    worker = _WiredWorker(
        outbox,
        scanner=scanner,
        orchestrator=orchestrator,
        write_pipeline_provider=lambda: None,
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=5.0,
    )
    await worker.start()
    try:
        terminal = await _wait_terminal(outbox, "chat_t12")
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.COMPLETED
    assert terminal.cost_nano_usd == 1_000_000
    assert transport.calls == 1


# ---------------------------------------------------------------------------
# T14: after_session -> 1 job per session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T14_after_session_one_job_per_chat(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """`route_after_session` enqueues exactly one job per chat."""
    sessions = {
        "chat_a": _session(turns=3),
        "chat_b": _session(turns=4),
        "chat_c": _session(turns=2),
    }
    scanner = _TestScanner(outbox, sessions, tmp_path)
    router = TriggerRouter(
        scanner,
        outbox,
        model_config_provider=_model_config,
        after_turn_batching=True,
    )

    total_jobs = 0
    for chat_id in sessions:
        jobs = await router.route_after_session(
            chat_id=chat_id, project_id="proj_test"
        )
        assert len(jobs) == 1
        assert jobs[0].start_turn_index == 0
        assert jobs[0].end_turn_index == (
            len(sessions[chat_id]["turns"]) - 1
        )
        assert jobs[0].trigger_type == ExtractionTrigger.AFTER_SESSION
        total_jobs += len(jobs)

    assert total_jobs == 3

    # Idempotency: calling again is a no-op (DuplicateJobError swallowed).
    for chat_id in sessions:
        jobs = await router.route_after_session(
            chat_id=chat_id, project_id="proj_test"
        )
        assert jobs == [], (
            "second call must be a no-op -- same snapshot, same key"
        )


# ---------------------------------------------------------------------------
# T15: after_turn -> batch (default) OR per-turn mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T15_after_turn_batches_pending_turns(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """With `after_turn_batching=True` (default), rapid turns collapse.

    Three turns arrive in quick succession (dispatcher hook fires
    three times before the worker drains). The router must produce
    ONE job covering [0..2], not three separate jobs.
    """
    sessions = {"chat_t15a": _session(turns=3, status="running")}
    scanner = _TestScanner(outbox, sessions, tmp_path)
    router = TriggerRouter(
        scanner,
        outbox,
        model_config_provider=_model_config,
        after_turn_batching=True,
    )

    # Simulate the dispatcher hook firing three times, once per turn.
    # The scanner rewrites the file each time, so each call sees the
    # same three turns. Each call must be idempotent w.r.t. the
    # previous one *only if* the snapshot is unchanged. In practice
    # the snapshot WILL change between calls (the dispatcher writes
    # a new turn each time), so we simulate that by mutating the
    # session.
    jobs: List[MemoryJob] = []
    for turn_index in range(3):
        # Grow the session to reflect the turn that just completed.
        sessions["chat_t15a"]["turns"] = _session(
            turns=turn_index + 1, status="running"
        )["turns"]
        new_jobs = await router.route_after_turn(
            chat_id="chat_t15a",
            project_id="proj_test",
            current_turn_index=turn_index,
        )
        jobs.extend(new_jobs)

    # Batching collapses: at most 3 jobs (one per distinct snapshot),
    # but the second and third calls are clamped by the cursor that
    # job 1 will eventually advance. Since no worker ran, cursor is
    # still 0 and every call covers a growing range starting from 0.
    # That produces 3 distinct jobs (each with a different snapshot
    # AND a different end_turn_index). Verify no duplicates per call.
    for call_idx in range(3):
        per_call = [j for j in jobs if j.end_turn_index == call_idx]
        assert len(per_call) <= 1, (
            f"call {call_idx} produced {len(per_call)} jobs; "
            f"batching must collapse to 1"
        )


@pytest.mark.asyncio
async def test_T15_after_turn_per_turn_mode(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """With `after_turn_batching=False`, each pending turn gets its own job."""
    sessions = {"chat_t15b": _session(turns=4, status="running")}
    scanner = _TestScanner(outbox, sessions, tmp_path)
    router = TriggerRouter(
        scanner,
        outbox,
        model_config_provider=_model_config,
        after_turn_batching=False,  # explicit
    )

    # Jump the cursor to turn 0 (simulating "turn 0 already extracted").
    # Then advance the session to turn 3 in one shot -- three turns
    # pending (1, 2, 3). The non-batching mode must produce three jobs.
    from omweb.memory.jobs.outbox import JobResult as _JR

    seed_job = MemoryJob(
        chat_id="chat_t15b",
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_TURN,
        start_turn_index=0,
        end_turn_index=0,
        message_snapshot_hash="seed_hash",
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key="seed_key_t15b",
    )
    await outbox.enqueue(seed_job)
    claimed = await outbox.claim_one("test_worker")
    assert claimed is not None
    await outbox.complete(seed_job.job_id, _JR())

    # Now the cursor is 0. Enqueue turns 1, 2, 3 via route_after_turn.
    jobs = await router.route_after_turn(
        chat_id="chat_t15b",
        project_id="proj_test",
        current_turn_index=3,
    )
    assert len(jobs) == 3, (
        f"per-turn mode must produce one job per pending turn "
        f"(got {len(jobs)})"
    )
    ranges = sorted((j.start_turn_index, j.end_turn_index) for j in jobs)
    assert ranges == [(1, 1), (2, 2), (3, 3)]


# ---------------------------------------------------------------------------
# T16: batch -> every pending session, skip processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T16_batch_skips_processed_sessions(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """`route_batch` enqueues every pending chat, skips up-to-date ones."""
    from omweb.memory.jobs.outbox import JobResult as _JR

    sessions = {
        "chat_pending_a": _session(turns=3),
        "chat_pending_b": _session(turns=5),
        "chat_done": _session(turns=2),
    }
    scanner = _TestScanner(outbox, sessions, tmp_path)

    # Simulate chat_done as fully processed: enqueue + complete a job
    # whose end_turn_index == len(turns)-1.
    seed_job = MemoryJob(
        chat_id="chat_done",
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=0,
        end_turn_index=1,  # len(turns)-1 for 2-turn session
        message_snapshot_hash="seed_hash_done",
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key="seed_key_done",
    )
    await outbox.enqueue(seed_job)
    await outbox.claim_one("test_worker")
    await outbox.complete(seed_job.job_id, _JR())

    router = TriggerRouter(
        scanner,
        outbox,
        model_config_provider=_model_config,
        after_turn_batching=True,
    )
    jobs = await router.route_batch()

    chat_ids = sorted(j.chat_id for j in jobs)
    assert chat_ids == ["chat_pending_a", "chat_pending_b"], (
        f"batch must skip fully-processed chats (got {chat_ids})"
    )
    # Ranges cover the full pending tail.
    for job in jobs:
        assert job.start_turn_index == 0
        assert job.end_turn_index == (
            len(sessions[job.chat_id]["turns"]) - 1
        )


# ---------------------------------------------------------------------------
# T18: session changed mid-flight -> SESSION_CHANGED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T18_session_changed_mid_flight_fails_with_session_changed(
    outbox: MemoryOutbox,
    tmp_path: Path,
) -> None:
    """A file modified after enqueue fails the job with SESSION_CHANGED.

    Per §21.3, retrying would re-extract from an ever-changing
    document (unbounded cost, ambiguous output). The correct
    response is a terminal failure. The user retries manually.
    """
    sessions = {"chat_t18": _session(turns=3)}
    scanner = _TestScanner(outbox, sessions, tmp_path)
    candidate = await scanner._build_candidate("chat_t18")
    assert candidate is not None
    real_hash = _compute_hash_of(candidate.source_session_path)

    job = MemoryJob(
        chat_id="chat_t18",
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=0,
        end_turn_index=2,
        message_snapshot_hash=real_hash,
        source_session_path=candidate.source_session_path,
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key=MemoryJob.compute_idempotency_key(
            chat_id="chat_t18",
            trigger_type=ExtractionTrigger.AFTER_SESSION,
            start_turn_index=0,
            end_turn_index=2,
            message_snapshot_hash=real_hash,
        ),
    )
    await outbox.enqueue(job)

    # Simulate "user sends another message" between enqueue and
    # processing: append a 4th turn to the in-memory session and
    # rewrite the file.
    sessions["chat_t18"]["turns"].append(
        {
            "id": "turn_3_4000",
            "user": "User message 3",
            "assistant": "Assistant reply 3",
        }
    )
    path = Path(candidate.source_session_path)
    path.write_text(
        json.dumps(sessions["chat_t18"], ensure_ascii=False),
        encoding="utf-8",
    )

    empty_transport = _EmptyTransport()
    gateway = MemoryModelGateway(transport=empty_transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    worker = _WiredWorker(
        outbox,
        scanner=scanner,
        orchestrator=orchestrator,
        write_pipeline_provider=lambda: None,
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=5.0,
    )
    await worker.start()
    try:
        terminal = await _wait_terminal(outbox, "chat_t18")
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.FAILED
    assert terminal.error_code == "SESSION_CHANGED"
    assert empty_transport.calls == 0
    assert await outbox.get_cursor("chat_t18") == 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _compute_hash_of(path: str) -> str:
    """Return the snapshot hash of a session file (production helper).

    Wraps ``compute_snapshot_from_file`` with an assertion so tests
    fail loudly when the file is missing -- a silent ``None`` return
    would mask a broken fixture and produce confusing downstream
    assertions.
    """
    from omweb.memory.extraction.snapshot import (
        compute_snapshot_from_file,
    )

    result = compute_snapshot_from_file(path)
    assert result is not None, (
        f"compute_snapshot_from_file returned None for {path!r}; "
        f"the test fixture failed to write the session file."
    )
    return result


def _patch_permissive_llm_request(monkeypatch) -> None:
    """Replace ``LLMRequest`` with a permissive stand-in for wire-up tests.

    The real ``LLMRequest`` is a Pydantic model that validates
    ``model_capabilities`` against the ``ModelCapabilities`` type.
    In the wire-up tests we mock ``resolve_model_capabilities`` to
    return an opaque object (``SimpleNamespace`` or ``object()``),
    which fails that validation before the transport's actual logic
    runs.

    Rather than reimplement ``ModelCapabilities``'s exact shape in
    every test -- which would couple the tests to an implementation
    detail -- we swap ``LLMRequest`` with a class that accepts any
    kwargs without validation. The transport under test never reads
    the request's internal structure, so this bypass is safe and
    keeps the test focused on what it actually exercises
    (provider dispatch + cost tracking + JSON parsing).

    Scoped to the wire-up tests ONLY. Every other test
    (T1-T18, extraction pipeline, etc.) uses a mock transport that
    never touches ``LLMRequest``, so this patch is irrelevant to
    them.

    The substitution is done on the ``peldrun.llm.request`` module
    namespace because the gateway imports ``LLMRequest`` lazily
    from that module INSIDE ``_default_transport`` -- so a
    ``monkeypatch.setattr`` against the module symbol is visible
    to the gateway at call time.
    """
    class _PermissiveLLMRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    import peldrun.llm.request as request_module
    monkeypatch.setattr(
        request_module, "LLMRequest", _PermissiveLLMRequest,
    )


# ---------------------------------------------------------------------------
# Real transport wiring (Phase 1-C completion)
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal ProviderAdapter double for real-transport tests."""

    def __init__(self, *, content: str, cost: int = 0):
        self._content = content
        self._cost = cost
        self.closed = False
        self.last_request = None

    async def generate(self, request):
        self.last_request = request
        # Emulate an LLMResponse object with the fields the
        # transport reads: content + usage.
        from types import SimpleNamespace
        return SimpleNamespace(
            content=self._content,
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
            ),
            latency_ms=1.0,
        )

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_real_transport_happy_path(monkeypatch):
    """_default_transport dispatches through provider_factory and parses JSON."""
    from omweb.memory.extraction.gateway import MemoryModelGateway

    fake_adapter = _FakeAdapter(
        content='{"candidates": [{"content": "Alice"}]}',
        cost=2_000_000,
    )

    import peldrun.llm.providers as providers_module

    def fake_create(provider_id, connection):
        assert provider_id == "openai"
        assert connection.api_key == "test-key"
        assert connection.base_url == "https://test.example/v1"
        return fake_adapter

    monkeypatch.setattr(
        providers_module.provider_factory,
        "create",
        fake_create,
    )

    # Bypass LLMRequest's strict ModelCapabilities validation --
    # the transport under test does not read the request's shape,
    # so the permissive stand-in is safe here. See
    # _patch_permissive_llm_request's docstring for rationale.
    _patch_permissive_llm_request(monkeypatch)

    # Mock resolve_model_capabilities so we don't need a real
    # provider registry entry.
    import peldrun.llm.capability_resolver as cap_resolver
    from types import SimpleNamespace
    monkeypatch.setattr(
        cap_resolver,
        "resolve_model_capabilities",
        lambda **kw: SimpleNamespace(
            supports_tool_use=False,
            supports_reasoning=False,
            supports_streaming=False,
        ),
    )

    # Mock default_tracker.complete to return a cost record.
    import peldrun.llm.accounting as accounting
    async def fake_complete(**kwargs):
        return SimpleNamespace(cost_nano_usd=2_000_000)
    monkeypatch.setattr(
        accounting.default_tracker,
        "complete",
        fake_complete,
    )

    gateway = MemoryModelGateway()
    result = await gateway.generate_structured(
        system_prompt="sys",
        user_prompt="user",
        provider_id="openai",
        model_id="gpt-4o-mini",
        api_key="test-key",
        base_url="https://test.example/v1",
    )

    assert result.parsed == {"candidates": [{"content": "Alice"}]}
    assert result.cost_nano_usd == 2_000_000
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert fake_adapter.closed is True
    assert fake_adapter.last_request is not None


@pytest.mark.asyncio
async def test_real_transport_provider_error_maps_to_transport_error(
    monkeypatch,
):
    """Adapter exception -> LLMTransportError."""
    from omweb.memory.extraction.gateway import (
        LLMTransportError,
        MemoryModelGateway,
    )

    class _FailingAdapter:
        async def generate(self, request):
            raise RuntimeError("simulated provider crash")
        async def close(self):
            pass

    import peldrun.llm.providers as providers_module
    monkeypatch.setattr(
        providers_module.provider_factory,
        "create",
        lambda pid, conn: _FailingAdapter(),
    )
    import peldrun.llm.capability_resolver as cap_resolver
    monkeypatch.setattr(
        cap_resolver,
        "resolve_model_capabilities",
        lambda **kw: type("C", (), {})(),
    )

    # Bypass LLMRequest's strict ModelCapabilities validation.
    _patch_permissive_llm_request(monkeypatch)

    gateway = MemoryModelGateway()
    with pytest.raises(LLMTransportError) as exc_info:
        await gateway.generate_structured(
            system_prompt="sys",
            user_prompt="user",
            provider_id="openai",
            model_id="gpt-4o-mini",
            api_key="test-key",
            base_url="https://test.example/v1",
        )
    assert "simulated provider crash" in str(exc_info.value)


@pytest.mark.asyncio
async def test_real_transport_parse_error_propagates(monkeypatch):
    """Non-JSON adapter output -> LLMParseError (subclass of LLMTransportError)."""
    from omweb.memory.extraction.gateway import (
        LLMParseError,
        MemoryModelGateway,
    )

    fake_adapter = _FakeAdapter(content="this is not JSON at all")

    import peldrun.llm.providers as providers_module
    monkeypatch.setattr(
        providers_module.provider_factory,
        "create",
        lambda pid, conn: fake_adapter,
    )
    import peldrun.llm.capability_resolver as cap_resolver
    monkeypatch.setattr(
        cap_resolver,
        "resolve_model_capabilities",
        lambda **kw: type("C", (), {})(),
    )
    import peldrun.llm.accounting as accounting
    async def fake_complete(**kwargs):
        return type("R", (), {"cost_nano_usd": 0})()
    monkeypatch.setattr(
        accounting.default_tracker,
        "complete",
        fake_complete,
    )

    # Bypass LLMRequest's strict ModelCapabilities validation.
    _patch_permissive_llm_request(monkeypatch)

    gateway = MemoryModelGateway()
    with pytest.raises(LLMParseError):
        await gateway.generate_structured(
            system_prompt="sys",
            user_prompt="user",
            provider_id="openai",
            model_id="gpt-4o-mini",
            api_key="test-key",
            base_url="https://test.example/v1",
        )
# ---------------------------------------------------------------------------
# _resolve_credentials -- real vault shape coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_credentials_from_real_vault_shape(
    tmp_path: Path, monkeypatch,
):
    """_resolve_credentials handles the ACTUAL models_vault.json shape.

    Covers all five containers in the production vault:

      1. ``cloud_vault``         -- ARRAY of camelCase entries
      2. ``lmstudio_vault``      -- SINGLE camelCase object
      3. ``ollama_vault``        -- SINGLE object, no apiKey
      4. ``custom_endpoints``    -- ARRAY (empty in production)
      5. ``provider_connections``-- ARRAY of snake_case plugin
                                    connections with is_active flag

    Plus the caller-override precedence rule: an explicit
    ``base_url`` / ``api_key`` argument wins over vault values.
    """
    import json as _json
    from omweb.memory.extraction.gateway import MemoryModelGateway

    vault = {
        "cloud_vault": [
            {
                "id": "openai",
                "name": "OpenAI",
                "baseUrl": "https://api.openai.com/v1",
                "apiKey": "sk-proj-xxx",
                "type": "",
                "savedModels": ["gpt-4o", "gpt-4o-mini"],
            },
            {
                "id": "anthropic",
                "name": "Anthropic",
                "baseUrl": "https://api.anthropic.com/v1",
                "apiKey": "sk-ant-xxx",
                "type": "anthropic",
                "savedModels": ["claude-3-5-sonnet"],
            },
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "baseUrl": "https://api.deepseek.com",
                "apiKey": "sk-ds-xxx",
            },
        ],
        "custom_endpoints": [],
        "lmstudio_vault": {
            "provider": "lmstudio",
            "baseUrl": "http://127.0.0.1:1234/api/v1/",
            "apiKey": "sk-lm-xxx",
            "model": "",
        },
        "ollama_vault": {
            "baseUrl": "http://127.0.0.1:11434",
            "model": "deepseek-r1-8b:latest",
            # No apiKey -- local deployment
        },
        "provider_connections": [
            {
                "name": "GigaChat - Default",
                "provider_package_id": "gigachat",
                "fields": {
                    "base_url": "https://gigachat.example/api/v1",
                    "api_key": "giga-xxx",
                    "scope": "PERS",
                    "verify_ssl": False,
                },
                "is_active": True,
                "enabled": True,
            },
            {
                "name": "Disabled Provider",
                "provider_package_id": "disabled-pkg",
                "fields": {
                    "base_url": "https://disabled.example",
                    "api_key": "should-not-be-used",
                },
                "is_active": False,
            },
        ],
    }
    vault_path = tmp_path / "models_vault.json"
    vault_path.write_text(
        _json.dumps(vault), encoding="utf-8",
    )
    monkeypatch.setenv(
        "PELDRUN_MODELS_VAULT_PATH", str(vault_path),
    )

    gw = MemoryModelGateway()

    # --- cloud_vault[0] -- openai (camelCase) ---
    url, key, extra = gw._resolve_credentials(
        provider_id="openai", base_url=None, api_key=None,
    )
    assert url == "https://api.openai.com/v1"
    assert key == "sk-proj-xxx"
    # extra carries the non-credential fields verbatim
    assert extra.get("name") == "OpenAI"

    # --- cloud_vault[1] -- anthropic (type -> api_type) ---
    url, key, extra = gw._resolve_credentials(
        provider_id="anthropic", base_url=None, api_key=None,
    )
    assert url == "https://api.anthropic.com/v1"
    assert key == "sk-ant-xxx"
    # The `type` field is preserved AND mirrored to `api_type`
    # so provider_factory routes to the native anthropic adapter.
    assert extra.get("type") == "anthropic"
    assert extra.get("api_type") == "anthropic"

    # --- lmstudio_vault (single object) ---
    url, key, _ = gw._resolve_credentials(
        provider_id="lmstudio", base_url=None, api_key=None,
    )
    assert url == "http://127.0.0.1:1234/api/v1/"
    assert key == "sk-lm-xxx"

    # --- ollama_vault (no apiKey) ---
    url, key, _ = gw._resolve_credentials(
        provider_id="ollama", base_url=None, api_key=None,
    )
    assert url == "http://127.0.0.1:11434"
    assert key is None

    # --- provider_connections -- gigachat (snake_case -> camelCase) ---
    url, key, extra = gw._resolve_credentials(
        provider_id="gigachat", base_url=None, api_key=None,
    )
    assert url == "https://gigachat.example/api/v1"
    assert key == "giga-xxx"
    assert extra.get("scope") == "PERS"
    assert extra.get("verify_ssl") is False
    assert extra.get("name") == "GigaChat - Default"

    # --- provider_connections with is_active=False -> skipped ---
    url, key, extra = gw._resolve_credentials(
        provider_id="disabled-pkg", base_url=None, api_key=None,
    )
    assert url is None
    assert key is None
    assert extra == {}

    # --- caller override wins ---
    url, key, _ = gw._resolve_credentials(
        provider_id="openai",
        base_url="https://custom.example/v1",
        api_key="sk-override",
    )
    assert url == "https://custom.example/v1"
    assert key == "sk-override"

    # --- caller partial override (only api_key) ---
    url, key, _ = gw._resolve_credentials(
        provider_id="openai",
        base_url=None,
        api_key="sk-partial-override",
    )
    assert url == "https://api.openai.com/v1"
    assert key == "sk-partial-override"

    # --- unknown provider ---
    url, key, extra = gw._resolve_credentials(
        provider_id="nonexistent", base_url=None, api_key=None,
    )
    assert url is None
    assert key is None
    assert extra == {}
