# backend/tests/memory/test_phase1b_extraction.py
"""
Phase 1-B acceptance tests: extraction pipeline.

Covers the four acceptance tests from the plan:

    T5   -- Mock gateway success (orchestrator produces candidates)
    T6   -- Mock gateway error -> retry -> terminal failure
    T11  -- Password in messages -> never reaches LLM
    T13  -- Evidence verbatim + normalization (comprehensive)

Every test exercises the FULL stack:

    MemoryOutbox   ->  MemoryExtractionWorker
                   ->  ExtractionOrchestrator
                   ->  MemoryModelGateway
                   ->  (mock transport)

No real LLM, no network, no credentials. The mock transport is
injected at the gateway boundary, which means the orchestrator's
prompt construction, minimization, validation, and result
assembly are all real code paths.

The worker is subclassed to override two things:

  1. `_process_job` -- calls the real orchestrator instead of the
     Phase 1-A stub.
  2. `_compute_backoff_seconds` -- returns 0 (immediate retry)
     so T6 runs in milliseconds rather than minutes.

This subclass is exactly what Phase 1-C's production worker will
look like, so the tests double as a wiring blueprint.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import pytest_asyncio

from omweb.memory.storage.migrations import apply_schema
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
    MemoryJobType,
)
from omweb.memory.jobs.outbox import (
    JobResult,
    MemoryOutbox,
)
from omweb.memory.jobs.worker import MemoryExtractionWorker
from omweb.memory.extraction.contracts import (
    Evidence,
    ExtractionResult,
)
from omweb.memory.extraction.gateway import (
    LLMStructuredResponse,
    LLMTransportError,
    MemoryModelGateway,
)
from omweb.memory.extraction.orchestrator import (
    ExtractionOrchestrator,
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
    """Return a fresh SQLite path with the memory schema applied."""
    path = tmp_path / "memory_test_phase1b.db"
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
# Test doubles
# ---------------------------------------------------------------------------


class _CapturingTransport:
    """A transport that records prompts and returns a fixed response.

    Used to test what the orchestrator SENDS to the LLM (T11) and
    to verify the happy path (T5). The `captured` list is appended
    to on every call so a test can inspect the actual prompt the
    orchestrator constructed.
    """

    def __init__(
        self,
        *,
        parsed: Optional[Dict[str, Any]] = None,
        cost_nano_usd: int = 0,
    ) -> None:
        self.parsed = parsed if parsed is not None else {"candidates": []}
        self.cost_nano_usd = cost_nano_usd
        self.captured: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> LLMStructuredResponse:
        self.captured.append(dict(kwargs))
        return LLMStructuredResponse(
            parsed=self.parsed,
            raw_text='{"candidates": []}',
            cost_nano_usd=self.cost_nano_usd,
            input_tokens=100,
            output_tokens=50,
        )


class _FailingTransport:
    """A transport that always raises `LLMTransportError`.

    Used to test the retry -> terminal-failure path (T6). Records
    the number of calls so the test can assert the retry count.
    """

    def __init__(self, message: str = "Simulated provider error"):
        self.message = message
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> LLMStructuredResponse:
        self.call_count += 1
        raise LLMTransportError(
            self.message,
            provider_id=kwargs.get("provider_id"),
            model_id=kwargs.get("model_id"),
        )


class _WiredWorker(MemoryExtractionWorker):
    """A worker that calls the real orchestrator (Phase 1-C preview).

    Overrides:

      * `_process_job` -- calls `ExtractionOrchestrator.extract()`
        against a session provided by `session_loader`. The
        returned `JobResult` carries the extraction summary.
      * `_compute_backoff_seconds` -- returns 0 so retries are
        immediate in tests (production uses exponential backoff).

    The session loader is a simple callable `(chat_id) -> dict`.
    Tests supply a dict directly; Phase 1-C will supply a loader
    that reads from disk and normalizes to `session.json` or
    `session_context.json` shape.
    """

    def __init__(
        self,
        outbox: MemoryOutbox,
        orchestrator: ExtractionOrchestrator,
        session_loader: Callable[[str], Dict[str, Any]],
        *,
        recommended_source: str = "session_json",
        **kwargs: Any,
    ) -> None:
        super().__init__(outbox, **kwargs)
        self._orchestrator = orchestrator
        self._session_loader = session_loader
        self._recommended_source = recommended_source

    async def _process_job(self, job: MemoryJob) -> JobResult:
        session = self._session_loader(job.chat_id)
        result: ExtractionResult = await self._orchestrator.extract(
            job=job,
            session=session,
            recommended_source=self._recommended_source,
        )
        return JobResult(
            candidates_count=len(result.candidates),
            created_memories_count=0,  # Phase 1-C wires the write pipeline
            cost_nano_usd=result.cost_nano_usd,
            decisions=result.decisions,
        )

    def _compute_backoff_seconds(self, attempts: int) -> int:
        # Zero backoff in tests so retries are immediate.
        return 0


# ---------------------------------------------------------------------------
# Job / session builders
# ---------------------------------------------------------------------------


def _make_job(
    *,
    chat_id: str = "chat_test",
    start: int = 0,
    end: int = 2,
    max_attempts: int = 3,
) -> MemoryJob:
    key = MemoryJob.compute_idempotency_key(
        chat_id=chat_id,
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash="snap_aaaaaaaaaaaa",
    )
    return MemoryJob(
        chat_id=chat_id,
        project_id="proj_test",
        scope_type=MemoryScope.PROJECT,
        scope_id="proj_test",
        trigger_type=ExtractionTrigger.AFTER_SESSION,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash="snap_aaaaaaaaaaaa",
        model_provider_id="openai",
        model_id="gpt-4o-mini",
        idempotency_key=key,
        max_attempts=max_attempts,
    )


def _session() -> Dict[str, Any]:
    """Three-turn sample session in `session.json` shape."""
    return {
        "turns": [
            {
                "id": "turn_0_100",
                "user": "Hello",
                "assistant": "Hi there",
            },
            {
                "id": "turn_1_200",
                "user": "My name is Alice",
                "assistant": "Nice to meet you",
            },
            {
                "id": "turn_2_300",
                "user": "I prefer dark mode",
                "assistant": "Noted",
            },
        ]
    }


async def _drain(
    outbox: MemoryOutbox, expected_completed: int, *, timeout: float = 10.0
) -> None:
    """Poll until `expected_completed` jobs are done or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        completed = await outbox.count_by_status(
            MemoryJobStatus.COMPLETED
        )
        if completed >= expected_completed:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Queue did not reach {expected_completed} completed within {timeout}s"
    )


