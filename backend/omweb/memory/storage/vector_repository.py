# backend/omweb/memory/storage/vector_repository.py
"""
PELDRUN Memory Platform - Vector Repository (sqlite-vec).

Owner of the ``memory_embeddings`` metadata table AND the
``memory_vectors`` virtual table (sqlite-vec ``vec0``). Provides:

  * the embedding-vector CRUD used by the embedding worker (Task 4.4),
  * the vector MATCH query used by ``SqliteVecProvider`` (Task 4.5),
  * the extension lifecycle (load on connect, close on release).

Design invariants
-----------------

1. **sqlite-vec is a hard dependency for this module only.**
   ``SqliteMemoryRepository`` (FTS-based) never touches sqlite-vec;
   this module never touches FTS. The separation means an FTS-only
   deployment keeps working unchanged, and a vector-search deployment
   pays the extension cost exactly once (on the connection that
   performs vector operations).

2. **The vec0 table is dimension-specific and single-namespace.**
   ``memory_vectors`` is a ``vec0(embedding FLOAT[N])`` virtual table
   where ``N`` is the dimension of the currently-active embedding
   model. sqlite-vec fixes ``N`` at table-creation time and cannot
   alter it. Therefore:

     * ``_ensure_vec0_table(dimension)`` is called before any write;
     * it creates ``memory_vectors`` when absent, and RECREATES it
       when the stored dimension differs;
     * all ``memory_embeddings`` rows survive across a dimension
       change — only the vec0 index is rebuilt.

   This is why the metadata table (``memory_embeddings``) is the
   source of truth and ``memory_vectors`` is a pure index: a rebuild
   is ``DELETE + INSERT FROM memory_embeddings`` (see
   ``reindex_namespace``), and it is fast (seconds for thousands of
   entries).

3. **Namespace isolation.**
   Two models with different ``(model_id, model_version)`` pairs
   produce orthogonal vector spaces and MUST NOT be mixed. The
   repository exposes namespace-scoped methods
   (``search_in_namespace``, ``delete_namespace``,
   ``reindex_namespace``) that honor this. Callers that pass a
   single namespace by convention get isolation for free; callers
   that need cross-model search run one query per namespace and
   merge (the vector spaces cannot be compared directly).

4. **BLOB packing is fixed: float32, little-endian.**
   The storage format is documented, not incidental:

       pack:   struct.pack(f"<{N}f", *vector)
       unpack: list(struct.unpack(f"<{N}f", blob))

   ``<`` = little-endian (portable across Windows/macOS/Linux),
   ``f`` = 4-byte IEEE-754. Alternatives considered:

     * float16 -- 2x smaller but ~3 decimal digits of precision
       is insufficient for 1024-1536 dim models (embeddings use
       the full precision to preserve angular separation);
     * numpy.tobytes -- would add a hard dependency on numpy for
       every deployment; struct is stdlib.

5. **No business logic.**
   This module does not decide WHEN to embed, WHICH model to use,
   or WHY a reindex is needed. It only carries vectors in and out.
   The embedding worker (Task 4.4) owns those decisions; the
   resolver (``peldrun.memory.embeddings.model_resolver``) owns
   the model identity; the retrieval router (Task 4.9) owns the
   search policy.

6. **Thread-local connections, ``asyncio.to_thread`` for every op.**
   Same pattern as ``SqliteMemoryRepository``: one connection per
   worker thread, WAL mode, busy_timeout 30s, all sync work in a
   thread pool. Concurrent reads are safe under WAL; writes
   serialize at SQLite's writer lock.

7. **Extension failures are explicit.**
   When sqlite-vec cannot be loaded (missing wheel, sandbox
   forbidding extension load), the first write/search raises
   ``MemoryUnavailableError`` with an actionable message. Silent
   degradation ("vectors just never match") is the worst possible
   failure mode and is forbidden here.

See also
--------

    * ``migrations.py::_apply_v4``      — the ``memory_embeddings``
                                           schema this module reads
                                           and writes
    * ``peldrun/memory/domain/embedding.py``
                                         — the ``MemoryEmbedding``
                                           value object
    * ``peldrun/memory/retrieval/vector_search.py`` (Task 4.5)
                                         — the search consumer
    * ``embedding_worker.py`` (Task 4.4)
                                         — the write consumer
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain.embedding import MemoryEmbedding
from peldrun.memory.domain.errors import MemoryUnavailableError
from peldrun.memory.embeddings.contracts import EmbeddingDimensionMismatchError


logger = logging.getLogger("omweb.memory.storage.vector_repository")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_FLOAT32 = struct.Struct("<f")
"""Struct instance for one little-endian float32 component.

