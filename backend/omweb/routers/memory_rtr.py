# backend/omweb/routers/memory_rtr.py
"""
PELDRUN Memory Platform - Memory API Aggregator.

Single mount point for every memory HTTP router. Mounted at
``/api/memory`` by ``omweb/main.py``.

**CRITICAL ORDERING RULE**: specific-prefix routers MUST be
registered BEFORE the generic CRUD router. ``memory_crud.py``
exposes ``GET /{memory_id}`` at the mount root, which would
otherwise shadow ``/settings``, ``/projects``, ``/import``,
``/export``, and ``/usage`` -- FastAPI matches in REGISTRATION
ORDER, not by specificity. See the ``_include_*`` sequence below.

The same rule applies to ``skills_rtr`` in ``main.py`` (catalog
before the main router) -- this is a recurring pattern in the
codebase, documented once here so future maintainers can copy
the example.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

# NOTE: ``memory_crud`` is deliberately NOT imported here. It is
# mounted DIRECTLY by ``omweb/main.py`` AFTER this router, because
# FastAPI rejects ``include_router(router, prefix="")`` when the
# child router has an empty-path route (``@router.get("")``). The
# CRUD router's list/create endpoints use "" as the path, so it
# can only be mounted at a non-empty prefix at the app level.
#
# The ordering guarantee -- CRUD's ``/{memory_id}`` catch-all must
# come AFTER settings/projects/import/export/usage -- is preserved
# by main.py's include order:
#
#     app.include_router(memory_rtr.router,    prefix="/api/memory")
#     app.include_router(memory_crud.router,   prefix="/api/memory")
#
# See main.py for the same rule applied to skills' catalog router.
from omweb.memory.api.memory_automation import (
    router as _automation_router,
)
from omweb.memory.api.memory_batch import router as _batch_router
from omweb.memory.api.memory_export import router as _export_router
from omweb.memory.api.memory_extraction import (
    router as _extraction_router,
)
from omweb.memory.api.memory_import import router as _import_router
from omweb.memory.api.memory_jobs import router as _jobs_router
from omweb.memory.api.memory_projects import router as _projects_router
from omweb.memory.api.memory_settings import router as _settings_router
from omweb.memory.api.memory_usage import router as _usage_router


logger = logging.getLogger("omweb.routers.memory")


router = APIRouter()


# ---------------------------------------------------------------------------
# Route ordering -- LOAD-BEARING
# ---------------------------------------------------------------------------
#
# DO NOT reorder without understanding the consequences. Concrete
# examples of the failure mode this prevents:
#
#     * ``GET /api/memory/settings``        -> would match
#       ``crud.GET /{memory_id}`` with memory_id="settings" -> 400
#     * ``GET /api/memory/projects``        -> memory_id="projects"
#     * ``POST /api/memory/import/preview`` -> memory_id="import"
#     * ``GET /api/memory/usage``           -> memory_id="usage"
#     * ``POST /api/memory/search``         -> memory_id="search"
#       (this one is registered INSIDE crud_router, so it would be
#        matched by name before ``/{memory_id}`` -- but only
#        because it was declared first in that file)
#
# The rule is: fixed-path prefixes first, generic ``/{x}`` last.

# 1. Settings -- fixed prefix, no ambiguity.
router.include_router(
    _settings_router,
    prefix="/settings",
    tags=["memory", "settings"],
)

# 2. Projects -- fixed prefix + ``/projects/{id}/...`` sub-paths.
#    No conflict with ``/{memory_id}`` because the CRUD router is
#    registered LAST.
router.include_router(
    _projects_router,
    prefix="/projects",
    tags=["memory", "projects"],
)

# 3. Import -- fixed prefix, two sub-paths (``/preview``, ``/apply``)
#    plus ``/adapters``.
router.include_router(
    _import_router,
    prefix="/import",
    tags=["memory", "import"],
)

# 4. Export -- fixed prefix, single GET endpoint.
router.include_router(
    _export_router,
    prefix="/export",
    tags=["memory", "export"],
)

# 5. Usage -- fixed prefix, three sub-paths (summary/breakdown/
#    timeseries).
router.include_router(
    _usage_router,
    prefix="/usage",
    tags=["memory", "usage"],
)

# 6. Automation -- Phase 3 AI extraction control surface.
#
# All four sub-routers are mounted under the SAME /automation
# prefix. The resulting URL namespace is:
#
#     /automation/settings             (automation: GET, PATCH)
#     /automation/test-model           (automation: POST)
#     /automation/jobs                 (jobs: GET)
#     /automation/jobs/{id}            (jobs: GET)
#     /automation/jobs/{id}/retry      (jobs: POST)
#     /automation/jobs/{id}/cancel     (jobs: POST)
#     /automation/extract/from-session (extraction: POST)
#     /automation/extract/range        (extraction: POST)
#     /automation/batch                (batch: GET, POST)
#     /automation/batch/estimate       (batch: GET)
#     /automation/batch/{id}           (batch: GET)
#     /automation/batch/{id}/pause     (batch: POST)
#     /automation/batch/{id}/resume    (batch: POST)
#     /automation/batch/{id}/cancel    (batch: POST)
#
# Route-ordering note: each sub-router handles its OWN ordering
# (see each module's "Route-ordering contract" docstring --
# especially ``memory_batch.py``, where ``/batch/estimate`` MUST
# precede ``/batch/{batch_id}``). Across sub-routers, no path
# overlap exists: ``/settings``, ``/jobs``, ``/extract``, and
# ``/batch`` are disjoint fixed prefixes, so their mutual
# registration order is irrelevant for correctness. We register
# them as one contiguous block for readability.
#
# Collision-safety with ``memory_crud``: every route in this
# block has at least TWO path segments after ``/api/memory``
# (``automation`` + at least one more). The CRUD router's
# ``/{memory_id}`` catch-all matches a SINGLE segment, so it
# cannot match ``/automation/settings`` (2 segments) or
# ``/automation/jobs/{id}`` (3 segments). No shadowing is
# possible, regardless of mount order in ``main.py``.

# 6a. Automation -- /automation/settings + /automation/test-model.
router.include_router(
    _automation_router,
    prefix="/automation",
    tags=["memory", "automation"],
)

# 6b. Jobs -- /automation/jobs + per-job actions.
router.include_router(
    _jobs_router,
    prefix="/automation",
    tags=["memory", "automation", "jobs"],
)

# 6c. Extraction -- /automation/extract/from-session, /range.
router.include_router(
    _extraction_router,
    prefix="/automation",
    tags=["memory", "automation", "extraction"],
)

# 6d. Batch -- /automation/batch + per-batch actions.
router.include_router(
    _batch_router,
    prefix="/automation",
    tags=["memory", "automation", "batch"],
)

# CRUD is mounted separately in main.py -- see the import-block
# comment above for why. This aggregator only carries the
# fixed-prefix routers (settings / projects / import / export /
# usage). Those are guaranteed to be registered BEFORE the CRUD
# catch-all because main.py includes this router first.
#
# Any future fixed-prefix router (e.g. ``/audit``) belongs here,
# ABOVE the comment block that points at main.py. Any future
# catch-all router (e.g. ``/{token_id}``) belongs in main.py,
# AFTER the memory_crud include, mirroring CRUD's placement.


__all__ = ["router"]
