# backend/tests/memory/test_memory_jobs_api.py
"""
Phase 3 - Extraction Jobs HTTP API test suite.

Covers the four endpoints in ``omweb/memory/api/memory_jobs.py``
against a real ``MemoryOutbox`` mounted on a minimal FastAPI
app. No network, no live server -- ``TestClient`` drives the
ASGI app in-process.

Test isolation strategy
-----------------------
The router reads the singleton via ``get_memory_service``, which
raises when no service is installed. Tests use
``set_memory_service_for_tests`` to inject a minimal fake
service that exposes ONLY the ``.outbox`` property (which is
all the router accesses). Teardown nulls the singleton so no
state leaks between tests.

Run with::

    pytest backend/tests/memory/test_memory_jobs_api.py -v
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)

from omweb.memory import set_memory_service_for_tests
from omweb.memory.api.memory_jobs import router as jobs_router
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
)
from omweb.memory.jobs.outbox import (
    JobResult,
    MemoryOutbox,
)
from omweb.memory.storage.migrations import apply_schema


# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal ``MemoryService`` stand-in.

    The jobs router only reads ``service.outbox``. A full
    ``MemoryService`` construction would drag in lifecycle,
    extraction, and settings dependencies that are irrelevant
    to these tests. The duck-typed stand-in keeps tests fast
    and focused on the router's contract.
    """

    def __init__(self, outbox: Optional[MemoryOutbox]) -> None:
        self.outbox = outbox


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    """Temp DB with the full schema applied."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory_test.db"
        conn = sqlite3.connect(str(path))
        try:
            apply_schema(conn)
        finally:
            conn.close()
        yield path


@pytest.fixture
def client(db_path):
    """``TestClient`` with a live outbox installed on the fake service.

    **Why this fixture owns its own event loop**
    ============================================
    The naive pattern ``asyncio.get_event_loop()`` is BROKEN when
    another test file in the same pytest session uses
    ``pytest-asyncio`` in ``asyncio_mode=auto``:

      1. pytest-asyncio runs each ``async def test_*`` on a
         per-test loop it creates and CLOSES on teardown.
      2. Teardown also calls ``asyncio.set_event_loop(None)``,
         which sets the thread-local ``_set_called = True`` flag.
      3. In Python 3.12+, the next ``asyncio.get_event_loop()``
         call in the SAME thread sees ``_set_called = True`` and
         raises ``RuntimeError("There is no current event loop")``
         instead of auto-creating one.

    The result: this fixture's file passes when run alone (the
    ``get_event_loop()`` auto-creation path fires, with a
    DeprecationWarning), but fails with 17 errors when run AFTER
    ``test_outbox_phase3.py`` (which contains async tests).

    The fix: create an explicit loop with
    ``asyncio.new_event_loop()``. Do NOT call
    ``asyncio.set_event_loop()`` -- we deliberately avoid
    polluting the main thread's current-loop state, so no OTHER
    test file can be affected by our fixture's lifecycle.

    **Why the shared ``asyncio.Lock`` still works**
    ===============================================
    ``MemoryOutbox._lock`` is an ``asyncio.Lock`` created at
    construction. In Python 3.10+, an UNCONTESTED ``acquire()``
    does NOT bind to a loop -- it just flips ``_locked = True``
    and returns. Loop binding happens only on contention.

    Our tests are fully sequential (``_seed_job`` -> ``client.get``
    -> ``_seed_job`` -> ...), so every lock acquisition is
    uncontested. The fixture's loop and ``TestClient``'s internal
    portal loop can share the lock safely.
    """
    # Fresh loop for this test ONLY. Local scope; never installed
    # as the thread's current loop (see docstring for why).
    loop = asyncio.new_event_loop()

    outbox = MemoryOutbox(db_path)
    loop.run_until_complete(outbox.open())

    set_memory_service_for_tests(_FakeService(outbox))

    app = FastAPI()
    app.include_router(jobs_router)

    try:
        with TestClient(app) as c:
            # Expose the outbox so tests can seed jobs directly.
            c._test_outbox = outbox
            c._test_event_loop = loop
            yield c
    finally:
        set_memory_service_for_tests(None)
        try:
            loop.run_until_complete(outbox.close())
        finally:
            # Close the loop to release its resources. Safe even
            # if close() raised -- the `finally` guarantees it.
            loop.close()


@pytest.fixture
def client_no_extraction():
    """``TestClient`` with a fake service whose outbox is None.

    Simulates "AI extraction is disabled". Every endpoint must
    return 503 with ``error="extraction_unavailable"``.
    """
    set_memory_service_for_tests(_FakeService(None))
    app = FastAPI()
    app.include_router(jobs_router)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        set_memory_service_for_tests(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_job(
    client: TestClient,
    *,
    chat_id: str = "chat_1",
    trigger: ExtractionTrigger = ExtractionTrigger.MANUAL,
    start: int = 0,
    end: int = 1,
    idem: Optional[str] = None,
) -> MemoryJob:
    """Enqueue a job directly via the outbox (bypassing the router)."""
    job = MemoryJob(
        chat_id=chat_id,
        scope_type=MemoryScope.CHAT,
        scope_id=chat_id,
        trigger_type=trigger,
        start_turn_index=start,
        end_turn_index=end,
        message_snapshot_hash="cafebabe" * 8,
        model_provider_id="lmstudio",
        model_id="test-model",
        idempotency_key=idem or f"key_{chat_id}_{start}_{end}",
    )
    client._test_event_loop.run_until_complete(
        client._test_outbox.enqueue(job)
    )
    return job


def _fail_job(client: TestClient, job_id: str) -> None:
    """Transition a job to failed for retry tests."""
    client._test_event_loop.run_until_complete(
        client._test_outbox.fail(job_id, "ERR_CODE", "boom")
    )


def _complete_job(client: TestClient, job_id: str) -> None:
    """Transition a job to completed for cancel-rejection tests."""
    client._test_event_loop.run_until_complete(
        client._test_outbox.complete(job_id, JobResult())
    )


# ---------------------------------------------------------------------------
# GET /jobs
# ---------------------------------------------------------------------------


def test_list_jobs_empty(client):
    """Empty table -> well-formed empty response, not an error."""
    r = client.get("/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "jobs_list"
    assert body["jobs"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["has_more"] is False


def test_list_jobs_returns_seeded_jobs(client):
    """Two seeded jobs appear in the list."""
    _seed_job(client, chat_id="c1", idem="k1")
    _seed_job(client, chat_id="c2", idem="k2")

    r = client.get("/jobs")
    body = r.json()
    assert body["total"] == 2
    assert len(body["jobs"]) == 2
    assert body["has_more"] is False


def test_list_jobs_filters_by_status(client):
    """Status filter narrows the list; string and 'all' both work."""
    job1 = _seed_job(client, chat_id="c1", idem="k1")
    _seed_job(client, chat_id="c2", idem="k2")
    _fail_job(client, job1.job_id)

    pending = client.get("/jobs", params={"status": "pending"}).json()
    failed = client.get("/jobs", params={"status": "failed"}).json()
    all_ = client.get("/jobs", params={"status": "all"}).json()

    assert len(pending["jobs"]) == 1
    assert len(failed["jobs"]) == 1
    assert len(all_["jobs"]) == 2
    assert failed["jobs"][0]["job_id"] == job1.job_id


def test_list_jobs_filters_by_chat_id(client):
    """chat_id filter narrows the list."""
    _seed_job(client, chat_id="c1", idem="k1")
    _seed_job(client, chat_id="c2", idem="k2")

    body = client.get("/jobs", params={"chat_id": "c1"}).json()
    assert body["total"] == 1
    assert body["jobs"][0]["chat_id"] == "c1"


def test_list_jobs_pagination(client):
    """limit + offset partition without overlap; has_more correct."""
    for i in range(5):
        _seed_job(client, chat_id=f"c{i}", idem=f"k{i}")

    page1 = client.get(
        "/jobs", params={"limit": 2, "offset": 0}
    ).json()
    page2 = client.get(
        "/jobs", params={"limit": 2, "offset": 2}
    ).json()
    page3 = client.get(
        "/jobs", params={"limit": 2, "offset": 4}
    ).json()

    assert len(page1["jobs"]) == 2
    assert page1["has_more"] is True
    assert len(page2["jobs"]) == 2
    assert page2["has_more"] is True
    assert len(page3["jobs"]) == 1
    assert page3["has_more"] is False

    ids = (
        {j["job_id"] for j in page1["jobs"]}
        | {j["job_id"] for j in page2["jobs"]}
        | {j["job_id"] for j in page3["jobs"]}
    )
    assert len(ids) == 5


def test_list_jobs_invalid_status_returns_400(client):
    """Unknown status -> structured 400, not FastAPI's 422."""
    r = client.get("/jobs", params={"status": "bogus"})
    assert r.status_code == 400
    body = r.json()["detail"]
    assert body["error"] == "invalid_status"


