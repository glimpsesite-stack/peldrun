# backend/tests/memory/test_memory_extraction_api.py
"""
Phase 3 - Manual Extraction HTTP API test suite.

Covers the two endpoints in
``omweb/memory/api/memory_extraction.py``:

    POST /extract/from-session
    POST /extract/range

The real ``MemoryService`` construction requires a live
extraction pipeline (trigger router, scanner, outbox). Those
internals are already tested elsewhere; here we focus
EXCLUSIVELY on the router's contract -- validation, status
codes, response envelope, argument forwarding. A minimal
``_FakeService`` stands in for the service.

Run with::

    pytest backend/tests/memory/test_memory_extraction_api.py -v
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)

from omweb.memory import set_memory_service_for_tests
from omweb.memory.api.memory_extraction import (
    router as extraction_router,
)
from omweb.memory.jobs.models import MemoryJob


# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal service stand-in recording every call.

    Records the exact kwargs the router passed so tests can
    assert both response shape AND argument forwarding. The
    ``extraction_enabled`` attribute is the only state the
    router reads; the two ``enqueue_*`` coroutines return
    whatever the test set on ``*_return``.
    """

    def __init__(self) -> None:
        self.extraction_enabled: bool = True
        self.from_session_calls: list = []
        self.range_calls: list = []
        self.from_session_return: Any = None
        self.range_return: Any = None

    async def enqueue_extraction_from_session(self, **kwargs):
        self.from_session_calls.append(kwargs)
        if isinstance(self.from_session_return, BaseException):
            raise self.from_session_return
        return self.from_session_return

    async def enqueue_extraction(self, **kwargs):
        self.range_calls.append(kwargs)
        if isinstance(self.range_return, BaseException):
            raise self.range_return
        return self.range_return


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """``TestClient`` with a ``_FakeService`` installed."""
    service = _FakeService()
    set_memory_service_for_tests(service)

    app = FastAPI()
    app.include_router(extraction_router)

    try:
        with TestClient(app) as c:
            c._test_service = service
            yield c
    finally:
        set_memory_service_for_tests(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    chat_id: str = "chat_1",
    trigger: ExtractionTrigger = ExtractionTrigger.MANUAL,
    start: int = 0,
    end: int = 2,
) -> MemoryJob:
    """Build a valid ``MemoryJob`` to return from the fake service."""
    return MemoryJob(
        chat_id=chat_id,
        scope_type=MemoryScope.CHAT,
        scope_id=chat_id,
        trigger_type=trigger,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash="deadbeef" * 8,
        model_provider_id="lmstudio",
        model_id="test-model",
        idempotency_key=f"key_{chat_id}_{start}_{end}",
    )


# ---------------------------------------------------------------------------
# POST /extract/from-session
# ---------------------------------------------------------------------------