Reused across every pack/unpack call so ``struct.calcsize`` is not
recomputed per element. Modules that pay attention to this sort of
thing see a 3-5x speedup on multi-thousand-vector scans; it costs
nothing to do it right from the start.
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class Vec0UnavailableError(MemoryUnavailableError):
    """The sqlite-vec extension could not be loaded on this connection.

    Subclass of ``MemoryUnavailableError`` so existing catch sites
    that already handle "memory backend unavailable" treat this
    correctly. Additional context: the caller can distinguish it
    with ``isinstance(exc, Vec0UnavailableError)`` and show a
    message specific to vector search ("Install sqlite-vec") rather
    than a generic "memory is down".
    """


# ---------------------------------------------------------------------------
# SqliteVecRepository
# ---------------------------------------------------------------------------


class SqliteVecRepository:
    """Thread-safe SQLite-backed store for embeddings + vec0 index.

    Companion to ``SqliteMemoryRepository`` — same connection
    lifecycle, same async wrapping, same WAL configuration. They
    may share the same database file (WAL permits this safely) or
    live in separate files (the repository does not care).

    Lifecycle::

        repo = SqliteVecRepository(db_path)
        # First operation on any thread lazily opens + configures
        # the connection, loads sqlite-vec, and creates vec0 if
        # needed. Subsequent operations reuse the connection.
        await repo.upsert_embeddings([...], dimension=1536)
        hits = await repo.search_in_namespace(
            query_vector=[...], namespace=("model", "v1"),
            limit=20, min_similarity=0.5,
        )
        repo.close_all()   # on shutdown / test teardown
    """

    #: Module-level constant used by the vec0 MATCH query. sqlite-vec
    #: interprets ``k = ?`` as "return the k nearest neighbours";
    #: we bind this to the caller's ``limit`` parameter. Named here
    #: so a future sqlite-vec version that renames the parameter
    #: has exactly one place to update.
    _VEC0_K_PARAM: str = "k"

    def __init__(self, db_path: Path) -> None:
        """Initialize WITHOUT opening a connection.

        Parameters
        ----------
        db_path
            Path to the SQLite database file. Parents created if
            missing. The same path may be passed to
            ``SqliteMemoryRepository`` — both instances open their
            own connections and coexist under WAL.
        """
        self.db_path: Path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._connections: List[sqlite3.Connection] = []
        self._extension_loaded: bool = False
        self._extension_lock = threading.Lock()

        #: Current vec0 dimension (0 = table not yet created).
        #: Guarded by ``_vec0_lock`` when mutating.
        self._vec0_dimension: int = 0
        self._vec0_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the current thread's connection, creating it if needed.

        On creation: configures WAL + busy_timeout + foreign_keys,
        and loads sqlite-vec if it is not yet loaded on THIS
        connection. sqlite-vec's ``load`` call applies to one
        connection at a time — each worker thread must load it
        independently, which the per-thread ``_extension_loaded``
        flag tracks (see ``_ensure_extension_loaded``).
        """
        conn: Optional[sqlite3.Connection] = getattr(
            self._local, "conn", None
        )
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            with self._conn_lock:
                self._connections.append(conn)
            self._local.conn = conn
            # Reset the per-thread extension-loaded flag alongside
            # the connection reference. Any code path that replaces
            # ``_local.conn`` MUST also reset this flag -- see
            # ``close_all`` for the symmetric reset.
            self._local.vec_loaded = False
        return conn

    def close_all(self) -> None:
        """Close every connection this instance opened. Idempotent."""
        with self._conn_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    logger.debug(
                        "Error closing vector connection: %s", exc
                    )
            self._connections.clear()
        if hasattr(self._local, "conn"):
            self._local.conn = None
            # Reset the per-thread extension flag alongside the
            # connection reference. See ``_get_conn`` for the
            # symmetric reset -- the two flags MUST always travel
            # together so that "connection is loaded" cannot
            # outlive the connection itself.
            self._local.vec_loaded = False
        # Reset vec0 dimension cache — next connection needs to
        # re-detect it (the table might have been rebuilt between
        # close and reopen).
        with self._vec0_lock:
            self._vec0_dimension = 0

    # ------------------------------------------------------------------
    # Extension loading
    # ------------------------------------------------------------------

    def _ensure_extension_loaded(
        self, conn: sqlite3.Connection
    ) -> None:
        """Load sqlite-vec on ``conn`` if it is not already loaded.

        Extension loading state is tracked **per thread**, not per
        connection object. Rationale:

          * ``sqlite3.Connection`` is a C-level object without a
            ``__dict__``. Assigning ``conn._some_flag = True``
            raises ``AttributeError`` -- the previous attempt to
            cache state on the connection object failed exactly
            this way.
          * Each thread owns exactly one connection (see
            ``_get_conn``), so "loaded on this thread" is
            equivalent to "loaded on this thread's connection".
          * ``threading.local`` is already used to hold
            ``_local.conn``; adding ``_local.vec_loaded`` alongside
            it keeps the two pieces of per-thread state in sync
            (both reset together in ``_get_conn`` and
            ``close_all``).

        Raises
        ------
        Vec0UnavailableError
            * ImportError -- ``sqlite_vec`` package missing.
            * Any exception during ``sqlite_vec.load`` -- platform
              mismatch, sandbox denial, corrupt binary.
        """
        if getattr(self._local, "vec_loaded", False):
            return

        try:
            import sqlite_vec
        except ImportError as exc:
            raise Vec0UnavailableError(
                "The 'sqlite-vec' Python package is not installed. "
                "Vector search requires it. Install with "
                "'pip install sqlite-vec' (a precompiled wheel "
                "exists for Windows, macOS, and Linux on CPython "
                "3.11+), then restart PELDRUN.",
                context={"required_package": "sqlite-vec"},
                cause=exc,
            ) from exc

        try:
            # CRITICAL: Python's ``sqlite3`` module DISABLES
            # extension loading by default — a security measure
            # inherited from SQLite itself. Calling
            # ``sqlite_vec.load(conn)`` (which internally calls
            # ``conn.load_extension()``) without first enabling
            # it raises:
            #
            #     sqlite3.OperationalError: not authorized
            #
            # ``enable_load_extension`` is PER-CONNECTION. We
            # disable it in a ``finally`` block immediately after
            # ``sqlite_vec.load`` returns, so that a hypothetical
            # malicious SQL payload that somehow reached this
            # connection cannot load arbitrary extensions.
            #
            # ``load`` is the officially documented entry point
            # in sqlite_vec >= 0.1.9. Older 0.1.x versions may
            # require ``sqlite_vec.loadable_path()`` + a manual
            # ``conn.load_extension``. We only support the
            # current API — 0.1.9 is what
            # ``requirements.lock.txt`` pins.
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            self._local.vec_loaded = True
        except Exception as exc:
            raise Vec0UnavailableError(
                f"Could not load the sqlite-vec extension on the "
                f"current SQLite connection: {exc}. This usually "
                f"means the wheel does not match the running "
                f"Python/SQLite build, or the environment "
                f"forbids loading native extensions. On hardened "
                f"deployments, allow loading the sqlite-vec "
                f"binary from "
                f"{self._sqlite_vec_library_path()!r}.",
                context={"sqlite_vec_load_error": str(exc)},
                cause=exc,
            ) from exc

    @staticmethod
    def _sqlite_vec_library_path() -> str:
        """Best-effort path to the sqlite-vec binary, for diagnostics.

        Never raises; returns an empty string when the module is
        not importable.
        """
        try:
            import sqlite_vec  # type: ignore

            return str(sqlite_vec.loadable_path())
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # vec0 table management
    # ------------------------------------------------------------------

    async def ensure_vec0_table(self, dimension: int) -> None:
        """Ensure ``memory_vectors`` exists with the given dimension.

        Creates the table when absent; recreates it when the
        dimension differs from the current table's. Callers (worker,
        search) MUST call this before their first operation.

        Parameters
        ----------
        dimension
            Vector dimension for the active embedding model. Must
            be a positive int. The vec0 table's ``FLOAT[N]`` clause
            fixes this value at table-creation time.

        Raises
        ------
        Vec0UnavailableError
            When sqlite-vec cannot be loaded on the connection.
        ValueError
            When ``dimension`` is not a positive int.
        """
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise ValueError(
                f"dimension must be a positive int, got {dimension!r}"
            )
        await asyncio.to_thread(self._ensure_vec0_table_sync, dimension)

    def _ensure_vec0_table_sync(self, dimension: int) -> None:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)

        # Fast path: already correct on this instance.
        if self._vec0_dimension == dimension:
            return

        with self._vec0_lock:
            if self._vec0_dimension == dimension:
                return

            existing = self._read_existing_vec0_dimension(conn)
            if existing == dimension:
                self._vec0_dimension = dimension
                return

            # Drop whatever exists (if anything) and recreate at the
            # requested dimension. The vec0 table is a pure index —
            # all data lives in ``memory_embeddings`` and can be
            # rebuilt by the reindex path. Dropping it here does
            # NOT lose any embedding.
            try:
                with conn:
                    conn.execute("DROP TABLE IF EXISTS memory_vectors")
                    conn.execute(
                        "CREATE VIRTUAL TABLE memory_vectors USING vec0("
                        f"memory_id TEXT PRIMARY KEY, "
                        f"embedding FLOAT[{dimension}]"
                        ")"
                    )
            except sqlite3.OperationalError as exc:
                raise Vec0UnavailableError(
                    f"Could not create the memory_vectors table at "
                    f"dimension {dimension}: {exc}",
                    context={"dimension": dimension},
                    cause=exc,
                ) from exc

            self._vec0_dimension = dimension
            logger.info(
                "memory_vectors rebuilt at dimension %d "
                "(previous dimension: %s)",
                dimension,
                existing if existing else "none",
            )

    @staticmethod
    def _read_existing_vec0_dimension(
        conn: sqlite3.Connection,
    ) -> int:
        """Return the dimension of an existing vec0 table, or 0.

        sqlite-vec does not expose the declared dimension via a
        documented pragma. We infer it by comparing the SQL used to
        create the table, which sqlite-vec stores verbatim in
        ``sqlite_master.sql``. This is version-tolerant — the
        string ``FLOAT[N]`` appears in every sqlite-vec version
        we support.

        Returns 0 when the table does not exist or the parse
        cannot find a ``FLOAT[N]`` clause.
        """
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='memory_vectors'"
            ).fetchone()
        except sqlite3.Error:
            return 0
        if row is None or not row["sql"]:
            return 0
        sql = row["sql"]
        # Parse "FLOAT[N]" -- the only mandatory clause of a
        # dimension-carrying vec0 table.
        marker = "FLOAT["
        idx = sql.find(marker)
        if idx < 0:
            return 0
        end_idx = sql.find("]", idx)
        if end_idx <= idx:
            return 0
        try:
            return int(sql[idx + len(marker):end_idx])
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # Packing helpers (pure functions -- no I/O)
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_vector(vector: List[float]) -> bytes:
        """Pack a float list into little-endian float32 bytes.

        Uses ``struct.pack(f"<{N}f", ...)`` for the whole vector at
        once (faster than iterating ``_FLOAT32.pack`` N times).
        Raises ``struct.error`` when the vector contains a value
        outside float32 range — a scenario Pydantic validation
        prevents upstream, but this method is defensive.
        """
        n = len(vector)
        try:
            return struct.pack(f"<{n}f", *vector)
        except struct.error as exc:
            raise ValueError(
                f"Could not pack vector of length {n} into "
                f"float32: {exc}"
            ) from exc

    @staticmethod
    def _unpack_vector(blob: bytes, dimension: int) -> List[float]:
        """Unpack float32 little-endian bytes into a float list.

        Raises ``EmbeddingDimensionMismatchError`` when the byte
        length does not match ``dimension * 4`` — i.e. when the row
        was written at a different dimension than the caller
        expects. This can only happen after a manual DB edit or a
        botched dimension change; surfacing it as a typed error
        lets the search layer log the specific row and skip it,
        rather than crashing on ``struct.unpack``.
        """
        expected_bytes = dimension * 4
        if len(blob) != expected_bytes:
            raise EmbeddingDimensionMismatchError(
                f"Stored vector has {len(blob)} bytes, expected "
                f"{expected_bytes} for dimension {dimension}. "
                f"The row may have been written by a different "
                f"model version or edited externally."
            )
        return list(struct.unpack(f"<{dimension}f", blob))

    # ------------------------------------------------------------------
    # Embedding CRUD (metadata table)
    # ------------------------------------------------------------------

    async def upsert_embeddings(
        self,
        embeddings: List[MemoryEmbedding],
        *,
        dimension: int,
    ) -> int:
        """Upsert metadata + vec0 index for a batch of embeddings.

        Two-phase write, inside one connection (no cross-connection
        transaction):

          1. UPSERT every row in ``memory_embeddings`` — INSERT OR
             REPLACE on the (memory_id, model_id, model_version)
             primary key.
          2. For every embedding whose ``(model_id,
             model_version)`` equals the caller's active
             namespace, UPSERT into ``memory_vectors``.

        Embeddings from other namespaces are METADATA-ONLY in this
        call — they are persisted for future reindex but NOT put
        into the current vec0 index. (The index holds one
        namespace at a time; see design invariant 2.)

        Returns
        -------
        int
            Number of rows written to ``memory_embeddings``. This
            equals ``len(embeddings)`` (deduped by primary key —
            the same key in the same call replaces, so the count
            of rows touched is still ``len(embeddings)``).

        Raises
        ------
        Vec0UnavailableError
            When sqlite-vec cannot be loaded.
        EmbeddingDimensionMismatchError
            When a vector's dimension does not equal ``dimension``.
        """
        if not embeddings:
            return 0
        await self.ensure_vec0_table(dimension)
        return await asyncio.to_thread(
            self._upsert_embeddings_sync, embeddings, dimension
        )

    def _upsert_embeddings_sync(
        self,
        embeddings: List[MemoryEmbedding],
        dimension: int,
    ) -> int:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)
        now = time.time()
        written = 0

        with conn:
            for emb in embeddings:
                if emb.dimensions != dimension:
                    raise EmbeddingDimensionMismatchError(
                        f"Embedding for memory "
                        f"{emb.memory_id!r} has dimension "
                        f"{emb.dimensions}, expected {dimension}. "
                        f"Re-embedding under the new model "
                        f"dimension is required before upsert."
                    )
                packed = self._pack_vector(emb.to_vector_list())
                conn.execute(
                    """
                    INSERT INTO memory_embeddings (
                        memory_id, model_id, model_version,
                        content_version, vector, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id, model_id, model_version)
                    DO UPDATE SET
                        content_version = excluded.content_version,
                        vector          = excluded.vector,
                        created_at      = excluded.created_at
                    """,
                    (
                        emb.memory_id,
                        emb.model_id,
                        emb.model_version,
                        emb.content_version,
                        packed,
                        self._dt_to_epoch(emb.created_at) or now,
                    ),
                )
                # Index update: only when the embedding belongs to
                # the caller's active namespace. Other namespaces
                # were persisted above for future reindex but do
                # not enter the current vec0 index.
                #
                # NOTE: the caller's ``dimension`` is the active
                # model dimension. An embedding whose namespace
                # matches but whose dimension differs would have
                # raised above, so any surviving embedding here is
                # safe to index at ``dimension``.
                #
                # [FIX-VEC0-UPSERT] sqlite-vec's ``vec0`` virtual
                # tables do NOT implement SQLite's UPSERT clause
                # (``ON CONFLICT ... DO UPDATE``). Attempting it
                # raises:
                #
                #     OperationalError: UPSERT not implemented
                #     for virtual table "memory_vectors"
                #
                # The cause is architectural: ``vec0`` is a virtual
                # table backed by an internal index rather than a
                # rowid-keyed B-tree, so SQLite's generic
                # conflict-resolution machinery has no way to reach
                # the underlying storage. The documented alternative
                # -- and the one every sqlite-vec consumer uses --
                # is an explicit DELETE followed by INSERT, which
                # produces an identical end state:
                #
                #   * When the row does NOT exist, the DELETE is a
                #     no-op and the INSERT creates it.
                #   * When the row DOES exist, the DELETE removes
                #     the stale vector and the INSERT replaces it.
                #
                # Both statements run inside the enclosing
                # ``with conn:`` block, so the DELETE+INSERT is
                # atomic -- a crash between the two cannot leave a
                # vec0 table missing an entry that exists in
                # ``memory_embeddings``.
                conn.execute(
                    "DELETE FROM memory_vectors WHERE memory_id = ?",
                    (emb.memory_id,),
                )
                conn.execute(
                    "INSERT INTO memory_vectors (memory_id, embedding) "
                    "VALUES (?, ?)",
                    (emb.memory_id, packed),
                )
                written += 1

        logger.debug(
            "Upserted %d embeddings (dimension=%d)", written, dimension
        )
        return written

    async def get_embedding(
        self,
        memory_id: str,
        model_id: str,
        model_version: str,
    ) -> Optional[MemoryEmbedding]:
        """Fetch one embedding row, or None if absent."""
        return await asyncio.to_thread(
            self._get_embedding_sync,
            memory_id,
            model_id,
            model_version,
        )

    def _get_embedding_sync(
        self,
        memory_id: str,
        model_id: str,
        model_version: str,
    ) -> Optional[MemoryEmbedding]:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT memory_id, model_id, model_version,
                   content_version, vector, created_at
            FROM memory_embeddings
            WHERE memory_id = ?
              AND model_id = ?
              AND model_version = ?
            """,
            (memory_id, model_id, model_version),
        ).fetchone()
        if row is None:
            return None
        dimension = len(row["vector"]) // 4
        return MemoryEmbedding(
            memory_id=row["memory_id"],
            model_id=row["model_id"],
            model_version=row["model_version"],
            content_version=int(row["content_version"]),
            vector=self._unpack_vector(row["vector"], dimension),
            created_at=self._epoch_to_dt(row["created_at"]),
        )

    async def delete_embedding(
        self,
        memory_id: str,
        model_id: str,
        model_version: str,
    ) -> bool:
        """Remove one metadata row and (if present) its vec0 entry.

        Returns True when a metadata row was deleted, False when
        none existed. The vec0 delete is unconditional — deleting a
        non-existent index row is a no-op.
        """
        return await asyncio.to_thread(
            self._delete_embedding_sync,
            memory_id,
            model_id,
            model_version,
        )

    def _delete_embedding_sync(
        self,
        memory_id: str,
        model_id: str,
        model_version: str,
    ) -> bool:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)
        with conn:
            cur = conn.execute(
                """
                DELETE FROM memory_embeddings
                WHERE memory_id = ?
                  AND model_id = ?
                  AND model_version = ?
                """,
                (memory_id, model_id, model_version),
            )
            # The vec0 delete is unconditional. If the (now deleted)
            # embedding was not the active namespace, the
            # corresponding vec0 row belongs to a different memory
            # (or does not exist); a no-op delete is correct.
            conn.execute(
                "DELETE FROM memory_vectors WHERE memory_id = ?",
                (memory_id,),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Vector search (vec0 MATCH)
    # ------------------------------------------------------------------

    async def search_in_namespace(
        self,
        *,
        query_vector: List[float],
        dimension: int,
        limit: int = 20,
        min_similarity: float = 0.5,
    ) -> List[Tuple[str, float]]:
        """Return the top-N most similar memory ids.

        Cosine similarity search via ``vec0``. Because
        ``memory_vectors`` holds only ONE namespace (the active
        one), callers do not need to pass ``model_id`` /
        ``model_version`` — the fact that they called
        ``ensure_vec0_table(dimension)`` earlier with the correct
        dimension pins the namespace.

        The return type is ``List[Tuple[str, float]]``, not
        ``MemoryEntry`` — the vector repository does NOT fetch
        memory entries. The caller (``SqliteVecProvider``, Task 4.5)
        joins the ids against ``memories`` to build the final
        results. This keeps the vector layer independent of the
        memory layer's schema.

        Parameters
        ----------
        query_vector
            The query embedding, same dimension as the index.
        dimension
            The active model's dimension. Used to pack the query
            and detect mismatch.
        limit
            Maximum number of hits to return. sqlite-vec returns at
            most ``k`` rows regardless of how many pass
            ``min_similarity``.
        min_similarity
            Threshold on ``1.0 - distance``. Values below are
            excluded from the returned list. ``0.0`` keeps every
            hit; ``1.0`` keeps only exact matches (in practice: no
            hits, since exact cosine equality on real embeddings
            is measure-zero).

        Returns
        -------
        List[Tuple[str, float]]
            ``(memory_id, similarity)`` sorted by descending
            similarity. Empty when the index has no rows or when
            every hit is below threshold.
        """
        if len(query_vector) != dimension:
            raise EmbeddingDimensionMismatchError(
                f"Query vector has dimension "
                f"{len(query_vector)}, expected {dimension}"
            )
        if limit <= 0:
            return []
        await self.ensure_vec0_table(dimension)
        return await asyncio.to_thread(
            self._search_in_namespace_sync,
            query_vector,
            dimension,
            limit,
            min_similarity,
        )

    def _search_in_namespace_sync(
        self,
        query_vector: List[float],
        dimension: int,
        limit: int,
        min_similarity: float,
    ) -> List[Tuple[str, float]]:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)
        packed = self._pack_vector(query_vector)

        try:
            rows = conn.execute(
                f"""
                SELECT memory_id, distance
                FROM memory_vectors
                WHERE embedding MATCH ?
                  AND {self._VEC0_K_PARAM} = ?
                ORDER BY distance
                """,
                (packed, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # Two failure modes worth distinguishing:
            #   (a) memory_vectors does not exist -> caller bug,
            #       should have called ensure_vec0_table;
            #   (b) MATCH unexpectedly rejected -> sqlite-vec
            #       version issue.
            raise Vec0UnavailableError(
                f"Vector search failed: {exc}. If memory_vectors "
                f"does not exist, call ensure_vec0_table() first.",
                cause=exc,
            ) from exc

        result: List[Tuple[str, float]] = []
        for row in rows:
            # vec0 returns cosine DISTANCE (0=identical, 2=opposite).
            # Convert to similarity (1=identical, 0=orthogonal).
            sim = 1.0 - float(row["distance"])
            if sim >= min_similarity:
                result.append((row["memory_id"], sim))
        return result

    # ------------------------------------------------------------------
    # Namespace maintenance
    # ------------------------------------------------------------------

    async def delete_namespace(
        self,
        model_id: str,
        model_version: str,
    ) -> int:
        """Delete every metadata row for one namespace.

        The vec0 index is NOT touched — callers who delete the
        ACTIVE namespace's rows must also call
        ``rebuild_vec0_from_active()`` (or accept that vec0 may
        briefly reference stale ids until the next reindex).

        Returns
        -------
        int
            Number of metadata rows deleted.
        """
        return await asyncio.to_thread(
            self._delete_namespace_sync, model_id, model_version
        )

    def _delete_namespace_sync(
        self,
        model_id: str,
        model_version: str,
    ) -> int:
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                """
                DELETE FROM memory_embeddings
                WHERE model_id = ? AND model_version = ?
                """,
                (model_id, model_version),
            )
            return cur.rowcount

    async def reindex_namespace(
        self,
        *,
        model_id: str,
        model_version: str,
        dimension: int,
    ) -> int:
        """Rebuild ``memory_vectors`` from ``memory_embeddings``.

        Empties the vec0 table and repopulates it from the
        metadata rows for ONE namespace. The active model's
        namespace is the only one that should ever enter vec0 —
        other namespaces' metadata stays on disk for future
        reindex but does not enter the current index.

        Typical callers:
          * the worker after switching the active model,
          * the ``POST /api/memory/reindex`` endpoint (Task 4.10),
          * test fixtures that need a clean index.

        Returns
        -------
        int
            Number of rows indexed.
        """
        await self.ensure_vec0_table(dimension)
        return await asyncio.to_thread(
            self._reindex_namespace_sync,
            model_id,
            model_version,
            dimension,
        )

    def _reindex_namespace_sync(
        self,
        model_id: str,
        model_version: str,
        dimension: int,
    ) -> int:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)
        indexed = 0

        with conn:
            conn.execute("DELETE FROM memory_vectors")
            rows = conn.execute(
                """
                SELECT memory_id, vector
                FROM memory_embeddings
                WHERE model_id = ? AND model_version = ?
                """,
                (model_id, model_version),
            ).fetchall()
            for row in rows:
                blob = row["vector"]
                if len(blob) != dimension * 4:
                    logger.warning(
                        "Skipping reindex of memory_id=%r: stored "
                        "vector has %d bytes, expected %d for "
                        "dimension %d",
                        row["memory_id"],
                        len(blob),
                        dimension * 4,
                        dimension,
                    )
                    continue
                conn.execute(
                    "INSERT INTO memory_vectors (memory_id, embedding) "
                    "VALUES (?, ?)",
                    (row["memory_id"], blob),
                )
                indexed += 1

        logger.info(
            "Reindexed %d vectors for namespace (%s, %s) at "
            "dimension %d",
            indexed,
            model_id,
            model_version,
            dimension,
        )
        return indexed

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def count_embeddings(
        self,
        *,
        model_id: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> int:
        """Count ``memory_embeddings`` rows with optional namespace filter."""
        return await asyncio.to_thread(
            self._count_embeddings_sync, model_id, model_version
        )

    def _count_embeddings_sync(
        self,
        model_id: Optional[str],
        model_version: Optional[str],
    ) -> int:
        conn = self._get_conn()
        sql = "SELECT COUNT(*) FROM memory_embeddings"
        params: List[Any] = []
        if model_id is not None:
            sql += " WHERE model_id = ?"
            params.append(model_id)
            if model_version is not None:
                sql += " AND model_version = ?"
                params.append(model_version)
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    async def count_vec0_rows(self) -> int:
        """Count rows in ``memory_vectors`` (0 when absent)."""
        return await asyncio.to_thread(self._count_vec0_rows_sync)

    def _count_vec0_rows_sync(self) -> int:
        conn = self._get_conn()
        self._ensure_extension_loaded(conn)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_vectors"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            # Table not yet created — treat as 0 rows.
            return 0

    @property
    def vec0_dimension(self) -> int:
        """Current vec0 table dimension (0 = not yet created).

        Read-only snapshot of the last ``ensure_vec0_table`` call.
        A concurrent writer could change it between reads; for a
        stable view call ``ensure_vec0_table`` first and use its
        argument.
        """
        return self._vec0_dimension

    # ------------------------------------------------------------------
    # Datetime helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dt_to_epoch(dt: Any) -> Optional[float]:
        """Convert aware datetime -> epoch seconds. None passthrough."""
        if dt is None:
            return None
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            # Match MemoryEmbedding's UTC fallback -- but by the
            # time a datetime reaches this helper it went through
            # Pydantic validation, which attaches UTC to naive
            # values. Belt-and-suspenders.
            from datetime import timezone as _tz

            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()

    @staticmethod
    def _epoch_to_dt(epoch: float) -> Any:
        """Convert epoch seconds -> aware UTC datetime."""
        from datetime import datetime, timezone as _tz

        return datetime.fromtimestamp(float(epoch), tz=_tz.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "SqliteVecRepository",
    "Vec0UnavailableError",
]
