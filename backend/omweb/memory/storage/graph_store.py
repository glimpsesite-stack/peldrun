# backend/omweb/memory/storage/graph_store.py
"""
PELDRUN Memory Platform - SQLite Knowledge Graph Store.

The persistence half of the Phase 6 knowledge graph. Owns every SQL
statement against the three v6 tables:

    graph_entities         -- one row per identity key
                              ``(entity_type, canonical_name)``
    graph_relations        -- typed, directed edges between entities
    memory_entity_links    -- which memories mention which entities

Layering
--------
``peldrun`` defines WHAT an entity/relation is
(``peldrun.memory.graph.contracts``). This module decides HOW they
are stored, and it is the only place that knows the tables exist.
``peldrun`` never imports it.

Connection reuse
----------------
Like ``vector_hydration`` and ``vector_repository``, this class does
NOT own a database connection. It is handed the live
``SqliteMemoryRepository`` and borrows its thread-local connection
via ``_get_conn()``. Rationale:

  * SQLite permits one writer per file; a second connection pool
    would multiply ``database is locked`` windows for no benefit.
  * The schema is already applied (the repository applies it lazily
    on first ``_get_conn``), so no migration call is needed here.
  * Shutdown ordering stays simple: closing the repository closes
    this store.

Design invariants
-----------------

1. **Every write is idempotent.**
   ``add_entity`` derives the primary key from
   ``(entity_type, canonical_name)`` (``make_entity_id``), so a
   duplicate observation collides on the primary key and is skipped
   by ``INSERT OR IGNORE``. ``add_relation`` does the same with
   ``make_relation_id``. ``link_memory_to_entity`` uses
   ``INSERT OR REPLACE`` on the ``(memory_id, entity_id)`` primary
   key, so re-indexing a memory refreshes the weight instead of
   duplicating the link. This is what makes the background indexer
   safe to re-run and safe to run twice concurrently.

2. **Traversal is capped THREE ways.**
   ``traverse`` enforces ``max_depth``, ``max_relations_per_entity``
   AND ``max_total_relations``. Depth alone is not enough: with 20
   relations per node, depth 2 is 400 edges and depth 3 is 8000 --
   each becoming a separate hydration query. The total cap converts
   a pathological graph into a bounded cost, and the per-node cap
   prevents one hub entity (a TOPIC mentioned in every memory) from
   consuming the entire budget on its own.

3. **Breadth-first, deterministic order.**
   Nodes are expanded queue-style and neighbours are ordered by
   ``confidence DESC, relation_id ASC``. BFS guarantees the recorded
   ``distance`` is the SHORTEST hop distance (which the
   ``1/(1+distance)`` scoring depends on), and the tie-break makes
   two identical traversals return identical lists -- required for
   reproducible retrieval and for golden tests.

4. **Scope is enforced at hydration, not at the link table.**
   ``get_memories_for_entity`` accepts ``scope_refs`` and filters on
   the memory's scope. The link table itself has no scope column: an
   entity is global, but a memory is scoped, and graph results MUST
   respect the same scope boundary as lexical/vector search.
   Omitting the filter would leak one project's memories into
   another's retrieval -- a correctness AND privacy bug, not a
   ranking nit.

5. **Dangling links are tolerated.**
   ``memory_entity_links`` has no foreign key to ``memories`` (see
   migration v6's rationale), so a purged memory leaves an orphan
   link row. Every read path JOINs against ``memories``, so an
   orphan simply produces no row. ``prune_orphan_links`` exists for
   the maintenance path that wants to reclaim the space.

6. **One corrupt row never breaks a read.**
   Row reconstruction goes through the repository's
   ``_rows_to_entries``, which drops unparseable rows with a
   warning. Graph search degrades to "fewer results", never to a
  500.

See also
--------

    * ``omweb/memory/storage/migrations.py`` -- ``_apply_v6`` DDL
    * ``omweb/memory/storage/repository.py``  -- connection owner
    * ``omweb/memory/graph/indexer.py``        -- write-time caller
    * ``peldrun/memory/graph/contracts.py``    -- value objects
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from peldrun.memory.domain import MemoryEntry
from peldrun.memory.graph.contracts import (
    Entity,
    Relation,
    RelationHop,
    canonicalize_name,
    make_entity_id,
    make_relation_id,
    normalize_entity_type,
)

from omweb.memory.storage.repository import SqliteMemoryRepository


logger = logging.getLogger(
    "omweb.memory.storage.graph_store"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_DEPTH: int = 2
"""Default BFS depth.

Two hops is the Phase 6 sweet spot: it answers "who is the user's
manager" (person -> org -> memories) and "what does my manager think
about the project" (person -> project -> statements) while keeping
the fan-out tractable. Depth 3 is exposed but discouraged -- see the
class docstring's cap rationale.
"""


DEFAULT_MAX_RELATIONS_PER_ENTITY: int = 20
"""Upper bound on edges EXPANDED per node during a traversal.

