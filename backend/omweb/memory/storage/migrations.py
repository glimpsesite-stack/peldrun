# backend/omweb/memory/storage/migrations.py
"""
PELDRUN Memory Platform - Storage Schema and Idempotent Migrations.

Single source of truth for the SQL schema that backs the memory
subsystem, together with the migration runner that brings an empty
or older database up to ``CURRENT_VERSION``.

Design invariants
-----------------

1. Additive-only migrations.
   Every change is expressed as ``CREATE ... IF NOT EXISTS`` or
   ``ALTER TABLE ... ADD COLUMN``. Destructive operations (DROP,
   TRUNCATE, column renames) are FORBIDDEN here. When a genuinely
   destructive change becomes necessary, it lives in a NEW
   migration with an explicit backup step -- never as an edit to
   an existing one. Historical databases must be able to walk
   forward without losing data.

2. Version tracking is monotonic.
   ``memory_schema_version`` records each applied version as an
   INSERT-only row. Reading the highest row gives the current
   version. There is no rollback path -- downgrades require a
   fresh restore from backup (documented in P1.7's operator
   guide).

3. External-content FTS5.
   ``memories_fts`` uses ``content='memories'`` so the FTS index
   stores only the inverted index, not a duplicate copy of the
   content. This roughly halves the on-disk footprint for a
   memory-heavy deployment. Every INSERT / UPDATE / DELETE on
   ``memories`` is mirrored into FTS via triggers.

4. The UPDATE trigger has a ``WHEN`` guard.
   The plan's reference spec fires the FTS rebuild on EVERY update
   to ``memories``, including the extremely frequent bulk
   ``mark_used`` calls (one per retrieval, updating N rows) and
   status changes. That design would:
     * rewrite every FTS entry every time an entry is retrieved,
     * grow the WAL by megabytes per hour on an active deployment,
     * create a real risk of FTS drift if a trigger fails midway.
   The ``WHEN new.content != old.content OR new.normalized_content
   != old.normalized_content`` guard skips the rebuild when the
   indexed columns are unchanged, so FTS work is proportional to
   ACTUAL content changes -- not to retrieval frequency.

5. Unicode-aware tokenization.
   ``tokenize='unicode61 remove_diacritics 2'`` gives us:
     * correct token boundaries for Arabic, Russian, and English
       without an external ICU dependency,
     * diacritic folding (Arabic harakat, Cyrillic accents),
     * case folding (``unicode61`` lowercases internally).
   The companion ``normalizer.py`` applies additional,
   language-specific folding BEFORE insert, so FTS sees the
   already-normalized form alongside the raw content.

6. Idempotent application.
   ``apply_schema(conn)`` is safe to call on:
     * an empty database (creates everything, records v1),
     * a database already at ``CURRENT_VERSION`` (no-op),
     * a database at an older version (applies the pending
       migrations in order).
   The migration body uses ``executescript`` -- the fast path --
   and version recording uses an explicit INSERT. A crash between
   the two leaves the schema created but the version
   unrecorded; the next call re-applies the (idempotent) DDL and
   records the version correctly. No data is lost, no drift
   occurs.

7. FTS5 availability is a HARD dependency.
   Python ships with FTS5 since 3.11, but custom builds and some
   Linux distributions compile it out. ``apply_schema`` detects
   ``OperationalError('no such module: fts5')`` and raises
   ``MemoryUnavailableError`` with a specific, actionable
   message. Silently proceeding without FTS would make
   ``search`` return nothing forever with no diagnostic signal.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List, Tuple

from peldrun.memory.domain.errors import MemoryUnavailableError


logger = logging.getLogger("omweb.memory.storage.migrations")


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


CURRENT_VERSION: int = 4
"""Highest schema version this module knows how to produce.

Bump only when adding a new migration. Every bump must be paired
with an entry in ``_MIGRATIONS`` below.
"""


# ---------------------------------------------------------------------------
# DDL -- version table (always created first, before version check)
# ---------------------------------------------------------------------------


_VERSION_TABLE_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# DDL -- v1 (the baseline schema)
# ---------------------------------------------------------------------------
#
# Every statement below is idempotent (``IF NOT EXISTS``). This is
# what makes re-running a partially-applied migration safe.

_V1_MEMORIES_TABLE_DDL: str = """
CREATE TABLE IF NOT EXISTS memories (
    memory_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id          TEXT    NOT NULL UNIQUE,
    memory_type        TEXT    NOT NULL,
    scope_type         TEXT    NOT NULL,
    scope_id           TEXT    NOT NULL,
    content            TEXT    NOT NULL,
    normalized_content TEXT    NOT NULL,
    tags_json          TEXT    NOT NULL DEFAULT '[]',
    importance         INTEGER NOT NULL DEFAULT 50,
    confidence         REAL    NOT NULL DEFAULT 1.0,
    status             TEXT    NOT NULL DEFAULT 'active',
    is_pinned          INTEGER NOT NULL DEFAULT 0,
    source_type        TEXT    NOT NULL,
    source_chat_id     TEXT,
    source_run_id      TEXT,
    source_turn_id     TEXT,
    imported_from      TEXT,
    content_hash       TEXT    NOT NULL,
    created_at         REAL    NOT NULL,
    updated_at         REAL    NOT NULL,
    last_used_at       REAL,
    expires_at         REAL,
    UNIQUE(scope_type, scope_id, content_hash)
);
"""

_V1_MEMORIES_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope_type, scope_id, status, importance DESC);

CREATE INDEX IF NOT EXISTS idx_memories_used
    ON memories(last_used_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_type
    ON memories(memory_type, status);

CREATE INDEX IF NOT EXISTS idx_memories_pinned
    ON memories(is_pinned, status)
    WHERE is_pinned = 1;
"""

