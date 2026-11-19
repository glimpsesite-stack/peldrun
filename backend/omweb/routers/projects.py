# backend/omweb/routers/projects.py

"""
PELDRUN Projects API Router.

Provides REST endpoints for project workspace management, file operations,
Git repository discovery, migration, and task tracking.

Endpoints:
    - GET    /api/projects/{project_id}/workspace/file
    - PUT    /api/projects/{project_id}/workspace/file
    - GET    /api/projects/{project_id}/workspace/files
    - DELETE /api/projects/{project_id}/workspace/file
    - GET    /api/projects/{project_id}/source-control/repositories
    - GET    /api/projects/{project_id}/source-control/repositories/{repo_id}/status
    - GET    /api/projects/{project_id}/artifacts
    - GET    /api/projects/{project_id}/artifacts/{artifact_id}
    - POST   /api/projects/{project_id}/migration/dry-run
    - POST   /api/projects/{project_id}/migration/execute
    - POST   /api/projects/{project_id}/migration/rollback
    - GET    /api/projects/{project_id}/tasks
    - GET    /api/projects/{project_id}/tasks/{task_id}
    - POST   /api/projects/{project_id}/tasks
    - PATCH  /api/projects/{project_id}/tasks/{task_id}/status
    - GET    /api/projects/{project_id}/audit-log
"""

from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import os
from pathlib import Path

from omweb.projects import (
    WorkspaceService,
    MigrationEngine,
    TaskManager,
    WorkspaceFileReadResponse,
    WorkspaceFileUpdateRequest,
    GitRepositoryInfo,
    ArtifactMetadataResponse,
    MigrationManifestResponse,
    TaskResponse,
    AuditLogEntryResponse,
)
from omweb.project_manager import project_manager
from omweb.config import STORAGE_ROOT

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ==============================================================================
# Helper Functions
# ==============================================================================

def _get_project_workspace_path(project_id: str) -> Path:
    """
    Retrieves the workspace path for a project.
    If not set, defaults to storage/projects/{project_id}/workspace
    """
    project = project_manager.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    workspace_path = project.get("workspace_path", "")
    if not workspace_path:
        # Default workspace path
        workspace_path = str(STORAGE_ROOT / "projects" / project_id / "workspace")
        # Update project metadata
        project["workspace_path"] = workspace_path
        # Note: In production, you'd want to persist this update

    return Path(workspace_path).resolve()


def _get_workspace_service(project_id: str) -> WorkspaceService:
    """Creates a WorkspaceService instance for the given project."""
    workspace_path = _get_project_workspace_path(project_id)
    return WorkspaceService(str(workspace_path))


def _get_task_manager() -> TaskManager:
    """Creates a TaskManager instance using the canonical storage path."""
    db_path = STORAGE_ROOT / "projects_tasks.db"
    return TaskManager(str(db_path))


# ==============================================================================
# Directory Browser (for External workspace folder picker)
# ==============================================================================

def _list_common_roots() -> List[Dict[str, Any]]:
    """Return platform-appropriate workspace shortcuts.

    Windows: every existing drive letter (C:\\, D:\\, ...).
    macOS/Linux: home directory plus common mount roots.

    Non-existent paths are silently filtered so the caller always
    receives a usable list.
    """
    roots: List[Dict[str, Any]] = []

    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            try:
                if os.path.exists(drive):
                    roots.append({
                        "name": f"{letter}:",
                        "path": drive,
                        "is_dir": True,
                    })
            except OSError:
                continue
        return roots

    # Unix-like: try a curated list, keep only what actually exists.
    candidates = [
        str(Path.home()),
        "/",
        "/home",
        "/Users",      # macOS
        "/Volumes",    # macOS external mounts
        "/mnt",
        "/media",
        "/opt",
    ]
    seen: set[str] = set()
    for c in candidates:
        try:
            p = Path(c).expanduser()
            if not p.exists() or not p.is_dir():
                continue
            p = p.resolve()
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            roots.append({
                "name": key,
                "path": key,
                "is_dir": True,
            })
        except (OSError, RuntimeError):
            continue
    return roots