def test_list_jobs_invalid_trigger_returns_400(client):
    """Unknown trigger -> structured 400."""
    r = client.get("/jobs", params={"trigger_type": "bogus"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_trigger"


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_get_job_success(client):
    """Existing job id -> 200 with full job payload."""
    job = _seed_job(client)
    r = client.get(f"/jobs/{job.job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["job"]["job_id"] == job.job_id
    assert body["job"]["status"] == "pending"


def test_get_job_unknown_returns_404(client):
    """Unknown id -> 404, not 500."""
    r = client.get("/jobs/mjob_does_not_exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "job_not_found"
    assert body["job_id"] == "mjob_does_not_exist"


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/retry
# ---------------------------------------------------------------------------


def test_retry_failed_job_succeeds(client):
    """failed -> 200; the returned job is pending with attempts=0."""
    job = _seed_job(client)
    _fail_job(client, job.job_id)

    r = client.post(f"/jobs/{job.job_id}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "job_retry"
    assert body["job"]["status"] == "pending"
    assert body["job"]["attempts"] == 0
    assert body["job"]["error_code"] is None


def test_retry_pending_job_returns_409(client):
    """pending jobs are not terminal; retry must 409."""
    job = _seed_job(client)
    r = client.post(f"/jobs/{job.job_id}/retry")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "invalid_state"
    assert body["job_id"] == job.job_id


def test_retry_completed_job_returns_409(client):
    """completed jobs must never be retried."""
    job = _seed_job(client)
    _complete_job(client, job.job_id)

    r = client.post(f"/jobs/{job.job_id}/retry")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"


def test_retry_unknown_job_returns_404(client):
    """Unknown id -> 404, not 409."""
    r = client.post("/jobs/mjob_does_not_exist/retry")
    assert r.status_code == 404
    assert r.json()["error"] == "job_not_found"


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_pending_job_succeeds(client):
    """pending -> 200; returned job is cancelled."""
    job = _seed_job(client)
    r = client.post(f"/jobs/{job.job_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["operation"] == "job_cancel"
    assert body["job"]["status"] == "cancelled"


def test_cancel_completed_job_returns_409(client):
    """completed jobs must never be cancelled."""
    job = _seed_job(client)
    _complete_job(client, job.job_id)

    r = client.post(f"/jobs/{job.job_id}/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"


def test_cancel_failed_job_returns_409(client):
    """failed jobs must never be cancelled (would hide the diagnostic)."""
    job = _seed_job(client)
    _fail_job(client, job.job_id)

    r = client.post(f"/jobs/{job.job_id}/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"


def test_cancel_unknown_job_returns_404(client):
    """Unknown id -> 404."""
    r = client.post("/jobs/mjob_does_not_exist/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Availability -- outbox is None
# ---------------------------------------------------------------------------


def test_list_jobs_without_extraction_returns_503(client_no_extraction):
    """No extraction pipeline -> 503 with a clear message."""
    r = client_no_extraction.get("/jobs")
    assert r.status_code == 503
    assert r.json()["error"] == "extraction_unavailable"


def test_get_job_without_extraction_returns_503(client_no_extraction):
    """Same policy for the single-job endpoint."""
    r = client_no_extraction.get("/jobs/mjob_anything")
    assert r.status_code == 503


def test_retry_without_extraction_returns_503(client_no_extraction):
    r = client_no_extraction.post("/jobs/mjob_anything/retry")
    assert r.status_code == 503


def test_cancel_without_extraction_returns_503(client_no_extraction):
    r = client_no_extraction.post("/jobs/mjob_anything/cancel")
    assert r.status_code == 503