_V1_FTS_TABLE_DDL: str = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    normalized_content,
    content='memories',
    content_rowid='memory_pk',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_V1_TRIGGERS_DDL: str = """
CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(rowid, content, normalized_content)
    VALUES (new.memory_pk, new.content, new.normalized_content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories
BEGIN
    INSERT INTO memories_fts(
        memories_fts, rowid, content, normalized_content
    )
    VALUES (
        'delete',
        old.memory_pk,
        old.content,
        old.normalized_content
    );
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE ON memories
WHEN new.content != old.content
  OR new.normalized_content != old.normalized_content
BEGIN
    INSERT INTO memories_fts(
        memories_fts, rowid, content, normalized_content
    )
    VALUES (
        'delete',
        old.memory_pk,
        old.content,
        old.normalized_content
    );
    INSERT INTO memories_fts(rowid, content, normalized_content)
    VALUES (new.memory_pk, new.content, new.normalized_content);
END;
"""


# ---------------------------------------------------------------------------
# Migration v1 -- full function body
# ---------------------------------------------------------------------------


def _apply_v1(conn: sqlite3.Connection) -> None:
    """Apply the baseline schema (v1).

    Every DDL statement in this function uses ``CREATE ... IF NOT
    EXISTS``. Re-running on a partially-created schema is a no-op
    for the tables that already exist and completes the missing
    ones. This is what makes the whole migration crash-safe without
    true transactions (see the module docstring's note on
    ``executescript``).
    """
    conn.executescript(_V1_MEMORIES_TABLE_DDL)
    conn.executescript(_V1_MEMORIES_INDEXES_DDL)
    conn.executescript(_V1_FTS_TABLE_DDL)
    conn.executescript(_V1_TRIGGERS_DDL)


# ---------------------------------------------------------------------------
# DDL -- v2 (memory_jobs + memory_chat_cursors + extraction meta columns)
# ---------------------------------------------------------------------------
#
# v2 introduces the durable extraction pipeline: a job queue, a
# per-chat cursor table that enforces the cursor invariant (§19.3),
# and two new columns on `memories` that bind each extracted entry
# to the job that produced it (§22 ExtractionMeta).
#
# Every DDL statement below is either `CREATE ... IF NOT EXISTS`
# (idempotent) or wrapped by a `_column_exists` probe in
# `_apply_v2` (SQLite has no `ADD COLUMN IF NOT EXISTS`). This
# preserves the module's crash-safety guarantee: a crash between
# DDL and version recording re-runs the whole migration safely.

_V2_MEMORY_JOBS_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL DEFAULT 'extraction',
    chat_id TEXT NOT NULL,
    project_id TEXT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,

    trigger_type TEXT NOT NULL DEFAULT 'after_session',
    source_session_path TEXT,
    start_turn_index INTEGER,
    end_turn_index INTEGER,
    last_processed_turn_index_at_enqueue INTEGER DEFAULT 0,

    message_snapshot_hash TEXT NOT NULL,
    snapshot_verified_at REAL,

    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    model_provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    lease_owner TEXT,
    lease_expires_at REAL,
    heartbeat_at REAL,

    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    next_retry_at REAL,

    error_code TEXT,
    error_message TEXT,

    candidates_count INTEGER DEFAULT 0,
    created_memories_count INTEGER DEFAULT 0,
    cost_nano_usd INTEGER DEFAULT 0,
    decisions_json TEXT DEFAULT '[]'
);
"""

_V2_MEMORY_JOBS_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memory_jobs_pending
ON memory_jobs(status, next_retry_at)
WHERE status IN ('pending', 'retry_pending');

CREATE INDEX IF NOT EXISTS idx_memory_jobs_chat
ON memory_jobs(chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_jobs_trigger
ON memory_jobs(trigger_type, status);

CREATE INDEX IF NOT EXISTS idx_memory_jobs_chat_fifo
ON memory_jobs(chat_id, created_at ASC)
WHERE status IN ('pending', 'retry_pending');
"""