@router.get("/browse-directories")
async def browse_directories(
    path: Optional[str] = Query(
        default=None,
        description=(
            "Absolute directory path to list. Omit to get the list "
            "of platform shortcuts (drives on Windows, home/roots "
            "on Unix)."
        ),
    ),
):
    """List immediate subdirectories of ``path`` for the folder picker.

    Cross-platform: uses ``pathlib`` only, no shell commands.

    Response shape::

        {
            "current":  str,           # resolved absolute path, or ""
            "parent":   str | None,    # absolute parent, None if at root
            "entries":  [{"name": str, "path": str, "is_dir": true}, ...],
            "roots":    [{"name": str, "path": str, "is_dir": true}, ...]
        }

    ``roots`` is always present so the picker can render a persistent
    sidebar of shortcuts regardless of the current browsing depth.

    Errors:
        400 - the path is syntactically invalid
        404 - the path does not exist or is not a directory
        403 - the directory exists but cannot be read
    """
    roots = _list_common_roots()

    # ── No path → return shortcuts only ──
    if not path:
        return {
            "current": "",
            "parent": None,
            "entries": [],
            "roots": roots,
        }

    # ── Resolve and validate the target ──
    try:
        target = Path(path).expanduser()
        if not target.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Directory not found: {path}",
            )
        if not target.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Not a directory: {path}",
            )
        target = target.resolve()
    except HTTPException:
        raise
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid path: {path} ({exc})",
        )

    # ── Enumerate immediate subdirectories ──
    # Windows-only system directories are silently skipped: they
    # cannot be opened without admin privileges and would only
    # surface "Permission denied" errors during normal browsing.
    windows_blocklist = {
        "System Volume Information",
        "$Recycle.Bin",
        "Recovery",
        "Config.Msi",
    }

    entries: List[Dict[str, Any]] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            try:
                if not child.is_dir():
                    continue
                if os.name == "nt" and child.name in windows_blocklist:
                    continue
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": True,
                })
            except (PermissionError, OSError):
                # Individual entries can be inaccessible (bad symlinks,
                # junctions, ACL-restricted dirs). Skip rather than
                # abort the whole listing.
                continue
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {target}",
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read directory: {exc}",
        )

    # ── Parent path (None at filesystem root) ──
    parent: Optional[str] = None
    try:
        if target.parent != target:
            parent = str(target.parent)
    except (OSError, RuntimeError):
        parent = None

    return {
        "current": str(target),
        "parent": parent,
        "entries": entries,
        "roots": roots,
    }


# ==============================================================================
# Project Metadata (update)
# ==============================================================================

class ProjectUpdateRequest(BaseModel):
    """Payload for PATCH /api/projects/{project_id}.

    All fields are optional; only fields actually present in the
    request body are applied to the persisted record. ``description``
    intentionally allows empty strings (the frontend clears the
    description this way). ``name`` requires at least one character
    because an empty name would break sidebar rendering.
    """
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    workspace_path: Optional[str] = None
    workspace_type: Optional[str] = None


@router.patch("/{project_id}")
async def update_project(project_id: str, req: ProjectUpdateRequest):
    """Apply a partial update to a project's metadata.

    Returns the freshly-persisted project dict (not wrapped) so that
    the frontend can merge it into its Zustand store with a single
    object-spread — the frontend already expects this shape.
    """
    if not project_manager.get_project(project_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}",
        )

    updates = req.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided to update",
        )

    updated = project_manager.update_project(project_id, **updates)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist project update",
        )

    return updated


# ==============================================================================
# Workspace File Operations
# ==============================================================================

@router.get("/{project_id}/workspace/file", response_model=WorkspaceFileReadResponse)
async def read_workspace_file(
    project_id: str,
    path: str = Query(..., description="Relative path of the file within the workspace")
):
    """
    Reads a file from the project workspace securely.
    Returns file content, version hash, and MIME type.
    """
    service = _get_workspace_service(project_id)
    return service.read_file(path)


@router.put("/{project_id}/workspace/file", response_model=WorkspaceFileReadResponse)
async def update_workspace_file(
    project_id: str,
    request: WorkspaceFileUpdateRequest
):
    """
    Updates a file in the project workspace with Optimistic Concurrency Control.
    Returns 409 Conflict if the file has been modified since the expected version.
    """
    service = _get_workspace_service(project_id)
    return service.update_file(
        relative_path=request.path,
        content=request.content,
        expected_version=request.expected_version
    )


