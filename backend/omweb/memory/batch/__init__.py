# backend/omweb/memory/batch/__init__.py
"""
PELDRUN Memory Platform - Batch Subsystem Package.

Public surface of the retroactive batch extraction subsystem
(Phase 3). The module exports:

    * ``MemoryBatch``       -- the domain model (Pydantic value
                              object, mirrors ``memory_batches``).
    * ``MemoryBatchStatus`` -- the closed lifecycle-state enum.

The orchestration service (``MemoryBatchService``) lives in
``omweb.memory.batch.service`` and is NOT re-exported here to
avoid a circular import with the jobs/outbox layer (which
itself imports the models from this package). Callers that need
the service import it explicitly:

    from omweb.memory.batch.service import MemoryBatchService

Keeping the models in ``models.py`` and the service in
``service.py`` mirrors the ``omweb/memory/jobs/`` layout
(``models.py`` + ``outbox.py`` + ``worker.py``) so a reader who
knows one package knows both.
"""

from omweb.memory.batch.models import (
    MemoryBatch,
    MemoryBatchStatus,
)


__all__ = [
    "MemoryBatch",
    "MemoryBatchStatus",
]
