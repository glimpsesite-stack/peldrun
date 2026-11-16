# backend/omweb/memory/storage/vector_hydration.py
"""
PELDRUN Memory Platform - Vector Search Hydration Backend.

Adapter that satisfies the ``MemoryHydrationBackend`` Protocol
defined in ``peldrun.memory.retrieval.vector_search``. Translates
a list of memory ids (from a vector similarity search) into full
``MemoryEntry`` instances, applying the filters that make the
result usable for retrieval:

  * **Scope isolation** -- only entries whose
    ``(scope_type, scope_id)`` matches one of the caller's
    ``scope_refs`` are returned.
  * **Status filter** -- only ``status='active'`` entries.
  * **Expiry filter** -- only entries whose ``expires_at`` is
    NULL or in the future.
  * **Deletion tolerance** -- ids that no longer exist in
    ``memories`` are skipped silently.

All four filters are pushed into a single SQL query. This is
deliberate:

  * The vector search may return 20-50 candidate ids per query;
    doing 20-50 individual lookups would dwarf the search cost.
  * ``WHERE ... AND ... AND ...`` lets SQLite use the primary key
    index on ``memory_id`` for the initial candidate reduction,
    then evaluate the scope/status/expiry filters against the
    small surviving set.
  * A single query also gives us a stable, atomic view of the
    table -- no chance of an entry appearing between two
    sequential lookups.

Design invariants
-----------------

1. **Uses the memory repository's connection.**
   ``SqliteMemoryRepository`` already owns a thread-local
   connection configured with WAL + busy_timeout 30s + the
   schema. Opening a second connection here would:
     * consume another worker-thread slot,
     * double the file-lock surface on Windows,
     * require duplicating the pragma setup -- which would
       inevitably drift from the repository's setup.

   Reusing the connection is safe because both classes are in
   the same package (``omweb.memory.storage``) and the
   repository exposes ``_get_conn()`` as its stable integration
   point (already used by ``embedding_worker.py``). The method
   is documented as the way to obtain the connection; the
   leading underscore signals "internal to this package", not
   "do not use from a sibling module in the same package".

2. **Reuses the repository's scope-parsing and row-conversion.**
   ``_build_scope_filter`` and ``_row_to_entry`` already exist
   and are the canonical implementations:
     * ``_build_scope_filter`` calls ``_parse_scope_ref`` and
       produces the exact ``(scope_type = ? AND scope_id = ?)
       OR ...`` clause the repository uses for its own search.
     * ``_row_to_entry`` handles JSON tags, extraction-meta
       decoding, enum coercion, and silently returns ``None``
       for corrupt rows.

   Duplicating either would be a guaranteed source of
   divergence. Reuse here is the correct engineering choice,
   not a shortcut.

3. **Filters match the retrieval contract exactly.**
   ``SqliteVecProvider`` (``peldrun.memory.retrieval.vector_search``)
   assumes hydration returns only retrievable entries. That
   means:
     * ``status = 'active'`` -- archived and superseded
       memories are NOT results.
     * Not expired -- ``expires_at IS NULL OR expires_at >
       strftime('%s','now')`` -- same rule as
       ``SqliteMemoryRepository._build_expiry_filter``.

   Applying either rule less strictly would let
   ``SqliteVecProvider`` return entries the lexical path
   cannot, breaking the parity that ``fusion`` (Task 4.6)
   relies on.

4. **No chunking.**
   The vector search returns at most ``limit`` ids (20 by
   default, 50 hard max from
   ``MemoryRetrievalSettings.default_limit``). That is far
   below SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` (999 on older
   builds, 32766 on newer). Adding chunking would be code
   that cannot be triggered.

   A defensive warning is emitted if a caller ever passes more
   than 500 ids -- this should not happen given the current
   settings, but a warning (rather than silent performance
   degradation) is the right signal if a future change
   loosens the limit without updating this invariant.

5. **Corrupt rows are skipped, not fatal.**
   ``_row_to_entry`` returns ``None`` for rows that cannot be
   reconstructed (bad enum value, malformed JSON, missing
   required field). We filter ``None`` and continue. One
   corrupt row must never make hydration fail -- the whole
   point of hydration is to produce *something* useful from
   the vector search, not to fail loudly on a single bad
   record.

6. **No state after the query.**
   The class holds only the injected ``SqliteMemoryRepository``
   reference. Every call is independent; concurrent calls are
   safe because the repository's connection is thread-local
   and SQLite serializes writers under WAL.

See also
--------

    * ``peldrun/memory/retrieval/vector_search.py``
        -- the Protocol this class satisfies + the consumer
    * ``omweb/memory/storage/repository.py``
        -- the connection, scope parser, and row converter
           this class reuses
    * ``omweb/memory/storage/vector_repository.py``
        -- the vector backend that produces the ids
    * ``omweb/memory/jobs/embedding_worker.py``
        -- the other consumer of ``_get_conn()``; this file
           follows the same pattern
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import List

from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.domain.enums import MemoryStatus

from omweb.memory.storage.repository import SqliteMemoryRepository


logger = logging.getLogger(
    "omweb.memory.storage.vector_hydration"
)


# ---------------------------------------------------------------------------
# Sanity limits (see design invariant 4)
# ---------------------------------------------------------------------------


_MAX_EXPECTED_IDS: int = 500
"""Threshold above which hydration emits a warning.