@router.get("/{project_id}/workspace/files")
async def list_workspace_files(
    project_id: str,
    path: str = Query(default="", description="Relative path to list files from")
):
    """
    Lists all files in the project workspace.
    Returns a list of relative file paths.
    """
    workspace_path = _get_project_workspace_path(project_id)

    if not workspace_path.exists():
        return []

    target_dir = workspace_path / path if path else workspace_path

    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Directory not found: {path}"
        )

    files = []
    for file_path in target_dir.rglob("*"):
        if file_path.is_file():
            relative_path = file_path.relative_to(workspace_path)
            files.append(str(relative_path))

    return sorted(files)


@router.delete("/{project_id}/workspace/file")
async def delete_workspace_file(
    project_id: str,
    path: str = Query(..., description="Relative path of the file to delete")
):
    """
    Deletes a file from the project workspace.
    """
    from omweb.projects.workspace_service import resolve_safe_path

    workspace_path = _get_project_workspace_path(project_id)
    safe_path = resolve_safe_path(str(workspace_path), path)

    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}"
        )

    try:
        safe_path.unlink()
        return {"status": "deleted", "path": path}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file: {str(e)}"
        )


# ==============================================================================
# Git Repository Discovery
# ==============================================================================

@router.get("/{project_id}/source-control/repositories", response_model=List[GitRepositoryInfo])
async def discover_git_repositories(project_id: str):
    """
    Discovers all Git repositories within the project workspace.
    Searches for .git directories or files (worktrees) up to 3 levels deep.
    """
    import subprocess

    workspace_path = _get_project_workspace_path(project_id)

    if not workspace_path.exists():
        return []

    repositories = []

    # Search for .git directories/files up to 3 levels deep
    for git_path in workspace_path.rglob(".git"):
        # Limit depth to 3 levels
        relative_depth = len(git_path.relative_to(workspace_path).parts)
        if relative_depth > 3:
            continue

        # Determine repository root
        if git_path.is_dir():
            repo_root = git_path.parent
        elif git_path.is_file():
            # Worktree: .git is a file containing the path to the actual git dir
            repo_root = git_path.parent
        else:
            continue

        # Get repository info
        try:
            # Get current branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5
            )
            current_branch = result.stdout.strip() if result.returncode == 0 else None

            # Get git status
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5
            )
            git_status = "clean" if result.returncode == 0 and not result.stdout.strip() else "dirty"

            repo_id = f"repo_{len(repositories)}"
            repo_name = repo_root.name

            repositories.append(GitRepositoryInfo(
                id=repo_id,
                name=repo_name,
                root_path=str(repo_root),
                git_status=git_status,
                current_branch=current_branch
            ))
        except Exception as e:
            # Skip repositories that can't be inspected
            continue

    return repositories


@router.get("/{project_id}/source-control/repositories/{repo_id}/status", response_model=GitRepositoryInfo)
async def get_repository_status(project_id: str, repo_id: str):
    """
    Gets the current status of a specific repository.
    """
    repos = await discover_git_repositories(project_id)

    for repo in repos:
        if repo.id == repo_id:
            return repo

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Repository not found: {repo_id}"
    )


# ==============================================================================
# Artifact Metadata
# ==============================================================================

@router.get("/{project_id}/artifacts", response_model=List[ArtifactMetadataResponse])
async def get_project_artifacts(
    project_id: str,
    session_id: Optional[str] = Query(default=None, description="Filter by session ID")
):
    """
    Retrieves artifact metadata for a project.
    Optionally filters by session ID.
    """
    # TODO: Implement artifact metadata retrieval from storage
    # For now, return empty list
    return []


@router.get("/{project_id}/artifacts/{artifact_id}", response_model=ArtifactMetadataResponse)
async def get_artifact(project_id: str, artifact_id: str):
    """
    Retrieves a specific artifact by ID.
    """
    # TODO: Implement artifact metadata retrieval
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Artifact not found: {artifact_id}"
    )


# ==============================================================================
# Migration Engine
# ==============================================================================

@router.post("/{project_id}/migration/dry-run", response_model=MigrationManifestResponse)
async def migration_dry_run(
    project_id: str,
    session_id: str = Query(..., description="Session ID to migrate from")
):
    """
    Performs a dry-run migration analysis.
    Returns a manifest showing what would be migrated without making changes.
    """
    workspace_path = _get_project_workspace_path(project_id)

    # Get session files directory
    session_files_dir = project_manager.get_chat_files_dir(session_id)

    engine = MigrationEngine(
        session_files_root=str(session_files_dir),
        workspace_root=str(workspace_path),
        project_id=project_id,
        session_id=session_id
    )

    manifest = engine.dry_run()

    return MigrationManifestResponse(**manifest.to_dict())


