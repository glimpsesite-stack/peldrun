# backend/omweb/projects/task_manager.py

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from contextlib import contextmanager


class TaskValidationError(Exception):
    """Raised when a task state transition is invalid."""
    pass


class TaskManager:
    """
    SQLite-based task management with comprehensive audit logging.
    Supports task relationships, state transitions validation, and full traceability.
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    @contextmanager
    def _get_connection(self):
        """Context manager for safe database connection handling."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self):
        """Initializes the database schema with tasks, relationships, and audit log."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Tasks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'todo',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    assignee TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT
                )
            """)

            # Task relationships table (dependencies, parent-child)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_task_id TEXT NOT NULL,
                    target_task_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (source_task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    UNIQUE(source_task_id, target_task_id, relationship_type)
                )
            """)

            # Audit log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT,
                    reason TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)

            # Indexes for performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)")

    def create_task(
        self,
        task_id: str,
        project_id: str,
        title: str,
        description: Optional[str] = None,
        priority: str = "medium",
        assignee: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "system"
    ) -> Dict[str, Any]:
        """Creates a new task and logs the creation in audit log."""
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (id, project_id, title, description, status, priority, assignee, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)
            """, (task_id, project_id, title, description, priority, assignee, now, now, json.dumps(metadata or {})))

            # Log creation
            cursor.execute("""
                INSERT INTO audit_log (task_id, actor, timestamp, action, previous_state, new_state, reason)
                VALUES (?, ?, ?, 'created', NULL, 'todo', 'Task created')
            """, (task_id, actor, now))

        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a task by ID with its relationships."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()

            if not row:
                return None

            task = dict(row)
            task["metadata"] = json.loads(task["metadata"]) if task["metadata"] else {}

            # Fetch relationships
            cursor.execute("""
                SELECT source_task_id, target_task_id, relationship_type
                FROM task_relationships
                WHERE source_task_id = ? OR target_task_id = ?
            """, (task_id, task_id))
            task["relationships"] = [dict(rel) for rel in cursor.fetchall()]

            return task

    def update_task_status(
        self,
        task_id: str,
        new_status: str,
        actor: str,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Updates task status with validation and audit logging.
        Prevents invalid transitions (e.g., agent cannot move to 'done' without verification).
        """
        valid_statuses = {"todo", "in_progress", "review", "done", "blocked"}
        if new_status not in valid_statuses:
            raise TaskValidationError(f"Invalid status: {new_status}. Must be one of {valid_statuses}")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, assignee FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()

            if not row:
                raise TaskValidationError(f"Task not found: {task_id}")

            previous_status = row["status"]

            # Validation: Agent cannot directly transition to 'done' without review
            if actor == "agent" and new_status == "done" and previous_status != "review":
                raise TaskValidationError(
                    "Agent cannot move task directly to 'done'. Must go through 'review' status first."
                )

            now = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?
            """, (new_status, now, task_id))

            # Log the transition
            cursor.execute("""
                INSERT INTO audit_log (task_id, actor, timestamp, action, previous_state, new_state, reason)
                VALUES (?, ?, ?, 'status_changed', ?, ?, ?)
            """, (task_id, actor, now, previous_status, new_status, reason or "Status updated"))

        return self.get_task(task_id)

    def add_relationship(
        self,
        source_task_id: str,
        target_task_id: str,
        relationship_type: str,
        actor: str = "system"
    ):
        """
        Adds a relationship between tasks (e.g., 'depends_on', 'blocks', 'parent_of').
        """
        valid_types = {"depends_on", "blocks", "parent_of", "related_to"}
        if relationship_type not in valid_types:
            raise TaskValidationError(f"Invalid relationship type: {relationship_type}")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO task_relationships (source_task_id, target_task_id, relationship_type, created_at)
                VALUES (?, ?, ?, ?)
            """, (source_task_id, target_task_id, relationship_type, now))

            # Log relationship creation
            cursor.execute("""
                INSERT INTO audit_log (task_id, actor, timestamp, action, previous_state, new_state, reason)
                VALUES (?, ?, ?, 'relationship_added', NULL, NULL, ?)
            """, (source_task_id, actor, now, f"Added {relationship_type} relationship to {target_task_id}"))

    def get_project_tasks(self, project_id: str, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves all tasks for a project, optionally filtered by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if status_filter:
                cursor.execute("SELECT id FROM tasks WHERE project_id = ? AND status = ?", (project_id, status_filter))
            else:
                cursor.execute("SELECT id FROM tasks WHERE project_id = ?", (project_id,))

            task_ids = [row["id"] for row in cursor.fetchall()]

        return [self.get_task(tid) for tid in task_ids if self.get_task(tid)]

    def get_audit_log(self, task_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieves audit log entries, optionally filtered by task ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if task_id:
                cursor.execute("""
                    SELECT * FROM audit_log WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?
                """, (task_id, limit))
            else:
                cursor.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,))

            return [dict(row) for row in cursor.fetchall()]