_V2_MEMORY_CHAT_CURSORS_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_chat_cursors (
    chat_id TEXT PRIMARY KEY,
    last_processed_turn_index INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_chat_cursors_updated
ON memory_chat_cursors(updated_at DESC);
"""

_V2_MEMORIES_EXTRACTION_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memories_job
ON memories(extraction_job_id)
WHERE extraction_job_id IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# Migration v2 -- full function body
# ---------------------------------------------------------------------------


def _column_exists(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    """Return True when `column_name` already exists on `table_name`.

    SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS``. To keep ``_apply_v2`` idempotent (which the module's
    crash-safety guarantee requires), we query a ``PRAGMA`` before
    issuing the ADD COLUMN.

    `table_xinfo` vs `table_info` -- why BOTH
    -----------------------------------------
    `PRAGMA table_info` returns every column EXCEPT VIRTUAL
    GENERATED columns in some SQLite builds and versions. The
    `extraction_job_id` column added by `_apply_v2` IS a virtual
    generated column, so `table_info` alone would report it as
    "missing" even after a successful ALTER TABLE -- causing this
    guard to re-run the ADD COLUMN and raise "duplicate column
    name" on a crash-retry path.

    `PRAGMA table_xinfo` was introduced specifically to include
    generated columns (with an extra `hidden` flag at position 6).
    It is the authoritative source. We prefer `table_xinfo` when
    available and fall back to `table_info` on very old SQLite
    builds that lack it. The fallback path is safe because the
    only column this migration adds that needs it is
    `extraction_job_id` -- on a build old enough to lack
    `table_xinfo`, generated columns are almost certainly
    unsupported too, and the ALTER TABLE would have failed with a
    clear error rather than a silent duplicate.

    The `table_name` and `column_name` arguments MUST be hardcoded
    literals supplied by migration code. This function does NOT
    sanitize them -- it is not a public API and never accepts
    user-supplied values.
    """
    # Prefer table_xinfo (includes generated columns).
    try:
        cur = conn.execute(f"PRAGMA table_xinfo({table_name})")
        for row in cur.fetchall():
            # PRAGMA table_xinfo columns: cid, name, type, notnull,
            # dflt_value, pk, hidden. ``name`` is at index 1.
            if row[1] == column_name:
                return True
        return False
    except sqlite3.Error:
        pass  # fall through to table_info

    # Fallback for SQLite versions without table_xinfo.
    try:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        for row in cur.fetchall():
            if row[1] == column_name:
                return True
    except sqlite3.Error:
        return False
    return False


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Apply the Phase 1 schema: jobs, cursors, extraction columns.

    Layout
    ------
      1. `memory_jobs` table + 4 indexes (FIFO, pending, trigger,
         chat).
      2. `memory_chat_cursors` table + 1 index (recency).
      3. `memories.extraction_meta_json` -- JSON blob per entry,
         NULL for user-created entries (§22 ExtractionMeta).
      4. `memories.extraction_job_id` -- VIRTUAL generated column
         that mirrors `extraction_meta_json.job_id` into a plain
         TEXT column. This is the review note #5 fix: a functional
         index on `json_extract(...)` is NOT used by SQLite's
         query planner for plain `WHERE job_id = ?` lookups; a
         generated column IS.
      5. Index on the generated column so
         `GET /api/memory/jobs/{job_id}` is a single index lookup.

    Idempotency
    -----------
    Tables and indexes use ``CREATE ... IF NOT EXISTS``. The two
    ``ALTER TABLE ADD COLUMN`` statements are guarded by
    ``_column_exists`` because SQLite has no conditional ADD
    COLUMN syntax. A crash between any step and
    ``_record_version(conn, 2)`` re-runs the whole function
    safely -- tables are skipped (already exist), indexes are
    skipped (already exist), and columns are skipped (already
    exist).

    FTS5 is not touched by this migration
    -------------------------------------
    The existing FTS5 external-content table
    (`memories_fts`) tracks only ``content`` and
    ``normalized_content``. Adding new columns to ``memories``
    has no effect on the FTS index. The existing UPDATE trigger
    already has a ``WHEN`` guard that skips the FTS rebuild when
    those two columns are unchanged, so routine updates to
    ``extraction_meta_json`` will not trigger an FTS rewrite.
    """
    # 1. Job queue table + indexes.
    conn.executescript(_V2_MEMORY_JOBS_DDL)
    conn.executescript(_V2_MEMORY_JOBS_INDEXES_DDL)

    # 2. Per-chat cursor table + index.
    conn.executescript(_V2_MEMORY_CHAT_CURSORS_DDL)

    # 3. Extraction metadata column on `memories`.
    if not _column_exists(conn, "memories", "extraction_meta_json"):
        conn.execute(
            "ALTER TABLE memories "
            "ADD COLUMN extraction_meta_json TEXT DEFAULT NULL"
        )

    # 4. Generated column mirroring `$.job_id`. VIRTUAL (not
    #    STORED) because VIRTUAL is the only kind SQLite allows
    #    in `ALTER TABLE ADD COLUMN`. The generated column is
    #    never written by the application; it is populated
    #    automatically from `extraction_meta_json`.
    if not _column_exists(conn, "memories", "extraction_job_id"):
        conn.execute(
            "ALTER TABLE memories "
            "ADD COLUMN extraction_job_id TEXT "
            "GENERATED ALWAYS AS "
            "(json_extract(extraction_meta_json, '$.job_id')) VIRTUAL"
        )

    # 5. Index on the generated column.
    conn.executescript(_V2_MEMORIES_EXTRACTION_INDEX_DDL)


# ---------------------------------------------------------------------------
# DDL -- v3 (memory_batches + jobs.batch_id + extraction_job_id safety net)
# ---------------------------------------------------------------------------
#
# v3 introduces the retroactive batch extraction subsystem (Phase 3):
#
#   * ``memory_batches`` -- one row per batch run. Tracks the scope
#     filters, the frozen model configuration, progress counters,
#     cost estimate vs actual, and lifecycle timestamps.
#
#   * ``memory_jobs.batch_id`` -- nullable FK-like link from a job
#     to the batch that spawned it. NULL for on-the-fly jobs
#     (``after_turn`` / ``after_session``); non-NULL only for
#     retroactive jobs created by a batch.
#
#   * A **defensive re-check** for ``extraction_job_id`` on
#     ``memories``. The report from the Phase 1 audit noted that
#     some deployments reached ``memory_schema_version=2`` BEFORE
#     the generated column was added to ``_apply_v2``. Those
#     databases will never see ``_apply_v2`` run again (version
#     gate), so the column stays missing. v3 self-heals them:
#     if the column is absent, add it. If it is already present
#     (the normal path), the guard is a no-op.
#
# Every DDL below uses ``CREATE ... IF NOT EXISTS``; every ALTER is
# guarded by ``_column_exists``. Re-running v3 on a partially
# applied database is safe (crash-safety guarantee preserved).

_V3_MEMORY_BATCHES_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_batches (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    triggered_by TEXT NOT NULL DEFAULT 'manual',

    project_id TEXT,
    chat_ids_json TEXT NOT NULL DEFAULT '[]',
    date_from REAL,
    date_to REAL,

    session_count INTEGER NOT NULL DEFAULT 0,
    job_count INTEGER NOT NULL DEFAULT 0,
    completed_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    cancelled_jobs INTEGER NOT NULL DEFAULT 0,
    total_candidates INTEGER NOT NULL DEFAULT 0,
    total_memories_created INTEGER NOT NULL DEFAULT 0,

    estimated_cost_nano_usd INTEGER NOT NULL DEFAULT 0,
    actual_cost_nano_usd INTEGER NOT NULL DEFAULT 0,

    model_provider_id TEXT,
    model_id TEXT,
    min_confidence REAL,

    created_at REAL NOT NULL,
    started_at REAL,
    paused_at REAL,
    completed_at REAL,
    cancelled_at REAL,
    last_progress_at REAL,

    error_message TEXT,
    options_json TEXT NOT NULL DEFAULT '{}'
);
"""

_V3_MEMORY_BATCHES_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memory_batches_status
    ON memory_batches(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_batches_created
    ON memory_batches(created_at DESC);
"""

_V3_MEMORY_JOBS_BATCH_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memory_jobs_batch
    ON memory_jobs(batch_id, status)
    WHERE batch_id IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# Migration v3 -- full function body
# ---------------------------------------------------------------------------


def _apply_v3(conn: sqlite3.Connection) -> None:
    """Apply the Phase 3 schema: batch table + job batch link.

    Layout
    ------
      1. Defensive re-check of ``memories.extraction_meta_json``
         and ``memories.extraction_job_id``. The normal path
         (v2 already applied) finds both present, so both
         ``ALTER`` statements are no-ops. On a drifted database
         that reached v2 before the columns were added, the
         guards fire and complete the missing columns.
      2. ``memory_batches`` table + 2 indexes.
      3. ``memory_jobs.batch_id`` column (guarded ADD).
      4. Partial index on ``memory_jobs(batch_id, status)`` for
         fast batch progress queries.

    Idempotency
    -----------
    All CREATE statements use ``IF NOT EXISTS``. Both ALTERs are
    guarded by ``_column_exists``. A crash between any step and
    ``_record_version(conn, 3)`` re-runs the whole function
    safely.
    """
    # 1. Defensive re-check: extraction columns on `memories`.
    #    On the normal path both checks return True and neither
    #    ALTER fires. On a drifted database the missing column
    #    (or columns) are added here.
    if not _column_exists(conn, "memories", "extraction_meta_json"):
        conn.execute(
            "ALTER TABLE memories "
            "ADD COLUMN extraction_meta_json TEXT DEFAULT NULL"
        )
    if not _column_exists(conn, "memories", "extraction_job_id"):
        conn.execute(
            "ALTER TABLE memories "
            "ADD COLUMN extraction_job_id TEXT "
            "GENERATED ALWAYS AS "
            "(json_extract(extraction_meta_json, '$.job_id')) VIRTUAL"
        )

    # 2. Batch table + indexes.
    conn.executescript(_V3_MEMORY_BATCHES_DDL)
    conn.executescript(_V3_MEMORY_BATCHES_INDEXES_DDL)

    # 3. batch_id on memory_jobs (nullable; NULL for on-the-fly
    #    jobs, non-NULL only for batch-spawned retroactive jobs).
    if not _column_exists(conn, "memory_jobs", "batch_id"):
        conn.execute(
            "ALTER TABLE memory_jobs "
            "ADD COLUMN batch_id TEXT DEFAULT NULL"
        )

    # 4. Partial index for batch progress queries.
    conn.executescript(_V3_MEMORY_JOBS_BATCH_INDEX_DDL)


# ---------------------------------------------------------------------------
# DDL -- v4 (Phase 4: embeddings metadata + embedding jobs)
# ---------------------------------------------------------------------------
#
# v4 introduces the vector search foundation:
#
#   * ``memory_embeddings`` -- the persistent store of embedding
#     vectors. One row per (memory_id, model_id, model_version)
#     triple, so that:
#
#       - switching the embedding model does NOT corrupt old
#         vectors (they remain, under a different namespace),
#       - rolling back to a previous model re-uses its rows
#         without re-embedding,
#       - a model upgrade (same model_id, new model_version)
#         builds a new vector space without mixing with the old.
#
#     The ``vector`` column is a BLOB: the repository (Task 4.4)
#     decides the packed format. float32 is the convention, but
#     the schema does not enforce it -- if a future model needs
#     float16, the repository can pack accordingly and store the
#     dimension out-of-band (see ``len(vector) // bytes_per_elem``
#     note in ``vector_repository.py``).
#
#   * ``memory_embedding_jobs`` -- the durable queue for the
#     embedding worker. Same lifecycle semantics as
#     ``memory_jobs`` (Phase 1-A), but WITHOUT the cursor / FIFO
#     per-chat invariants: embedding jobs are independent (one
#     per memory per model), not sequential per chat. Multiple
#     embedding jobs can run in parallel; the worker claims
#     ``pending`` rows whose ``next_retry_at`` has elapsed.
#
# NOT created here: ``memory_vectors`` (the sqlite-vec virtual
# table). See the module docstring's "Migration v4 does NOT
# create vec0" note at the top of ``_apply_v4``.

_V4_MEMORY_EMBEDDINGS_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id        TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    content_version  INTEGER NOT NULL,
    vector           BLOB NOT NULL,
    created_at       REAL NOT NULL,
    PRIMARY KEY (memory_id, model_id, model_version)
);
"""

_V4_MEMORY_EMBEDDINGS_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
    ON memory_embeddings(model_id, model_version, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_memory
    ON memory_embeddings(memory_id);
"""

