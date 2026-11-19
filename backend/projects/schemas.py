# backend/omweb/projects/schemas.py

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

# ==============================================================================
# 1. Secure Workspace File Operations (Optimistic Concurrency Control)
# ==============================================================================

class WorkspaceFileReadResponse(BaseModel):
    """Schema for returning file content and metadata from the workspace."""
    path: str = Field(..., description="Relative path of the file within the workspace")
    content: str = Field(..., description="Content of the file")
    version: str = Field(..., description="SHA256 hash of the current file content")
    mime_type: str = Field(..., description="MIME type of the file")


class WorkspaceFileUpdateRequest(BaseModel):
    """Schema for updating a file in the workspace with OCC."""
    path: str = Field(..., description="Relative path of the file within the workspace")
    content: str = Field(..., description="New content of the file")
    expected_version: str = Field(..., description="Expected SHA256 hash for Optimistic Concurrency Control")


class WorkspaceFileUpdateConflictResponse(BaseModel):
    """Schema returned when an OCC conflict (409) occurs."""
    error: str = Field(default="Conflict", description="Error type identifier")
    message: str = Field(..., description="Human-readable conflict message")
    current_version: str = Field(..., description="Current SHA256 hash of the file on the server")


# ==============================================================================
# 2. Git Repository Discovery
# ==============================================================================

class GitRepositoryInfo(BaseModel):
    """Schema representing a discovered Git repository within the project workspace."""
    id: str = Field(..., description="Unique identifier for the repository within the project")
    name: str = Field(..., description="Name of the repository (e.g., directory name)")
    root_path: str = Field(..., description="Absolute safe path to the repository root")
    git_status: str = Field(..., description="Current git status, e.g., 'clean' or 'dirty'")
    current_branch: Optional[str] = Field(default=None, description="Current active branch name")


# ==============================================================================
# 3. Artifact Metadata
# ==============================================================================

class ArtifactMetadataResponse(BaseModel):
    """Schema for artifact metadata, serving as the source of truth."""
    artifact_id: str = Field(..., description="Unique identifier for the artifact")
    project_id: str = Field(..., description="ID of the project this artifact belongs to")
    session_id: Optional[str] = Field(default=None, description="ID of the chat session, null if purely project-scoped")
    job_id: str = Field(..., description="ID of the job that generated this artifact")
    storage_scope: str = Field(..., description="Scope of storage, e.g., 'project_workspace' or 'session_storage'")
    artifact_type: str = Field(..., description="MIME type or category of the artifact")
    relative_path: str = Field(..., description="Relative path within the storage scope")
    created_at: datetime = Field(..., description="ISO 8601 timestamp of artifact creation")