Hub entities (a broad TOPIC like "Python") can be adjacent to
hundreds of relations. Without this cap, ONE hub consumes the entire
traversal budget and the query returns only hub noise. 20 keeps the
most confident neighbours (ordered by confidence) and moves on.
"""


DEFAULT_MAX_TOTAL_RELATIONS: int = 100
"""Absolute ceiling on edges collected across the whole traversal.

This is the real guard against combinatorial explosion. It bounds
BOTH the CPU (BFS work) and, more importantly, the I/O: each
collected edge later becomes a ``get_memories_for_entity`` query. A
traversal that hits this cap returns a valid, truncated result --
logged at DEBUG -- rather than stalling a user request.
"""


DEFAULT_MEMORIES_PER_ENTITY: int = 50
"""Cap on memories hydrated for ONE entity.

An entity's memory list is fed into the retrieval fusion, which only
needs a handful of candidates per entity. 50 bounds the query for an
entity mentioned in thousands of memories.
"""


_ENTITY_TABLE: str = "graph_entities"
_RELATION_TABLE: str = "graph_relations"
_LINK_TABLE: str = "memory_entity_links"


#: Columns needed to reconstruct a ``MemoryEntry`` via the
#: repository's ``_row_to_entry``. Listed explicitly (rather than
#: ``SELECT m.*``) so the JOIN stays index-friendly and a future
#: column addition does not silently change the row shape the
#: reconstruction code assumes.
_MEMORY_COLUMNS: str = (
    "m.memory_id, m.memory_type, m.scope_type, m.scope_id, "
    "m.content, m.normalized_content, m.tags_json, m.importance, "
    "m.confidence, m.status, m.is_pinned, m.source_type, "
    "m.source_chat_id, m.source_run_id, m.source_turn_id, "
    "m.imported_from, m.content_hash, m.created_at, m.updated_at, "
    "m.last_used_at, m.expires_at, m.extraction_meta_json"
)


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


class GraphStore:
    """SQLite-backed store for entities, relations, and memory links.

    Every public method is ``async`` and delegates the blocking
    SQLite work to ``asyncio.to_thread`` -- the same discipline
    ``SqliteMemoryRepository`` follows, so a slow query never blocks
    the event loop that is also serving other requests.

    Construct once per process and reuse: the class is stateless
    (it holds only the repository reference), so concurrent calls
    from multiple tasks are safe.
    """

    def __init__(self, memory_repository: SqliteMemoryRepository) -> None:
        """Bind the store to an existing repository.

        Parameters
        ----------
        memory_repository
            The live repository. Its thread-local connections are
            borrowed, and its ``_row_to_entry`` reconstruction is
            reused for hydration. The store does NOT open, close, or
            own anything on it.

        Raises
        ------
        TypeError
            When ``memory_repository`` is ``None``. A store with no
            connection source is a wiring bug that should surface at
            construction, not on the first query.
        """
        if memory_repository is None:
            raise TypeError(
                "GraphStore requires a SqliteMemoryRepository for "
                "its connection; got None."
            )
        self._repo = memory_repository

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return the repository's connection for THIS thread.

        Thin alias so the SQL bodies below read cleanly and so a
        future change to the connection strategy is a one-line edit
        here instead of a search-and-replace across the module.
        """
        return self._repo._get_conn()

    # ------------------------------------------------------------------
    # Entities -- write
    # ------------------------------------------------------------------

    async def add_entity(self, entity: Entity) -> Entity:
        """Insert ``entity`` or return the existing row for its key.

        Idempotent on ``(entity_type, canonical_name)``: the primary
        key is derived from that pair, so a second call for the same
        identity is a no-op insert followed by a read of the
        existing row.

        Aliases
        -------
        An existing entity's alias set is UNIONED with the incoming
        one. So "Ahmed" indexed from memory A and later observed as
        "Ahmed" with an extra alias from memory B ends up with both
        aliases, never with the last writer's set. The alias merge
        also refreshes ``updated_at``, which is what makes
        ``idx_entities_type_updated`` meaningful as a "recently
        touched entities" feed.

        Parameters
        ----------
        entity
            An ``Entity``. ``entity_id`` may be empty (the normal
            extractor case); a non-empty id is IGNORED in favour of
            the derived one, because honoring it could produce two
            rows for one identity and break the dedupe guarantee.

        Returns
        -------
        Entity
            The PERSISTED entity, with ``entity_id`` set and the
            merged alias tuple.

        Raises
        ------
        ValueError
            When the entity's canonical name folds to empty (cannot
            happen through ``Entity``, which rejects empty names,
            but guarded because a bad id would merge unrelated
            rows).
        """
        return await asyncio.to_thread(self._add_entity_sync, entity)

    def _add_entity_sync(self, entity: Entity) -> Entity:
        """Blocking implementation of :meth:`add_entity`."""
        canonical = entity.canonical_name or canonicalize_name(
            entity.name
        )
        entity_type = normalize_entity_type(entity.entity_type)
        entity_id = make_entity_id(entity_type, canonical)
        now = time.time()

        conn = self._conn()
        with conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {_ENTITY_TABLE} (
                    entity_id, entity_type, name, canonical_name,
                    aliases_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    entity_type,
                    entity.name,
                    canonical,
                    json.dumps(list(entity.aliases), ensure_ascii=False),
                    now,
                    now,
                ),
            )

            row = conn.execute(
                f"""
                SELECT entity_id, entity_type, name, canonical_name,
                       aliases_json
                  FROM {_ENTITY_TABLE}
                 WHERE entity_id = ?
                """,
                (entity_id,),
            ).fetchone()

            if row is None:
                # Cannot happen with the INSERT above unless the row
                # was deleted between the two statements. Surface the
                # honest failure instead of returning an unpersisted
                # entity that callers would then link by a
                # non-existent id.
                raise RuntimeError(
                    "GraphStore.add_entity: row vanished immediately "
                    f"after insert (entity_id={entity_id!r})."
                )

            stored_aliases = _load_aliases(row["aliases_json"])
            merged = _merge_aliases(stored_aliases, entity.aliases)
            aliases_changed = merged != stored_aliases

            if aliases_changed:
                conn.execute(
                    f"""
                    UPDATE {_ENTITY_TABLE}
                       SET aliases_json = ?, updated_at = ?
                     WHERE entity_id = ?
                    """,
                    (
                        json.dumps(
                            list(merged), ensure_ascii=False
                        ),
                        now,
                        entity_id,
                    ),
                )

        return Entity(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            name=row["name"],
            canonical_name=row["canonical_name"],
            aliases=merged,
        )

    # ------------------------------------------------------------------
    # Relations -- write
    # ------------------------------------------------------------------

    async def add_relation(self, relation: Relation) -> Relation:
        """Insert ``relation`` or return the existing identical edge.

        Idempotent on ``(source, target, type)``: the relation id is
        derived from that triple (``make_relation_id``), and the v6
        schema additionally declares a UNIQUE index on it, so a
        duplicate insert is skipped even if a caller supplied a
        different id.

        Returns
        -------
        Relation
            The PERSISTED relation. On a duplicate, the EXISTING
            row is returned -- the first observation wins, including
            its ``source_memory_id`` and ``confidence``. Overwriting
            would make the edge's provenance depend on indexing
            order, which is not reproducible.

        Raises
        ------
        sqlite3.IntegrityError
            When either endpoint entity does not exist. That is a
            genuine caller bug (the v6 foreign keys exist precisely
            to catch it) and is re-raised rather than swallowed:
            silently dropping an edge would hide the missing-entity
            bug that caused it. ``GraphIndexer`` catches it and logs,
            so the write path is still safe.
        """
        return await asyncio.to_thread(self._add_relation_sync, relation)

    def _add_relation_sync(self, relation: Relation) -> Relation:
        """Blocking implementation of :meth:`add_relation`."""
        relation_id = relation.relation_id or make_relation_id(
            relation.source_entity_id,
            relation.target_entity_id,
            relation.relation_type,
        )
        now = time.time()

        conn = self._conn()
        with conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {_RELATION_TABLE} (
                    relation_id, source_entity_id, target_entity_id,
                    relation_type, source_memory_id, confidence,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.relation_type,
                    relation.source_memory_id,
                    relation.confidence,
                    now,
                ),
            )

            # Read back whatever actually won the insert. The UNIQUE
            # index means an id collision is possible even when the
            # caller minted its own id, so the lookup is by TRIPLE,
            # not by id -- that is the real identity of the edge.
            row = conn.execute(
                f"""
                SELECT relation_id, source_entity_id,
                       target_entity_id, relation_type,
                       source_memory_id, confidence
                  FROM {_RELATION_TABLE}
                 WHERE source_entity_id = ?
                   AND target_entity_id = ?
                   AND relation_type = ?
                """,
                (
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.relation_type,
                ),
            ).fetchone()

        if row is None:
            raise RuntimeError(
                "GraphStore.add_relation: edge vanished immediately "
                f"after insert (relation_id={relation_id!r})."
            )

        return Relation(
            relation_id=row["relation_id"],
            source_entity_id=row["source_entity_id"],
            target_entity_id=row["target_entity_id"],
            relation_type=row["relation_type"],
            source_memory_id=row["source_memory_id"],
            confidence=float(row["confidence"]),
        )

    # ------------------------------------------------------------------
    # Memory links -- write
    # ------------------------------------------------------------------

    async def link_memory_to_entity(
        self,
        memory_id: str,
        entity_id: str,
        weight: float = 1.0,
    ) -> None:
        """Bind a memory to an entity (idempotent upsert).

        ``INSERT OR REPLACE`` on the ``(memory_id, entity_id)``
        primary key: re-indexing the same memory refreshes ``weight``
        instead of adding a second link, so repeated indexing cannot
        inflate an entity's memory count.

        Parameters
        ----------
        memory_id, entity_id
            Both non-empty. A blank value would create an unusable
            link row; rejected before touching the database.
        weight
            Relevance/centrality hint in ``[0, 1]``; clamped. Default
            1.0 = "directly mentioned". Reserved for a future pass
            that distinguishes a central mention from a passing one.

        Raises
        ------
        ValueError
            When either id is empty.
        sqlite3.IntegrityError
            When ``entity_id`` does not exist (the v6 foreign key).
        """
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError(
                "link_memory_to_entity requires a non-empty "
                f"memory_id; got {memory_id!r}"
            )
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError(
                "link_memory_to_entity requires a non-empty "
                f"entity_id; got {entity_id!r}"
            )
        await asyncio.to_thread(
            self._link_memory_sync, memory_id, entity_id, weight
        )

    def _link_memory_sync(
        self, memory_id: str, entity_id: str, weight: float
    ) -> None:
        """Blocking implementation of :meth:`link_memory_to_entity`."""
        try:
            numeric = float(weight)
        except (TypeError, ValueError):
            numeric = 1.0
        if numeric != numeric:  # NaN
            numeric = 1.0
        numeric = min(1.0, max(0.0, numeric))

        conn = self._conn()
        with conn:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {_LINK_TABLE} (
                    memory_id, entity_id, weight
                ) VALUES (?, ?, ?)
                """,
                (memory_id, entity_id, numeric),
            )

    # ------------------------------------------------------------------
    # Entities -- read
    # ------------------------------------------------------------------

    async def find_entity(
        self,
        name: str,
        entity_type: Optional[str] = None,
    ) -> Optional[Entity]:
        """Resolve a surface form to a persisted entity.

        Lookup is by ``canonical_name`` (folded here with the SAME
        rule the extractor used, so "Ahmed", "AHMED", and " ahmed "
        all hit the same row). When ``entity_type`` is given it
        narrows the match; when omitted, the most recently updated
        match wins -- an entity that was just re-indexed is more
        likely to be the one the user means than a stale namesake.

        Returns ``None`` for an unknown name, a blank name, or a
        non-string. This is the expected, frequent case: most
        queries do NOT mention a known entity, and the caller
        (``GraphSearch``) turns ``None`` into "no graph results".
        """
        return await asyncio.to_thread(
            self._find_entity_sync, name, entity_type
        )

    def _find_entity_sync(
        self, name: str, entity_type: Optional[str]
    ) -> Optional[Entity]:
        """Blocking implementation of :meth:`find_entity`."""
        if not isinstance(name, str):
            return None
        canonical = canonicalize_name(name)
        if not canonical:
            return None

        conn = self._conn()
        if entity_type is not None:
            row = conn.execute(
                f"""
                SELECT entity_id, entity_type, name, canonical_name,
                       aliases_json
                  FROM {_ENTITY_TABLE}
                 WHERE canonical_name = ? AND entity_type = ?
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (canonical, normalize_entity_type(entity_type)),
            ).fetchone()
        else:
            row = conn.execute(
                f"""
                SELECT entity_id, entity_type, name, canonical_name,
                       aliases_json
                  FROM {_ENTITY_TABLE}
                 WHERE canonical_name = ?
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (canonical,),
            ).fetchone()

        return _row_to_entity(row)

    async def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Return the entity with ``entity_id``, or ``None``."""
        return await asyncio.to_thread(self._get_entity_sync, entity_id)

    async def find_entities_by_names(
        self,
        names: Sequence[str],
        entity_type: Optional[str] = None,
    ) -> List[Entity]:
        """Resolve MANY surface forms in ONE query.

        This is the hot-path lookup used by ``GraphSearch`` to find
        the entities a user query mentions. Doing it in one
        ``IN (...)`` query matters: a naive per-name ``find_entity``
        loop would issue one indexed SELECT per candidate name (a
        dozen or more per search) on a path that runs on every
        retrieval turn.

        Candidates are folded with ``canonicalize_name`` first, then
        deduplicated, so callers may pass raw query n-grams freely.

        Parameters
        ----------
        names
            Candidate surface forms. Non-strings and blanks are
            ignored. An empty result after filtering returns ``[]``
            without touching the database.
        entity_type
            Optional type filter (rarely useful here: a query rarely
            disambiguates the type).

        Returns
        -------
        List[Entity]
            Matches in no particular order. Duplicate canonical
            names may appear once per distinct ``entity_type`` --
            that is intended, since "Ahmed" (PERSON) and "Ahmed"
            (PLACE) are different nodes.
        """
        return await asyncio.to_thread(
            self._find_entities_by_names_sync, names, entity_type
        )

    def _find_entities_by_names_sync(
        self,
        names: Sequence[str],
        entity_type: Optional[str],
    ) -> List[Entity]:
        """Blocking implementation of :meth:`find_entities_by_names`."""
        canonical: List[str] = []
        seen: set = set()
        for name in names:
            if not isinstance(name, str):
                continue
            key = canonicalize_name(name)
            if not key or key in seen:
                continue
            seen.add(key)
            canonical.append(key)
        if not canonical:
            return []

        # SQLite has a default SQLITE_MAX_VARIABLE_NUMBER (999 on
        # older builds, 32766 on modern ones). 400 candidates is far
        # beyond what a search query yields and stays safely inside
        # every build's limit.
        canonical = canonical[:400]
        placeholders = ", ".join("?" for _ in canonical)
        params: List[Any] = list(canonical)

        sql = (
            "SELECT entity_id, entity_type, name, canonical_name, "
            f"aliases_json FROM {_ENTITY_TABLE} "
            f"WHERE canonical_name IN ({placeholders})"
        )
        if entity_type is not None:
            sql += " AND entity_type = ?"
            params.append(normalize_entity_type(entity_type))
        sql += " ORDER BY updated_at DESC"

        rows = self._conn().execute(sql, params).fetchall()
        entities = [_row_to_entity(row) for row in rows]
        return [entity for entity in entities if entity is not None]

    def _get_entity_sync(self, entity_id: str) -> Optional[Entity]:
        """Blocking implementation of :meth:`get_entity`."""
        if not isinstance(entity_id, str) or not entity_id:
            return None
        row = self._conn().execute(
            f"""
            SELECT entity_id, entity_type, name, canonical_name,
                   aliases_json
              FROM {_ENTITY_TABLE}
             WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchone()
        return _row_to_entity(row)

    async def get_entities_for_memory(
        self, memory_id: str
    ) -> List[Entity]:
        """Return the entities linked to one memory, by weight desc.

        Used by diagnostics ("what did the graph see in this
        memory?") and by the indexer's verification path.
        """
        return await asyncio.to_thread(
            self._get_entities_for_memory_sync, memory_id
        )

    def _get_entities_for_memory_sync(
        self, memory_id: str
    ) -> List[Entity]:
        """Blocking implementation of :meth:`get_entities_for_memory`."""
        if not isinstance(memory_id, str) or not memory_id:
            return []
        rows = self._conn().execute(
            f"""
            SELECT e.entity_id, e.entity_type, e.name,
                   e.canonical_name, e.aliases_json
              FROM {_LINK_TABLE} l
              JOIN {_ENTITY_TABLE} e ON e.entity_id = l.entity_id
             WHERE l.memory_id = ?
             ORDER BY l.weight DESC, e.updated_at DESC
            """,
            (memory_id,),
        ).fetchall()
        entities = [_row_to_entity(r) for r in rows]
        return [e for e in entities if e is not None]

    # ------------------------------------------------------------------
    # Memories -- read
    # ------------------------------------------------------------------

    async def get_memories_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = DEFAULT_MEMORIES_PER_ENTITY,
        scope_refs: Optional[Sequence[str]] = None,
    ) -> List[MemoryEntry]:
        """Return ACTIVE memories linked to ``entity_id``.

        Parameters
        ----------
        entity_id
            The entity to expand.
        limit
            Maximum memories returned (default 50).
        scope_refs
            Canonical ``"<type>:<id>"`` references. When given, only
            memories in those scopes are returned. ``None`` means
            NO scope restriction -- appropriate for maintenance and
            diagnostics, NEVER for user-facing retrieval (see the
            class docstring's invariant #4).

            An EMPTY sequence is treated as "match nothing", not as
            "no restriction": that is the same convention
            ``SqliteMemoryRepository._build_scope_filter`` uses, and
            it prevents a caller whose scope resolution failed from
            accidentally widening to every scope.

        Returns
        -------
        List[MemoryEntry]
            Ordered by link weight desc, then recency. Only
            ``status='active'`` rows: archived/superseded memories
            must never re-enter retrieval through the graph.

        Invalid scope references are logged and treated as
        "match nothing" rather than raising -- a malformed ref must
        not turn a graph lookup into a 500.
        """
        return await asyncio.to_thread(
            self._get_memories_for_entity_sync,
            entity_id,
            limit,
            scope_refs,
        )

    def _get_memories_for_entity_sync(
        self,
        entity_id: str,
        limit: int,
        scope_refs: Optional[Sequence[str]],
    ) -> List[MemoryEntry]:
        """Blocking implementation of :meth:`get_memories_for_entity`."""
        if not isinstance(entity_id, str) or not entity_id:
            return []
        if limit <= 0:
            return []

        params: List[Any] = [entity_id]
        scope_clause = ""
        if scope_refs is not None:
            try:
                clause, scope_params = _build_scope_clause(
                    list(scope_refs)
                )
            except ValueError as exc:
                logger.warning(
                    "GraphStore.get_memories_for_entity: invalid "
                    "scope reference (%s); returning no memories.",
                    exc,
                )
                return []
            scope_clause = f" AND {clause}"
            params.extend(scope_params)
        params.append(int(limit))

        rows = self._conn().execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
              FROM {_LINK_TABLE} l
              JOIN memories m ON m.memory_id = l.memory_id
             WHERE l.entity_id = ?
               AND m.status = 'active'
               {scope_clause}
             ORDER BY l.weight DESC, m.created_at DESC
             LIMIT ?
            """,
            params,
        ).fetchall()

        return self._repo._rows_to_entries(rows)

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def traverse(
        self,
        start_entity_id: str,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_relations_per_entity: int = (
            DEFAULT_MAX_RELATIONS_PER_ENTITY
        ),
        max_total_relations: int = DEFAULT_MAX_TOTAL_RELATIONS,
    ) -> List[RelationHop]:
        """Breadth-first traversal from ``start_entity_id``.

        Edges are followed in BOTH directions (a relation is stored
        once, but "who works at Acme" and "where does Ahmed work"
        are both legitimate two-hop questions), so the walk treats
        the graph as undirected for REACHABILITY while preserving
        each edge's stored direction in the returned ``Relation``.

        Parameters
        ----------
        start_entity_id
            The entity to start from. Empty/unknown -> ``[]``.
            The start node itself is never returned; only edges.
        max_depth
            Maximum hops. ``0`` or negative -> ``[]``. Clamped to
            ``_HARD_MAX_DEPTH``.
        max_relations_per_entity
            Edges expanded per visited node (highest confidence
            first). ``0`` or negative -> ``[]``.
        max_total_relations
            Absolute ceiling on collected edges. When reached, the
            walk stops and returns what it has (logged at DEBUG).

        Returns
        -------
        List[RelationHop]
            One ``RelationHop`` per discovered edge, ordered by
            ``distance`` ascending then ``relation_id`` ascending, so
            the output is deterministic. Each edge appears at most
            once: the FIRST (shortest) discovery wins.

        Why edges and not entities
        --------------------------
        ``GraphSearch`` needs the FAR endpoint and the distance to
        score a memory. Returning edges keeps both available without
        a second lookup, and it lets the searcher ignore edges whose
        far endpoint yields no memories.
        """
        return await asyncio.to_thread(
            self._traverse_sync,
            start_entity_id,
            max_depth,
            max_relations_per_entity,
            max_total_relations,
        )

    def _traverse_sync(
        self,
        start_entity_id: str,
        max_depth: int,
        max_relations_per_entity: int,
        max_total_relations: int,
    ) -> List[RelationHop]:
        """Blocking implementation of :meth:`traverse`."""
        if not isinstance(start_entity_id, str) or not start_entity_id:
            return []
        depth_cap = min(max(0, int(max_depth)), _HARD_MAX_DEPTH)
        per_entity_cap = max(0, int(max_relations_per_entity))
        total_cap = max(0, int(max_total_relations))
        if depth_cap == 0 or per_entity_cap == 0 or total_cap == 0:
            return []

        conn = self._conn()

        # distance of each DISCOVERED entity from the start node.
        distances: Dict[str, int] = {start_entity_id: 0}
        # entity ids whose edges have already been expanded.
        expanded: set = set()
        # relation ids already collected (dedupe by edge identity,
        # since the same edge is seen from both its endpoints).
        seen_relations: set = set()
        collected: List[RelationHop] = []

        frontier: List[str] = [start_entity_id]

        while frontier and len(collected) < total_cap:
            next_frontier: List[str] = []

            for entity_id in frontier:
                if len(collected) >= total_cap:
                    break
                if entity_id in expanded:
                    continue
                expanded.add(entity_id)

                current_distance = distances[entity_id]
                if current_distance >= depth_cap:
                    # Expanding this node would exceed max_depth.
                    continue

                rows = conn.execute(
                    f"""
                    SELECT relation_id, source_entity_id,
                           target_entity_id, relation_type,
                           source_memory_id, confidence
                      FROM {_RELATION_TABLE}
                     WHERE source_entity_id = ?
                        OR target_entity_id = ?
                     ORDER BY confidence DESC, relation_id ASC
                     LIMIT ?
                    """,
                    (entity_id, entity_id, per_entity_cap),
                ).fetchall()

                for row in rows:
                    if len(collected) >= total_cap:
                        break
                    relation = _row_to_relation(row)
                    if relation.relation_id in seen_relations:
                        continue
                    seen_relations.add(relation.relation_id)

                    far_end = (
                        relation.target_entity_id
                        if relation.source_entity_id == entity_id
                        else relation.source_entity_id
                    )
                    hop_distance = current_distance + 1
                    collected.append(
                        RelationHop(
                            relation=relation,
                            distance=hop_distance,
                        )
                    )

                    # Enqueue the far endpoint only on FIRST
                    # discovery: BFS guarantees this distance is the
                    # shortest, so a later (longer) route to the
                    # same node must not overwrite it or expand it
                    # again.
                    if far_end not in distances:
                        distances[far_end] = hop_distance
                        next_frontier.append(far_end)

            frontier = next_frontier

        if len(collected) >= total_cap:
            logger.debug(
                "GraphStore.traverse: hit max_total_relations=%d "
                "starting from %r; result truncated.",
                total_cap,
                start_entity_id,
            )

        collected.sort(
            key=lambda hop: (
                hop.distance,
                hop.relation.relation_id,
            )
        )
        return collected

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def list_unindexed_memories(
        self, *, limit: int = 20
    ) -> List[MemoryEntry]:
        """Return ACTIVE memories that have NO graph links yet.

        This is the work queue for the background backfill. It is
        what makes graph indexing eventually-consistent for writes
        that never went through ``MemoryWritePipeline``:

          * the ``memory_save`` tool writes straight through the
            provider,
          * an import batch,
          * an indexing task that failed or was cancelled at
            shutdown,
          * every memory that existed before graph indexing was
            enabled.

        Ordering is oldest-first so a backlog drains in creation
        order and progress is monotonic -- a memory that keeps
        failing does not starve the rest.

        Parameters
        ----------
        limit
            Maximum entries returned. ``<= 0`` yields ``[]``.

        Returns
        -------
        List[MemoryEntry]
            Fully hydrated entries, so the caller can index them
            without a second lookup.
        """
        return await asyncio.to_thread(
            self._list_unindexed_memories_sync, limit
        )

    def _list_unindexed_memories_sync(
        self, limit: int
    ) -> List[MemoryEntry]:
        """Blocking implementation of :meth:`list_unindexed_memories`."""
        if limit <= 0:
            return []
        rows = self._conn().execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
              FROM memories m
             WHERE m.status = 'active'
               AND NOT EXISTS (
                   SELECT 1 FROM {_LINK_TABLE} l
                    WHERE l.memory_id = m.memory_id
               )
             ORDER BY m.created_at ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return self._repo._rows_to_entries(rows)

    async def delete_memory_links(self, memory_id: str) -> int:
        """Remove every link belonging to ``memory_id``.

        Called when a memory is deleted, so a future re-index of the
        same id cannot resurrect stale entity associations. Returns
        the number of link rows removed.
        """
        return await asyncio.to_thread(
            self._delete_memory_links_sync, memory_id
        )

    def _delete_memory_links_sync(self, memory_id: str) -> int:
        """Blocking implementation of :meth:`delete_memory_links`."""
        if not isinstance(memory_id, str) or not memory_id:
            return 0
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                f"DELETE FROM {_LINK_TABLE} WHERE memory_id = ?",
                (memory_id,),
            )
            return int(cursor.rowcount or 0)

    async def prune_orphan_links(self) -> int:
        """Delete links whose memory no longer exists.

        ``memory_entity_links`` deliberately has no FK to
        ``memories`` (see migration v6), so a purge leaves orphan
        rows behind. They are harmless -- every read JOINs and gets
        nothing -- but they cost space and skew a naive
        ``COUNT(*)`` diagnostic. The maintenance path calls this.

        Returns
        -------
        int
            Number of orphan link rows removed.
        """
        return await asyncio.to_thread(self._prune_orphan_sync)

    def _prune_orphan_sync(self) -> int:
        """Blocking implementation of :meth:`prune_orphan_links`."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                f"""
                DELETE FROM {_LINK_TABLE}
                 WHERE memory_id NOT IN (
                     SELECT memory_id FROM memories
                 )
                """
            )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def count_entities(self) -> int:
        """Total persisted entities."""
        return await asyncio.to_thread(
            _count_sync, self, _ENTITY_TABLE
        )

    async def count_relations(self) -> int:
        """Total persisted relations."""
        return await asyncio.to_thread(
            _count_sync, self, _RELATION_TABLE
        )

    async def count_links(self) -> int:
        """Total memory-to-entity links."""
        return await asyncio.to_thread(
            _count_sync, self, _LINK_TABLE
        )

    def __repr__(self) -> str:
        return (
            f"GraphStore(db={str(self._repo.db_path)!r})"
        )


# ---------------------------------------------------------------------------
# Row conversion helpers
# ---------------------------------------------------------------------------


def _row_to_entity(row: Optional[sqlite3.Row]) -> Optional[Entity]:
    """Convert a ``graph_entities`` row into an ``Entity``.

    Returns ``None`` (with a WARNING) when the row cannot be
    reconstructed -- a corrupt ``aliases_json`` or an unusable name.
    A single bad row must not break a lookup that has other
    candidates.
    """
    if row is None:
        return None
    try:
        return Entity(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            name=row["name"],
            canonical_name=row["canonical_name"],
            aliases=tuple(_load_aliases(row["aliases_json"])),
        )
    except Exception as exc:  # noqa: BLE001 -- one bad row only
        logger.warning(
            "Skipping corrupt graph_entities row "
            "(entity_id=%r): %s",
            row["entity_id"] if "entity_id" in row.keys() else "?",
            exc,
        )
        return None


def _row_to_relation(row: sqlite3.Row) -> Relation:
    """Convert a ``graph_relations`` row into a ``Relation``."""
    return Relation(
        relation_id=row["relation_id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relation_type=row["relation_type"],
        source_memory_id=row["source_memory_id"],
        confidence=float(row["confidence"]),
    )


def _load_aliases(raw: Any) -> List[str]:
    """Parse an ``aliases_json`` column into a list of strings.

    A malformed or non-list payload yields ``[]`` rather than
    raising: aliases are decoration, and a corrupt value must not
    make an entity unreadable.
    """
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, str)]


def _merge_aliases(
    existing: Sequence[str],
    incoming: Sequence[str],
) -> Tuple[str, ...]:
    """Union two alias collections, de-duplicated case-insensitively.

    Order: existing first (it is already persisted and any UI
    ordering the operator saw must not reshuffle), then genuinely
    new incoming spellings. Comparison uses
    ``canonicalize_name`` so "Ahmed Ali" and "ahmed ali" do not both
    appear.
    """
    result: List[str] = []
    seen: set = set()
    for alias in tuple(existing) + tuple(incoming):
        if not isinstance(alias, str):
            continue
        display = " ".join(alias.split())
        if not display:
            continue
        key = canonicalize_name(display)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(display)
    return tuple(result)


def _build_scope_clause(
    scope_refs: Sequence[str],
) -> Tuple[str, List[str]]:
    """Build an aliased ``(m.scope_type, m.scope_id) OR (...)`` clause.

    Mirrors ``SqliteMemoryRepository._build_scope_filter`` but with
    the ``m.`` alias this module's JOINs require, so the two cannot
    silently diverge on the "empty list means match nothing"
    convention.

    Raises
    ------
    ValueError
        When a reference is not a well-formed ``"<type>:<id>"`` pair
        with a non-empty id. The caller maps this to "match nothing"
        instead of raising at the SQLite boundary.
    """
    if not scope_refs:
        return "1=0", []

    clauses: List[str] = []
    params: List[str] = []
    for ref in scope_refs:
        if not isinstance(ref, str):
            raise ValueError(
                f"scope reference must be a string, got {type(ref).__name__}"
            )
        scope_type, sep, scope_id = ref.partition(":")
        if not sep or not scope_type.strip() or not scope_id.strip():
            raise ValueError(
                f"malformed scope reference {ref!r}; expected "
                f"'<type>:<id>'"
            )
        clauses.append("(m.scope_type = ? AND m.scope_id = ?)")
        params.extend([scope_type.strip(), scope_id.strip()])
    return "(" + " OR ".join(clauses) + ")", params


def _count_sync(store: GraphStore, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>``.

    ``table`` is always a module-level constant (never user input),
    so the f-string interpolation is safe -- this is the same
    convention ``migrations._safe_count`` uses.
    """
    try:
        row = store._conn().execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# Hard limits
# ---------------------------------------------------------------------------


_HARD_MAX_DEPTH: int = 3
"""Absolute ceiling on traversal depth, regardless of caller input.

Depth 3 with 20 relations per node is already ~8000 potential edges;
depth 4+ exists only to exhaust memory. A caller asking for more is
clamped rather than rejected so a mis-set configuration degrades to
"bounded" instead of "error".
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_RELATIONS_PER_ENTITY",
    "DEFAULT_MAX_TOTAL_RELATIONS",
    "DEFAULT_MEMORIES_PER_ENTITY",
    "GraphStore",
]