_V4_MEMORY_EMBEDDING_JOBS_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_embedding_jobs (
    job_id          TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    created_at      REAL NOT NULL,
    started_at      REAL,
    completed_at    REAL,
    next_retry_at   REAL,
    error_code      TEXT,
    error_message   TEXT,
    cost_nano_usd   INTEGER NOT NULL DEFAULT 0
);
"""

_V4_MEMORY_EMBEDDING_JOBS_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_embedding_jobs_pending
    ON memory_embedding_jobs(status, next_retry_at)
    WHERE status IN ('pending', 'retry_pending');

CREATE INDEX IF NOT EXISTS idx_embedding_jobs_memory
    ON memory_embedding_jobs(memory_id, model_id, model_version);

CREATE INDEX IF NOT EXISTS idx_embedding_jobs_created
    ON memory_embedding_jobs(created_at DESC);
"""


# ---------------------------------------------------------------------------
# Migration v4 -- full function body
# ---------------------------------------------------------------------------


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Apply the Phase 4 schema: embeddings metadata + embedding jobs.

    Layout
    ------
      1. ``memory_embeddings`` -- persistent vector store.
      2. Two indexes: ``(model_id, model_version, created_at DESC)``
         for "all embeddings for this model", and ``(memory_id)``
         for "does this memory have an embedding?".
      3. ``memory_embedding_jobs`` -- durable worker queue.
      4. Three indexes: pending-queue scan (partial), per-memory
         status check, and recency for the API's "recent jobs"
         view.

    Idempotency
    -----------
    Every statement uses ``CREATE ... IF NOT EXISTS``. No ALTER
    TABLE, so no ``_column_exists`` guard needed. A crash between
    the DDL and ``_record_version(conn, 4)`` re-runs the whole
    function safely -- every object is skipped (already exists).

    Migration v4 does NOT create the ``memory_vectors`` virtual table
    -----------------------------------------------------------------
    That table is a sqlite-vec ``vec0`` virtual table, requiring:

      1. the extension to be loaded (``sqlite_vec.load(conn)``),
      2. a FIXED dimension (``FLOAT[N]``), which is the property of
         the currently-active embedding model.

    Neither condition is guaranteed inside ``apply_schema``:

      * the migration runs on the shared repository connection,
        which does NOT load sqlite-vec by default (loading it is
        an opt-in performed by ``vector_repository.py`` at the
        moment it needs to write a vector),
      * the dimension changes when the operator switches models,
        at which point the old table must be dropped and a new
        one created -- a per-connection operation, not a
        once-per-schema one.

    The vector_repository (Task 4.4) therefore:
      * creates ``memory_vectors`` on first use, with the
        active model's dimension,
      * drops and re-creates it when the dimension changes
        (via the reindex path),
      * verifies the extension is loaded, raising a
        ``MemoryUnavailableError`` when it is not.

    This keeps the migration pure SQLite-versioned DDL, and
    isolates the extension's lifecycle to the single module that
    actually uses it.
    """
    conn.executescript(_V4_MEMORY_EMBEDDINGS_DDL)
    conn.executescript(_V4_MEMORY_EMBEDDINGS_INDEXES_DDL)
    conn.executescript(_V4_MEMORY_EMBEDDING_JOBS_DDL)
    conn.executescript(_V4_MEMORY_EMBEDDING_JOBS_INDEXES_DDL)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
#
# (target_version, apply_function). Ordered ascending. Adding a new
# migration means:
#
#   1. Add ``_apply_v<N>`` above.
#   2. Append ``(N, _apply_v<N>)`` here.
#   3. Bump ``CURRENT_VERSION`` to N.
#   4. Update ``tests/memory/test_repository.py`` to exercise the
#      new migration if it changes any user-visible behavior.

_MIGRATIONS: List[Tuple[int, Any]] = [
    (1, _apply_v1),
    (2, _apply_v2),
    (3, _apply_v3),
    (4, _apply_v4),
]


# ---------------------------------------------------------------------------
# Public API -- apply
# ---------------------------------------------------------------------------


def _get_recorded_version(conn: sqlite3.Connection) -> int:
    """Return the highest recorded schema version, or 0 if none."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM memory_schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        # The version table does not exist yet -- fresh database.
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    """Record ``version`` as applied. Idempotent via INSERT OR IGNORE.

    Commits IMMEDIATELY after the INSERT so the write transaction
    does not remain open when ``apply_schema`` returns.

    Why this matters
    ----------------
    SQLite permits only ONE writer at a time per database file.
    An open write transaction on one connection BLOCKS every
    other connection's attempt to write to the same file --
    they wait up to ``busy_timeout`` milliseconds, then raise
    ``database is locked``.

    The scenario this prevents:

      1. FastAPI's lifespan runs on the main thread and opens
         connection A. ``apply_schema(conn_A)`` runs the DDL
         (autocommit) and then ``INSERT INTO
         memory_schema_version`` (which -- without an explicit
         commit -- starts a write transaction that stays open).

      2. The lifespan continues. Connection A is now stored in
         the repository's ``_connections`` list for cleanup,
         still inside that open write transaction.

      3. A request arrives. FastAPI runs the handler on a
         WORKER thread, which gets connection B from
         ``threading.local``.

      4. The handler calls ``provider.create(...)``, which does
         ``with conn_B: conn_B.execute("INSERT INTO memories
         ...")``. SQLite tries to acquire the write lock --
         held by conn A's never-committed INSERT -- blocks for
         30 seconds, then raises ``database is locked``.

    Committing inside ``_record_version`` releases the write
    lock before ``apply_schema`` returns, so any concurrent
    writer on any thread can acquire it immediately.
    """
    conn.execute(
        "INSERT OR IGNORE INTO memory_schema_version "
        "(version, applied_at) VALUES (?, ?)",
        (version, time.time()),
    )
    # Ensure the write lock is released before returning. A crash
    # between ``execute`` and ``commit`` re-runs the (idempotent)
    # INSERT on next startup -- no data loss, no drift.
    conn.commit()


