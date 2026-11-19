# backend/omweb/projects/workspace_service.py

import os
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, status

from .schemas import (
    WorkspaceFileReadResponse,
    WorkspaceFileUpdateRequest,
    WorkspaceFileUpdateConflictResponse,
)


def calculate_sha256(file_path: Path) -> str:
    """
    Calculates the SHA256 hash of a file's content.
    Returns an empty string if the file does not exist.
    """
    if not file_path.exists() or not file_path.is_file():
        return ""

    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def resolve_safe_path(workspace_root: str, requested_path: str) -> Path:
    """
    Resolves a requested path securely within the workspace root.
    Prevents Path Traversal and Symlink attacks.
    """
    root_resolved = Path(workspace_root).resolve()
    target_resolved = (root_resolved / requested_path).resolve()

    # Ensure the resolved target is strictly within the workspace root
    # is_relative_to is available in Python 3.9+ and is the safest method
    if not target_resolved.is_relative_to(root_resolved):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Path traversal attempt detected."
        )

    return target_resolved


class WorkspaceService:
    """
    Service responsible for secure workspace file operations
    with Optimistic Concurrency Control (OCC).
    """

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()
        # Ensure workspace root exists
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def read_file(self, relative_path: str) -> WorkspaceFileReadResponse:
        """
        Reads a file from the workspace securely.
        """
        safe_path = resolve_safe_path(str(self.workspace_root), relative_path)

        if not safe_path.exists() or not safe_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File not found: {relative_path}"
            )

        # Read content
        try:
            content = safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is not a valid UTF-8 text file."
            )

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(safe_path))
        mime_type = mime_type or "application/octet-stream"

        # Calculate current version hash
        current_version = calculate_sha256(safe_path)

        return WorkspaceFileReadResponse(
            path=relative_path,
            content=content,
            version=current_version,
            mime_type=mime_type
        )

    def update_file(
        self,
        relative_path: str,
        content: str,
        expected_version: str
    ) -> WorkspaceFileReadResponse:
        """
        Updates a file in the workspace using Optimistic Concurrency Control (OCC).
        Uses atomic write (os.replace) to prevent TOCTOU vulnerabilities.
        """
        safe_path = resolve_safe_path(str(self.workspace_root), relative_path)

        # Check current state
        current_version = calculate_sha256(safe_path)

        # OCC Check: If the file exists and the hash doesn't match, reject the update
        if safe_path.exists() and current_version != expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=WorkspaceFileUpdateConflictResponse(
                    error="Conflict",
                    message="File has been modified by another process or user.",
                    current_version=current_version
                ).model_dump()
            )

        # Ensure parent directory exists
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic Write: Write to a temporary file first, then replace
        temp_path = safe_path.with_suffix(safe_path.suffix + ".tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            # os.replace is atomic on POSIX systems and most modern Windows setups
            os.replace(temp_path, safe_path)
        except Exception as e:
            # Clean up temp file if something goes wrong
            if temp_path.exists():
                temp_path.unlink()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to write file: {str(e)}"
            )

        # Return the new state
        new_version = calculate_sha256(safe_path)
        mime_type, _ = mimetypes.guess_type(str(safe_path))
        mime_type = mime_type or "application/octet-stream"

        return WorkspaceFileReadResponse(
            path=relative_path,
            content=content,
            version=new_version,
            mime_type=mime_type
        )