async def _wait_for_terminal(
    outbox: MemoryOutbox, chat_id: str, timeout: float = 10.0
) -> MemoryJob:
    """Poll until the chat's single job is in a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = await outbox.list_by_chat(chat_id, limit=1)
        if jobs and jobs[0].status.is_terminal:
            return jobs[0]
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Job for {chat_id} did not reach terminal state within {timeout}s"
    )


# ---------------------------------------------------------------------------
# T5 -- Mock gateway success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T5_mock_gateway_success(
    outbox: MemoryOutbox,
) -> None:
    """A successful extraction produces candidates and advances the cursor.

    Verifies:
      * The orchestrator parses valid candidates from the LLM.
      * `JobResult.candidates_count` matches the accepted count.
      * The job ends COMPLETED with `candidates_count > 0`.
      * The cursor advances to `end_turn_index` (2).
    """
    parsed = {
        "candidates": [
            {
                "content": "User name is Alice",
                "memory_type": "semantic",
                "importance": 80,
                "confidence": 0.9,
                "evidence": {
                    "turn_index": 1,
                    "turn_id": "turn_1_200",
                    "snippet": "My name is Alice",
                    "message_role": "user",
                },
            },
            {
                "content": "User prefers dark mode",
                "memory_type": "preference",
                "importance": 60,
                "confidence": 0.85,
                "evidence": {
                    "turn_index": 2,
                    "snippet": "I prefer dark mode",
                    "message_role": "user",
                },
            },
        ]
    }
    transport = _CapturingTransport(
        parsed=parsed, cost_nano_usd=2_000_000
    )
    gateway = MemoryModelGateway(transport=transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    job = _make_job(chat_id="chat_t5", start=0, end=2)
    await outbox.enqueue(job)

    worker = _WiredWorker(
        outbox,
        orchestrator,
        session_loader=lambda chat_id: _session(),
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        await _drain(outbox, expected_completed=1)
    finally:
        await worker.stop(timeout_seconds=3)

    # Verify the job row.
    fetched = await outbox.get_by_id(job.job_id)
    assert fetched is not None
    assert fetched.status == MemoryJobStatus.COMPLETED
    assert fetched.candidates_count == 2
    assert fetched.cost_nano_usd == 2_000_000
    assert len(fetched.decisions) == 2
    assert all(d["action"] == "accepted" for d in fetched.decisions)

    # Cursor advanced.
    assert await outbox.get_cursor("chat_t5") == 2

    # The transport was called once, and with the system prompt
    # the orchestrator built.
    assert len(transport.captured) == 1
    captured = transport.captured[0]
    assert "durable facts" in captured["system_prompt"]
    assert "=== Turn 1 ===" in captured["user_prompt"]
    assert "=== Turn 2 ===" in captured["user_prompt"]
    assert captured["provider_id"] == "openai"
    assert captured["model_id"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# T6 -- Mock gateway error -> retry -> terminal failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T6_gateway_error_retry_then_fail(
    outbox: MemoryOutbox,
) -> None:
    """A persistently failing gateway exhausts retries and fails the job.

    Verifies the full retry policy:
      * The worker retries on `LLMTransportError`.
      * After `max_attempts` failures, the job transitions to FAILED.
      * `error_code` is set (the worker sets `EXTRACTION_FAILED`).
      * The cursor does NOT advance (invariant from §19.3).
      * The transport was called exactly `max_attempts` times.
    """
    transport = _FailingTransport("Simulated persistent error")
    gateway = MemoryModelGateway(transport=transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    chat_id = "chat_t6"
    job = _make_job(chat_id=chat_id, start=0, end=2, max_attempts=2)
    await outbox.enqueue(job)

    worker = _WiredWorker(
        outbox,
        orchestrator,
        session_loader=lambda chat_id: _session(),
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        terminal = await _wait_for_terminal(outbox, chat_id)
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.FAILED
    assert terminal.error_code == "EXTRACTION_FAILED"
    assert "Simulated persistent error" in (terminal.error_message or "")
    assert terminal.attempts == 2
    assert transport.call_count == 2

    # Cursor invariant: never advanced.
    assert await outbox.get_cursor(chat_id) == 0


# ---------------------------------------------------------------------------
# T11 -- Password never reaches the LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T11_password_never_reaches_llm(
    outbox: MemoryOutbox,
) -> None:
    """A message containing a password is minimized before LLM dispatch.

    Verifies:
      * The literal secret does NOT appear in any prompt sent to
        the LLM.
      * The `[REDACTED]` marker DOES appear, proving the
        minimizer engaged.
      * The job still completes successfully (minimization is not
        a rejection; the surrounding context remains intact).
    """
    secret = "hunter2_supersecret_xyz"
    session_with_secret = {
        "turns": [
            {
                "id": "turn_0_100",
                "user": f"My password is {secret}",
                "assistant": "Got it, I won't repeat it.",
            },
        ]
    }

    transport = _CapturingTransport(parsed={"candidates": []})
    gateway = MemoryModelGateway(transport=transport)
    orchestrator = ExtractionOrchestrator(gateway=gateway)

    chat_id = "chat_t11"
    job = _make_job(chat_id=chat_id, start=0, end=0)
    await outbox.enqueue(job)

    worker = _WiredWorker(
        outbox,
        orchestrator,
        session_loader=lambda cid: session_with_secret,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        await _drain(outbox, expected_completed=1)
    finally:
        await worker.stop(timeout_seconds=3)

    # Exactly one call, one prompt.
    assert len(transport.captured) == 1
    prompt = transport.captured[0]["user_prompt"]

    # The secret MUST NOT appear anywhere.
    assert secret not in prompt, (
        f"SECRET LEAKED INTO PROMPT: {prompt!r}"
    )
    # The redaction marker MUST appear.
    assert "[REDACTED]" in prompt

    # The job completed successfully -- minimization does not
    # block extraction.
    fetched = await outbox.get_by_id(job.job_id)
    assert fetched is not None
    assert fetched.status == MemoryJobStatus.COMPLETED


# ---------------------------------------------------------------------------
# T13 -- Evidence verbatim + normalization (comprehensive)
# ---------------------------------------------------------------------------


class TestT13EvidenceVerbatim:
    """Exercise the verbatim rule across the ranges it must enforce.

    Each test sends one candidate through the full orchestrator
    and inspects the resulting decision. Passing these together
    proves the plan's §21.5 rules are enforced end-to-end.
    """

    @staticmethod
    def _build_parsed(
        *,
        snippet: str,
        turn_index: int = 1,
        message_role: str = "user",
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {
            "turn_index": turn_index,
            "snippet": snippet,
            "message_role": message_role,
        }
        if turn_id is not None:
            evidence["turn_id"] = turn_id
        return {
            "candidates": [
                {
                    "content": "User name is Alice",
                    "memory_type": "semantic",
                    "importance": 80,
                    "confidence": 0.9,
                    "evidence": evidence,
                }
            ]
        }

    async def _extract_one(
        self, snippet: str, **kwargs: Any
    ) -> ExtractionResult:
        transport = _CapturingTransport(
            parsed=self._build_parsed(snippet=snippet, **kwargs)
        )
        gateway = MemoryModelGateway(transport=transport)
        orch = ExtractionOrchestrator(gateway=gateway)
        job = _make_job(chat_id="chat_t13", start=0, end=2)
        return await orch.extract(
            job=job,
            session=_session(),
            recommended_source="session_json",
        )

    @pytest.mark.asyncio
    async def test_exact_match_accepted(self) -> None:
        result = await self._extract_one("My name is Alice")
        assert len(result.candidates) == 1
        assert result.decisions[0]["action"] == "accepted"

    @pytest.mark.asyncio
    async def test_whitespace_normalized(self) -> None:
        result = await self._extract_one(
            "My     name   is    Alice"
        )
        assert len(result.candidates) == 1
        assert result.decisions[0]["action"] == "accepted"

    @pytest.mark.asyncio
    async def test_case_normalized(self) -> None:
        result = await self._extract_one("MY NAME IS ALICE")
        assert len(result.candidates) == 1
        assert result.decisions[0]["action"] == "accepted"

    @pytest.mark.asyncio
    async def test_paraphrase_rejected(self) -> None:
        result = await self._extract_one(
            "The user introduced themselves as Alice"
        )
        assert len(result.candidates) == 0
        assert (
            result.decisions[0]["reason"] == "snippet_not_verbatim"
        )

    @pytest.mark.asyncio
    async def test_typo_rejected(self) -> None:
        result = await self._extract_one("My nmae is Alice")
        assert len(result.candidates) == 0
        assert (
            result.decisions[0]["reason"] == "snippet_not_verbatim"
        )

    @pytest.mark.asyncio
    async def test_out_of_range_turn_rejected(self) -> None:
        result = await self._extract_one(
            "My name is Alice", turn_index=999
        )
        assert len(result.candidates) == 0
        assert (
            result.decisions[0]["reason"]
            == "turn_index_out_of_range"
        )

    @pytest.mark.asyncio
    async def test_turn_id_mismatch_rejected(self) -> None:
        result = await self._extract_one(
            "My name is Alice",
            turn_id="turn_wrong_999",
        )
        assert len(result.candidates) == 0
        assert (
            result.decisions[0]["reason"] == "turn_id_mismatch"
        )

    @pytest.mark.asyncio
    async def test_missing_evidence_rejected(self) -> None:
        parsed = {
            "candidates": [
                {
                    "content": "User name is Alice",
                    "memory_type": "semantic",
                    "importance": 80,
                    "confidence": 0.9,
                    # no evidence key at all
                }
            ]
        }
        transport = _CapturingTransport(parsed=parsed)
        gateway = MemoryModelGateway(transport=transport)
        orch = ExtractionOrchestrator(gateway=gateway)
        job = _make_job(chat_id="chat_t13_no_ev", start=0, end=2)
        result = await orch.extract(
            job=job,
            session=_session(),
            recommended_source="session_json",
        )
        assert len(result.candidates) == 0
        assert result.decisions[0]["reason"] == "no_evidence"

    @pytest.mark.asyncio
    async def test_snippet_too_short_rejected(self) -> None:
        # `abc` is 3 chars, which is the MINIMUM. Use 2 chars.
        result = await self._extract_one("Hi")
        assert len(result.candidates) == 0
        # "Hi" is 2 chars -> snippet_too_short BEFORE verbatim check.
        assert result.decisions[0]["reason"] == "snippet_too_short"

    @pytest.mark.asyncio
    async def test_role_none_resolves_user_side_only(self) -> None:
        # S2: "Nice to meet you" is the ASSISTANT side. A missing
        # message_role is treated as "user" and resolved against the
        # USER side only, so an assistant-only snippet is rejected.
        parsed = {
            "candidates": [
                {
                    "content": "Alice was welcomed",
                    "memory_type": "episodic",
                    "confidence": 0.9,
                    "evidence": {
                        "turn_index": 1,
                        "snippet": "Nice to meet you",
                        # no message_role
                    },
                }
            ]
        }
        transport = _CapturingTransport(parsed=parsed)
        gateway = MemoryModelGateway(transport=transport)
        orch = ExtractionOrchestrator(gateway=gateway)
        job = _make_job(chat_id="chat_t13_role", start=0, end=2)
        result = await orch.extract(
            job=job,
            session=_session(),
            recommended_source="session_json",
        )
        assert len(result.candidates) == 0
        assert (
            result.decisions[0]["reason"] == "snippet_not_verbatim"
        )


# ---------------------------------------------------------------------------
# Additional integration checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_happy_path_direct(
    outbox: MemoryOutbox,
) -> None:
    """Orchestrator in isolation (no worker) produces the expected result.

    T5 above exercises it via the worker; this test verifies the
    orchestrator can be used directly by callers who do not want
    the worker's retry semantics.
    """
    parsed = {
        "candidates": [
            {
                "content": "User name is Alice",
                "memory_type": "semantic",
                "confidence": 0.9,
                "evidence": {
                    "turn_index": 1,
                    "snippet": "My name is Alice",
                    "message_role": "user",
                },
            }
        ]
    }
    transport = _CapturingTransport(
        parsed=parsed, cost_nano_usd=1_500_000
    )
    gateway = MemoryModelGateway(transport=transport)
    orch = ExtractionOrchestrator(gateway=gateway)
    job = _make_job(chat_id="chat_direct", start=0, end=2)

    result = await orch.extract(
        job=job,
        session=_session(),
        recommended_source="session_json",
    )
    assert len(result.candidates) == 1
    assert result.cost_nano_usd == 1_500_000
    assert len(result.decisions) == 1
    assert result.decisions[0]["action"] == "accepted"


@pytest.mark.asyncio
async def test_parse_error_is_retryable(
    outbox: MemoryOutbox,
) -> None:
    """A transport that returns unparseable text should also retry.

    `LLMParseError` is a subclass of `LLMTransportError`, so the
    worker's generic exception handler treats it identically. This
    test confirms the worker sees it as a retryable failure.
    """
    from omweb.memory.extraction.gateway import LLMParseError

    class _ParseFailTransport:
        def __init__(self):
            self.call_count = 0

        async def __call__(self, **kwargs):
            self.call_count += 1
            raise LLMParseError(
                "Simulated parse failure",
                raw_text="not json at all",
            )

    transport = _ParseFailTransport()
    gateway = MemoryModelGateway(transport=transport)
    orch = ExtractionOrchestrator(gateway=gateway)

    chat_id = "chat_parse_fail"
    job = _make_job(chat_id=chat_id, max_attempts=2)
    await outbox.enqueue(job)

    worker = _WiredWorker(
        outbox,
        orch,
        session_loader=lambda cid: _session(),
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        terminal = await _wait_for_terminal(outbox, chat_id)
    finally:
        await worker.stop(timeout_seconds=3)

    assert terminal.status == MemoryJobStatus.FAILED
    assert transport.call_count == 2
    assert await outbox.get_cursor(chat_id) == 0
