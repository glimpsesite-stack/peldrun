# backend/migrate_default_project_memories.py
# One-shot script: move memories from the sentinel scope to user:default.
import sqlite3
import time
from pathlib import Path

def migrate(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Find candidates for migration
        rows = conn.execute(
            """
            SELECT memory_id, content_hash, content
            FROM memories
            WHERE scope_type = 'project'
              AND scope_id = 'default_project'
              AND status = 'active'
            """
        ).fetchall()
        print(f"Found {len(rows)} memories to migrate")

        # Skip ones that would violate UNIQUE(scope_type, scope_id, content_hash)
        skipped = []
        moved = []
        with conn:
            for row in rows:
                exists = conn.execute(
                    """
                    SELECT 1 FROM memories
                    WHERE scope_type = 'user'
                      AND scope_id = 'default'
                      AND content_hash = ?
                    """,
                    (row["content_hash"],),
                ).fetchone()
                if exists:
                    skipped.append(row["memory_id"])
                    continue
                conn.execute(
                    """
                    UPDATE memories
                    SET scope_type = 'user',
                        scope_id = 'default',
                        updated_at = ?
                    WHERE memory_id = ?
                    """,
                    (time.time(), row["memory_id"]),
                )
                moved.append(row["memory_id"])

        print(f"Moved: {len(moved)}")
        print(f"Skipped (duplicate content in user:default): {len(skipped)}")
        for mid in skipped:
            print(f"  - {mid}")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python migrate_default_project_memories.py <db_path>")
        sys.exit(1)
    migrate(Path(sys.argv[1]))
