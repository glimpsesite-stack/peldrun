"""Smoke test for BatchScheduler integration. Delete after use."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from omweb.memory.jobs.batch_scheduler import BatchScheduler
from omweb.memory.batch.models import MemoryBatchStatus


async def test_lifecycle():
    service = MagicMock()
    service.batch_service = None
    scheduler = BatchScheduler(service)
    await scheduler.start()
    await scheduler.start()  # idempotent
    assert scheduler.is_running
    await scheduler.stop(timeout_seconds=2)
    await scheduler.stop(timeout_seconds=2)
    assert not scheduler.is_running
    print("OK lifecycle: idempotent start/stop")


async def test_none_schedule():
    service = MagicMock()
    service.settings.ai.batch_enabled = True
    service.settings.ai.batch_auto_schedule_hours = None
    service.batch_service = MagicMock()
    service.batch_service.create_batch = AsyncMock()
    service.batch_service.list_batches = AsyncMock(return_value=[])

    scheduler = BatchScheduler(service)
    scheduler._DEFAULT_POLL_INTERVAL_SECONDS = 0.05
    await scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop(timeout_seconds=1)

    assert service.batch_service.create_batch.call_count == 0
    print("OK none schedule: no batch created")


async def test_batch_created():
    service = MagicMock()
    service.settings.ai.batch_enabled = True
    service.settings.ai.batch_auto_schedule_hours = 1
    service.batch_service = MagicMock()
    fake_batch = MagicMock()
    fake_batch.batch_id = "batch_test123"
    fake_batch.status = MemoryBatchStatus.RUNNING
    fake_batch.job_count = 5
    service.batch_service.create_batch = AsyncMock(return_value=fake_batch)
    service.batch_service.list_batches = AsyncMock(return_value=[])

    scheduler = BatchScheduler(service)

    async def _instant_wait(seconds):
        return False

    scheduler._wait = _instant_wait
    await scheduler.start()
    await asyncio.sleep(0.3)
    await scheduler.stop(timeout_seconds=1)

    assert service.batch_service.create_batch.call_count >= 1
    print(f"OK batch created: create_batch called "
          f"{service.batch_service.create_batch.call_count}x")


async def test_running_batch_skipped():
    """Guard: existing RUNNING batch prevents new creation."""
    service = MagicMock()
    service.settings.ai.batch_enabled = True
    service.settings.ai.batch_auto_schedule_hours = 1
    service.batch_service = MagicMock()
    existing = MagicMock()
    existing.batch_id = "batch_existing"
    service.batch_service.create_batch = AsyncMock()
    service.batch_service.list_batches = AsyncMock(
        side_effect=lambda status, limit: (
            [existing] if status == MemoryBatchStatus.RUNNING else []
        )
    )

    scheduler = BatchScheduler(service)

    async def _instant_wait(seconds):
        return False

    scheduler._wait = _instant_wait
    await scheduler.start()
    await asyncio.sleep(0.3)
    await scheduler.stop(timeout_seconds=1)

    assert service.batch_service.create_batch.call_count == 0
    print("OK running batch skipped: no new batch created")


async def test_interruptible_sleep():
    service = MagicMock()
    service.settings.ai.batch_enabled = True
    service.settings.ai.batch_auto_schedule_hours = 24
    service.batch_service = MagicMock()
    service.batch_service.list_batches = AsyncMock(return_value=[])
    service.batch_service.create_batch = AsyncMock()

    scheduler = BatchScheduler(service)
    await scheduler.start()
    await asyncio.sleep(0.1)
    import time
    t0 = time.time()
    await scheduler.stop(timeout_seconds=2)
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"stop took {elapsed}s (expected <1.5)"
    print(f"OK interruptible sleep: stop took {elapsed:.2f}s")


async def main():
    await test_lifecycle()
    await test_none_schedule()
    await test_batch_created()
    await test_running_batch_skipped()
    await test_interruptible_sleep()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