@router.post("/{project_id}/migration/execute", response_model=MigrationManifestResponse)
async def migration_execute(
    project_id: str,
    session_id: str = Query(..., description="Session ID to migrate from")
):
    """
    Executes the migration based on a dry-run analysis.
    """
    workspace_path = _get_project_workspace_path(project_id)
    session_files_dir = project_manager.get_chat_files_dir(session_id)

    engine = MigrationEngine(
        session_files_root=str(session_files_dir),
        workspace_root=str(workspace_path),
        project_id=project_id,
        session_id=session_id
    )

    manifest = engine.execute()

    return MigrationManifestResponse(**manifest.to_dict())


@router.post("/{project_id}/migration/rollback", response_model=MigrationManifestResponse)
async def migration_rollback(
    project_id: str,
    manifest: MigrationManifestResponse
):
    """
    Rolls back a previous migration.
    """
    workspace_path = _get_project_workspace_path(project_id)

    # Create a temporary engine just for rollback
    engine = MigrationEngine(
        session_files_root="",  # Not needed for rollback
        workspace_root=str(workspace_path),
        project_id=project_id,
        session_id=manifest.session_id
    )

    # Convert Pydantic model to dict for MigrationEngine
    from omweb.projects.migration_engine import MigrationManifest
    manifest_obj = MigrationManifest(manifest.project_id, manifest.session_id)
    manifest_obj.operations = [op.dict() for op in manifest.operations]
    manifest_obj.conflicts = [c.dict() for c in manifest.conflicts]

    engine.rollback(manifest_obj)

    return manifest


# ==============================================================================
# Task Management
# ==============================================================================

@router.get("/{project_id}/tasks", response_model=List[TaskResponse])
async def get_project_tasks(
    project_id: str,
    status: Optional[str] = Query(default=None, description="Filter by task status")
):
    """
    Retrieves all tasks for a project, optionally filtered by status.
    """
    task_manager = _get_task_manager()
    tasks = task_manager.get_project_tasks(project_id, status)

    return [TaskResponse(**task) for task in tasks]


@router.get("/{project_id}/tasks/{task_id}", response_model=TaskResponse)
async def get_task(project_id: str, task_id: str):
    """
    Retrieves a specific task by ID.
    """
    task_manager = _get_task_manager()
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}"
        )

    if task["project_id"] != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found in project: {task_id}"
        )

    return TaskResponse(**task)


@router.post("/{project_id}/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    project_id: str,
    task: TaskResponse
):
    """
    Creates a new task in the project.
    """
    import uuid

    task_manager = _get_task_manager()
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    created_task = task_manager.create_task(
        task_id=task_id,
        project_id=project_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        assignee=task.assignee,
        metadata=task.metadata
    )

    return TaskResponse(**created_task)


@router.patch("/{project_id}/tasks/{task_id}/status", response_model=TaskResponse)
async def update_task_status(
    project_id: str,
    task_id: str,
    status: str = Query(..., description="New task status"),
    actor: str = Query(default="user", description="Actor making the change"),
    reason: Optional[str] = Query(default=None, description="Reason for status change")
):
    """
    Updates task status with validation and audit logging.
    """
    task_manager = _get_task_manager()

    try:
        updated_task = task_manager.update_task_status(
            task_id=task_id,
            new_status=status,
            actor=actor,
            reason=reason
        )

        if updated_task["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task not found in project: {task_id}"
            )

        return TaskResponse(**updated_task)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/{project_id}/audit-log", response_model=List[AuditLogEntryResponse])
async def get_audit_log(
    project_id: str,
    task_id: Optional[str] = Query(default=None, description="Filter by task ID"),
    limit: int = Query(default=100, description="Maximum number of entries to return")
):
    """
    Retrieves audit log entries for a project or specific task.
    """
    task_manager = _get_task_manager()
    entries = task_manager.get_audit_log(task_id, limit)

    return [AuditLogEntryResponse(**entry) for entry in entries]
