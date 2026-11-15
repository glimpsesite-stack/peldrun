# backend/diagnose_memory.py
"""
PELDRUN Memory diagnostic — one-shot schema + jobs + memories report.

Run from backend directory:
    python diagnose_memory.py

Or with explicit DB path:
    python diagnose_memory.py path\\to\\memory.db
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def find_db() -> Path | None:
    """Locate memory.db using the same resolution the app uses."""
    # Explicit CLI arg wins.
    if len(sys.argv) > 1:
        return Path(sys.argv[1])

    backend = Path(__file__).resolve().parent
    candidates = [
        backend / "memory.db",
        backend / "storage" / "memory.db",
        backend / "data" / "memory.db",
    ]
    # Also try PELDRUN_STORAGE_ROOT if set.
    import os
    env_root = os.environ.get("PELDRUN_STORAGE_ROOT", "").strip()
    if env_root:
        candidates.insert(0, Path(env_root) / "memory.db")
        candidates.insert(1, Path(env_root) / "storage" / "memory.db")

    for c in candidates:
        if c.is_file():
            return c
    return None


def main() -> None:
    db = find_db()
    if db is None:
        print("=" * 70)
        print("ERROR: memory.db not found.")
        print("Searched:")
        print("  <backend>/memory.db")
        print("  <backend>/storage/memory.db")
        print("  <backend>/data/memory.db")
        print("  $PELDRUN_STORAGE_ROOT/memory.db")
        print()
        print("Pass the path explicitly:")
        print("  python diagnose_memory.py C:\\path\\to\\memory.db")
        print("=" * 70)
        sys.exit(1)

    print("=" * 70)
    print(f"DB: {db}")
    print(f"Size: {db.stat().st_size:,} bytes")
    print("=" * 70)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- SCHEMA ---
    print("\n[SCHEMA]")
    tables = {
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "memory_entries",
        "memory_jobs",
        "memory_chat_cursors",
    }
    for t in sorted(required):
        mark = "OK " if t in tables else "MISSING"
        print(f"  [{mark}] table: {t}")

    # Column check for extraction_meta_json.
    if "memory_entries" in tables:
        cols = {
            r[1]
            for r in cur.execute(
                "PRAGMA table_info(memory_entries)"
            ).fetchall()
        }
        mark = "OK " if "extraction_meta_json" in cols else "MISSING"
        print(f"  [{mark}] column: memory_entries.extraction_meta_json")
        print(f"  memory_entries total columns: {len(cols)}")

    # --- JOBS ---
    print("\n[JOBS] (latest 10)")
    if "memory_jobs" in tables:
        try:
            rows = cur.execute(
                "SELECT job_id, chat_id, status, "
                "candidates_count, created_memories_count, "
                "attempts, error_code, error_message, created_at "
                "FROM memory_jobs "
                "ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            if not rows:
                print("  (no jobs in table)")
            for r in rows:
                print(
                    f"  job={str(r['job_id'])[:14]:14}.. "
                    f"chat={str(r['chat_id'])[:18]:18} "
                    f"status={str(r['status']):12} "
                    f"cand={r['candidates_count'] or 0:3} "
                    f"created={r['created_memories_count'] or 0:3} "
                    f"attempts={r['attempts'] or 0} "
                    f"err={r['error_code'] or '-'}"
                )
                if r["error_code"] and r["error_message"]:
                    msg = str(r["error_message"])[:200]
                    print(f"     -> {msg}")
        except Exception as exc:
            print(f"  ERROR reading jobs: {exc}")
    else:
        print("  (memory_jobs table missing)")

    # --- CURSORS ---
    print("\n[CURSORS]")
    if "memory_chat_cursors" in tables:
        try:
            rows = cur.execute(
                "SELECT chat_id, last_processed_turn_index, updated_at "
                "FROM memory_chat_cursors ORDER BY updated_at DESC LIMIT 5"
            ).fetchall()
            if not rows:
                print("  (no cursors yet)")
            for r in rows:
                print(
                    f"  chat={str(r['chat_id'])[:30]:30} "
                    f"cursor={r['last_processed_turn_index']}"
                )
        except Exception as exc:
            print(f"  ERROR reading cursors: {exc}")

    # --- MEMORIES ---
    print("\n[MEMORIES] (latest 10)")
    if "memory_entries" in tables:
        try:
            rows = cur.execute(
                "SELECT id, content, memory_type, source_type, "
                "scope_type, scope_id, created_at "
                "FROM memory_entries "
                "ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            if not rows:
                print("  (NO MEMORIES SAVED YET)")
            for r in rows:
                content = str(r["content"] or "")
                if len(content) > 120:
                    content = content[:117] + "..."
                print(
                    f"\n  id={str(r['id'])[:14]}.. "
                    f"type={r['memory_type']} "
                    f"src={r['source_type']}"
                )
                print(
                    f"  scope: {r['scope_type']}:{r['scope_id']}"
                )
                print(f"  content: {content}")

            # Count with extraction_meta.
            meta_count = cur.execute(
                "SELECT COUNT(*) FROM memory_entries "
                "WHERE extraction_meta_json IS NOT NULL"
            ).fetchone()[0]
            total_count = cur.execute(
                "SELECT COUNT(*) FROM memory_entries"
            ).fetchone()[0]
            print(
                f"\n  Total: {total_count} "
                f"(with extraction_meta: {meta_count})"
            )

            # Show one extraction_meta sample.
            sample = cur.execute(
                "SELECT extraction_meta_json FROM memory_entries "
                "WHERE extraction_meta_json IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if sample and sample["extraction_meta_json"]:
                print("\n  [SAMPLE extraction_meta_json]")
                try:
                    data = json.loads(
                        sample["extraction_meta_json"]
                    )
                    print(
                        "  "
                        + json.dumps(
                            data, indent=2, ensure_ascii=False
                        )[:600]
                    )
                except Exception as exc:
                    print(f"  (parse failed: {exc})")
        except Exception as exc:
            print(f"  ERROR reading memories: {exc}")
    else:
        print("  (memory_entries table missing)")

    print("\n" + "=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    print(
        "If JOBS shows a row with status=completed and\n"
        "  candidates_count > 0 and created_memories_count > 0,\n"
        "  -> pipeline works end-to-end.\n\n"
        "If candidates_count > 0 but created_memories_count = 0,\n"
        "  -> _candidate_to_entry or pipeline.save_many is failing.\n"
        "  -> Check the backend log for 'failed to build MemoryEntry' or\n"
        "     'write pipeline failed'.\n\n"
        "If candidates_count = 0 and created_memories_count = 0,\n"
        "  -> The model returned no candidates (prompt or model issue).\n"
        "  -> Check prompt v5 is applied and try a larger model.\n\n"
        "If JOBS is empty,\n"
        "  -> No jobs enqueued: dispatcher hook not firing, or AI extraction\n"
        "     is disabled in settings."
    )
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    main()