def apply_schema(conn: sqlite3.Connection) -> int:
    """Ensure ``conn``'s database is at ``CURRENT_VERSION``.

    Idempotent: safe to call on every startup, on an empty database,
    on a partially-migrated database, and on a database already at
    ``CURRENT_VERSION``.

    Parameters
    ----------
    conn
        An OPEN SQLite connection. The caller (``SqliteMemoryRepository``)
        owns transaction scope, pragmas, and thread-local setup.

    Returns
    -------
    int
        The schema version AFTER this call. Equal to
        ``CURRENT_VERSION`` on success.

    Raises
    ------
    MemoryUnavailableError
        When FTS5 is not compiled into the running SQLite library
        (``OperationalError('no such module: fts5')``). This is a
        deployment-level problem the operator must fix before the
        memory subsystem can function.
    sqlite3.Error
        For any other SQL-level failure (corrupt file, permission
        denied, disk full). The caller (repository) wraps these in
        ``MemoryUnavailableError`` with a retry hint.
    """
    # 1. Ensure the version-tracking table exists. This is a single
    #    statement executed directly (not via executescript) so it
    #    is safe even on a locked database.
    try:
        conn.execute(_VERSION_TABLE_DDL)
    except sqlite3.OperationalError as exc:
        # Some custom SQLite builds reject even ``CREATE TABLE IF
        # NOT EXISTS`` due to an immutable-db lock. Surface this
        # with enough context to be diagnosable.
        raise MemoryUnavailableError(
            f"Failed to create memory_schema_version table: {exc}. "
            f"The database may be open in read-only mode or "
            f"locked by another process.",
            context={"sqlite_error": str(exc)},
            cause=exc,
        ) from exc

    # 2. Read the current version.
    current_version = _get_recorded_version(conn)

    # 3. Apply each pending migration in order.
    for target_version, apply_fn in _MIGRATIONS:
        if current_version >= target_version:
            continue

        logger.info(
            "Applying memory schema migration v%d (current=%d).",
            target_version,
            current_version,
        )

        try:
            apply_fn(conn)
        except sqlite3.OperationalError as exc:
            err_str = str(exc).lower()
            if "no such module" in err_str and "fts5" in err_str:
                # FTS5 missing from the SQLite build. This is not
                # retryable at runtime -- the operator must install
                # an FTS5-enabled Python/SQLite. Flag it clearly.
                raise MemoryUnavailableError(
                    "The running SQLite library does not include the "
                    "FTS5 extension. The memory subsystem requires "
                    "FTS5 for full-text search. Install an "
                    "FTS5-enabled Python (standard CPython >= 3.11 "
                    "bundles it) or rebuild SQLite with "
                    "-DSQLITE_ENABLE_FTS5.",
                    context={
                        "required_extension": "fts5",
                        "sqlite_version": _sqlite_version(conn),
                    },
                    cause=exc,
                ) from exc
            raise MemoryUnavailableError(
                f"Failed to apply memory schema migration "
                f"v{target_version}: {exc}",
                context={
                    "migration_version": target_version,
                    "sqlite_error": str(exc),
                },
                cause=exc,
            ) from exc

        # Record the version -- INSERT OR IGNORE so a crash between
        # apply_fn and this line is recovered by the next run.
        _record_version(conn, target_version)
        current_version = target_version

        logger.info(
            "Memory schema migration v%d applied successfully.",
            target_version,
        )

    # Defense in depth: commit any residual writes.
    #
    # ``_record_version`` already commits, but this trailing commit
    # covers two edge cases:
    #
    #   1. A future migration function that adds writes but forgets
    #      to commit. The trailing commit releases its lock so
    #      concurrent writers do not time out.
    #
    #   2. A migration function that crashes mid-loop AFTER opening
    #      a transaction. SQLite rolls back on connection close,
    #      but the connection stays alive in the pool -- the
    #      trailing commit (which succeeds in this case because
    #      SQLite allows commit of an empty transaction) normalizes
    #      the state.
    #
    # When no migrations ran (the database was already at
    # CURRENT_VERSION), there is no pending transaction, and the
    # commit is a harmless no-op.
    try:
        conn.commit()
    except sqlite3.Error as exc:
        # A failure to commit here would be caught on the NEXT
        # operation anyway (still locked). Log and let the caller
        # surface the error with better context.
        logger.warning(
            "Final commit in apply_schema failed: %s", exc
        )

    return current_version