The current retrieval settings cap ``default_limit`` at 50, so a
real call never carries more than 50 ids. Reaching this threshold
means either a settings regression or a caller bypassing
``SqliteVecProvider`` entirely -- both worth a log line, neither
worth a hard error (the query is still correct, just less
index-friendly).
"""


# ---------------------------------------------------------------------------
# SqliteVectorHydration
# ---------------------------------------------------------------------------


class SqliteVectorHydration:
    """Hydrates memory ids into ``MemoryEntry`` rows with scope filtering.

    Satisfies ``peldrun.memory.retrieval.vector_search.MemoryHydrationBackend``
    structurally (the Protocol only checks method presence). The
    ``isinstance`` check works too since the Protocol is
    ``runtime_checkable`` -- useful for construction-time
    validation in ``MemoryService``.

    Lifecycle
    ---------
    Construct with a ``SqliteMemoryRepository`` instance; the
    repository MUST be alive for the lifetime of this hydrator
    (the hydrator borrows its connection on every call and does
    not close it). No ``open()`` / ``close()`` methods -- the
    repository owns both.

    Example
    -------
    ::

        repo = SqliteMemoryRepository(db_path)
        hydrator = SqliteVectorHydration(repo)
        entries = await hydrator.get_entries_by_ids(
            memory_ids=["mem_a", "mem_b", "mem_c"],
            scope_refs=["project:proj_1", "user:default"],
        )
        # ``entries`` contains only ACTIVE, non-expired memories
        # from the requested scopes that still exist.
    """

    def __init__(
        self,
        memory_repository: SqliteMemoryRepository,
    ) -> None:
        """Initialize the hydrator.

        Parameters
        ----------
        memory_repository
            The repository whose connection, scope parsing, and
            row conversion this hydrator reuses. The repository
            MUST remain open for the hydrator's lifetime.

        Raises
        ------
        TypeError
            When ``memory_repository`` is ``None``. Failing
            here is preferable to failing on the first query
            with an opaque ``AttributeError``.
        """
        if memory_repository is None:
            raise TypeError(
                "SqliteVectorHydration requires a "
                "SqliteMemoryRepository instance"
            )
        self._repo = memory_repository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_entries_by_ids(
        self,
        memory_ids: List[str],
        scope_refs: List[str],
    ) -> List[MemoryEntry]:
        """Fetch and filter ``MemoryEntry`` rows for the given ids.

        See the ``MemoryHydrationBackend`` Protocol docstring for
        the full contract. Provider-specific behavior:

        * **Order is not preserved.** The returned list may be
          in any order. The caller (``SqliteVecProvider``)
          re-orders by the vector backend's similarity ranking.
        * **Empty inputs return ``[]``** without a query.
        * **Invalid scope refs raise.**
          ``_build_scope_filter`` calls ``_parse_scope_ref``,
          which raises ``MemoryValidationError`` for malformed
          refs. This is a caller bug, not a "return empty"
          condition -- failing at the boundary gives a clear
          error instead of an empty result with no explanation.
        """
        if not isinstance(memory_ids, list):
            raise TypeError(
                f"memory_ids must be a list, got "
                f"{type(memory_ids).__name__}"
            )
        if not isinstance(scope_refs, list):
            raise TypeError(
                f"scope_refs must be a list, got "
                f"{type(scope_refs).__name__}"
            )
        if not memory_ids or not scope_refs:
            return []

        if len(memory_ids) > _MAX_EXPECTED_IDS:
            logger.warning(
                "SqliteVectorHydration: get_entries_by_ids called "
                "with %d ids (expected <= %d). This suggests a "
                "settings regression in MemoryRetrievalSettings "
                "or a caller bypassing SqliteVecProvider. The "
                "query remains correct but may be slower than "
                "necessary.",
                len(memory_ids),
                _MAX_EXPECTED_IDS,
            )

        return await asyncio.to_thread(
            self._get_entries_by_ids_sync,
            memory_ids,
            scope_refs,
        )

    # ------------------------------------------------------------------
    # Internal -- sync implementation
    # ------------------------------------------------------------------

    def _get_entries_by_ids_sync(
        self,
        memory_ids: List[str],
        scope_refs: List[str],
    ) -> List[MemoryEntry]:
        """Sync implementation of ``get_entries_by_ids``.

        Runs on a worker thread via ``asyncio.to_thread``. All
        repository access -- connection, scope parsing, row
        conversion -- happens here so the async wrapper stays a
        thin ``to_thread`` shim.
        """
        # ---- 1. Build the scope filter clause ---------------
        #
        # Reuses the repository's own builder so the clause
        # matches byte-for-byte what `_search_ranked_sync` emits.
        # If the repository later adds a new scope-related
        # filter (e.g., tenant scoping), this hydrator inherits
        # the fix automatically.
        scope_clause, scope_params = self._repo._build_scope_filter(
            scope_refs
        )
        # _build_scope_filter returns ("1=0", []) for empty input,
        # but we already filtered that above. Keep the defensive
        # check in case a future refactor changes the behavior.
        if not scope_params:
            return []

        # ---- 2. Build the IN-list placeholders -------------
        placeholders = ", ".join("?" for _ in memory_ids)

        # ---- 3. Compose the query --------------------------
        #
        # Filter order is intentional:
        #   1. memory_id IN (...)  -- uses the primary key index
        #      to reduce candidates to at most len(memory_ids).
        #   2. scope clause        -- small set, cheap scan.
        #   3. status = 'active'   -- equality against an indexed
        #      column (idx_memories_scope includes status).
        #   4. expiry check        -- string/numeric comparison
        #      on a nullable column, no index needed since the
        #      row set is already small.
        #
        # SQLite's query planner will reorder as needed, but
        # writing the clauses in this order documents the intent
        # and helps future readers.
        sql = f"""
            SELECT * FROM memories
            WHERE memory_id IN ({placeholders})
              AND {scope_clause}
              AND status = ?
              AND (expires_at IS NULL
                   OR expires_at > strftime('%s','now'))
        """

        params: List[object] = (
            list(memory_ids)
            + list(scope_params)
            + [MemoryStatus.ACTIVE.value]
        )

        # ---- 4. Execute ------------------------------------
        conn = self._repo._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            # Hydration is a best-effort enrichment: a failed
            # query means the vector search still returned
            # valid ids, but we cannot map them to entries.
            # Returning [] (rather than raising) lets
            # SqliteVecProvider surface an empty result, which
            # the retrieval router treats as "no semantic
            # results" -- the correct degradation when the
            # memory store is briefly unavailable.
            logger.warning(
                "SqliteVectorHydration: query failed "
                "(returning empty): %s",
                exc,
            )
            return []

        # ---- 5. Convert rows -> entries --------------------
        #
        # `_row_to_entry` returns None for corrupt rows. We
        # filter those out and log a count, not per-row --
        # repeated hydration of the same query could otherwise
        # flood the log with the same "corrupt row X" message
        # on every retrieval.
        entries: List[MemoryEntry] = []
        corrupt_count = 0
        for row in rows:
            entry = self._repo._row_to_entry(row)
            if entry is not None:
                entries.append(entry)
            else:
                corrupt_count += 1

        if corrupt_count:
            logger.warning(
                "SqliteVectorHydration: skipped %d corrupt row(s) "
                "during hydration (query returned %d raw rows, "
                "%d usable entries)",
                corrupt_count,
                len(rows),
                len(entries),
            )

        logger.debug(
            "SqliteVectorHydration: hydrated %d/%d ids "
            "(%d scope refs)",
            len(entries),
            len(memory_ids),
            len(scope_refs),
        )
        return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["SqliteVectorHydration"]
