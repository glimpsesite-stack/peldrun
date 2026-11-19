# backend/omweb/projects/migration_engine.py

import os
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from .workspace_service import resolve_safe_path, calculate_sha256


class MigrationManifest:
    """
    Represents the result of a migration dry-run or execution.
    Serves as an audit log and a blueprint for rollback if necessary.
    """
    def __init__(self, project_id: str, session_id: str):
        self.project_id = project_id
        self.session_id = session_id
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.operations: List[Dict[str, Any]] = []
        self.conflicts: List[Dict[str, Any]] = []

    def add_operation(self, source: str, destination: str, status: str, details: Optional[str] = None):
        self.operations.append({
            "source": source,
            "destination": destination,
            "status": status,  # "pending", "success", "skipped", "conflict", "failed"
            "details": details
        })

    def add_conflict(self, source: str, destination: str, review_path: str, source_hash: str, dest_hash: str):
        self.conflicts.append({
            "source": source,
            "destination": destination,
            "review_path": review_path,
            "source_hash": source_hash,
            "dest_hash": dest_hash
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "operations": self.operations,
            "conflicts": self.conflicts
        }


class MigrationEngine:
    """
    Handles the safe migration of session files to a project workspace.
    Core Invariant: Never automatically deletes source files.
    """

    def __init__(self, session_files_root: str, workspace_root: str, project_id: str, session_id: str):
        self.session_files_root = Path(session_files_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.project_id = project_id
        self.session_id = session_id
        self.review_dir = self.workspace_root / "_migration_review"

    def dry_run(self) -> MigrationManifest:
        """
        Analyzes the session files and generates a migration manifest
        without making any actual changes to the filesystem.
        """
        manifest = MigrationManifest(self.project_id, self.session_id)

        if not self.session_files_root.exists():
            return manifest

        for source_file in self.session_files_root.rglob("*"):
            if source_file.is_file():
                relative_path = source_file.relative_to(self.session_files_root)
                dest_path = self.workspace_root / relative_path

                try:
                    # Enforce path traversal safety check
                    resolve_safe_path(str(self.workspace_root), str(relative_path))
                except PermissionError as e:
                    manifest.add_operation(str(source_file), str(dest_path), "skipped", str(e))
                    continue

                source_hash = calculate_sha256(source_file)

                if dest_path.exists() and dest_path.is_file():
                    dest_hash = calculate_sha256(dest_path)
                    if source_hash != dest_hash:
                        review_filename = f"{relative_path.stem}_conflict_{self.session_id}{relative_path.suffix}"
                        review_path = self.review_dir / relative_path.parent / review_filename

                        manifest.add_conflict(
                            str(source_file),
                            str(dest_path),
                            str(review_path),
                            source_hash,
                            dest_hash
                        )
                        manifest.add_operation(str(source_file), str(dest_path), "conflict", "Hash mismatch detected")
                    else:
                        manifest.add_operation(str(source_file), str(dest_path), "skipped", "File already exists with identical content")
                else:
                    manifest.add_operation(str(source_file), str(dest_path), "pending", "Ready to migrate")

        return manifest

    def execute(self, manifest: Optional[MigrationManifest] = None) -> MigrationManifest:
        """
        Executes the migration based on a dry-run manifest.
        If no manifest is provided, it generates one and executes it immediately.
        """
        if manifest is None:
            manifest = self.dry_run()

        # Ensure review directory exists for handling conflicts
        self.review_dir.mkdir(parents=True, exist_ok=True)

        for op in manifest.operations:
            if op["status"] == "pending":
                source_path = Path(op["source"])
                dest_path = Path(op["destination"])

                try:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    # Safe copy (preserves metadata)
                    shutil.copy2(source_path, dest_path)
                    op["status"] = "success"
                    op["details"] = "Successfully copied"
                except Exception as e:
                    op["status"] = "failed"
                    op["details"] = str(e)

            elif op["status"] == "conflict":
                source_path = Path(op["source"])
                review_path = Path(op["review_path"])

                try:
                    review_path.parent.mkdir(parents=True, exist_ok=True)
                    # Copy conflicting file to review directory with unique name
                    shutil.copy2(source_path, review_path)
                    op["status"] = "conflict_resolved"
                    op["details"] = f"Saved to review: {review_path.name}"
                except Exception as e:
                    op["status"] = "failed"
                    op["details"] = str(e)

        # Save manifest to workspace for audit and tracking purposes
        manifest_path = self.workspace_root / "migration_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)

        return manifest

    def rollback(self, manifest: MigrationManifest):
        """
        Rolls back a migration by deleting files that were successfully copied in this session.
        NOTE: This does NOT restore overwritten files, as we prevent overwrites
        by routing them to the _migration_review directory instead.
        Source files in chats/{id}/files/ are NEVER touched.
        """
        for op in manifest.operations:
            if op["status"] == "success":
                dest_path = Path(op["destination"])
                if dest_path.exists() and dest_path.is_file():
                    try:
                        dest_path.unlink()
                        op["status"] = "rolled_back"
                        op["details"] = "File removed during rollback"
                    except Exception as e:
                        op["status"] = "rollback_failed"
                        op["details"] = str(e)

        # Update manifest to reflect rollback state
        manifest_path = self.workspace_root / "migration_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)
