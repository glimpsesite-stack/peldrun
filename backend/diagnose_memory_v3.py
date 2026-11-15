# backend/diagnose_memory_v3.py
"""
PELDRUN Memory diagnostic — v3.

CORRECTS the naming bug in v2: the canonical entry table is
``memories`` (see migrations.py line ~180), NOT
``memory_entries``. v2 searched for the wrong name, saw 0 rows,
and falsely reported "NO SCHEMA" for a DB that actually has
the full schema.

This v3 uses the CORRECT names:
  - ``memories``             (canonical entry table)
  - ``memory_jobs``          (extraction job queue)
  - ``memory_chat_cursors``  (per-chat cursor)
  - ``memories_fts``         (FTS5 external-content index)

Run from backend:
    python diagnose_memory_v3.py
    python diagnose_memory_v3.py "D:\\AI\\peldrun\\storage\\memory.db"
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def find_db() -> Path | None:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    try:
        from peldrun.storage_paths import get_storage_root
        root = Path(get_storage_root())
        for c in (root / "memory.db", root / "storage" / "memory.db"):
            if c.is_file():
                return c
    except Exception:
        pass
    backend = Path(__file__).resolve().parent
    for c in (
        backend / "memory.db",
        backend / "storage" / "memory.db",
    ):
        if c.is_file():
            return c
    return None


def main() -> None:
    db = find_db()
    if db is None or not db.is_file():
        print("ERROR: memory.db not found.")
        sys.exit(1)

    print("=" * 76)
    print(f"DB: {db}")
    print(f"Size: {db.stat().st_size:,} bytes")
    print("=" * 76)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- SCHEMA (correct table names) ---
    print("\n[SCHEMA]")
    tables = {
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for t in (
        "memories",
        "memory_jobs",
        "memory_chat_cursors",
        "memories_fts",
        "memory_schema_version",
    ):
        mark = "OK     " if t in tables else "MISSING"
        print(f"  [{mark}] {t}")

    if "memories" in tables:
        cols = {
            r[1]
            for r in cur.execute(
                "PRAGMA table_info(memories)"
            ).fetchall()
        }
        for col in (
            "extraction_meta_json",
            "extraction_job_id",
        ):
            mark = "OK     " if col in cols else "MISSING"
            print(f"  [{mark}] memories.{col}")

    # Schema version
    if "memory_schema_version" in tables:
        try:
            v = cur.execute(
                "SELECT MAX(version) FROM memory_schema_version"
            ).fetchone()[0]
            print(f"  Schema version: {v}")
        except Exception:
            pass

    # --- JOBS by status ---
    print("\n[JOBS BY STATUS]")
    if "memory_jobs" in tables:
        rows = cur.execute(
            "SELECT status, COUNT(*) AS cnt FROM memory_jobs "
            "GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
        if not rows:
            print("  (no jobs at all)")
        for r in rows:
            print(f"  {r['status']:15} {r['cnt']}")

        # Totals: candidates vs created
        totals = cur.execute(
            "SELECT "
            "SUM(candidates_count) AS total_cand, "
            "SUM(created_memories_count) AS total_created, "
            "SUM(cost_nano_usd) AS total_cost "
            "FROM memory_jobs"
        ).fetchone()
        print(
            f"\n  SUM candidates_count        = "
            f"{totals['total_cand'] or 0}"
        )
        print(
            f"  SUM created_memories_count  = "
            f"{totals['total_created'] or 0}"
        )
        print(
            f"  SUM cost_nano_usd           = "
            f"{totals['total_cost'] or 0}"
        )

    # --- LATEST JOBS ---
    print("\n[LATEST 10 JOBS]")
    if "memory_jobs" in tables:
        rows = cur.execute(
            "SELECT job_id, chat_id, status, "
            "candidates_count, created_memories_count, "
            "attempts, error_code, "
            "datetime(created_at, 'unixepoch') AS created_at "
            "FROM memory_jobs ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        if not rows:
            print("  (no jobs)")
        for r in rows:
            print(
                f"  {str(r['job_id'])[:14]:14}.. "
                f"status={str(r['status']):12} "
                f"cand={r['candidates_count'] or 0:3} "
                f"created={r['created_memories_count'] or 0:3} "
                f"att={r['attempts'] or 0} "
                f"err={r['error_code'] or '-':20} "
                f"{r['created_at']}"
            )

    # --- FAILED JOBS with messages ---
    print("\n[FAILED / RETRY_PENDING JOBS (latest 5)]")
    if "memory_jobs" in tables:
        rows = cur.execute(
            "SELECT job_id, status, error_code, error_message "
            "FROM memory_jobs "
            "WHERE status IN ('failed', 'retry_pending') "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        if not rows:
            print("  (none)")
        for r in rows:
            msg = (r["error_message"] or "")[:150]
            print(
                f"  {str(r['job_id'])[:14]}.. "
                f"{r['status']} | {r['error_code']} | {msg}"
            )

    # --- MEMORIES (THE ANSWER) ---
    print("\n[MEMORIES — THE ANSWER]")
    if "memories" in tables:
        total = cur.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        print(f"  Total rows in 'memories': {total}")

        if total > 0:
            # Breakdown by status
            print("\n  By status:")
            for r in cur.execute(
                "SELECT status, COUNT(*) c FROM memories "
                "GROUP BY status"
            ).fetchall():
                print(f"    {r['status']:15} {r['c']}")

            # Breakdown by source_type
            print("\n  By source_type:")
            for r in cur.execute(
                "SELECT source_type, COUNT(*) c FROM memories "
                "GROUP BY source_type"
            ).fetchall():
                print(f"    {r['source_type']:15} {r['c']}")

            # Latest 10 entries
            print("\n  Latest 10 entries:")
            rows = cur.execute(
                "SELECT memory_id, content, memory_type, "
                "source_type, scope_type, scope_id, "
                "datetime(created_at, 'unixepoch') AS created_at "
                "FROM memories ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            for r in rows:
                content = str(r["content"] or "")[:100]
                print(
                    f"\n    [{r['memory_type']}] "
                    f"scope={r['scope_type']}:{r['scope_id']} "
                    f"src={r['source_type']}"
                )
                print(f"    content: {content}")

            # Sample extraction_meta
            sample = cur.execute(
                "SELECT extraction_meta_json FROM memories "
                "WHERE extraction_meta_json IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if sample and sample["extraction_meta_json"]:
                print("\n  Sample extraction_meta_json:")
                try:
                    data = json.loads(
                        sample["extraction_meta_json"]
                    )
                    print(
                        "  " + json.dumps(
                            data, indent=2, ensure_ascii=False
                        )[:500]
                    )
                except Exception as exc:
                    print(f"  (parse failed: {exc})")
        else:
            print("  (EMPTY — no memories saved)")

    # --- CURSORS ---
    print("\n[CURSORS (latest 5)]")
    if "memory_chat_cursors" in tables:
        rows = cur.execute(
            "SELECT chat_id, last_processed_turn_index, "
            "datetime(updated_at, 'unixepoch') AS updated_at "
            "FROM memory_chat_cursors "
            "ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
        if not rows:
            print("  (none)")
        for r in rows:
            print(
                f"  chat={str(r['chat_id'])[:24]:24} "
                f"cursor={r['last_processed_turn_index']} "
                f"{r['updated_at']}"
            )

    # --- DECISION_JSON SPREAD (what LLM returned) ---
    print("\n[DECISIONS SAMPLE — from latest 3 completed jobs]")
    if "memory_jobs" in tables:
        rows = cur.execute(
            "SELECT job_id, decisions_json FROM memory_jobs "
            "WHERE status='completed' "
            "ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        if not rows:
            print("  (no completed jobs yet)")
        for r in rows:
            try:
                decisions = json.loads(
                    r["decisions_json"] or "[]"
                )
                if not decisions:
                    print(
                        f"  {str(r['job_id'])[:14]}.. "
                        f"decisions=[]"
                    )
                    continue
                accepted = sum(
                    1 for d in decisions
                    if d.get("action") == "accepted"
                )
                rejected = sum(
                    1 for d in decisions
                    if d.get("action") == "rejected"
                )
                print(
                    f"  {str(r['job_id'])[:14]}.. "
                    f"accepted={accepted} rejected={rejected} "
                    f"total={len(decisions)}"
                )
                for d in decisions[:3]:
                    action = d.get("action", "?")
                    reason = d.get("reason", "")
                    detail = (d.get("detail") or "")[:100]
                    print(
                        f"     - {action} reason={reason} "
                        f"{detail}"
                    )
            except Exception as exc:
                print(f"  parse error: {exc}")

    print("\n" + "=" * 76)
    print("SUMMARY")
    print("=" * 76)

    # Final verdict
    if "memories" in tables:
        total = cur.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        if total > 0:
            print(
                f"  OK: {total} memories ARE stored in the DB.\n"
                f"  The save path WORKS."
            )
        else:
            cand_sum = 0
            if "memory_jobs" in tables:
                row = cur.execute(
                    "SELECT SUM(candidates_count) AS s "
                    "FROM memory_jobs"
                ).fetchone()
                cand_sum = row["s"] or 0
            if cand_sum > 0:
                print(
                    f"  WARN: jobs reported {cand_sum} candidates "
                    f"but 0 memories saved.\n"
                    f"  -> save path is broken. Send "
                    f"write_pipeline.py + entry.py + latest log."
                )
            else:
                print(
                    "  WARN: no candidates extracted yet.\n"
                    "  -> prompt v5 not applied, model returns empty, "
                    "or jobs fail before extraction.\n"
                    "  -> Check [LATEST 10 JOBS] above."
                )
    print("=" * 76)

    conn.close()


if __name__ == "__main__":
    main()
