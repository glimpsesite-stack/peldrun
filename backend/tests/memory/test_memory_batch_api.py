# backend/tests/memory/test_memory_batch_api.py
"""
Phase 3 - M6c tests: batch HTTP API.

Covers the 7 endpoints in ``omweb/memory/api/memory_batch.py``:

    POST   /batch
    GET    /batch
    GET    /batch/estimate    (declared BEFORE /{batch_id})
    GET    /batch/{batch_id}
    POST   /batch/{batch_id}/pause
    POST   /batch/{batch_id}/resume
    POST   /batch/{batch_id}/cancel

Uses the REAL ``MemoryBatchService`` (backed by the real outbox
+ a fake scanner), so the router contract AND the service
contract are exercised together. Only the scanner and the
snapshot function are mocked.

Run with::

    pytest backend/tests/memory/test_memory_batch_api.py -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional

import pytest
import pytest_asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from peldrun.memory.domain.enums import ExtractionTrigger

from omweb.memory import set_memory_service_for_tests
from omweb.memory.api.memory_batch import router as batch_router
from omweb.memory.batch.models import MemoryBatchStatus
from omweb.memory.batch.service import MemoryBatchService
from omweb.memory.extraction.session_scanner import (
    SessionCandidate,
)
from omweb.memory.extraction.trigger_router import (
    ExtractionModelConfig,
    TriggerRouter,
)
from omweb.memory.jobs.outbox import (
    JobResult,
    MemoryOutbox,
)
from omweb.memory.storage.migrations import apply_schema


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeExtraction:
    def __init__(self, provider_id: str, model_id: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id


class _FakeAISettings:
    def __init__(
        self,
        *,
        enabled: bool = True,
        extraction_enabled: bool = True,
        provider_id: Optional[str] = "lmstudio",
        model_id: Optional[str] = "test-model",
    ) -> None:
        self.enabled = enabled
        self.extraction_enabled = extraction_enabled
        self.min_confidence = 0.7
        self.extraction = _FakeExtraction(
            provider_id or "", model_id or "",
        )


class _FakeScanner:
    def __init__(
        self, candidates: List[SessionCandidate],
    ) -> None:
        self._candidates = list(candidates)

    async def scan_all_sessions(
        self, *, only_unprocessed: bool = True,
    ) -> List[SessionCandidate]:
        if only_unprocessed:
            return [c for c in self._candidates if c.pending_turns > 0]
        return list(self._candidates)

    async def get_candidate(
        self, chat_id: str,
    ) -> Optional[SessionCandidate]:
        for c in self._candidates:
            if c.chat_id == chat_id:
                return c
        return None


class _FakeService:
    """MemoryService stand-in exposing only ``.batch_service``."""

    def __init__(self, batch_service: Optional[Any]) -> None:
        self.batch_service = batch_service


def _make_candidate(
    chat_id: str,
    *,
    total_turns: int = 5,
    last_extracted: int = -1,
) -> SessionCandidate:
    pending = (
        total_turns if last_extracted < 0
        else max(0, total_turns - last_extracted - 1)
    )
    return SessionCandidate(
        chat_id=chat_id,
        project_id=None,
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


def _model_config() -> ExtractionModelConfig:
    return ExtractionModelConfig(
        provider_id="lmstudio", model_id="test-model",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "memory_test.db"
        conn = sqlite3.connect(str(p))
        try:
            apply_schema(conn)
        finally:
            conn.close()
        yield p


@pytest.fixture
def patch_snapshot(monkeypatch):
    async def _fake(path: str) -> str:
        return f"fake_{hash(path) & 0xFFFFFFFF:08x}"
    monkeypatch.setattr(
        "omweb.memory.extraction.trigger_router."
        "compute_snapshot_from_file_async",
        _fake,
    )


def _make_client(
    db_path: Path,
    candidates: List[SessionCandidate],
    *,
    ai_settings: Optional[_FakeAISettings] = None,
):
    """Build a TestClient with a live MemoryBatchService.

    Uses an explicit fresh event loop (``asyncio.new_event_loop``)
    rather than ``get_event_loop()`` -- see
    ``test_memory_jobs_api.py`` for the rationale (isolation from
    pytest-asyncio's teardown across files).
    """
    import asyncio

    loop = asyncio.new_event_loop()

    outbox = MemoryOutbox(db_path)
    loop.run_until_complete(outbox.open())

    scanner = _FakeScanner(candidates)
    router_obj = TriggerRouter(
        scanner, outbox,
        model_config_provider=_model_config,
        after_turn_batching=True,
    )
    _settings = ai_settings or _FakeAISettings()

    def _settings_provider():
        return _settings

    batch_service = MemoryBatchService(
        outbox=outbox,
        scanner=scanner,
        trigger_router=router_obj,
        settings_provider=_settings_provider,
    )

    set_memory_service_for_tests(_FakeService(batch_service))

    app = FastAPI()
    app.include_router(batch_router)

    class _Handle:
        client: TestClient
        outbox: MemoryOutbox
        loop: Any
        batch_service: MemoryBatchService

    h = _Handle()
    h.outbox = outbox
    h.loop = loop
    h.batch_service = batch_service
    return h, app


@pytest.fixture
def client_factory(db_path, patch_snapshot):
    """Factory fixture yielding (client, handle) and cleaning up."""
    created = []

    def _factory(
        candidates: List[SessionCandidate],
        *,
        ai_settings: Optional[_FakeAISettings] = None,
    ):
        h, app = _make_client(
            db_path, candidates, ai_settings=ai_settings,
        )
        c = TestClient(app)
        c.__enter__()
        created.append((c, h))
        return c, h

    try:
        yield _factory
    finally:
        for c, h in created:
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass
            try:
                h.loop.run_until_complete(h.outbox.close())
            finally:
                h.loop.close()
        set_memory_service_for_tests(None)


@pytest.fixture
def client_no_extraction():
    """Client whose ``batch_service`` is None -> every route 503."""
    set_memory_service_for_tests(_FakeService(None))
    app = FastAPI()
    app.include_router(batch_router)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        set_memory_service_for_tests(None)


# ===========================================================================
# POST /batch
# ===========================================================================


def test_create_batch_success(client_factory):
    """Two candidate sessions -> 200 with RUNNING batch + progress."""
    c, _ = client_factory(
        [_make_candidate("c1"), _make_candidate("c2")]
    )
    r = c.post("/batch", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "batch_create"
    assert body["batch"]["status"] == "running"
    assert body["batch"]["job_count"] == 2
    assert body["batch"]["session_count"] == 2
    assert body["progress"]["total"] == 2
    assert body["progress"]["pending_work"] == 2


def test_create_batch_empty_workspace_completes_immediately(
    client_factory,
):
    """No candidates -> 200 with COMPLETED batch."""
    c, _ = client_factory([])
    r = c.post("/batch", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["status"] == "completed"
    assert body["batch"]["job_count"] == 0
    assert body["progress"]["total"] == 0


def test_create_batch_forwards_metadata(client_factory):
    """All body fields reach the service."""
    c, h = client_factory([_make_candidate("c1")])
    r = c.post(
        "/batch",
        json={
            "triggered_by": "auto_schedule",
            "project_id": "proj_7",
            "chat_ids": ["c1"],
            "options": {"note": "nightly"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    batch_id = body["batch"]["batch_id"]
    # Reload from DB to check the persisted values.
    batch = h.loop.run_until_complete(
        h.outbox.get_batch(batch_id)
    )
    assert batch.triggered_by == "auto_schedule"
    assert batch.project_id == "proj_7"
    assert batch.chat_ids == ["c1"]
    assert batch.options == {"note": "nightly"}


def test_create_batch_rejects_when_ai_disabled(client_factory):
    """ai.enabled=False -> 503."""
    c, _ = client_factory(
        [],
        ai_settings=_FakeAISettings(enabled=False),
    )
    r = c.post("/batch", json={})
    assert r.status_code == 503
    assert r.json()["error"] == "extraction_unavailable"


def test_create_batch_rejects_when_model_id_missing(
    client_factory,
):
    """model_id missing -> 503.

    The backend's ``create_batch`` requires ``model_id`` (the
    worker cannot infer a model from a provider -- only the
    reverse). ``provider_id=None`` is a legitimate state, so a
    request with a valid ``model_id`` but null ``provider_id``
    succeeds (see the ``accepts_missing_provider_id`` test
    below).
    """
    c, _ = client_factory(
        [],
        ai_settings=_FakeAISettings(model_id=None),
    )
    r = c.post("/batch", json={})
    assert r.status_code == 503
    assert r.json()["error"] == "extraction_unavailable"


def test_create_batch_accepts_missing_provider_id(
    client_factory,
):
    """provider_id=None with a valid model_id -> 200.

    The batch is created; because the workspace has no pending
    sessions, it transitions to COMPLETED immediately and the
    response carries the batch row with ``model_provider_id:
    null`` and ``model_id: "test-model"``. This documents the
    contract the worker relies on: only ``model_id`` is
    required at enqueue time.
    """
    c, _ = client_factory(
        [],
        ai_settings=_FakeAISettings(provider_id=None),
    )
    r = c.post("/batch", json={})
    assert r.status_code == 200, (
        f"Expected 200, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["batch"]["status"] == "completed"
    assert body["batch"]["model_provider_id"] is None
    assert body["batch"]["model_id"] == "test-model"


def test_create_batch_without_extraction_pipeline_returns_503(
    client_no_extraction,
):
    """batch_service is None -> 503."""
    r = client_no_extraction.post("/batch", json={})
    assert r.status_code == 503
    assert r.json()["error"] == "extraction_unavailable"


# ===========================================================================
# GET /batch
# ===========================================================================


def test_list_batches_empty(client_factory):
    """No batches -> 200 with empty list and zero totals."""
    c, _ = client_factory([])
    r = c.get("/batch")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "batch_list"
    assert body["batches"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_list_batches_returns_created(client_factory):
    """Two creates -> two rows in the list, newest first."""
    c, _ = client_factory([_make_candidate("c1")])
    c.post("/batch", json={})
    c.post("/batch", json={})

    r = c.get("/batch")
    body = r.json()
    assert body["total"] == 2
    assert len(body["batches"]) == 2
    for prev, curr in zip(body["batches"], body["batches"][1:]):
        assert prev["created_at"] >= curr["created_at"]


def test_list_batches_filters_by_status(client_factory):
    """Status filter narrows correctly."""
    c, _ = client_factory([_make_candidate("c1")])
    c.post("/batch", json={})  # running
    c.post("/batch", json={})  # also running (no sessions complete them)

    # Both running
    running = c.get("/batch", params={"status": "running"}).json()
    completed = c.get(
        "/batch", params={"status": "completed"}
    ).json()
    assert running["total"] == 2
    assert completed["total"] == 0


def test_list_batches_invalid_status_returns_400(client_factory):
    """Unknown status -> structured 400."""
    c, _ = client_factory([])
    r = c.get("/batch", params={"status": "bogus"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_status"


def test_list_batches_pagination(client_factory):
    """limit + offset partition without overlap."""
    c, _ = client_factory([_make_candidate("c1")])
    for _ in range(3):
        c.post("/batch", json={})

    page1 = c.get(
        "/batch", params={"limit": 2, "offset": 0}
    ).json()
    page2 = c.get(
        "/batch", params={"limit": 2, "offset": 2}
    ).json()

    assert len(page1["batches"]) == 2
    assert page1["has_more"] is True
    assert len(page2["batches"]) == 1
    assert page2["has_more"] is False

    ids = (
        {b["batch_id"] for b in page1["batches"]}
        | {b["batch_id"] for b in page2["batches"]}
    )
    assert len(ids) == 3


def test_list_batches_without_extraction_returns_503(
    client_no_extraction,
):
    r = client_no_extraction.get("/batch")
    assert r.status_code == 503


# ===========================================================================
# GET /batch/estimate
# ===========================================================================


def test_estimate_empty(client_factory):
    """Empty workspace -> all zeros."""
    c, _ = client_factory([])
    r = c.get("/batch/estimate")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "batch_estimate"
    assert body["estimate"]["session_count"] == 0
    assert body["estimate"]["estimated_tokens"] == 0
    assert body["estimate"]["estimated_cost_nano_usd"] == 0


def test_estimate_with_pending(client_factory):
    """2 sessions with pending turns -> nonzero estimate."""
    c, _ = client_factory([
        _make_candidate("c1", total_turns=5),
        _make_candidate("c2", total_turns=3),
    ])
    r = c.get("/batch/estimate")
    body = r.json()
    est = body["estimate"]
    assert est["session_count"] == 2
    assert est["estimated_jobs"] == 2
    assert est["total_pending_turns"] == 8
    assert est["estimated_tokens"] > 0
    assert est["estimated_cost_nano_usd"] > 0


def test_estimate_route_not_shadowed_by_batch_id(client_factory):
    """The /batch/estimate route must win over /batch/{batch_id}.

    If the router registration order were wrong, this request
    would return 404 with ``batch_id="estimate"``.
    """
    c, _ = client_factory([])
    r = c.get("/batch/estimate")
    assert r.status_code == 200
    assert r.json()["operation"] == "batch_estimate"


# ===========================================================================
# GET /batch/{batch_id}
# ===========================================================================


def test_get_batch_success(client_factory):
    """Create -> get returns the same batch plus live progress."""
    c, _ = client_factory([_make_candidate("c1")])
    created = c.post("/batch", json={}).json()
    batch_id = created["batch"]["batch_id"]

    r = c.get(f"/batch/{batch_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["batch_id"] == batch_id
    assert body["batch"]["status"] == "running"
    assert body["progress"]["total"] == 1


def test_get_batch_unknown_returns_404(client_factory):
    c, _ = client_factory([])
    r = c.get("/batch/batch_does_not_exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "batch_not_found"
    assert body["batch_id"] == "batch_does_not_exist"


def test_get_batch_self_heals_to_completed(client_factory):
    """When all jobs finish, get_batch returns COMPLETED."""
    c, h = client_factory([_make_candidate("c1")])
    created = c.post("/batch", json={}).json()
    batch_id = created["batch"]["batch_id"]

    # Complete every job in the batch manually.
    jobs = h.loop.run_until_complete(
        h.outbox.list_jobs(batch_id=batch_id)
    )
    for j in jobs:
        h.loop.run_until_complete(
            h.outbox.complete(j.job_id, JobResult())
        )

    r = c.get(f"/batch/{batch_id}")
    body = r.json()
    assert body["batch"]["status"] == "completed"


# ===========================================================================
# POST /batch/{batch_id}/pause
# ===========================================================================


def test_pause_running_batch(client_factory):
    """Running batch -> pause -> 200 with PAUSED status."""
    c, _ = client_factory(
        [_make_candidate("c1"), _make_candidate("c2")]
    )
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]

    r = c.post(f"/batch/{batch_id}/pause")
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["status"] == "paused"
    # Both jobs were pending -> both cancelled by pause.
    assert body["progress"]["by_status"].get("cancelled", 0) == 2


def test_pause_non_running_returns_409(client_factory):
    """Pause on a batch that is not RUNNING -> 409."""
    c, _ = client_factory([])
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]

    # Empty workspace -> batch is COMPLETED.
    r = c.post(f"/batch/{batch_id}/pause")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "invalid_state"


def test_pause_unknown_returns_404(client_factory):
    c, _ = client_factory([])
    r = c.post("/batch/batch_does_not_exist/pause")
    assert r.status_code == 404
    assert r.json()["error"] == "batch_not_found"


# ===========================================================================
# POST /batch/{batch_id}/resume
# ===========================================================================


def test_resume_paused_batch(client_factory):
    """Pause -> resume -> 200 with RUNNING + jobs pending again."""
    c, _ = client_factory([_make_candidate("c1")])
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]
    c.post(f"/batch/{batch_id}/pause")

    r = c.post(f"/batch/{batch_id}/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["status"] == "running"
    assert body["progress"]["pending_work"] == 1


def test_resume_non_paused_returns_409(client_factory):
    c, _ = client_factory([_make_candidate("c1")])
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]
    # Status is RUNNING, not PAUSED.
    r = c.post(f"/batch/{batch_id}/resume")
    assert r.status_code == 409


# ===========================================================================
# POST /batch/{batch_id}/cancel
# ===========================================================================


def test_cancel_running_batch(client_factory):
    """Cancel -> 200 with CANCELLED + all jobs cancelled."""
    c, _ = client_factory(
        [_make_candidate("c1"), _make_candidate("c2")]
    )
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]

    r = c.post(f"/batch/{batch_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["status"] == "cancelled"
    assert body["progress"]["by_status"].get("cancelled", 0) == 2


def test_cancel_terminal_returns_409(client_factory):
    """Cancelling an already-COMPLETED batch -> 409."""
    c, _ = client_factory([])
    batch_id = c.post("/batch", json={}).json()["batch"]["batch_id"]
    r = c.post(f"/batch/{batch_id}/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"


def test_cancel_unknown_returns_404(client_factory):
    c, _ = client_factory([])
    r = c.post("/batch/batch_does_not_exist/cancel")
    assert r.status_code == 404


# ===========================================================================
# Availability contract
# ===========================================================================


def test_all_batch_routes_return_503_without_extraction(
    client_no_extraction,
):
    """Every route 503s with the same envelope when batch_service=None."""
    endpoints = [
        ("GET", "/batch"),
        ("GET", "/batch/estimate"),
        ("GET", "/batch/batch_any"),
        ("POST", "/batch/batch_any/pause"),
        ("POST", "/batch/batch_any/resume"),
        ("POST", "/batch/batch_any/cancel"),
    ]
    for method, path in endpoints:
        r = client_no_extraction.request(method, path)
        assert r.status_code == 503, (
            f"{method} {path} -> {r.status_code}, expected 503"
        )
        assert r.json()["error"] == "extraction_unavailable"