# ---------------------------------------------------------------------------
# Public API -- FTS rebuild (recovery)
# ---------------------------------------------------------------------------


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from the authoritative ``memories`` table.

    Called when:
      * ``schema_status(conn)["needs_rebuild"]`` is True
        (memories / fts row counts diverge -- usually after a
        hard crash during a write),
      * the operator manually requests a rebuild via P1.7's
        maintenance endpoint.

    FTS5 supports this via its special ``'rebuild'`` command on the
    external content table. The rebuild reads every row of
    ``memories`` and regenerates the inverted index -- O(N) with N
    = number of entries. For typical deployments (thousands of
    entries), the rebuild takes under a second.

    Safe to call concurrently? NO. The rebuild holds a write lock
    on ``memories_fts`` for its duration; other writers block.
    Callers must ensure no other memory write is in flight (the
    service's write pipeline takes a lock, so this is naturally
    serialized in production).
    """
    try:
        conn.execute(
            "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"
        )
    except sqlite3.OperationalError as exc:
        raise MemoryUnavailableError(
            f"Failed to rebuild FTS5 index: {exc}",
            context={"operation": "fts_rebuild"},
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Public API -- diagnostics
# ---------------------------------------------------------------------------


def schema_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Return a diagnostic snapshot of the memory schema.

    Used by:
      * the repository's ``__init__`` (informational log line),
      * P1.7's health endpoint,
      * the ``test_repository`` test that asserts the schema is
        fully applied after ``apply_schema``.

    Returns a dict with the following keys:

      * ``current_version`` -- schema version currently recorded,
      * ``target_version``  -- ``CURRENT_VERSION`` (what this
                                module knows how to produce),
      * ``up_to_date``      -- ``current_version >= target_version``,
      * ``memories_count``  -- row count of ``memories``,
      * ``fts_count``       -- row count of ``memories_fts``,
      * ``needs_rebuild``   -- True when the two counts diverge
                                (only meaningful when ``up_to_date``),
      * ``fts5_available``  -- True when FTS5 is compiled into the
                                running SQLite (checked via a
                                ``PRAGMA compile_options`` probe).

    All integer values are non-negative. All booleans are strict
    ``bool`` (not truthy ints) so callers can serialize the dict
    to JSON without special-casing.
    """
    current_version = _get_recorded_version(conn)
    up_to_date = current_version >= CURRENT_VERSION

    memories_count = _safe_count(conn, "memories")
    fts_count = _safe_count(conn, "memories_fts")
    needs_rebuild = (
        up_to_date
        and memories_count > 0
        and memories_count != fts_count
    )

    return {
        "current_version": current_version,
        "target_version": CURRENT_VERSION,
        "up_to_date": up_to_date,
        "memories_count": memories_count,
        "fts_count": fts_count,
        "needs_rebuild": needs_rebuild,
        "fts5_available": _fts5_available(conn),
        # Phase 4 diagnostics -- additive fields. Both are False/0
        # on pre-v4 databases and on any connection where the
        # respective extension is not loaded. Callers that expect
        # only the pre-v4 keys are unaffected (dict access by key).
        "vec0_available": _vec0_available(conn),
        "embedding_count": _safe_count(conn, "memory_embeddings"),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_count(conn: sqlite3.Connection, table_name: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` with a safe fallback."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _sqlite_version(conn: sqlite3.Connection) -> str:
    """Return the SQLite library version as a string."""
    try:
        row = conn.execute("SELECT sqlite_version()").fetchone()
        return str(row[0]) if row else "unknown"
    except sqlite3.Error:
        return "unknown"


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True when FTS5 is compiled into the running SQLite.

    Uses ``PRAGMA compile_options`` when available (SQLite >=
    3.6.18); falls back to attempting a no-op FTS5 CREATE in a
    savepoint when the pragma is not exposed. The fallback path
    leaves no trace because the savepoint is rolled back.
    """
    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
        for row in rows:
            value = str(row[0]).upper()
            if "ENABLE_FTS5" in value:
                return True
    except sqlite3.Error:
        pass

    # Fallback: try a probe. Uses SAVEPOINT so failure leaves no
    # side effects, even on databases where DDL is otherwise
    # restricted.
    try:
        conn.execute("SAVEPOINT fts5_probe")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)"
            )
            conn.execute("DROP TABLE temp.fts5_probe")
            return True
        finally:
            conn.execute("RELEASE fts5_probe")
    except sqlite3.Error:
        return False


def _vec0_available(conn: sqlite3.Connection) -> bool:
    """Return True when sqlite-vec is loaded on this connection.

    Detection strategy
    ------------------
    The ``sqlite-vec`` extension exports a SQL function
    ``vec_version()``. When the extension is loaded on the
    connection, this call succeeds and returns a version string.
    When it is not loaded, SQLite raises
    ``OperationalError: no such function: vec_version`` -- which
    we catch and translate to ``False``.

    Why not ``SELECT * FROM pragma_module_list``?
    ---------------------------------------------
    The virtual-table module list can be inspected via
    ``pragma_module_list``, and a ``vec0`` entry would indicate
    availability. However, this pragma is only stable on recent
    SQLite builds and its column shape has changed across minor
    versions (3.30 vs 3.40). Calling the extension's exported
    function is the documented, version-independent check.

    Why not just try ``CREATE VIRTUAL TABLE temp.t USING
    vec0(...)``?
    ---------------------------------------------
    That would CREATE and DROP a temporary table on every
    diagnostics call -- measurable overhead in a hot path
    (``schema_status`` runs on every repository init). The
    ``vec_version()`` probe is a single SELECT with no DDL, no
    locks, no I/O.

    This function DOES NOT attempt to load the extension. Loading
    is the responsibility of ``vector_repository.py`` (Task 4.4),
    which owns the extension's lifecycle on the connection. The
    migration only needs to DETECT whether it happens to be
    loaded already -- a False result here is expected on a fresh
    repository connection and is not an error.
    """
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CURRENT_VERSION",
    "apply_schema",
    "rebuild_fts",
    "schema_status",
]
