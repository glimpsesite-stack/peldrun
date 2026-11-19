# backend/omweb/projects/__init__.py

"""
PELDRUN Projects Subsystem.

Provides secure workspace management, migration engine, and task tracking
for project-scoped operations. This package is the foundation for Phase 1-5
of the PELDRUN Projects engineering plan.

Core Invariants:
    1. Strict domain separation: Session Files (temporary) ≠ Project Workspace (shared/persistent)
    2. Security first: All file operations use Secure Path Resolution (prevents Path Traversal,
       Symlink attacks, and TOCTOU vulnerabilities)
    3. Data safety: No automatic deletion of source files during migration.
       Optimistic Concurrency Control (OCC) prevents conflicting writes.
    4. Source of truth: Backend is the single source of truth for file state,
       artifacts, and Git repository discovery.
    5. Smart reuse: Chat components (ChatThread, Composer) are reused as
       "Dumb Components" within project context, without code duplication.

Public API:
    - WorkspaceService: Secure file operations with OCC
    - MigrationEngine: Safe session-to-project file migration with rollback
    - TaskManager: SQLite-based task management with audit logging
    - resolve_safe_path: Path traversal protection utility
    - calculate_sha256: File content hashing utility
"""

from .workspace_service import (
    WorkspaceService,
    resolve_safe_path,
    calculate_sha256,
)
from .migration_engine import MigrationEngine, MigrationManifest
from .task_manager import TaskManager, TaskValidationError
from .schemas import (
    WorkspaceFileReadResponse,
    WorkspaceFileUpdateRequest,
    WorkspaceFileUpdateConflictResponse,
    GitRepositoryInfo,
    ArtifactMetadataResponse,
    MigrationManifestResponse,
    MigrationOperation,
    MigrationConflict,
    TaskResponse,
    TaskRelationship,
    AuditLogEntryResponse,
)

__all__ = [
    # Services
    "WorkspaceService",
    "MigrationEngine",
    "MigrationManifest",
    "TaskManager",
    # Exceptions
    "TaskValidationError",
    # Utilities
    "resolve_safe_path",
    "calculate_sha256",
    # Schemas
    "WorkspaceFileReadResponse",
    "WorkspaceFileUpdateRequest",
    "WorkspaceFileUpdateConflictResponse",
    "GitRepositoryInfo",
    "ArtifactMetadataResponse",
    "MigrationManifestResponse",
    "MigrationOperation",
    "MigrationConflict",
    "TaskResponse",
    "TaskRelationship",
    "AuditLogEntryResponse",
]