def test_extract_from_session_success(client):
    """Happy path: job returned, MANUAL trigger passed to service."""
    service = client._test_service
    service.from_session_return = _make_job(chat_id="c1")

    r = client.post(
        "/extract/from-session", json={"chat_id": "c1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "extract_from_session"
    assert body["chat_id"] == "c1"
    assert body["job"] is not None
    assert body["job"]["chat_id"] == "c1"

    # Verify argument forwarding.
    assert len(service.from_session_calls) == 1
    call = service.from_session_calls[0]
    assert call["chat_id"] == "c1"
    assert call["project_id"] is None
    assert call["trigger_type"] == ExtractionTrigger.MANUAL


def test_extract_from_session_forwards_project_id(client):
    """``project_id`` is forwarded verbatim when provided."""
    service = client._test_service
    service.from_session_return = _make_job(chat_id="c1")

    r = client.post(
        "/extract/from-session",
        json={"chat_id": "c1", "project_id": "proj_42"},
    )
    assert r.status_code == 200
    assert service.from_session_calls[0]["project_id"] == "proj_42"


def test_extract_from_session_no_job_returns_null(client):
    """Service returns None -> 200 with ``job: null`` + message."""
    service = client._test_service
    service.from_session_return = None  # no job was enqueued

    r = client.post(
        "/extract/from-session", json={"chat_id": "c1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["job"] is None
    assert isinstance(body["message"], str)
    assert body["message"]  # non-empty


def test_extract_from_session_disabled_returns_503(client):
    """``extraction_enabled=False`` -> 503, service not called."""
    service = client._test_service
    service.extraction_enabled = False

    r = client.post(
        "/extract/from-session", json={"chat_id": "c1"}
    )
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] == "extraction_unavailable"
    # Service must not be called when the pipeline is disabled.
    assert service.from_session_calls == []


def test_extract_from_session_empty_chat_id_returns_400(client):
    """Empty / whitespace-only chat_id -> Pydantic validation failure.

    FastAPI converts Pydantic ``ValidationError`` to HTTP 422
    by default. This test asserts the value is rejected, not
    the specific code -- the router relies on Pydantic's
    validation which is triggered BEFORE the handler runs.
    """
    for payload in (
        {"chat_id": ""},
        {"chat_id": "   "},
        {},  # missing entirely
    ):
        r = client.post("/extract/from-session", json=payload)
        assert r.status_code == 422, (
            f"Expected 422 for payload {payload!r}, "
            f"got {r.status_code}"
        )


def test_extract_from_session_service_error_returns_500(client):
    """Unexpected exception from the service -> 500, no leak."""
    service = client._test_service
    service.from_session_return = RuntimeError("boom")

    r = client.post(
        "/extract/from-session", json={"chat_id": "c1"}
    )
    assert r.status_code == 500
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] == "RuntimeError"
    # The raw exception message must NOT be echoed verbatim.
    assert "boom" not in body["message"]


# ---------------------------------------------------------------------------
# POST /extract/range
# ---------------------------------------------------------------------------


def test_extract_range_success(client):
    """Happy path: job returned, MANUAL trigger + range forwarded."""
    service = client._test_service
    service.range_return = _make_job(
        chat_id="c1", start=2, end=5,
    )

    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": 2,
            "end_turn_index": 5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "extract_range"
    assert body["chat_id"] == "c1"
    assert body["start_turn_index"] == 2
    assert body["end_turn_index"] == 5
    assert body["job"] is not None

    assert len(service.range_calls) == 1
    call = service.range_calls[0]
    assert call["chat_id"] == "c1"
    assert call["start_turn_index"] == 2
    assert call["end_turn_index"] == 5
    assert call["trigger_type"] == ExtractionTrigger.MANUAL


def test_extract_range_forwards_project_id(client):
    """``project_id`` is forwarded for the range endpoint too."""
    service = client._test_service
    service.range_return = _make_job()

    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "project_id": "proj_7",
            "start_turn_index": 0,
            "end_turn_index": 0,
        },
    )
    assert r.status_code == 200
    assert service.range_calls[0]["project_id"] == "proj_7"


def test_extract_range_no_job_returns_null(client):
    """Service returns None -> 200 with ``job: null`` + message."""
    service = client._test_service
    service.range_return = None

    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": 0,
            "end_turn_index": 5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job"] is None
    assert body["message"]  # non-empty


def test_extract_range_disabled_returns_503(client):
    """``extraction_enabled=False`` -> 503, service not called."""
    service = client._test_service
    service.extraction_enabled = False

    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": 0,
            "end_turn_index": 1,
        },
    )
    assert r.status_code == 503
    assert r.json()["error"] == "extraction_unavailable"
    assert service.range_calls == []


def test_extract_range_negative_start_returns_422(client):
    """``start_turn_index < 0`` fails at the Pydantic ``ge=0`` gate."""
    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": -1,
            "end_turn_index": 3,
        },
    )
    assert r.status_code == 422


def test_extract_range_end_before_start_returns_422(client):
    """``start > end`` -> Pydantic model_validator raises -> 422."""
    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": 5,
            "end_turn_index": 2,
        },
    )
    assert r.status_code == 422


def test_extract_range_huge_end_returns_422(client):
    """``end_turn_index`` beyond the 10_000 cap -> 422 (typo guard)."""
    r = client.post(
        "/extract/range",
        json={
            "chat_id": "c1",
            "start_turn_index": 0,
            "end_turn_index": 1_000_000,
        },
    )
    assert r.status_code == 422


def test_extract_range_empty_chat_id_returns_422(client):
    """Whitespace-only chat_id -> Pydantic validation failure."""
    r = client.post(
        "/extract/range",
        json={
            "chat_id": "   ",
            "start_turn_index": 0,
            "end_turn_index": 1,
        },
    )
    assert r.status_code == 422
