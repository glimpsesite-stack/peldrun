# backend/omweb/memory/manager/memory_service.py
"""
PELDRUN Memory Platform - Memory Service Facade.

The single entry point every consumer of the memory subsystem
talks to (P1.7 task 1.7.3). Wraps the lifecycle manager and the
P1.4 retrieval pipeline in a façade with:

  * a clean public API (``retrieve``, ``reload_settings``),
  * mode enforcement (DISABLED -> always empty result),
  * read-only properties that expose the underlying provider and
    settings to trusted consumers (memory_save/memory_search tools),
  * a ``_record_usage`` stub the P1.9 monitoring subsystem will
    replace with the real ``MemoryUsageTracker``.

Design invariants
-----------------

1. **``retrieve`` always returns a ``MemoryContext``.**
   Never ``None``. Never raises for expected failure modes
   (disabled mode, empty query, no scopes, provider unavailable).
   Downstream consumers treat "empty context" and "no memories"
   identically -- the difference is only in the diagnostic
   fields (``router_decision``, ``dropped_for_budget``).

   The consequence for the dispatcher is a smaller try/except:
   the only exception ``retrieve`` can raise is a programmer
   error (``RuntimeError`` from accessing an uninitialized
   service), and that path should NEVER occur in production
   because FastAPI's lifespan initializes the service before
   serving requests.

2. **Write operations are deferred to P1.6.**
   The ``memory_save`` tool writes DIRECTLY to ``provider`` +
   ``settings`` -- it does not go through ``MemoryService``.
   This is deliberate: ``WritePipeline`` (the SKIP / SUPERSEDE /
   REACTIVATE decision layer, P1.7 task 1.7.3 / P1.6) has not
   landed yet, so a ``save()`` method here would be either
   incomplete or a duplicate of the eventual pipeline. Tools
   use the already-validated ``decide_write_action`` + provider
   primitives, which P1.6 will wrap without changing the tool's
   external contract.

3. **``_record_usage`` is a stub today, a real tracker tomorrow.**
   P1.9 introduces ``MemoryUsageTracker`` and a persistent
   ``memory_usage_ledger``. Until then, ``_record_usage`` logs
   the usage at INFO level so operators can see per-run token
   spend in the server log. P1.9 will replace the body without
   changing the method signature or any call site.

4. **Singleton enforcement lives in ``omweb.memory.__init__``.**
   This file exposes the CLASS. Process-wide singleton access
   (``get_memory_service()``, ``initialize_memory_service()``,
   ``shutdown_memory_service()``) lives in the package
   ``__init__.py`` so that tests can construct their own
   ``MemoryService`` instances with isolated settings + DB
   paths without touching the global.

5. **No FastAPI imports.**
   The service is a plain async class. Any lifespan wiring lives
   in ``omweb/main.py`` (deferred to the P1.7 lifespan task).
   This keeps the service unit-testable in isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from peldrun.memory.domain import (
    MemoryContext,
    MemoryMode,
    MemoryProvider,
    MemorySettings,
    default_settings,
)
from peldrun.memory.domain.enums import ExtractionTrigger
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.services import (
    Route,
    RouteDecision,
    build_empty_context,
    build_memory_context,
    classify_route,
)

from omweb.memory.manager.memory_lifecycle import (
    MemoryLifecycleManager,
)
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


logger = logging.getLogger("omweb.memory.manager.service")


# ---------------------------------------------------------------------------
# Extraction subsystem: forward-declared imports
# ---------------------------------------------------------------------------
#
# The extraction subsystem (Phase 1-C) is imported LAZILY inside the
# lifecycle methods (see `_initialize_extraction` / `_close_extraction`)
# rather than at module level. Rationale:
#
#   * `MemoryService` is imported by the P1 regression test suite,
#     which does NOT need the extraction pipeline. Deferring the
#     imports keeps that test path free of extraction-related
#     transitive imports (faster, fewer failure surfaces).
#   * A future refactor that decouples extraction into its own
#     optional package must not break `MemoryService`'s import.
#
# The type hints below are declared as strings (`"MemoryOutbox"`,
# etc.) so mypy still resolves them without the runtime import.


class _LazyGraphSearch:
    """Late-binding proxy that lets the hybrid see the live graph searcher.

    Why a proxy
    -----------
    ``MemoryService.ainitialize`` builds the HYBRID provider inside
    ``_initialize_embedding`` and the graph inside
    ``_initialize_graph`` afterwards. Passing ``self._graph_search``
    directly to the hybrid would therefore always pass ``None``.

    Reordering the two would fix the boot path but not the runtime
    one: ``reload_settings`` can enable graph search without
    rebuilding the hybrid (no embedding-model change), and the hybrid
    would keep its stale ``None``.

    A proxy resolves BOTH problems with one mechanism -- it looks up
    the live searcher on every call. It also re-reads
    ``retrieval.graph_search_enabled`` each time, so turning the
    feature OFF takes effect on the very next search, with no
    pipeline rebuild and no window where stale graph results leak.

    Failure isolation
    -----------------
    Every failure returns ``[]`` with a WARNING. The graph is an
    ADDITIONAL recall source; a graph problem must never turn into a
    failed search.
    """

    __slots__ = ("_service",)

    def __init__(self, service: "MemoryService") -> None:
        self._service = service

    async def find_related_for_query(
        self,
        query: str,
        *,
        scope_refs: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Any]:
        """Delegate to the live ``GraphSearch``, or return ``[]``.

        Mirrors the signature ``HybridMemoryProvider`` expects:
        ``find_related_for_query(query, scope_refs=..., limit=...)``.
        """
        service = self._service
        try:
            retrieval = getattr(
                service._lifecycle.settings, "retrieval", None
            )
            enabled = bool(
                getattr(retrieval, "graph_search_enabled", False)
            )
        except Exception:
            # Service not initialized (or being torn down).
            enabled = False
        if not enabled:
            return []

        searcher = getattr(service, "_graph_search", None)
        if searcher is None:
            return []
        try:
            return await searcher.find_related_for_query(
                query,
                scope_refs=scope_refs,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade only
            logger.warning(
                "MemoryService: graph search failed (%s: %s); "
                "continuing without the graph source.",
                type(exc).__name__,
                exc,
            )
            return []

    @property
    def is_available(self) -> bool:
        """True when a live searcher is currently installed."""
        return getattr(self._service, "_graph_search", None) is not None

    def __repr__(self) -> str:
        state = (
            "active" if self.is_available else "inactive"
        )
        return f"_LazyGraphSearch({state})"


class MemoryService:
    """Façade for the memory subsystem.

    See the module docstring for the design rationale, the write
    deferral decision, and the ``_record_usage`` stub plan.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        settings_path: Optional[Path] = None,
        *,
        outbox_db_path: Optional[Path] = None,
        extraction_settings_provider: Optional[
            Callable[[], Optional[object]]
        ] = None,
        transport_provider: Optional[
            Callable[[], Optional[object]]
        ] = None,
    ) -> None:
        """Construct an uninitialized ``MemoryService``.

        The service is NOT usable until ``ainitialize()`` completes.
        Properties that need an open provider raise
        ``RuntimeError`` before then -- see the lifecycle manager's
        module docstring for the rationale on fail-fast access.

        Parameters
        ----------
        db_path
            Override for the SQLite database file. ``None`` uses
            ``<storage_root>/memory.db``. Tests pass a temp path.
        settings_path
            Override for the settings JSON file. ``None`` uses
            ``<storage_root>/memory_settings.json``. Tests pass a
            temp path.
        outbox_db_path
            Override for the extraction outbox's SQLite file.
            ``None`` (default) reuses ``db_path`` -- the outbox
            and the memory repository share ONE database file
            (the schema for both was created by
            ``migrations.apply_schema``). Only override this in
            tests that need physically separated DBs.
        extraction_settings_provider
            Callable returning the extraction settings object
            (typically ``MemoryAISettings`` from Phase 3), or
            ``None`` when the extraction pipeline is not
            configured. The returned object is duck-typed: it
            MUST expose ``enabled``, ``extraction_enabled``,
            ``extraction_trigger``, and an ``extraction``
            attribute with ``provider_id`` and ``model_id``.

            When this parameter is ``None`` (the backward-
            compatible default), extraction is entirely disabled
            -- no outbox, no worker, no session watcher. Every
            extraction-related method on the service returns
            ``None`` immediately.
        transport_provider
            Optional callable returning a real LLM transport
            function (the same callable ``MemoryModelGateway``
            accepts as its ``transport=`` argument). This is
            the production injection point through which the
            ``peldrun.llm`` wiring supplies the real transport.

            When ``None`` (the default), the extraction pipeline
            is still BUILT -- outbox, scanner, router, watcher,
            orchestrator, and worker instances all exist and are
            accessible via their public properties -- but
            ``start_extraction_worker()`` REFUSES to start the
            worker. This preserves the safe default: jobs can be
            enqueued and inspected (for Batch UI, tests, and
            diagnostics), but no LLM call is ever attempted with
            the default stub transport.

            Tests inject a mock transport through this parameter
            to exercise the full extraction pipeline end-to-end.
        """
        self._settings_manager = MemorySettingsManager(path=settings_path)
        self._lifecycle = MemoryLifecycleManager(
            settings_manager=self._settings_manager,
            db_path=db_path,
        )
        self._initialized = False

        # --- Extraction subsystem (Phase 1-C) ---
        # All references are ``None`` until ``_initialize_extraction``
        # succeeds. Every public extraction entry point must check for
        # ``None`` before delegating.
        self._outbox_db_path: Optional[Path] = outbox_db_path
        self._extraction_settings_provider: Optional[
            Callable[[], Optional[object]]
        ] = extraction_settings_provider
        self._outbox: Optional["MemoryOutbox"] = None
        self._scanner: Optional["MemorySessionScanner"] = None
        self._trigger_router: Optional["TriggerRouter"] = None
        self._status_watcher: Optional["SessionStatusWatcher"] = None
        self._orchestrator: Optional["ExtractionOrchestrator"] = None
        self._worker: Optional["MemoryExtractionWorker"] = None

        # --- Transport (production LLM wiring) ---
        # ``_has_real_transport`` becomes True only when the caller
        # supplied a ``transport_provider`` AND it returned a non-None
        # object. ``start_extraction_worker`` uses this to refuse
        # starting the worker in "no real LLM" configurations --
        # which prevents queued jobs from failing repeatedly with
        # ``NotImplementedError`` from the default stub transport.
        self._transport_provider: Optional[
            Callable[[], Optional[object]]
        ] = transport_provider
        self._has_real_transport: bool = False

        # --- Batch subsystem (Phase 3) ---
        # Constructed lazily inside ``_initialize_extraction``
        # (only when extraction is enabled) and cleared in
        # ``_close_extraction``. None until then.
        self._batch_service: Optional["MemoryBatchService"] = None

        # --- Embedding subsystem (Phase 4) ---
        # All references are ``None`` until ``_initialize_embedding``
        # succeeds. Every public embedding entry point must check for
        # ``None`` before delegating.
        #
        # Unlike extraction, the embedding pipeline DOES NOT depend on
        # a real transport being injected -- embeddings always talk to
        # a real HTTP endpoint (LM Studio, OpenAI, Ollama, etc.). The
        # gate is instead "user has enabled embedding in settings AND
        # a model_key is configured".
        #
        # ``_embedding_active_model_key`` records the model_key used
        # to build the current resolver. ``reload_settings`` compares
        # it against the freshly loaded model_key to decide whether a
        # full pipeline rebuild is required.
        self._embedding_outbox: Optional["EmbeddingOutbox"] = None
        self._embedding_resolver: Optional["EmbeddingModelResolver"] = None
        self._embedding_worker: Optional["EmbeddingWorker"] = None
        self._vec_repository: Optional["SqliteVecRepository"] = None
        self._memory_repository: Optional["SqliteMemoryRepository"] = None
        self._embedding_active_model_key: Optional[str] = None

        # --- Hybrid retrieval (Phase 5) ---
        #
        # ``_vec_provider`` wraps the vector backend with a
        # long-lived embedder and the hydration adapter. It
        # implements the ``search`` surface the hybrid wrapper
        # consumes. Closed in ``_close_embedding`` alongside the
        # vec repository.
        #
        # ``_hybrid_provider`` wraps ``lifecycle.provider`` (the
        # FTS provider) and ``_vec_provider``. When both are
        # available, ``provider`` (see the property below)
        # returns the hybrid; otherwise it falls through to the
        # raw FTS provider.
        #
        # Both fields are ``None`` until ``_initialize_embedding``
        # succeeds AND dimensions are resolvable. Any failure in
        # that chain leaves them at ``None`` -- the memory
        # subsystem continues to serve retrieval via FTS exactly
        # as it did before hybrid was introduced.
        self._vec_provider: Optional["SqliteVecProvider"] = None
        self._hybrid_provider: Optional["HybridMemoryProvider"] = None

        # [FIX-BOOT-HANG] Background task that builds the hybrid
        # provider without blocking FastAPI startup. The build
        # involves an HTTP ``probe_dimensions`` call to the model
        # provider, which can hang for 30+ seconds when the
        # provider is unreachable. Running it in a background
        # task makes startup instant; ``service.provider`` falls
        # through to FTS until the task succeeds.
        self._hybrid_build_task: Optional[asyncio.Task] = None

        # --- Knowledge graph (Phase 6) ---
        #
        # The graph pipeline is INDEPENDENT of the embedding pipeline
        # in principle (it needs a repository + an LLM, not a vector
        # index), so it may run with embedding disabled. It reuses
        # ``_memory_repository`` when embedding built one, and
        # otherwise opens its own repository
        # (``_owns_graph_repository`` records which case applies so
        # ``_close_graph`` only closes what it opened).
        #
        # Every field is ``None`` until ``_initialize_graph``
        # succeeds. ``_write_pipeline_provider`` passes
        # ``_graph_indexer`` to the write pipeline, so a ``None``
        # indexer means "writes do not touch the graph" -- exactly
        # the pre-Phase-6 behavior.
        self._graph_repository: Optional[
            "SqliteMemoryRepository"
        ] = None
        self._owns_graph_repository: bool = False
        self._graph_store: Optional["GraphStore"] = None
        self._graph_extractor: Optional["EntityExtractor"] = None
        self._graph_indexer: Optional["GraphIndexer"] = None

        #: Phase 6B: the live graph search target. ``None`` when
        #: ``retrieval.graph_search_enabled`` is off or the graph has
        #: not been initialized. ``HybridMemoryProvider`` receives a
        #: ``_LazyGraphSearch`` wrapper (below) rather than this
        #: object directly, because the hybrid is built inside
        #: ``_initialize_embedding`` -- BEFORE ``_initialize_graph``
        #: runs. The wrapper resolves the live value on every search,
        #: so the ordering no longer matters and enabling the flag
        #: later does not require rebuilding the hybrid.
        self._graph_search: Optional["GraphSearch"] = None

        # Background backfill loop. Started only when the extractor
        # is actually configured; cancelled in ``_close_graph``.
        self._graph_backfill_task: Optional[asyncio.Task] = None

        # --- Agentic RAG (Phase 6, sub-phases 6C/6D) ---
        #
        # These components are reachable ONLY through
        # ``retrieve_agentic``. They are never consulted by
        # ``retrieve``, which must stay inside the dispatcher's
        # per-turn latency budget -- an agentic run costs several
        # LLM calls by design.
        #
        # All ``None`` until ``_initialize_agentic`` succeeds. That
        # initializer performs NO await, so it cannot hang startup;
        # a failure simply makes ``retrieve_agentic`` fall back to
        # the standard path.
        self._agentic_rewriter: Optional[
            "QueryRewriter"
        ] = None
        self._agentic_synthesizer: Optional[
            "EvidenceSynthesizer"
        ] = None
        self._agentic_retriever: Optional[
            "MultiHopRetriever"
        ] = None
        self._agentic_orchestrator: Optional[
            "AgenticRAGOrchestrator"
        ] = None

        # --- Retrieval failure observability (audit R5) ---
        # Incremented by the dispatcher when retrieve() raises. The
        # count and last error are surfaced via retrieval_failure_stats
        # so a retrieval failure is never silently invisible.
        self._retrieval_failure_count: int = 0
        self._last_retrieval_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ainitialize(self) -> None:
        """Initialize the underlying lifecycle manager and provider.

        Idempotent. On DISABLED mode, uses ``NullMemoryProvider`` and
        never touches the DB file (see ``MemoryLifecycleManager``).
        Failures during provider open degrade gracefully to a null
        provider -- the service stays "available" (initialized) but
        every ``retrieve`` returns an empty context.

        After the provider is ready, initializes the extraction
        pipeline (Phase 1-C) if the extraction settings say it is
        enabled AND the provider is a SQLite provider. Extraction
        failures are logged but never block startup -- a broken
        extraction subsystem degrades to "no auto-extraction",
        which is safer than failing the whole memory service.
        """
        if self._initialized:
            return
        await self._lifecycle.ainitialize()
        self._initialized = True

        # Phase 1-C: bring up extraction if configured.
        try:
            await self._initialize_extraction()
        except Exception as exc:
            logger.warning(
                "MemoryService: extraction initialization failed "
                "(auto-extraction will be unavailable): %s",
                exc,
                exc_info=True,
            )

        # Phase 4: bring up embedding pipeline if configured.
        #
        # Runs AFTER extraction so that the shared SqliteMemoryRepository
        # schema (memories + FTS) is guaranteed to be applied by the
        # extraction pipeline's first connection. In practice the schema
        # is applied lazily on the first _get_conn() call, but ordering
        # extraction first gives a warm schema for the embedding path.
        try:
            await self._initialize_embedding()
        except Exception as exc:
            logger.warning(
                "MemoryService: embedding initialization failed "
                "(semantic search / reindex will be unavailable "
                "until restart): %s",
                exc,
                exc_info=True,
            )

        # Phase 6: bring up knowledge-graph indexing if configured.
        #
        # Runs AFTER embedding so it can REUSE ``_memory_repository``
        # when the embedding pipeline built one (one connection pool
        # per process beats two competing writers on the same WAL
        # file). It still works with embedding disabled: it opens its
        # own repository in that case.
        #
        # ``_initialize_graph`` performs no await at all, so it cannot
        # contribute to a startup hang.
        try:
            await self._initialize_graph()
        except Exception as exc:
            logger.warning(
                "MemoryService: graph initialization failed "
                "(knowledge-graph indexing will be unavailable "
                "until restart): %s",
                exc,
                exc_info=True,
            )

        # Apply the process-wide LLM concurrency limit BEFORE any
        # background indexing task can start, so the very first batch
        # write is already serialized.
        self._apply_llm_concurrency()

        # Phase 6C: build the agentic pipeline (opt-in).
        #
        # Also await-free, so it cannot delay startup. Gated on
        # ``retrieval.agentic_enabled`` because an agentic run costs
        # several LLM calls; it is never reachable from the
        # dispatcher's per-turn ``retrieve``.
        try:
            await self._initialize_agentic()
        except Exception as exc:
            logger.warning(
                "MemoryService: agentic initialization failed "
                "(agentic retrieval will fall back to the standard "
                "path): %s",
                exc,
                exc_info=True,
            )

        logger.info(
            "MemoryService initialized (mode=%s, provider=%s, "
            "extraction=%s, embedding=%s, graph=%s).",
            self._lifecycle.settings.mode.value,
            self._lifecycle.settings.provider_id,
            "enabled" if self._outbox is not None else "disabled",
            "enabled" if self._embedding_worker is not None else "disabled",
            "enabled" if self._graph_indexer is not None else "disabled",
        )

    async def aclose(self) -> None:
        """Close the provider and reset state. Idempotent.

        Safe to call before ``ainitialize()`` -- FastAPI's lifespan
        hook runs this from a ``finally`` block that may execute
        after a failed startup.

        Closes the extraction subsystem FIRST (outbox, then
        lifecycle) so that no in-flight job can attempt to write
        to a provider being torn down underneath it.
        """
        if not self._initialized and not self._lifecycle.is_ready:
            return

        # Close the GRAPH first: it borrows the repository's
        # connections and dispatches background tasks. Cancelling it
        # before the embedding pipeline tears down that repository
        # is the only safe order -- otherwise a running backfill
        # would issue a query against a closing connection.
        await self._close_agentic()
        await self._close_graph()
        # Stop embedding FIRST -- it only depends on the SQLite
        # repositories and the embedding outbox, both of which are
        # owned exclusively by this pipeline. Stopping it before
        # extraction is a matter of ordering discipline: if a future
        # refactor adds a shared resource, this is the correct
        # dependency direction (embedding -> extraction -> lifecycle).
        await self._close_embedding()
        await self._close_extraction()
        await self._lifecycle.aclose()
        self._initialized = False
        logger.info("MemoryService closed.")

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True after ``ainitialize()`` until ``aclose()``.

        Being "available" does NOT guarantee a working provider --
        in degraded states (provider failed to open, mode disabled)
        the service is available but ``retrieve`` returns empty
        contexts. Health checks that need to distinguish these
        cases inspect ``mode`` and ``provider`` directly.
        """
        return self._initialized and self._lifecycle.is_ready

    @property
    def mode(self) -> MemoryMode:
        """Current effective mode. Raises before ``ainitialize()``."""
        return self._lifecycle.settings.mode

    @property
    def settings(self) -> MemorySettings:
        """Full settings snapshot. Raises before ``ainitialize()``."""
        return self._lifecycle.settings

    @property
    def provider(self) -> MemoryProvider:
        """The live provider. Raises before ``ainitialize()``.

        Tools (``memory_save``, ``memory_search``) need both
        ``provider`` and ``settings``. In DISABLED mode ``provider``
        is a ``NullMemoryProvider`` -- every write raises
        ``MemoryUnavailableError`` and every read returns empty,
        which is exactly the semantics DISABLED mode promises.

        [PHASE 5] When the hybrid pipeline is fully initialized
        AND dimensions are known, this returns a
        ``HybridMemoryProvider`` that fuses FTS + vector search
        on every ``search``/``search_ranked`` call. Otherwise it
        returns the raw FTS provider.

        Both providers implement the full ``MemoryProvider``
        contract. Every WRITE method on the hybrid delegates to
        FTS internally -- only the read path gains semantic
        recall, so switching between FTS-only and hybrid does
        not change write semantics.
        """
        if self._hybrid_provider is not None:
            return self._hybrid_provider
        return self._lifecycle.provider

    # ------------------------------------------------------------------
    # Extraction subsystem (Phase 1-C)
    # ------------------------------------------------------------------

    @property
    def extraction_enabled(self) -> bool:
        """True when the extraction pipeline is initialized and running.

        ``False`` when:
          * ``extraction_settings_provider`` was not supplied,
          * extraction is disabled in settings,
          * the provider is not SQLite (extraction requires SQLite),
          * initialization failed.
        """
        return self._outbox is not None

    @property
    def outbox(self) -> Optional["MemoryOutbox"]:
        """The extraction outbox, or ``None`` when extraction is off."""
        return self._outbox

    @property
    def trigger_router(self) -> Optional["TriggerRouter"]:
        """The extraction trigger router, or ``None``."""
        return self._trigger_router

    @property
    def status_watcher(self) -> Optional["SessionStatusWatcher"]:
        """The extraction status watcher, or ``None``."""
        return self._status_watcher

    @property
    def session_scanner(self) -> Optional["MemorySessionScanner"]:
        """The extraction session scanner, or ``None``."""
        return self._scanner

    @property
    def orchestrator(self) -> Optional["ExtractionOrchestrator"]:
        """The extraction orchestrator, or ``None``."""
        return self._orchestrator

    @property
    def worker(self) -> Optional["MemoryExtractionWorker"]:
        """The extraction worker instance, or ``None``.

        The worker is CREATED during ``_initialize_extraction``
        but only STARTED by an explicit ``start_extraction_worker``
        call. This separation gives ``main.py``'s lifespan full
        control over when the worker begins polling -- after
        recovery completes, after every other subsystem is up,
        etc. -- while still exposing the instance for inspection.
        """
        return self._worker

    @property
    def batch_service(self) -> Optional["MemoryBatchService"]:
        """The batch orchestration service, or ``None``.

        Available only when the extraction pipeline is up.
        Callers (the batch HTTP router) must treat ``None`` as
        "AI extraction is not running" and return 503 -- there
        is no meaningful way to run a batch without the outbox,
        scanner, and trigger router that ``_initialize_extraction``
        wires together.
        """
        return self._batch_service

    # ------------------------------------------------------------------
    # Embedding subsystem (Phase 4)
    # ------------------------------------------------------------------

    @property
    def embedding_enabled(self) -> bool:
        """True when the embedding pipeline is initialized.

        ``False`` when:
          * ``settings.embedding.enabled`` is ``False``,
          * no ``model_key`` is configured,
          * the memory mode does not permit writes,
          * the provider is not ``sqlite_fts``,
          * initialization failed.

        Note that ``embedding_enabled == True`` does NOT guarantee
        the worker is RUNNING -- use ``embedding_worker.is_running``
        to check the run-loop state. The worker is created at init
        but only started by ``start_embedding_worker()``.
        """
        return self._embedding_worker is not None

    @property
    def embedding_outbox(self) -> Optional["EmbeddingOutbox"]:
        """The embedding jobs outbox, or ``None`` when disabled."""
        return self._embedding_outbox

    @property
    def embedding_resolver(self) -> Optional["EmbeddingModelResolver"]:
        """The resolver built from the current vault + metadata snapshot.

        Exposed for diagnostics endpoints and tests. Production code
        should not need to look inside -- the worker owns the only
        consumer contract (``list_models()`` for reverse lookup).
        """
        return self._embedding_resolver

    @property
    def embedding_worker(self) -> Optional["EmbeddingWorker"]:
        """The embedding worker instance, or ``None``.

        Created during ``_initialize_embedding`` but only STARTED by
        an explicit ``start_embedding_worker`` call. This separation
        mirrors the extraction worker's design (see ``worker``
        property above) and gives the lifespan hook full control
        over when the worker begins claiming jobs.
        """
        return self._embedding_worker

    @property
    def vec_repository(self) -> Optional["SqliteVecRepository"]:
        """The sqlite-vec repository, or ``None``.

        Exposed so reindex / reembed admin endpoints can call
        ``reindex_namespace`` without needing a live worker.
        """
        return self._vec_repository

    @property
    def last_search_mode(self) -> str:
        """Strategy used by the most recent memory search.

        One of:

          * ``"hybrid"`` -- both FTS and vector contributed and
            their ranked lists were fused via RRF.
          * ``"vector"`` -- vector search produced results but
            FTS returned nothing (or failed).
          * ``"fts"``    -- FTS-only (the default; also the result
            when vector search was skipped for filter reasons,
            returned no results, or raised).

        The property NEVER returns ``"none"`` -- ``"fts"`` is the
        correct honest answer for both "no hybrid exists" and
        "hybrid exists but has not searched yet". A distinct
        ``"none"`` would mislead callers into thinking memory
        search is unconfigured, when in fact pure FTS search may
        be fully operational.

        Read by ``POST /api/memory/search`` to include a
        ``search_mode`` field in its response, so the operator
        can verify whether semantic recall actually contributed
        to the results they are looking at. Also useful for
        diagnostics dashboards that want to confirm hybrid is
        wired correctly.

        The property is safe to call at any time -- when hybrid
        is not initialized (embedding disabled, no model selected,
        or startup failed), it returns ``"fts"`` without raising,
        which is the correct answer: pure FTS was the only
        available strategy.
        """
        if self._hybrid_provider is None:
            # No hybrid pipeline -- by definition, the effective
            # search strategy is FTS-only. Do NOT return "none"
            # here: "none" is for "the service hasn't searched
            # yet", which is different from "no hybrid exists".
            # The caller of this property is typically building a
            # response right after a search, and "fts" is a
            # meaningful, accurate value.
            return "fts"
        mode = self._hybrid_provider.last_search_mode
        # ``last_search_mode`` on the hybrid is None only BEFORE
        # its first search. After a search, it is one of
        # "hybrid" / "vector" / "fts". Callers reach this
        # property post-search, so None is practically impossible
        # -- but default to "fts" defensively.
        return mode if mode is not None else "fts"

    def get_extraction_trigger(self) -> Optional[str]:
        """Return the current ``extraction_trigger`` mode or ``None``.

        Used by the dispatcher's After-Turn hook (Phase 1-C) to
        decide whether to fire the per-turn trigger. Returns
        ``None`` when extraction is disabled, which the dispatcher
        treats as "do nothing".

        [PHASE 3 FIX] Reads from ``self._lifecycle.settings.ai``
        FIRST, then falls back to ``extraction_settings_provider``.

        Why the primary source is lifecycle
        -----------------------------------
        In production, ``initialize_memory_service`` does NOT pass
        ``extraction_settings_provider`` (the parameter defaults to
        ``None``). The authoritative AI settings live on
        ``self._lifecycle.settings.ai`` after every successful
        ``ainitialize()`` / ``reload_settings()`` call.

        Before this fix, the method consulted ONLY
        ``extraction_settings_provider`` -- which is ``None`` in
        production -- so it always returned ``None``. The dispatcher
        hook then treated extraction as "disabled" and silently
        skipped every turn, producing the "no jobs enqueued"
        symptom users observed even after enabling AI in the UI.

        The fallback path preserves backward compatibility for
        embedders that wire their own settings source explicitly.
        """
        # --- Primary: lifecycle settings (authoritative) -------
        try:
            if self._lifecycle.is_ready:
                ai = getattr(self._lifecycle.settings, "ai", None)
                if ai is not None:
                    if not getattr(ai, "enabled", False):
                        return None
                    if not getattr(ai, "extraction_enabled", False):
                        return None
                    trigger = getattr(ai, "extraction_trigger", None)
                    if trigger in ("after_turn", "after_session", "both"):
                        return trigger
                    return None
        except Exception:
            pass

        # --- Fallback: caller-supplied provider (tests) --------
        if self._extraction_settings_provider is None:
            return None
        try:
            settings = self._extraction_settings_provider()
        except Exception:
            return None
        if settings is None:
            return None
        if not getattr(settings, "enabled", False):
            return None
        if not getattr(settings, "extraction_enabled", False):
            return None
        trigger = getattr(settings, "extraction_trigger", None)
        if trigger in ("after_turn", "after_session", "both"):
            return trigger
        return None

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        query: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> MemoryContext:
        """Retrieve memory context for a turn.

        This is THE retrieval entry point. Every consumer of the
        memory subsystem -- ``dispatcher.run_instrumented``,
        ``dispatcher.run_direct_chat``, the ``memory_search`` tool,
        future API endpoints -- goes through this method.

        Parameters
        ----------
        query
            The user's prompt (or the tool's search query). May be
            empty; the pipeline falls back to a recency listing in
            that case.
        chat_id
            Current chat id, if any. Used to build chat-scope refs.
        project_id
            Current project id, if any. Used for project-scope refs
            and to gate the PRECISE router path.
        user_id
            Optional explicit user id. Defaults to ``"user:default"``
            in single-user deployments.
        run_id
            Optional run id, forwarded to ``_record_usage`` for
            P1.9 attribution. When ``None`` (early bootstrap calls,
            tests), the usage log entry uses ``"<unknown>"``.

        Returns
        -------
        MemoryContext
            Always returns a context, never ``None``. The result
            ``is_empty`` is True in every failure / no-match /
            disabled case.

        Raises
        ------
        RuntimeError
            When the service has not been initialized. This is a
            programmer error (the lifespan hook must call
            ``ainitialize`` first); the internal fast paths below
            do NOT swallow it, so a missing initialization is
            immediately visible.
        """
        if not self._initialized:
            raise RuntimeError(
                "MemoryService is not initialized. Call "
                "initialize_memory_service() in FastAPI lifespan "
                "before serving requests."
            )

        # ------------------------------------------------------------------
        # Fast path 1: DISABLED mode
        # ------------------------------------------------------------------
        #
        # Return an empty context without touching the provider. The
        # lifecycle manager installed a NullMemoryProvider for
        # DISABLED mode, so even if we fell through to
        # build_memory_context, it would return [] -- but the fast
        # path avoids the pipeline overhead and produces a cleaner
        # diagnostic (router_decision="no_need" is honest about why
        # nothing was retrieved).
        settings = self._lifecycle.settings
        if not settings.mode.can_read:
            logger.debug(
                "MemoryService.retrieve: DISABLED mode, returning "
                "empty context (query=%r).",
                query[:60],
            )
            empty = build_empty_context(
                query=query,
                scope_refs=[],
                settings=settings,
                router_decision="no_need",
            )
            await self._record_usage(
                run_id=run_id,
                context=empty,
                chat_id=chat_id,
                project_id=project_id,
            )
            return empty

        # ------------------------------------------------------------------
        # Resolve scopes
        # ------------------------------------------------------------------
        try:
            scope_refs = resolve_scope_refs(
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as exc:
            # ``resolve_scope_refs`` raises only on invalid inputs
            # (a ``:`` inside an id, a control character, an empty
            # scope id). All are caller bugs. Log and return empty
            # rather than propagating -- a scope-resolution error
            # must not abort a run.
            logger.warning(
                "MemoryService.retrieve: scope resolution failed "
                "(chat_id=%r, project_id=%r): %s",
                chat_id,
                project_id,
                exc,
            )
            empty = build_empty_context(
                query=query,
                scope_refs=[],
                settings=settings,
                router_decision="no_need",
            )
            await self._record_usage(
                run_id=run_id,
                context=empty,
                chat_id=chat_id,
                project_id=project_id,
            )
            return empty

        # ------------------------------------------------------------------
        # Classify route (deterministic -- never raises)
        # ------------------------------------------------------------------
        #
        # [ALWAYS-RETRIEVE MODE]
        #
        # ``classify_route`` is a HEURISTIC gate: unless the query looks
        # like an explicit recall request ("what did we decide…",
        # "remember…") it returns NO_NEED, and ``build_memory_context``
        # then returns an empty context WITHOUT searching. The patterns
        # are English-centric, so in an Arabic deployment almost every
        # real question classified as NO_NEED and the model never saw a
        # single stored fact -- the memories existed, the search worked,
        # and the user still got an amnesiac assistant.
        #
        # ``retrieval.always_retrieve`` (default True) turns the gate
        # off: every non-empty turn retrieves from the user's memories,
        # in BOTH exec modes. The route is labelled DIRECTED -- "use the
        # query as-is" -- rather than DURING_EXECUTION, which is the
        # tool path and would misattribute the decision in diagnostics.
        #
        # Scope is unchanged: ``resolve_scope_refs`` still yields
        # ``user:<id>`` unconditionally plus project/chat when known, so
        # this widens WHEN we search, not WHAT is visible. Scope
        # isolation is a security boundary and is not touched here.
        always_retrieve = bool(
            getattr(settings.retrieval, "always_retrieve", False)
        )
        if always_retrieve and (query or "").strip():
            route = Route(
                decision=RouteDecision.DIRECTED,
                reason="always_retrieve_setting",
            )
        else:
            route = classify_route(
                query,
                has_active_project=bool(project_id),
            )

        # ------------------------------------------------------------------
        # Build context via the P1.4 pipeline
        # ------------------------------------------------------------------
        #
        # ``build_memory_context`` already handles NO_NEED routes,
        # empty scope_refs, zero budgets, and provider exceptions
        # internally -- every failure path returns a well-formed
        # context. The try/except here is defense in depth against
        # a bug in the pipeline itself.
        try:
            # [PHASE 5] Use ``self.provider`` rather than the raw
            # ``self._lifecycle.provider`` so the dispatcher's
            # auto-retrieval observes hybrid recall when available
            # (falls through to FTS otherwise). Consistent with
            # the /api/memory/search endpoint which also calls
            # ``service.provider``.
            context = await build_memory_context(
                query=query,
                scope_refs=scope_refs,
                route=route,
                provider=self.provider,
                settings=settings,
                # [ALWAYS-RETRIEVE MODE] With the intent gate off,
                # EVERY non-empty turn routes as DIRECTED, and
                # DIRECTED normally enables the recency fallback when
                # FTS finds no keyword match.
                #
                # We DISABLE it here. The fallback was switched on
                # temporarily because FTS could not match natural
                # questions (implicit AND plus unfiltered Arabic
                # prefixes), so relevance-only injection showed
                # nothing. That has since been fixed properly -- the
                # "any" mode ORs prefix-wildcarded content words -- so
                # the fallback is no longer needed and is now harmful:
                #
                #   "thank you"                    -> 0 keyword hits
                #   "ok i will see you next time"  -> farewell
                #
                # With the fallback on, both injected the most recent
                # memories and the agentic layer then answered "I
                # really enjoy drinking black coffee and black tea" to
                # a thank-you. That is the "the memories have nothing
                # to do with the context" complaint: unrelated turns
                # received confident, irrelevant context.
                #
                # `False` = search always runs, injection only on a
                # real keyword match. `None` (gate still on) keeps the
                # historical behaviour verbatim.
                allow_recency_fallback=(
                    False if always_retrieve else None
                ),
                # FTS operator: "any" for chat turns (recall over a
                # natural-language question), "all" (strict AND) for
                # the gate-on path so a deployment that keeps the
                # heuristic gets exactly the old behaviour.
                match_any=always_retrieve,
            )
        except Exception as exc:
            logger.warning(
                "MemoryService.retrieve: pipeline failed "
                "(query=%r, scopes=%r): %s. Returning empty context.",
                query[:60],
                scope_refs,
                exc,
            )
            context = build_empty_context(
                query=query,
                scope_refs=scope_refs,
                settings=settings,
                router_decision=route.decision.value,
            )

        await self._record_usage(
            run_id=run_id,
            context=context,
            chat_id=chat_id,
            project_id=project_id,
        )
        return context

    # ------------------------------------------------------------------
    # Agentic retrieval (Phase 6)
    # ------------------------------------------------------------------

    async def retrieve_agentic(
        self,
        *,
        query: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str] = None,
        max_hops: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> "AgenticRetrievalResult":
        """Agentic retrieval for a COMPLEX question.

        **This method does NOT replace ``retrieve``.** ``retrieve`` is
        called by the dispatcher on every turn and must stay in the
        hundreds-of-milliseconds range; an agentic run issues several
        LLM calls (rewrite + up to N gap analyses + synthesis) and
        belongs on an explicit, opt-in path:

          * ``POST /api/memory/search/agentic``,
          * a future ``memory_search`` tool mode for agents.

        The two methods share ONLY the read-only helpers
        (``settings``, ``provider``, scope resolution). ``retrieve``'s
        behaviour is unchanged by anything in this method.

        Behaviour
        ---------
          1. Not initialized / disabled / misconfigured -> return a
             result with ``is_available=False`` carrying the standard
             ``retrieve()`` context as ``fallback_context``. No
             exception, no 5xx.
          2. Empty query or unresolvable scope -> same fallback.
          3. Otherwise run the agentic pipeline under the configured
             ``agentic_timeout_seconds`` budget. On timeout the
             partial evidence gathered so far is returned.
          4. Any unexpected failure -> log + fall back to
             ``retrieve()`` and report ``error="failed"``.

        Parameters
        ----------
        query, chat_id, project_id, user_id
            Identical semantics to ``retrieve``.
        max_hops
            Optional override for ``retrieval.agentic_max_hops``.
        run_id
            Forwarded to ``_record_usage`` for the FALLBACK context
            only (an agentic run is metered by the LLM gateway, not
            by the memory usage ledger).

        Returns
        -------
        AgenticRetrievalResult
            Always well-formed; see
            ``peldrun.memory.agentic.contracts``.
        """
        from peldrun.memory.agentic.contracts import (
            AgenticRetrievalResult,
        )

        started = time.monotonic()

        def _fallback(reason: str) -> "AgenticRetrievalResult":
            return AgenticRetrievalResult(
                is_available=False,
                query=query if isinstance(query, str) else "",
                search_mode="agentic+fallback",
                error=reason,
                elapsed_ms=int(
                    (time.monotonic() - started) * 1000.0
                ),
            )

        if not self._initialized:
            raise RuntimeError(
                "MemoryService is not initialized. Call "
                "initialize_memory_service() in FastAPI lifespan "
                "before serving requests."
            )

        retriever = self._agentic_retriever
        orchestrator = self._agentic_orchestrator
        pipeline = orchestrator if orchestrator is not None else retriever

        if pipeline is None:
            # Agentic is off (or failed to initialize). Degrade to
            # the standard path so the caller still gets an answer.
            fallback_context = await self.retrieve(
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
                run_id=run_id,
            )
            result = _fallback("disabled")
            return AgenticRetrievalResult(
                is_available=result.is_available,
                query=result.query,
                search_mode=result.search_mode,
                error=result.error,
                elapsed_ms=result.elapsed_ms,
                fallback_context=fallback_context,
            )

        if not isinstance(query, str) or not query.strip():
            return await self._agentic_fallback(
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
                run_id=run_id,
                reason="empty_query",
                started=started,
            )

        try:
            scope_refs = resolve_scope_refs(
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "MemoryService.retrieve_agentic: scope resolution "
                "failed (chat_id=%r, project_id=%r): %s",
                chat_id,
                project_id,
                exc,
            )
            return await self._agentic_fallback(
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
                run_id=run_id,
                reason="invalid_scope",
                started=started,
            )

        settings = self._lifecycle.settings
        timeout_seconds = float(
            getattr(
                settings.retrieval, "agentic_timeout_seconds", 30.0
            )
            or 30.0
        )
        threshold = float(
            getattr(
                settings.retrieval,
                "agentic_confidence_threshold",
                0.6,
            )
            or 0.0
        )

        try:
            # The orchestrator owns the documented run budget and
            # returns PARTIAL evidence when it expires. This outer
            # wait_for is a looser backstop (budget + slack) that
            # only fires if the pipeline ignores its own deadline --
            # so the partial-result path normally wins the race.
            backstop = max(1.0, timeout_seconds) + 5.0
            multi_hop = await asyncio.wait_for(
                self._run_agentic_pipeline(
                    pipeline=pipeline,
                    query=query,
                    scope_refs=scope_refs,
                    max_hops=max_hops,
                ),
                timeout=backstop,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "MemoryService.retrieve_agentic: timed out after "
                "%.1fs (query=%r, hops<=%r).",
                timeout_seconds,
                query[:60],
                max_hops,
            )
            return await self._agentic_fallback(
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
                run_id=run_id,
                reason="timeout",
                started=started,
            )
        except Exception as exc:  # noqa: BLE001 -- documented
            logger.warning(
                "MemoryService.retrieve_agentic: pipeline failed "
                "(%s: %s); falling back to standard retrieval.",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return await self._agentic_fallback(
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
                run_id=run_id,
                reason="failed",
                started=started,
            )

        answer = multi_hop.synthesized_answer
        confidence = (
            answer.confidence if answer is not None else 0.0
        )
        return AgenticRetrievalResult(
            is_available=True,
            query=query,
            evidence=tuple(multi_hop.evidence),
            synthesized_answer=answer,
            hops_used=multi_hop.hops_used,
            sub_queries_used=tuple(multi_hop.sub_queries_used),
            search_mode="agentic",
            error=None,
            fallback_context=None,
            confidence_threshold_met=(
                answer is not None
                and answer.is_substantive
                and confidence >= threshold
            ),
            elapsed_ms=int((time.monotonic() - started) * 1000.0),
        )

    async def _run_agentic_pipeline(
        self,
        *,
        pipeline: Any,
        query: str,
        scope_refs: List[str],
        max_hops: Optional[int],
    ) -> Any:
        """Invoke the agentic pipeline with the current provider.

        Both the orchestrator (6D) and a bare ``MultiHopRetriever``
        (6C) accept the SAME ``retrieve(query, scope_refs,
        backend=..., max_hops=...)`` shape, so no branching is
        needed here. ``max_hops`` is a per-call argument rather than
        an instance mutation, because the shared pipeline serves
        concurrent requests.
        """
        return await pipeline.retrieve(
            query,
            scope_refs,
            backend=self.provider,
            max_hops=max_hops,
        )

    async def _agentic_fallback(
        self,
        *,
        query: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str],
        run_id: Optional[str],
        reason: str,
        started: float,
    ) -> "AgenticRetrievalResult":
        """Build an ``is_available=False`` result backed by ``retrieve``."""
        from peldrun.memory.agentic.contracts import (
            AgenticRetrievalResult,
        )

        fallback_context = await self.retrieve(
            query=query,
            chat_id=chat_id,
            project_id=project_id,
            user_id=user_id,
            run_id=run_id,
        )
        return AgenticRetrievalResult(
            is_available=False,
            query=query if isinstance(query, str) else "",
            search_mode="agentic+fallback",
            error=reason,
            fallback_context=fallback_context,
            elapsed_ms=int((time.monotonic() - started) * 1000.0),
        )

    # ------------------------------------------------------------------
    # Settings reload
    # ------------------------------------------------------------------

    async def reload_settings(self) -> MemorySettings:
        """Re-read settings from disk and apply them.

        Delegates to ``MemoryLifecycleManager.reload_settings``,
        which performs the surgical rebuild logic (rebuild the
        provider only when ``provider_id`` or the DISABLED boundary
        changed; otherwise just swap the settings reference).

        [PHASE 3 FIX] After the lifecycle reload, this method ALSO
        re-evaluates the extraction pipeline. This is what makes
        the "user enables AI in the UI, extraction starts immediately"
        flow work without a server restart:

          * settings flipped from disabled -> enabled:
            ``_close_extraction()`` then ``_initialize_extraction()``
            -- the pipeline is rebuilt with fresh settings.
          * settings flipped from enabled -> disabled:
            ``_close_extraction()`` -- the pipeline is torn down.
          * settings unchanged (enabled state same):
            no-op. The running pipeline stays as-is.

        The extraction re-init is best-effort: any failure is
        logged at WARNING and swallowed, so a broken settings file
        or a transport init error never breaks the caller (which
        is typically the ``PATCH /api/memory/settings`` endpoint).
        The caller receives the successfully-reloaded settings
        regardless of extraction state.

        Used by ``PATCH /api/memory/settings`` (P1.8) and by the
        ``main.py`` lifespan if a reload-on-SIGHUP hook is added
        later.

        Raises
        ------
        RuntimeError
            When called before ``ainitialize()``.
        """
        new_settings = await self._lifecycle.reload_settings()
        logger.info(
            "MemoryService settings reloaded (mode=%s, provider=%s).",
            new_settings.mode.value,
            new_settings.provider_id,
        )

        # --- Phase 3: re-evaluate extraction pipeline -------------
        try:
            ai = getattr(new_settings, "ai", None)
            want_enabled = (
                ai is not None
                and getattr(ai, "enabled", False)
                and getattr(ai, "extraction_enabled", False)
                and new_settings.mode.can_write
                and new_settings.provider_id == "sqlite_fts"
            )
            have_enabled = self._outbox is not None

            if want_enabled and not have_enabled:
                # Pipeline off -> on. Build a fresh pipeline.
                logger.info(
                    "MemoryService: AI extraction enabled via "
                    "settings reload; initializing pipeline."
                )
                try:
                    await self._initialize_extraction()
                    logger.info(
                        "MemoryService: extraction pipeline started "
                        "via reload."
                    )
                except Exception as init_err:
                    logger.warning(
                        "MemoryService: extraction reload failed "
                        "(non-fatal): %s", init_err,
                    )
            elif not want_enabled and have_enabled:
                # Pipeline on -> off. Tear down.
                logger.info(
                    "MemoryService: AI extraction disabled via "
                    "settings reload; stopping pipeline."
                )
                try:
                    await self._close_extraction()
                    logger.info(
                        "MemoryService: extraction pipeline stopped "
                        "via reload."
                    )
                except Exception as close_err:
                    logger.warning(
                        "MemoryService: extraction close during "
                        "reload failed (non-fatal): %s", close_err,
                    )
            # else: state unchanged -> nothing to do.
        except Exception as reload_err:
            logger.warning(
                "MemoryService: extraction reload check failed "
                "(non-fatal): %s", reload_err,
            )

        # --- Phase 4: re-evaluate embedding pipeline --------------
        #
        # Three cases drive action:
        #
        #   1. off -> on:  initialize fresh pipeline.
        #   2. on  -> off: tear down pipeline.
        #   3. on  -> on WITH model_key change: full rebuild --
        #      the resolver must be reconstructed against the new
        #      model so that _resolve_model() finds jobs whose
        #      model_id matches the newly-selected model.
        #
        # The "on -> on with no change" case is a no-op. Changing
        # unrelated settings (batch size, min relevance, etc.)
        # does not require rebuilding the embedding pipeline.
        #
        # All failure paths are logged and swallowed -- a broken
        # embedding reload must not prevent the caller (PATCH
        # /api/memory/settings) from returning the successfully-
        # reloaded settings.
        try:
            emb = getattr(new_settings, "embedding", None)
            want_embedding_enabled = (
                emb is not None
                and getattr(emb, "enabled", False)
                and isinstance(
                    getattr(emb, "model_key", None), str
                )
                and bool(getattr(emb, "model_key", "").strip())
                and new_settings.mode.can_write
                and new_settings.provider_id == "sqlite_fts"
            )
            have_embedding_enabled = (
                self._embedding_worker is not None
            )
            new_model_key = (
                getattr(emb, "model_key", None) if emb else None
            )
            new_model_key_clean = (
                new_model_key.strip()
                if isinstance(new_model_key, str)
                else None
            )
            model_key_changed = (
                have_embedding_enabled
                and new_model_key_clean
                != self._embedding_active_model_key
            )

            if want_embedding_enabled and not have_embedding_enabled:
                logger.info(
                    "MemoryService: embedding enabled via "
                    "settings reload; initializing pipeline."
                )
                try:
                    await self._initialize_embedding()
                    logger.info(
                        "MemoryService: embedding pipeline "
                        "started via reload."
                    )
                except Exception as init_err:
                    logger.warning(
                        "MemoryService: embedding init during "
                        "reload failed (non-fatal): %s",
                        init_err,
                    )
            elif (
                not want_embedding_enabled
                and have_embedding_enabled
            ):
                logger.info(
                    "MemoryService: embedding disabled via "
                    "settings reload; stopping pipeline."
                )
                try:
                    await self._close_embedding()
                    logger.info(
                        "MemoryService: embedding pipeline "
                        "stopped via reload."
                    )
                except Exception as close_err:
                    logger.warning(
                        "MemoryService: embedding close during "
                        "reload failed (non-fatal): %s",
                        close_err,
                    )
            elif (
                want_embedding_enabled
                and have_embedding_enabled
                and model_key_changed
            ):
                logger.info(
                    "MemoryService: embedding model_key changed "
                    "(%r -> %r) via settings reload; rebuilding "
                    "pipeline.",
                    self._embedding_active_model_key,
                    new_model_key_clean,
                )
                try:
                    await self._close_embedding()
                    await self._initialize_embedding()
                except Exception as rebuild_err:
                    logger.warning(
                        "MemoryService: embedding rebuild during "
                        "reload failed (non-fatal): %s",
                        rebuild_err,
                    )
            # else: state unchanged -> nothing to do.
        except Exception as emb_reload_err:
            logger.warning(
                "MemoryService: embedding reload check failed "
                "(non-fatal): %s", emb_reload_err,
            )

        # --- Phase 6: re-evaluate graph indexing ------------------
        #
        # Three cases drive action, mirroring the embedding block:
        #
        #   1. off -> on: initialize.
        #   2. on  -> off: tear down (stopping the backfill loop and
        #      draining in-flight tasks).
        #   3. on  -> on WITH a model change: rebuild, because the
        #      extractor captured the old provider/model pair at
        #      construction and would keep calling it otherwise.
        #
        # The "on -> on, no change" case is a no-op: changing
        # backfill interval or batch size is read from settings on
        # every tick, so it needs no rebuild.
        try:
            ai_cfg = getattr(new_settings, "ai", None)
            retrieval_cfg = getattr(new_settings, "retrieval", None)
            want_search = bool(
                getattr(
                    retrieval_cfg, "graph_search_enabled", False
                )
            )
            want_index = (
                ai_cfg is not None
                and getattr(ai_cfg, "enabled", False)
                and getattr(ai_cfg, "graph_indexing_enabled", False)
                and new_settings.mode.can_write
                and new_settings.provider_id == "sqlite_fts"
            )
            want_graph = want_search or want_index
            have_graph = (
                self._graph_indexer is not None
                or self._graph_search is not None
            )
            resolved = self._resolve_agentic_model() if want_index else None
            new_model_key = (
                f"{resolved[0]}::{resolved[1]}" if resolved else None
            )
            current_model_key = None
            if self._graph_extractor is not None:
                current_model_key = (
                    f"{self._graph_extractor.provider_id}"
                    f"::{self._graph_extractor.model_id}"
                )
            model_changed = (
                self._graph_extractor is not None
                and new_model_key is not None
                and new_model_key != current_model_key
            )

            if want_graph and not have_graph:
                logger.info(
                    "MemoryService: graph enabled via settings "
                    "reload; initializing."
                )
                await self._initialize_graph()
            elif not want_graph and have_graph:
                logger.info(
                    "MemoryService: graph disabled via settings "
                    "reload; stopping."
                )
                await self._close_graph()
            elif model_changed:
                logger.info(
                    "MemoryService: agentic model changed "
                    "(%r -> %r) via settings reload; rebuilding "
                    "graph pipeline.",
                    current_model_key,
                    new_model_key,
                )
                await self._close_graph()
                await self._initialize_graph()
            elif want_search and self._graph_search is None:
                # Search was on but the store failed to build at
                # boot (transient). Retry now that settings were
                # reloaded -- the operator's explicit action is a
                # reasonable trigger.
                await self._initialize_graph()
        except Exception as graph_reload_err:
            logger.warning(
                "MemoryService: graph reload check failed "
                "(non-fatal): %s", graph_reload_err,
            )

        # Re-apply the LLM concurrency limit: the operator may have
        # widened it for a hosted provider (or narrowed it back to 1
        # for a local model).
        self._apply_llm_concurrency()

        # --- Phase 6: re-evaluate agentic retrieval ---------------
        #
        # [FIX] This block was MISSING. ``_initialize_agentic`` ran
        # only from ``ainitialize``, so enabling agentic in the UI
        # (which persists through PATCH /settings -> reload_settings)
        # left ``_agentic_orchestrator`` at ``None`` until a server
        # RESTART. The observable symptom was exactly:
        #
        #   "Agentic search is enabled on the server but no model
        #    resolved. Save a model above to activate it."
        #
        # ...repeatedly, after the model HAD been saved, because
        # ``/agentic/status`` reports ``configured`` from the live
        # pipeline object, not from the stored settings.
        #
        # Four cases drive action, mirroring the graph block:
        #
        #   1. off -> on:  build the pipeline.
        #   2. on  -> off: tear it down.
        #   3. on  -> on WITH a model change: rebuild, because the
        #      rewriter/synthesizer captured the old provider/model
        #      pair at construction.
        #   4. on, unchanged, but the pipeline is missing: build it.
        #      Covers a boot-time failure (no network, provider
        #      down) recovering on the operator's next save.
        try:
            retrieval_now = getattr(
                new_settings, "retrieval", None
            )
            ai_now = getattr(new_settings, "ai", None)

            want_agentic = bool(
                getattr(retrieval_now, "agentic_enabled", False)
                and ai_now is not None
                and getattr(ai_now, "enabled", False)
            )
            # "have" is derived from the PIPELINE, not from the
            # stored flag -- that is the whole point of this fix.
            have_agentic = (
                self._agentic_orchestrator is not None
                or self._agentic_retriever is not None
            )

            resolved_agentic = (
                self._resolve_agentic_model()
                if want_agentic
                else None
            )
            new_agentic_key = (
                f"{resolved_agentic[0]}::{resolved_agentic[1]}"
                if resolved_agentic
                else None
            )
            current_agentic_key = None
            if self._agentic_synthesizer is not None:
                # `getattr` rather than direct attribute access: this
                # whole block is wrapped in a broad `except`, so a
                # missing attribute would abort the ENTIRE reload
                # (including the enable/disable transitions) and be
                # reported as a benign warning. That is exactly how
                # the first version of this fix failed.
                current_agentic_key = (
                    f"{getattr(self._agentic_synthesizer, 'provider_id', '')}"
                    f"::{getattr(self._agentic_synthesizer, 'model_id', '')}"
                )
            agentic_model_changed = (
                have_agentic
                and new_agentic_key is not None
                and new_agentic_key != current_agentic_key
            )

            if want_agentic and not have_agentic:
                logger.info(
                    "MemoryService: agentic retrieval enabled via "
                    "settings reload; initializing."
                )
                await self._initialize_agentic()
            elif not want_agentic and have_agentic:
                logger.info(
                    "MemoryService: agentic retrieval disabled via "
                    "settings reload; stopping."
                )
                await self._close_agentic()
            elif agentic_model_changed:
                logger.info(
                    "MemoryService: agentic model changed "
                    "(%r -> %r) via settings reload; rebuilding.",
                    current_agentic_key,
                    new_agentic_key,
                )
                await self._close_agentic()
                await self._initialize_agentic()
            elif want_agentic and not have_agentic:
                # Unreachable given the branch above; kept explicit
                # so a future reordering cannot silently skip the
                # build path.
                await self._initialize_agentic()

            # Report honestly: if the operator turned it on but no
            # model resolved, say so at WARNING -- the UI shows this
            # as "configured: false" and the user needs the log line
            # to know the backend agrees.
            if want_agentic and not (
                self._agentic_orchestrator is not None
                or self._agentic_retriever is not None
            ):
                logger.warning(
                    "MemoryService: agentic requested but the "
                    "pipeline could not be built (no resolvable "
                    "model, or ai.enabled is off). /agentic/status "
                    "will report configured=false until a model is "
                    "selected and saved."
                )
        except Exception as agentic_reload_err:
            logger.warning(
                "MemoryService: agentic reload check failed "
                "(non-fatal): %s", agentic_reload_err,
            )

        return new_settings

    # ------------------------------------------------------------------
    # Extraction queue (Phase 1-C)
    # ------------------------------------------------------------------

    async def enqueue_extraction(
        self,
        *,
        trigger_type: ExtractionTrigger,
        chat_id: str,
        project_id: Optional[str],
        start_turn_index: int,
        end_turn_index: int,
    ) -> Optional["MemoryJob"]:
        """Enqueue an extraction job with an EXPLICIT turn range.

        Used by callers that already know exactly which turns
        need processing -- typically the dispatcher's After-Turn
        hook (per-turn range) or the operator's "extract these
        turns" UI action.

        Returns the created ``MemoryJob``, or ``None`` when:

          * extraction is not initialized (disabled in settings,
            non-SQLite provider, or init failed),
          * the range is empty / invalid,
          * the session file cannot be read,
          * the idempotency key matches an existing job
            (duplicate suppression).

        Never raises for expected failure modes. A redundant
        call is a safe no-op.
        """
        if self._trigger_router is None:
            logger.debug(
                "enqueue_extraction: extraction disabled; skipping "
                "(chat=%r, trigger=%r).",
                chat_id, trigger_type.value,
            )
            return None

        jobs = await self._trigger_router.route_explicit(
            chat_id=chat_id,
            project_id=project_id,
            trigger_type=trigger_type,
            start_turn_index=start_turn_index,
            end_turn_index=end_turn_index,
        )
        return jobs[0] if jobs else None

    async def enqueue_extraction_from_session(
        self,
        *,
        trigger_type: ExtractionTrigger,
        chat_id: str,
        project_id: Optional[str],
    ) -> Optional["MemoryJob"]:
        """Enqueue an extraction job whose range is derived from the
        session's current state.

        Range derivation depends on the trigger type:

          * ``AFTER_SESSION``: pending tail (cursor+1 .. total-1).
            Skipped if already extracted (cursor is caught up).
          * ``MANUAL``:          whole session (0 .. total-1).
            Used by the "Extract from this chat" button, which
            intentionally re-processes the whole chat.

        Returns the created ``MemoryJob`` or ``None``. Same
        non-raising contract as ``enqueue_extraction``.
        """
        if self._trigger_router is None:
            logger.debug(
                "enqueue_extraction_from_session: extraction "
                "disabled; skipping (chat=%r, trigger=%r).",
                chat_id, trigger_type.value,
            )
            return None

        if trigger_type == ExtractionTrigger.MANUAL:
            jobs = await self._trigger_router.route_on_demand(
                chat_id=chat_id,
                project_id=project_id,
            )
        else:
            jobs = await self._trigger_router.route_after_session(
                chat_id=chat_id,
                project_id=project_id,
            )
        return jobs[0] if jobs else None

    async def notify_turn_completed(
        self,
        *,
        chat_id: str,
        project_id: Optional[str],
        current_turn_index: int,
    ) -> None:
        """Fire the After-Turn extraction hook. Called by the
        dispatcher after each successful turn.

        Respects the ``extraction_trigger`` setting:
          * ``after_turn`` / ``both``  -> fire the route,
          * ``after_session``          -> no-op.

        Never raises -- the caller has just completed a user turn
        and cannot afford a memory-subsystem failure to abort it.
        """
        if self._trigger_router is None:
            return
        trigger = self.get_extraction_trigger()
        if trigger not in ("after_turn", "both"):
            return

        try:
            jobs = await self._trigger_router.route_after_turn(
                chat_id=chat_id,
                project_id=project_id,
                current_turn_index=current_turn_index,
            )
            if jobs:
                logger.info(
                    "notify_turn_completed: enqueued %d After-Turn "
                    "job(s) for chat=%r at turn %d.",
                    len(jobs), chat_id, current_turn_index,
                )
        except Exception as exc:
            logger.warning(
                "notify_turn_completed failed for chat=%r "
                "(non-fatal): %s",
                chat_id, exc,
            )

    async def notify_session_updated(
        self,
        *,
        chat_id: str,
        project_id: Optional[str] = None,
    ) -> None:
        """Fire the After-Session hook. Called by the dispatcher after
        each turn (in addition to ``notify_turn_completed``) and by
        any code that observes a completed session.

        Delegates to ``SessionStatusWatcher``, which enforces the
        trigger-mode check, the ``status == 'completed'`` check,
        and idempotency. Never raises.
        """
        if self._status_watcher is None:
            return
        try:
            await self._status_watcher.notify_session_updated(
                chat_id=chat_id,
                project_id=project_id,
            )
        except Exception as exc:
            logger.warning(
                "notify_session_updated failed for chat=%r "
                "(non-fatal): %s",
                chat_id, exc,
            )

    async def recover_extractions(self) -> int:
        """Re-enqueue completed-but-unprocessed sessions at startup.

        Called once by ``main.py`` lifespan AFTER ``ainitialize``
        succeeds. Returns the number of jobs enqueued (0 when
        extraction is off or nothing needs recovery).
        """
        if self._status_watcher is None:
            return 0
        try:
            return await self._status_watcher.recover()
        except Exception as exc:
            logger.warning(
                "recover_extractions failed (non-fatal): %s", exc,
            )
            return 0

    async def start_extraction_worker(self) -> bool:
        """Start the extraction worker if the transport is real.

        Returns
        -------
        bool
            True when the worker is running after this call.
            False when the worker was NOT started for any of
            these reasons:

              * extraction pipeline was not initialized (settings
                disabled, provider not SQLite, or init failed),
              * no real transport was injected via
                ``transport_provider`` -- the default gateway
                uses a stub transport that raises
                ``NotImplementedError``; starting the worker
                would only produce failed jobs,
              * ``worker.start()`` itself raised (logged at
                ERROR; state is consistent).

            Idempotent: calling on an already-running worker is a
            no-op that returns True.

        Never raises. All failure modes are logged and reported
        as ``False``.
        """
        if self._worker is None:
            logger.debug(
                "start_extraction_worker: extraction pipeline not "
                "initialized; nothing to start."
            )
            return False
        if not self._has_real_transport:
            logger.warning(
                "start_extraction_worker: no real transport "
                "configured (transport_provider is None or it "
                "returned None). The worker will NOT start. Jobs "
                "may still be enqueued and will remain in "
                "'pending' status until a real transport is "
                "wired."
            )
            return False
        if self._worker.is_running:
            logger.debug(
                "start_extraction_worker: worker already running."
            )
            return True
        try:
            await self._worker.start()
            logger.info(
                "MemoryExtractionWorker started (worker_id=%s).",
                self._worker.worker_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "start_extraction_worker: worker.start() failed: %s",
                exc,
                exc_info=True,
            )
            return False

    async def stop_extraction_worker(
        self, *, timeout_seconds: int = 30
    ) -> None:
        """Stop the extraction worker gracefully.

        Idempotent: safe to call when the worker is not running
        or was never created. Never raises -- a shutdown path
        must not throw, because that would skip subsequent
        cleanup steps in the lifespan hook.
        """
        if self._worker is None or not self._worker.is_running:
            return
        try:
            await self._worker.stop(timeout_seconds=timeout_seconds)
            logger.info("MemoryExtractionWorker stopped.")
        except Exception as exc:
            logger.warning(
                "stop_extraction_worker failed (non-fatal): %s", exc,
            )

    # ------------------------------------------------------------------
    # Embedding queue (Phase 4)
    # ------------------------------------------------------------------

    async def start_embedding_worker(self) -> bool:
        """Start the embedding worker.

        Unlike ``start_extraction_worker``, no "real transport" gate
        is required: embeddings always talk to a real HTTP endpoint
        (LM Studio, OpenAI, Ollama, etc.). The worker is created
        during ``_initialize_embedding`` ONLY when the operator has
        enabled embedding AND a model_key is configured -- if it
        exists here, it is ready to run.

        Returns
        -------
        bool
            True when the worker is running after this call.
            False when:

              * the embedding pipeline was not initialized
                (embedding disabled, no model_key, mode not
                writable, provider not sqlite_fts, or init
                failed),
              * ``worker.start()`` itself raised (logged at
                ERROR; state is consistent).

            Idempotent: calling on an already-running worker is
            a no-op that returns True.

        Never raises. All failure modes are logged and reported
        as ``False``.
        """
        if self._embedding_worker is None:
            logger.debug(
                "start_embedding_worker: embedding pipeline not "
                "initialized; nothing to start."
            )
            return False
        if self._embedding_worker.is_running:
            logger.debug(
                "start_embedding_worker: worker already running."
            )
            return True
        try:
            await self._embedding_worker.start()
            logger.info(
                "EmbeddingWorker started (worker_id=%s, "
                "models_in_snapshot=%d).",
                self._embedding_worker.worker_id,
                len(self._embedding_resolver)
                if self._embedding_resolver is not None
                else 0,
            )
            return True
        except Exception as exc:
            logger.error(
                "start_embedding_worker: worker.start() failed: %s",
                exc,
                exc_info=True,
            )
            return False

    async def stop_embedding_worker(
        self, *, timeout_seconds: int = 30
    ) -> None:
        """Stop the embedding worker gracefully.

        Idempotent: safe to call when the worker is not running
        or was never created. Never raises -- a shutdown path
        must not throw, because that would skip subsequent
        cleanup steps in the lifespan hook.

        The current in-flight job's row will be recovered by
        ``recover_stale_running`` on the next startup.
        """
        if (
            self._embedding_worker is None
            or not self._embedding_worker.is_running
        ):
            return
        try:
            await self._embedding_worker.stop(
                timeout_seconds=timeout_seconds
            )
            logger.info("EmbeddingWorker stopped.")
        except Exception as exc:
            logger.warning(
                "stop_embedding_worker failed (non-fatal): %s", exc,
            )

    # ------------------------------------------------------------------
    # Internal: extraction lifecycle
    # ------------------------------------------------------------------

    async def _initialize_extraction(self) -> None:
        """Bring up the extraction subsystem when it is applicable.

        Phase 3 (P1.6 UI) — settings source resolution:
          1. ``self._lifecycle.settings.ai`` is the AUTHORITATIVE
             source. It is populated by ``_lifecycle.ainitialize``
             and refreshed by ``reload_settings``. Reading it here
             (rather than through ``extraction_settings_provider``)
             means PATCH ``/api/memory/settings`` from the UI
             controls extraction without any extra wiring in
             ``main.py``.
          2. ``extraction_settings_provider`` remains supported as
             a BACKWARD-COMPAT fallback (tests, embedders that wire
             their own settings source). When both are available,
             the lifecycle settings win.

        Additional preconditions (unchanged from Phase 1-C):
          3. ``settings.enabled`` AND ``settings.extraction_enabled``
             must both be True (dual-gate).
          4. Memory mode must permit writes (``mode.can_write``).
          5. The provider must be ``sqlite_fts`` -- extraction
             requires the ``memory_jobs`` / ``memory_chat_cursors``
             tables that only exist in the SQLite schema.
          6. A resolvable extraction model must exist (see
             ``_model_config_provider`` below for the null-
             ``provider_id`` fallback path).

        Any failure leaves ``self._outbox`` and friends at ``None``
        -- extraction is simply unavailable, which is the correct
        degradation.
        """

        # Local settings provider -- unified source of truth.
        # Defined as a CLOSURE (not a class method) so that the
        # multiple consumers below (TriggerRouter, SessionStatusWatcher,
        # and the gateway) all see the SAME resolution logic without
        # duplication.
        def _get_ai_settings():
            # Primary: lifecycle settings (authoritative after
            # ainitialize, changes on reload_settings).
            try:
                if self._lifecycle.is_ready:
                    ai = getattr(
                        self._lifecycle.settings, "ai", None
                    )
                    if ai is not None:
                        return ai
            except Exception:
                pass
            # Fallback: caller-supplied provider (backward compat).
            if self._extraction_settings_provider is not None:
                try:
                    return self._extraction_settings_provider()
                except Exception:
                    return None
            return None

        settings = _get_ai_settings()
        if settings is None:
            logger.debug(
                "MemoryService: no AI settings available; "
                "extraction disabled."
            )
            return
        if not getattr(settings, "enabled", False):
            return
        if not getattr(settings, "extraction_enabled", False):
            return

        # [PHASE 1-C FIX — T2] Extraction writes memories, which
        # requires a WRITABLE provider. In DISABLED mode the lifecycle
        # manager installed a NullMemoryProvider whose create/update
        # raise ``MemoryUnavailableError``. In READ_ONLY mode the
        # provider exists but ``MemoryWritePipeline._enforce_can_write``
        # raises on every ``save`` / ``save_many`` call.
        #
        # In BOTH cases the extraction pipeline would enqueue jobs
        # that fail on every persistence attempt -- visible noise in
        # the jobs list with ZERO chance of success. The correct
        # degradation is to skip extraction initialization entirely:
        # no outbox, no worker, no jobs. When the operator later
        # switches to ``read_write`` mode, ``reload_settings`` plus
        # the next ``ainitialize`` cycle will bring extraction up
        # cleanly.
        #
        # Two distinct settings objects gate extraction, and both
        # must pass:
        #
        #   1. ``self._lifecycle.settings``         — the MEMORY
        #      lifecycle settings. Its ``mode`` decides whether
        #      writes are physically possible (``can_write``).
        #
        #   2. ``settings`` (the local variable above) — the AI
        #      extraction settings (``MemoryAISettings``). Its
        #      ``enabled`` / ``extraction_enabled`` flags decide
        #      whether extraction is *wanted*.
        #
        # The check below is the "wanted AND possible" gate.
        if not self._lifecycle.settings.mode.can_write:
            logger.info(
                "MemoryService: extraction skipped -- memory mode "
                "%r does not permit writes (extraction would enqueue "
                "jobs that fail on every persistence attempt).",
                self._lifecycle.settings.mode.value,
            )
            return

        provider_id = self._lifecycle.settings.provider_id
        if provider_id != "sqlite_fts":
            logger.info(
                "Extraction enabled but provider %r is not "
                "sqlite_fts; skipping extraction pipeline init.",
                provider_id,
            )
            return

        # Lazy imports (see module docstring).
        from omweb.memory.extraction.session_scanner import (
            MemorySessionScanner,
        )
        from omweb.memory.extraction.status_watcher import (
            SessionStatusWatcher,
        )
        from omweb.memory.extraction.trigger_router import (
            ExtractionModelConfig,
            TriggerRouter,
        )
        from omweb.memory.jobs.outbox import MemoryOutbox

        # Resolve the outbox DB path (defaults to provider's DB).
        db_path = self._outbox_db_path or self._lifecycle.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Open the outbox.
        outbox = MemoryOutbox(db_path)
        await outbox.open()

        # Build the pipeline components.
        scanner = MemorySessionScanner(outbox)

        # Model config provider: converts the extraction settings'
        # ``extraction`` sub-object into an ``ExtractionModelConfig``.
        #
        # [PHASE 3 FIX] Re-reads settings on EVERY call so a PATCH
        # from the UI takes effect for the NEXT job without a
        # restart. Two resolution paths when ``provider_id`` is
        # null:
        #
        #   1. Model is set, provider is null:
        #      Look up the model in the vaults via
        #      ``_resolve_model_provider``. This handles the common
        #      UI flow where the user picks a model from the
        #      "memory-models" dropdown without an explicit
        #      provider association (e.g. LM Studio models,
        #      Ollama models).
        #
        #   2. Neither is set:
        #      Fall back to the FIRST marked-memory model, via
        #      ``_resolve_first_memory_model``. This gives the
        #      extraction pipeline a sensible default when the
        #      user enabled AI but has not yet picked a specific
        #      model.
        #
        # Both paths log at DEBUG when they cannot resolve, so the
        # reason a job was NOT enqueued is diagnosable from the
        # server log.
        def _model_config_provider():
            current = _get_ai_settings()
            if current is None:
                return None
            if not getattr(current, "enabled", False):
                return None
            if not getattr(current, "extraction_enabled", False):
                return None
            extraction = getattr(current, "extraction", None)
            if extraction is None:
                return None

            pid = getattr(extraction, "provider_id", None)
            mid = getattr(extraction, "model_id", None)

            # Path 1: model set, provider null -> infer provider.
            if mid and not pid:
                inferred = self._resolve_model_provider(str(mid))
                if inferred is not None:
                    pid = inferred
                    logger.info(
                        "MemoryService: inferred provider %r for "
                        "model %r (no explicit provider_id in "
                        "settings).",
                        pid, mid,
                    )

            # Path 2: neither set -> first marked memory model.
            if not mid:
                fallback = self._resolve_first_memory_model()
                if fallback is not None:
                    pid, mid = fallback
                    logger.info(
                        "MemoryService: using first marked memory "
                        "model %r::%r (no explicit selection in "
                        "settings).",
                        pid, mid,
                    )

            if not pid or not mid:
                logger.debug(
                    "MemoryService: extraction model not resolved "
                    "(provider=%r, model=%r); no job will be "
                    "created until the user picks a model in "
                    "Settings → Memory → AI.",
                    pid, mid,
                )
                return None

            return ExtractionModelConfig(
                provider_id=str(pid),
                model_id=str(mid),
            )

        # Batching flag (defaults to True per plan §12).
        after_turn_batching = bool(
            getattr(settings, "after_turn_batching", True)
        )

        router = TriggerRouter(
            scanner,
            outbox,
            model_config_provider=_model_config_provider,
            after_turn_batching=after_turn_batching,
        )
        watcher = SessionStatusWatcher(
            scanner,
            router,
            # Re-use the same closure as the router and gateway so
            # all three consumers see the same AI settings source.
            # Passing ``self._extraction_settings_provider`` directly
            # would break when it is ``None`` (the production case
            # with the current ``initialize_memory_service`` wiring).
            settings_provider=_get_ai_settings,
        )

        # ------------------------------------------------------------------
        # Build the extraction orchestrator with a gateway whose transport
        # is either the caller-supplied real transport (via
        # ``transport_provider``) or the default stub. The stub is
        # intentional and safe: ``start_extraction_worker`` checks
        # ``_has_real_transport`` and refuses to launch the worker when
        # the stub is in use. This is the Phase 1-C "graceful degradation"
        # contract -- the pipeline is fully built and inspectable, but
        # it cannot accidentally burn LLM cost or corrupt the cursor
        # without a real transport.
        # ------------------------------------------------------------------
        from omweb.memory.extraction.orchestrator import (
            ExtractionOrchestrator,
        )
        from omweb.memory.extraction.gateway import (
            MemoryModelGateway,
        )

        # ------------------------------------------------------------------
        # Build the extraction gateway. Three configuration modes:
        #
        #   1. ``transport_provider`` supplied AND returns a transport
        #      -> use that transport verbatim (test mock or custom
        #      production wiring). Worker starts.
        #
        #   2. ``transport_provider`` supplied BUT returns None
        #      -> explicit "no transport" signal. Gateway still gets
        #      the real default (so it is constructible), but the
        #      worker is prevented from starting. This is the
        #      operator's opt-out path.
        #
        #   3. ``transport_provider`` NOT supplied
        #      -> Phase 1-C complete: default transport is now REAL
        #      (peldrun.llm-backed, reads models_vault.json). Worker
        #      starts automatically. This is the production path.
        # ------------------------------------------------------------------
        transport = None
        if self._transport_provider is not None:
            try:
                transport = self._transport_provider()
            except Exception as exc:
                logger.warning(
                    "MemoryService: transport_provider raised; "
                    "extraction worker will not start in production "
                    "mode: %s",
                    exc,
                )
                transport = None

        # Resolve extraction_extra_body from the CURRENT settings so
        # that PATCH /api/memory/settings + reload_settings rebuilds
        # the gateway with the operator's preferred payload.
        _extraction_extra_body = dict(
            getattr(
                getattr(settings, "extraction", None),
                "extraction_extra_body",
                {},
            )
            or {}
        )

        if transport is not None:
            gateway = MemoryModelGateway(
                transport=transport,
                force_openai_compat=True,
                extraction_extra_body=_extraction_extra_body,
            )
            self._has_real_transport = True
            logger.info(
                "MemoryService: extraction gateway wired with "
                "caller-supplied transport."
            )
        elif self._transport_provider is not None:
            # Provider supplied but returned None -> explicit opt-out.
            gateway = MemoryModelGateway(
                force_openai_compat=True,
                extraction_extra_body=_extraction_extra_body,
            )
            self._has_real_transport = False
            logger.info(
                "MemoryService: transport_provider returned None -- "
                "worker will not start (explicit opt-out)."
            )
        else:
            # No provider supplied. Default transport is REAL
            # (peldrun.llm-backed). Worker starts.
            gateway = MemoryModelGateway(
                force_openai_compat=True,
                extraction_extra_body=_extraction_extra_body,
            )
            self._has_real_transport = True
            logger.info(
                "MemoryService: extraction gateway using DEFAULT "
                "REAL transport (peldrun.llm-backed, reads "
                "models_vault.json). Worker will start when "
                "start_extraction_worker() is called."
            )

        extraction_cfg = getattr(settings, "extraction", None)
        orchestrator = ExtractionOrchestrator(
            gateway=gateway,
            max_candidates_per_job=int(
                getattr(settings, "max_candidates_per_job", 20)
            ),
            min_confidence=float(
                getattr(settings, "min_confidence", 0.0)
            ),
            max_output_tokens=(
                int(getattr(extraction_cfg, "max_output_tokens", 1200))
                if extraction_cfg is not None
                else 1200
            ),
            timeout_seconds=(
                int(getattr(extraction_cfg, "timeout_seconds", 45))
                if extraction_cfg is not None
                else 45
            ),
        )

        # ------------------------------------------------------------------
        # Build the extraction worker. The write-pipeline provider is a
        # CALLABLE (not a fixed instance) so that a settings reload
        # between jobs is observed automatically -- see
        # ``MemoryExtractionWorker``'s module docstring, invariant #3.
        # ------------------------------------------------------------------
        from omweb.memory.jobs.worker import (
            MemoryExtractionWorker,
        )
        from omweb.memory.manager.write_pipeline import (
            MemoryWritePipeline,
        )

        def _write_pipeline_provider() -> "MemoryWritePipeline":
            # [PHASE 5] Use ``self.provider`` so the write pipeline
            # observes the SAME provider the rest of the service
            # uses. When hybrid is built, this returns the hybrid
            # wrapper (which delegates writes to FTS transparently
            # via __getattr__); otherwise the raw FTS provider.
            #
            # [PHASE 6] ``graph_indexer`` is passed by VALUE at
            # construction time. That is intentional: the provider is
            # rebuilt per job, and a job must observe the indexer
            # that was active when it started. A ``None`` indexer
            # (graph disabled) leaves the write path byte-for-byte
            # identical to Phase 5.
            return MemoryWritePipeline(
                port=self.provider,
                settings=self._lifecycle.settings,
                graph_indexer=self._graph_indexer,
            )

        heartbeat_seconds = float(
            getattr(
                settings,
                "snapshot_heartbeat_interval_seconds",
                5,
            )
        )
        worker = MemoryExtractionWorker(
            outbox,
            scanner=scanner,
            orchestrator=orchestrator,
            write_pipeline_provider=_write_pipeline_provider,
            heartbeat_interval_seconds=heartbeat_seconds,
        )

        # ------------------------------------------------------------------
        # Build the batch service (Phase 3). It composes the same
        # outbox / scanner / trigger router the extraction pipeline
        # already uses; the settings provider is the SAME closure
        # used by the router, so both see the same authoritative
        # settings at all times.
        # ------------------------------------------------------------------
        from omweb.memory.batch.service import MemoryBatchService

        batch_service = MemoryBatchService(
            outbox=outbox,
            scanner=scanner,
            trigger_router=router,
            settings_provider=_get_ai_settings,
        )

        # Commit the subsystem state ONLY after all steps succeed.
        self._outbox = outbox
        self._scanner = scanner
        self._trigger_router = router
        self._status_watcher = watcher
        self._orchestrator = orchestrator
        self._worker = worker  # created but NOT started
        self._batch_service = batch_service

        logger.info(
            "MemoryService: extraction pipeline initialized "
            "(outbox_db=%s, after_turn_batching=%s, "
            "real_transport=%s, heartbeat=%ss, batch_service=up).",
            db_path,
            after_turn_batching,
            self._has_real_transport,
            heartbeat_seconds,
        )

    # ------------------------------------------------------------------
    # Internal: extraction model resolution (Phase 3 / P1.6 UI)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model_provider(model_id: str) -> Optional[str]:
        """Infer provider id for a model from AUTHORITATIVE sources ONLY.

        Design contract -- no hard-coded knowledge anywhere:

          * NO provider-name constants (``"openai"``, ``"ollama"``,
            ``"lmstudio"``, ``"anthropic"``, ...). Provider ids are
            always READ from a data source, never embedded in this
            module.

          * NO URL substrings (ports, domains, path segments). The
            module never inspects a base_url to guess a provider.

          * NO model-name prefixes (``gpt-``, ``claude-``, ...).
            A model called "custom-gpt-xyz" is a legitimate custom
            deployment; it must not be silently routed to OpenAI.

        When nothing authoritative resolves the model, this method
        returns ``None``. ``_model_config_provider`` upstream treats
        ``None`` as "no job enqueue" and logs the reason. That is
        the CORRECT behavior: an unresolved model means the operator
        has not declared which provider owns it, and guessing would
        produce a job that fails on the first LLM call.

        Resolution order -- each step reads ONLY from what its
        source declares:

          1. ``models_metadata.json[model_id].provider``
             Explicit assignment written by /api/config/fetch-models.
             If present, it is the answer -- no further probing.

          2. ``models_vault.json`` per-entry assignment:
             a. ``cloud_vault[*].savedModels``      -> entry["id"]
             b. ``cloud_vault[*].popularModels``    -> entry["id"]
             c. ``lmstudio_vault.savedModels``      -> lmstudio_vault["provider"]
             d. ``ollama_vault.savedModels``        -> ollama_vault["provider"]
             e. ``provider_package_models[pkg_id]`` -> pkg_id (the dict key)

        Vault entry contract
        --------------------
        A vault entry MUST carry a ``provider`` field (or the
        cloud-vault's ``id`` field) to be resolvable. Entries
        without one -- e.g. an ``ollama_vault`` missing
        ``"provider": "ollama"`` -- return ``None``. Operators who
        need the model to be routable add the field; the UI's
        Save action writes it automatically for new entries.

        Vault file location resolution (unchanged from Phase 1-C):
          1. ``PELDRUN_MODELS_VAULT_PATH`` env var (tests).
          2. ``<backend>/data/models_vault.json``.
          3. ``<backend>/data/models_vault copy.json``.
        """
        import json
        import os
        from pathlib import Path

        if not model_id:
            return None

        # ------------------------------------------------------------------
        # Step 1 -- explicit provider in models_metadata.json
        # ------------------------------------------------------------------
        try:
            from peldrun.storage_paths import get_storage_root

            meta_path = get_storage_root() / "models_metadata.json"
            if meta_path.is_file():
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                entry = meta.get(model_id) or meta.get(
                    model_id.lower()
                )
                if isinstance(entry, dict):
                    explicit = entry.get("provider")
                    if (
                        isinstance(explicit, str)
                        and explicit.strip()
                    ):
                        return explicit.strip()
        except Exception as exc:
            logger.debug(
                "MemoryService._resolve_model_provider: metadata "
                "read failed for %r: %s", model_id, exc,
            )

        # ------------------------------------------------------------------
        # Step 2 -- models_vault.json per-entry assignment
        # ------------------------------------------------------------------
        try:
            env_override = os.environ.get(
                "PELDRUN_MODELS_VAULT_PATH", ""
            ).strip()
            vault_path: Optional[Path] = None
            if env_override:
                vault_path = Path(env_override).expanduser()
            else:
                backend_dir = Path(__file__).resolve().parents[3]
                for candidate in (
                    backend_dir / "data" / "models_vault.json",
                    backend_dir / "data" / "models_vault copy.json",
                ):
                    if candidate.is_file():
                        vault_path = candidate
                        break

            if vault_path is None or not vault_path.is_file():
                return None

            with open(vault_path, "r", encoding="utf-8") as f:
                vault = json.load(f)

            # 2a -- cloud_vault: entry["id"] IS the provider id.
            # Try savedModels first (authoritative -- user saved
            # the model under this provider explicitly), then
            # popularModels (provider-curated suggestions).
            for list_key in ("savedModels", "popularModels"):
                cloud_vault = vault.get("cloud_vault") or []
                if not isinstance(cloud_vault, list):
                    continue
                for entry in cloud_vault:
                    if not isinstance(entry, dict):
                        continue
                    models = entry.get(list_key) or []
                    if not isinstance(models, list):
                        continue
                    if model_id in models:
                        pid = entry.get("id")
                        if isinstance(pid, str) and pid.strip():
                            return pid.strip()

            # 2b + 2c -- local vaults: entry["provider"] IS the
            # provider id. The vault key (``lmstudio_vault``,
            # ``ollama_vault``, ...) is NOT used as a fallback
            # because that would re-introduce hard-coded provider
            # names. Entries without ``provider`` are unresolvable
            # by contract.
            for vault_key in (
                "lmstudio_vault",
                "ollama_vault",
            ):
                local_entry = vault.get(vault_key)
                if not isinstance(local_entry, dict):
                    continue
                saved = local_entry.get("savedModels") or []
                if not isinstance(saved, list):
                    continue
                if model_id not in saved:
                    continue
                pid = local_entry.get("provider")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()
                # Entry lacks ``provider``. Skip -- the next vault
                # (if any) might match, otherwise return None.

            # 2d -- provider_package_models: the DICT KEY is the
            # package id (which doubles as the provider id when
            # dispatching through provider_factory).
            pkg_models = vault.get("provider_package_models")
            if isinstance(pkg_models, dict):
                for pkg_id, pkg_data in pkg_models.items():
                    if not isinstance(pkg_data, dict):
                        continue
                    saved = pkg_data.get("savedModels") or []
                    if not isinstance(saved, list):
                        continue
                    if model_id in saved:
                        pid = str(pkg_id).strip()
                        if pid:
                            return pid

            # 2e -- custom_endpoints: user-added OpenAI-compatible
            # endpoints. Each entry carries ``providerId`` (the
            # canonical id passed to ``provider_factory``) and
            # ``savedModels``. The read-only contract is identical
            # to the other vault containers: we NEVER invent a
            # provider id -- we only return what the entry declares.
            custom = vault.get("custom_endpoints")
            if isinstance(custom, list):
                for entry in custom:
                    if not isinstance(entry, dict):
                        continue
                    saved = entry.get("savedModels") or []
                    if not isinstance(saved, list):
                        continue
                    if model_id not in saved:
                        continue
                    pid = entry.get("providerId") or entry.get("id")
                    if isinstance(pid, str) and pid.strip():
                        return pid.strip()
        except Exception as exc:
            logger.debug(
                "MemoryService._resolve_model_provider: vault lookup "
                "failed for %r: %s", model_id, exc,
            )

        return None

    @staticmethod
    def _resolve_first_memory_model() -> Optional[tuple]:
        """Return ``(provider_id, model_id)`` for the first marked model.

        Calls ``config_rtr.get_memory_models()`` -- the same function
        behind ``GET /api/config/memory-models``. It returns a list
        of STRINGS, each being either:

          * ``"provider_id::model_id"`` -- when the model has an
            explicit provider association, OR
          * ``"model_id"`` alone -- when the model is a local
            deployment (LM Studio / Ollama) without an explicit
            provider entry.

        In the second case, provider is inferred via
        ``_resolve_model_provider`` before returning.

        Returns ``None`` when no model is marked as memory, or
        when the function itself raises (the router is a critical
        startup dependency -- a broken config_rtr must NOT break
        extraction).
        """
        try:
            from omweb.routers.config_rtr import (
                get_memory_models,
            )
            marked = get_memory_models()
        except Exception as exc:
            logger.debug(
                "MemoryService._resolve_first_memory_model: "
                "get_memory_models() failed: %s", exc,
            )
            return None

        if not isinstance(marked, list) or not marked:
            return None

        first = marked[0]
        if not isinstance(first, str) or not first.strip():
            return None

        if "::" in first:
            pid, mid = first.split("::", 1)
            pid = pid.strip()
            mid = mid.strip()
            if pid and mid:
                return (pid, mid)
            return None

        # No explicit provider -- infer from vaults.
        mid = first.strip()
        pid = MemoryService._resolve_model_provider(mid)
        if pid is None:
            return None
        return (pid, mid)

    async def _initialize_embedding(self) -> None:
        """Bring up the embedding pipeline when it is applicable.

        Preconditions (all must pass, otherwise return early with a
        DEBUG/INFO log explaining why):

          1. ``settings.embedding`` exists and is a dict/object.
          2. ``settings.embedding.enabled`` is True.
          3. ``settings.embedding.model_key`` is a non-empty string.
          4. ``mode.can_write`` is True -- the worker writes
             embeddings (vectors) to ``memory_embeddings`` and
             ``memory_vectors``. Read-only or disabled mode cannot
             support that.
          5. ``provider_id == "sqlite_fts"`` -- the only provider
             that exposes the ``memory_embeddings`` / ``memory_vectors``
             tables this pipeline depends on.

        Construction order (matches the worker's dependency graph):

          1. ``build_embedding_model_snapshot`` -- read vault +
             metadata, produce the typed snapshot. Fail-soft.
          2. ``EmbeddingModelResolver(snapshot)`` -- typed registry.
             This WILL raise ``ValueError`` if the snapshot contains
             any key/model_key mismatch, which is why the
             ``snapshot_builder`` module must never register the
             same instance under a second (bare) key.
          3. ``EmbeddingOutbox(db_path).open()`` -- durable job
             queue for embedding work (separate from extraction).
          4. ``SqliteMemoryRepository(db_path)`` -- reads memories'
             content for embedding. Lazy connection (no open()
             needed).
          5. ``SqliteVecRepository(db_path)`` -- writes vectors into
             ``memory_embeddings`` + the vec0 index. Lazy connection
             (no open() needed).
          6. ``EmbeddingWorker(...)`` -- created but NOT started.

        The worker is intentionally NOT started here. ``main.py``'s
        lifespan hook calls ``start_embedding_worker()`` after this
        returns, which gives the shutdown hook the same explicit
        ordering control that the extraction worker already has.

        Any exception during construction is logged at WARNING and
        leaves every ``self._embedding_*`` field at ``None`` --
        embedding is simply unavailable, and the memory subsystem
        continues to serve retrieval / extraction normally. This is
        the correct degradation: a broken embedding pipeline must
        never block startup.
        """
        # ---- 1. Settings gate --------------------------------
        settings = self._lifecycle.settings
        embedding_cfg = getattr(settings, "embedding", None)
        if embedding_cfg is None:
            logger.debug(
                "MemoryService: no embedding settings; embedding "
                "pipeline disabled."
            )
            return
        if not getattr(embedding_cfg, "enabled", False):
            logger.debug(
                "MemoryService: embedding.enabled=False; "
                "embedding pipeline disabled."
            )
            return
        model_key = getattr(embedding_cfg, "model_key", None)
        if not isinstance(model_key, str) or not model_key.strip():
            logger.info(
                "MemoryService: embedding.enabled=True but no "
                "model_key configured. Embedding pipeline will "
                "NOT start until the operator selects a model "
                "via PATCH /api/memory/settings."
            )
            return

        # ---- 2. Mode + provider gate -------------------------
        if not settings.mode.can_write:
            logger.info(
                "MemoryService: embedding skipped -- memory mode "
                "%r does not permit writes (embedding writes "
                "vectors to the DB).",
                settings.mode.value,
            )
            return
        if settings.provider_id != "sqlite_fts":
            logger.info(
                "MemoryService: embedding skipped -- provider %r "
                "is not sqlite_fts.",
                settings.provider_id,
            )
            return

        # ---- 2b. Verify sqlite-vec is available -------------
        #
        # [TASK 4.12a-2] Fail FAST before the worker is built.
        #
        # Without this gate, a deployment with
        # ``embedding.enabled=True`` but missing the
        # ``sqlite-vec`` package would build the worker, let it
        # claim jobs, call the embedding provider N times, and
        # only THEN fail with ``Vec0UnavailableError`` on the
        # first vec0 write attempt -- retrying until each job's
        # ``max_attempts`` is exhausted.
        #
        # The check costs one :memory: connection (<20 ms). The
        # alternative costs one wasted API call per memory per
        # reindex attempt. Cheap insurance against a
        # configuration mistake that would otherwise be invisible
        # in the boot log.
        #
        # Failure mode: log at ERROR and return WITHOUT building
        # the pipeline. The embedding worker is not created, so
        # no jobs are ever claimed. The outbox remains on disk
        # with any existing ``pending`` jobs -- they survive the
        # restart and can be processed once the operator installs
        # the missing package. Memory retrieval continues to
        # operate on FTS (no semantic recall) during this
        # degraded state, which is the correct behavior.
        from omweb.memory.storage.vector_repository import (
            SqliteVecRepository,
        )
        if not SqliteVecRepository.probe_availability():
            logger.error(
                "MemoryService: embedding.enabled=True but "
                "sqlite-vec cannot be loaded on this Python "
                "installation. The embedding pipeline CANNOT "
                "start. Jobs already in the outbox will remain "
                "'pending' until sqlite-vec is installed. "
                "Install with:  pip install sqlite-vec  "
                "(a precompiled wheel exists for CPython 3.11+ "
                "on Windows, macOS, and Linux). Then restart "
                "the server. Memory retrieval will continue to "
                "serve results via FTS (keyword-only, no "
                "semantic recall) during this degraded state."
            )
            return

        # ---- 3. Lazy imports --------------------------------
        from omweb.memory.jobs.embedding_worker import (
            EmbeddingWorker,
        )
        from omweb.memory.jobs.outbox import EmbeddingOutbox
        from omweb.memory.manager.snapshot_builder import (
            build_embedding_model_snapshot,
        )
        from omweb.memory.storage.repository import (
            SqliteMemoryRepository,
        )
        from omweb.memory.storage.vector_repository import (
            SqliteVecRepository,
        )
        from peldrun.memory.embeddings.model_resolver import (
            EmbeddingModelResolver,
        )

        # ---- 4. Snapshot + resolver -------------------------
        try:
            snapshot = build_embedding_model_snapshot()
        except Exception as exc:
            logger.warning(
                "MemoryService: build_embedding_model_snapshot() "
                "raised; embedding pipeline will not start: %s",
                exc,
                exc_info=True,
            )
            return

        if not snapshot:
            logger.info(
                "MemoryService: embedding snapshot is EMPTY "
                "(no embedding models marked in metadata or in "
                "the vault's embedding_models index). Pipeline "
                "will not start until at least one model is "
                "configured. model_key=%r",
                model_key,
            )
            return

        if model_key not in snapshot:
            # Configured model_key does not resolve. This is NOT a
            # fatal error -- the operator may simply have hit
            # PATCH before running Scan Models on the provider.
            # Jobs enqueued by /reindex will fail with
            # MODEL_NOT_CONFIGURED at claim time, which is
            # diagnosable from the jobs table.
            logger.warning(
                "MemoryService: embedding model_key %r does not "
                "resolve in the snapshot (known keys: %s). The "
                "worker will start but every job for this model "
                "will fail with MODEL_NOT_CONFIGURED until the "
                "model is re-scanned and re-selected.",
                model_key,
                list(snapshot.keys())[:5],
            )

        try:
            resolver = EmbeddingModelResolver(snapshot)
        except Exception as exc:
            # This is the exact failure mode our snapshot_builder
            # fix prevents. If it fires, the snapshot_builder still
            # has a key/model_key mismatch bug -- log loudly.
            logger.error(
                "MemoryService: EmbeddingModelResolver "
                "construction failed -- the snapshot contains a "
                "key/model_key mismatch. See snapshot_builder.py "
                "for the key invariant. Cause: %s",
                exc,
                exc_info=True,
            )
            return

        # ---- 5. Open outbox --------------------------------
        db_path = self._outbox_db_path or self._lifecycle.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            embedding_outbox = EmbeddingOutbox(db_path)
            await embedding_outbox.open()
        except Exception as exc:
            logger.warning(
                "MemoryService: EmbeddingOutbox open failed; "
                "embedding pipeline will not start: %s",
                exc,
                exc_info=True,
            )
            return

        # ---- 6. Repositories (lazy, no await) -------------
        try:
            mem_repo = SqliteMemoryRepository(db_path)
            vec_repo = SqliteVecRepository(db_path)
        except Exception as exc:
            logger.warning(
                "MemoryService: repository construction failed; "
                "embedding pipeline will not start: %s",
                exc,
                exc_info=True,
            )
            try:
                await embedding_outbox.close()
            except Exception:
                pass
            return

        # ---- 7. Worker (created, NOT started) --------------
        try:
            worker = EmbeddingWorker(
                outbox=embedding_outbox,
                resolver=resolver,
                vector_repository=vec_repo,
                memory_repository=mem_repo,
            )
        except Exception as exc:
            logger.warning(
                "MemoryService: EmbeddingWorker construction "
                "failed; embedding pipeline will not start: %s",
                exc,
                exc_info=True,
            )
            try:
                await embedding_outbox.close()
            except Exception:
                pass
            return

        # ---- 8. Commit worker + repos FIRST --------------
        #
        # Commit these BEFORE attempting the hybrid build so
        # that a hybrid failure (e.g. LM Studio unreachable at
        # boot) does NOT prevent the worker from writing
        # embeddings. Hybrid is a retrieval enhancement -- it
        # must never disable the write path.
        self._embedding_outbox = embedding_outbox
        self._embedding_resolver = resolver
        self._embedding_worker = worker
        self._vec_repository = vec_repo
        self._memory_repository = mem_repo
        self._embedding_active_model_key = model_key.strip()

        # ---- 9. Attempt hybrid retrieval build (BACKGROUND) ----
        #
        # [FIX-BOOT-HANG] The hybrid build includes an HTTP
        # ``probe_dimensions`` call to the model provider (LM
        # Studio, OpenAI, ...) on the very first boot with
        # embedding enabled and no cached vec0 table. If the
        # provider is UNREACHABLE (LM Studio not started yet,
        # network down, credentials wrong), that HTTP call can
        # take 30+ seconds to time out. Running it synchronously
        # blocks the ENTIRE FastAPI startup -- the server does
        # not accept requests until the probe returns or fails.
        # That is the user-visible freeze observed at boot.
        #
        # Fix: run the entire hybrid build in a BACKGROUND TASK.
        # Startup returns immediately; ``service.provider`` falls
        # through to FTS until the task succeeds (typically
        # milliseconds when the provider is up, or up to 3
        # seconds when it is down thanks to the probe timeout
        # added in ``_resolve_embedding_dimensions``). Once the
        # task completes, ``_hybrid_provider`` is populated and
        # every subsequent search uses hybrid automatically --
        # no restart needed.
        #
        # Cancellation: if a PREVIOUS hybrid build task is still
        # running (only possible via ``reload_settings`` calling
        # ``_initialize_embedding`` again before the previous
        # task finished), cancel it first. Otherwise the previous
        # task could complete AFTER the new one and set
        # ``_hybrid_provider`` to a stale model's provider.
        _old_task = self._hybrid_build_task
        self._hybrid_build_task = None
        if _old_task is not None and not _old_task.done():
            try:
                _old_task.cancel()
                await _old_task
            except (asyncio.CancelledError, Exception):
                pass

        self._hybrid_build_task = asyncio.create_task(
            self._build_hybrid_provider_in_background(
                resolver=resolver,
                model_key=model_key.strip(),
                mem_repo=mem_repo,
                vec_repo=vec_repo,
                db_path=db_path,
            ),
            name="memory-hybrid-build",
        )

        logger.info(
            "MemoryService: embedding pipeline initialized "
            "(model_key=%r, models_in_snapshot=%d, outbox_db=%s, "
            "worker_id=%s, hybrid=%s). Worker will start when "
            "start_embedding_worker() is called.",
            self._embedding_active_model_key,
            len(resolver),
            db_path,
            worker.worker_id,
            "enabled"
            if self._hybrid_provider is not None
            else "disabled",
        )

    # ------------------------------------------------------------------
    # Internal: knowledge graph lifecycle (Phase 6)
    # ------------------------------------------------------------------

    def _graph_ai_settings(self) -> Optional[object]:
        """Return the current ``ai`` settings block, or ``None``."""
        try:
            if self._lifecycle.is_ready:
                return getattr(self._lifecycle.settings, "ai", None)
        except Exception:
            pass
        return None

    def _resolve_agentic_model(self) -> Optional[tuple]:
        """Resolve ``(provider_id, model_id)`` for the Phase 6 components.

        Resolution order -- each step only uses what the operator
        actually declared, never a guess from a model-name prefix:

          1. ``ai.agentic`` provider + model pair (explicit).
          2. ``ai.agentic.model_id`` alone -> infer the provider from
             ``models_metadata.json`` / ``models_vault.json``.
          3. ``ai.extraction`` pair (the operator already configured
             a memory model; reusing it keeps the UI honest -- one
             model pick drives both subsystems until they diverge).
          4. The first model marked as a memory model.

        Returns ``None`` when nothing resolves. ``EntityExtractor``
        then reports ``is_configured == False`` and every indexing
        call is a cheap no-op -- the documented degradation.
        """
        ai = self._graph_ai_settings()
        if ai is None:
            return None

        agentic = getattr(ai, "agentic", None)
        pid = getattr(agentic, "provider_id", None) if agentic else None
        mid = getattr(agentic, "model_id", None) if agentic else None
        if mid and not pid:
            inferred = self._resolve_model_provider(str(mid))
            if inferred:
                pid = inferred
        if pid and mid:
            return (str(pid).strip(), str(mid).strip())

        extraction = getattr(ai, "extraction", None)
        ep = (
            getattr(extraction, "provider_id", None)
            if extraction
            else None
        )
        em = (
            getattr(extraction, "model_id", None)
            if extraction
            else None
        )
        if em and not ep:
            inferred = self._resolve_model_provider(str(em))
            if inferred:
                ep = inferred
        if ep and em:
            return (str(ep).strip(), str(em).strip())

        return self._resolve_first_memory_model()

    def _build_agentic_gateway(self, extra_body: dict):
        """Build a ``MemoryModelGateway`` for the Phase 6 components.

        Mirrors the transport resolution that
        ``_initialize_extraction`` performs, WITHOUT modifying it:
        that path's three-mode logging is load-bearing for Phase 1-C
        behaviour and its tests, so it is left untouched. The small
        duplication is deliberate; if the transport contract ever
        changes, both call sites are listed here so neither is
        missed.
        """
        from omweb.memory.extraction.gateway import (
            MemoryModelGateway,
        )

        transport = None
        if self._transport_provider is not None:
            try:
                transport = self._transport_provider()
            except Exception as exc:
                logger.warning(
                    "MemoryService: transport_provider raised while "
                    "building the agentic gateway: %s", exc,
                )
                transport = None

        if transport is not None:
            return MemoryModelGateway(
                transport=transport,
                force_openai_compat=True,
                extraction_extra_body=extra_body,
            )
        return MemoryModelGateway(
            force_openai_compat=True,
            extraction_extra_body=extra_body,
        )

    async def _initialize_graph(self) -> None:
        """Bring up the knowledge-graph subsystem when applicable.

        Two INDEPENDENT halves
        ----------------------
        The graph has a read side and a write side, and they have
        different preconditions:

          * **READ** (graph search, sub-phase 6B) needs only a store
            over an existing v6 database. No LLM, no write
            permission -- searching a graph that was populated
            earlier is perfectly valid on a READ_ONLY deployment.

          * **WRITE** (indexing, sub-phase 6A) needs a writable
            mode, the AI master switch, the indexing flag, and a
            resolvable agentic model.

        Keeping them separate is what makes "search a graph that
        was built yesterday" work without forcing indexing back on.
        Preconditions for the READ half: ``retrieval.graph_search_enabled``
        and ``provider_id == "sqlite_fts"``.

        Boot safety
        -----------
        This method performs NO ``await``. Construction is pure
        Python object building, so it cannot contribute to a
        startup hang. The only background work it starts is the
        backfill loop, which sleeps before its first tick.

        Graceful degradation
        --------------------
        Any failure leaves the corresponding ``_graph_*`` fields at
        ``None``: graph search and/or indexing is simply
        unavailable, writes are unaffected, and retrieval stays on
        FTS/hybrid exactly as before.
        """
        settings = self._lifecycle.settings
        ai = getattr(settings, "ai", None)
        retrieval = getattr(settings, "retrieval", None)

        want_search = bool(
            getattr(retrieval, "graph_search_enabled", False)
        )
        want_index = bool(
            ai is not None
            and getattr(ai, "enabled", False)
            and getattr(ai, "graph_indexing_enabled", False)
        )

        if not want_search and not want_index:
            logger.debug(
                "MemoryService: graph disabled "
                "(graph_search_enabled=%s, graph_indexing_enabled=%s).",
                want_search,
                want_index,
            )
            return

        if settings.provider_id != "sqlite_fts":
            logger.info(
                "MemoryService: graph skipped -- provider %r is "
                "not sqlite_fts (the v6 tables only exist there).",
                settings.provider_id,
            )
            return

        # ---- Store (shared by both halves) ----------------------
        #
        # Lazy imports (consistent with the rest of this file).
        from omweb.memory.storage.graph_store import GraphStore
        from omweb.memory.storage.repository import (
            SqliteMemoryRepository,
        )

        # Reuse the embedding pipeline's repository when it exists:
        # ONE connection pool per process avoids two writers
        # competing on the same WAL file. Fall back to a dedicated
        # repository when embedding is disabled, and remember that
        # we own it so ``_close_graph`` can release it.
        repository = self._memory_repository
        owns_repository = False
        if repository is None:
            db_path = self._outbox_db_path or self._lifecycle.db_path
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                repository = SqliteMemoryRepository(db_path)
            except Exception as exc:
                logger.warning(
                    "MemoryService: could not open a repository for "
                    "the graph (%s: %s); graph disabled.",
                    type(exc).__name__, exc,
                )
                return
            owns_repository = True

        store = GraphStore(repository)
        self._graph_repository = repository
        self._owns_graph_repository = owns_repository
        self._graph_store = store

        # ---- READ half: graph search target ---------------------
        if want_search:
            from peldrun.memory.graph.graph_search import GraphSearch

            self._graph_search = GraphSearch(
                store,
                max_depth=int(
                    getattr(retrieval, "graph_search_max_depth", 2)
                    or 2
                ),
                memories_per_entity=int(
                    getattr(
                        retrieval,
                        "graph_search_memories_per_entity",
                        20,
                    )
                    or 20
                ),
                max_relations_per_entity=int(
                    getattr(
                        retrieval, "graph_search_max_relations", 20
                    )
                    or 20
                ),
                min_query_entities=int(
                    getattr(
                        retrieval,
                        "graph_search_min_query_entities",
                        1,
                    )
                    or 1
                ),
            )
            logger.info(
                "MemoryService: graph SEARCH enabled "
                "(max_depth=%d, memories_per_entity=%d).",
                self._graph_search.max_depth,
                self._graph_search._memories_per_entity
                if hasattr(self._graph_search, "_memories_per_entity")
                else 20,
            )

        # ---- WRITE half: indexing ------------------------------
        if not want_index:
            return
        if not settings.mode.can_write:
            logger.info(
                "MemoryService: graph indexing skipped -- mode %r "
                "does not permit writes (indexing persists graph "
                "rows).",
                settings.mode.value,
            )
            return

        model = self._resolve_agentic_model()
        if model is None:
            logger.info(
                "MemoryService: graph indexing enabled but no "
                "agentic model resolved (set ai.agentic.model_id "
                "in Settings -> Memory -> AI). Indexing stays "
                "inactive until a model is selected."
            )
            return
        provider_id, model_id = model

        from omweb.memory.graph.indexer import GraphIndexer
        from peldrun.memory.graph.entity_extractor import (
            EntityExtractor,
        )

        # ---- Gateway + extractor --------------------------------
        agentic_cfg = getattr(ai, "agentic", None)
        extra_body = dict(
            getattr(agentic_cfg, "extraction_extra_body", {}) or {}
        )
        gateway = self._build_agentic_gateway(extra_body)

        # Extraction tuning is shared with the Phase 1 extractor, so
        # the values are re-clamped here: a 45s per-memory deadline
        # (sensible for a whole conversation) would let a single
        # background indexing task occupy the provider for far too
        # long. 2..20s bounds it.
        raw_timeout = float(
            getattr(agentic_cfg, "timeout_seconds", 5) or 5
        )
        timeout_seconds = max(2.0, min(raw_timeout, 20.0))
        raw_tokens = int(
            getattr(agentic_cfg, "max_output_tokens", 800) or 800
        )
        max_output_tokens = max(128, min(raw_tokens, 4000))

        extractor = EntityExtractor(
            gateway,
            provider_id=provider_id,
            model_id=model_id,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        indexer = GraphIndexer(store=store, extractor=extractor)

        # ---- Commit the write half ------------------------------
        self._graph_extractor = extractor
        self._graph_indexer = indexer

        # ---- Background backfill --------------------------------
        interval = int(
            getattr(ai, "graph_backfill_interval_seconds", 900)
            or 900
        )
        self._graph_backfill_task = asyncio.create_task(
            self._graph_backfill_loop(interval),
            name="memory-graph-backfill",
        )

        logger.info(
            "MemoryService: graph INDEXING initialized "
            "(provider=%r, model=%r, timeout=%.1fs, "
            "backfill_interval=%ss, owns_repository=%s).",
            provider_id,
            model_id,
            timeout_seconds,
            interval,
            owns_repository,
        )

    async def _graph_backfill_loop(
        self, interval_seconds: int
    ) -> None:
        """Background loop that indexes un-indexed memories.

        This is what makes graph coverage EVENTUALLY complete for
        writes that bypass ``MemoryWritePipeline``: the
        ``memory_save`` tool, import batches, failed indexing tasks,
        and every memory created before graph indexing was enabled.

        The first tick is delayed (at most 60s) so startup never
        competes with the first burst of user traffic. A failure in
        one tick is logged and the loop continues -- a transient
        provider outage must not stop indexing for the rest of the
        process lifetime.
        """
        try:
            await asyncio.sleep(min(60, max(1, interval_seconds)))
            while True:
                try:
                    await self.run_graph_backfill_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MemoryService: graph backfill tick failed "
                        "(will retry): %s", exc,
                    )
                await asyncio.sleep(max(1, interval_seconds))
        except asyncio.CancelledError:
            logger.debug("MemoryService: graph backfill loop cancelled.")
            raise

    async def run_graph_backfill_once(self) -> int:
        """Index ONE batch of un-indexed memories.

        Returns the number of memories that produced at least one
        entity. Public so an operator endpoint (or a test) can force
        a tick without waiting for the timer.

        Never raises; ``0`` when graph indexing is inactive.
        """
        store = self._graph_store
        indexer = self._graph_indexer
        if store is None or indexer is None or not indexer.is_active:
            return 0

        ai = self._graph_ai_settings()
        batch = int(
            getattr(ai, "graph_backfill_batch_size", 20) or 20
        )
        try:
            entries = await store.list_unindexed_memories(
                limit=batch
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MemoryService: graph backfill could not list "
                "un-indexed memories: %s", exc,
            )
            return 0

        indexed = 0
        for entry in entries:
            try:
                produced = await indexer.index_memory(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "MemoryService: graph backfill failed for %r: %s",
                    getattr(entry, "memory_id", None), exc,
                )
                continue
            if produced:
                indexed += 1

        if indexed:
            logger.info(
                "MemoryService: graph backfill indexed %d "
                "memory/memories (batch=%d).",
                indexed, batch,
            )
        return indexed

    # ------------------------------------------------------------------
    # Internal: agentic RAG lifecycle (Phase 6C/6D)
    # ------------------------------------------------------------------

    async def _initialize_agentic(self) -> None:
        """Build the agentic pipeline when the operator opted in.

        Preconditions:

          1. ``retrieval.agentic_enabled`` is True.
          2. ``ai.enabled`` is True (the LLM master switch).
          3. An agentic model resolves (``_resolve_agentic_model``).

        Missing preconditions leave every ``_agentic_*`` field at
        ``None``, which makes ``retrieve_agentic`` fall back to the
        standard path -- the documented degradation.

        Boot safety
        -----------
        NO ``await``: object construction only. The three components
        are stateless, so building them costs microseconds and can
        never hang startup.

        Why the rewriter and synthesizer get different timeouts
        ------------------------------------------------------
        Both share one gateway and one model, but their deadlines
        reflect their role: a rewrite blocks the entire run, while
        the synthesis is the answer the user actually reads. The
        run-wide ceiling (``agentic_timeout_seconds``) is applied by
        ``retrieve_agentic`` on top of both.
        """
        settings = self._lifecycle.settings
        retrieval = getattr(settings, "retrieval", None)
        if not getattr(retrieval, "agentic_enabled", False):
            logger.debug(
                "MemoryService: retrieval.agentic_enabled=False; "
                "agentic retrieval inactive."
            )
            return

        ai = getattr(settings, "ai", None)
        if ai is None or not getattr(ai, "enabled", False):
            logger.info(
                "MemoryService: agentic_enabled=True but "
                "ai.enabled=False; agentic retrieval inactive "
                "(agentic runs need an LLM)."
            )
            return

        model = self._resolve_agentic_model()
        if model is None:
            logger.info(
                "MemoryService: agentic_enabled=True but no agentic "
                "model resolved (set ai.agentic.model_id in "
                "Settings -> Memory -> AI). Agentic retrieval will "
                "fall back to the standard path."
            )
            return
        provider_id, model_id = model

        from peldrun.memory.agentic.evidence_synthesizer import (
            EvidenceSynthesizer,
        )
        from peldrun.memory.agentic.multi_hop_retriever import (
            MultiHopRetriever,
        )
        from peldrun.memory.agentic.orchestrator import (
            AgenticRAGOrchestrator,
        )
        from peldrun.memory.agentic.query_rewriter import (
            QueryRewriter,
        )

        agentic_cfg = getattr(ai, "agentic", None)
        extra_body = dict(
            getattr(agentic_cfg, "extraction_extra_body", {}) or {}
        )
        gateway = self._build_agentic_gateway(extra_body)

        base_tokens = int(
            getattr(agentic_cfg, "max_output_tokens", 1200) or 1200
        )
        base_timeout = float(
            getattr(agentic_cfg, "timeout_seconds", 20) or 20
        )

        rewriter = QueryRewriter(
            gateway,
            provider_id=provider_id,
            model_id=model_id,
            max_output_tokens=max(128, min(base_tokens, 400)),
            # The rewrite blocks every hop; keep it tight.
            timeout_seconds=max(2.0, min(base_timeout, 10.0)),
        )
        synthesizer = EvidenceSynthesizer(
            gateway,
            provider_id=provider_id,
            model_id=model_id,
            max_output_tokens=max(256, min(base_tokens, 1500)),
            # The synthesis IS the answer; allow more room than the
            # rewrite but stay under the run-wide ceiling.
            timeout_seconds=max(3.0, min(base_timeout, 25.0)),
        )
        max_hops = int(
            getattr(retrieval, "agentic_max_hops", 3) or 3
        )
        retriever = MultiHopRetriever(
            rewriter,
            synthesizer,
            max_hops=max_hops,
        )

        # Phase 6D: the orchestrator adds the graph seed.
        #
        # ``_initialize_graph`` runs BEFORE this method, so when
        # ``retrieval.graph_search_enabled`` is on the searcher is
        # already built and is injected here. When it is off, the
        # orchestrator degrades to "the retriever plus a no-op
        # graph phase".
        run_timeout = float(
            getattr(retrieval, "agentic_timeout_seconds", 30.0)
            or 30.0
        )
        orchestrator = AgenticRAGOrchestrator(
            rewriter,
            retriever,
            synthesizer,
            self._graph_search,
            max_hops=max_hops,
            timeout_seconds=run_timeout,
        )

        self._agentic_rewriter = rewriter
        self._agentic_synthesizer = synthesizer
        self._agentic_retriever = retriever
        self._agentic_orchestrator = orchestrator

        logger.info(
            "MemoryService: agentic retrieval ENABLED "
            "(provider=%r, model=%r, max_hops=%d, "
            "run_timeout=%.0fs, graph=%s). Reachable only via "
            "retrieve_agentic / POST /api/memory/search/agentic.",
            provider_id,
            model_id,
            max_hops,
            run_timeout,
            "on" if orchestrator.graph_enabled else "off",
        )

    async def _close_agentic(self) -> None:
        """Tear down the agentic components. Idempotent; never raises.

        They hold no resources (no connections, no tasks) -- the
        gateway is shared and owned by the callers that built it --
        so dropping the references is the whole teardown. Present as
        a method so the shutdown sequence reads uniformly and a
        future component with a real lifecycle has an obvious home.
        """
        self._agentic_orchestrator = None
        self._agentic_retriever = None
        self._agentic_synthesizer = None
        self._agentic_rewriter = None

    @property
    def agentic_enabled(self) -> bool:
        """True when agentic retrieval can run.

        ``False`` means ``retrieve_agentic`` returns the standard
        ``retrieve`` context instead of a synthesized answer.
        """
        return (
            self._agentic_orchestrator is not None
            or self._agentic_retriever is not None
        )

    @property
    def agentic_graph_enabled(self) -> bool:
        """True when the agentic pipeline will consult the graph.

        Requires BOTH agentic retrieval and graph search to be
        enabled: the orchestrator receives ``_graph_search``, which
        only exists when ``retrieval.graph_search_enabled`` is on.
        """
        orchestrator = self._agentic_orchestrator
        if orchestrator is None:
            return False
        return bool(getattr(orchestrator, "graph_enabled", False))

    @property
    def agentic_retriever(self) -> Optional["MultiHopRetriever"]:
        """The agentic multi-hop retriever, or ``None``."""
        return self._agentic_retriever

    @property
    def agentic_orchestrator(
        self,
    ) -> Optional["AgenticRAGOrchestrator"]:
        """The agentic orchestrator, or ``None`` (sub-phase 6D)."""
        return self._agentic_orchestrator

    async def _close_graph(self) -> None:
        """Tear down graph indexing. Idempotent; never raises.

        Order matters:

          1. Cancel the backfill loop (it holds the longest-lived
             reference to the store).
          2. Drain per-write indexing tasks so no task is left
             writing into a closing repository -- an orphan task
             would log "Task was destroyed but it is pending" and
             leave half-written links.
          3. Close the repository ONLY when this service opened it
             (the embedding pipeline owns the other case).
        """
        task = self._graph_backfill_task
        self._graph_backfill_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            from omweb.memory.manager.write_pipeline import (
                drain_graph_tasks,
            )

            drained = await drain_graph_tasks(5.0)
            if drained:
                logger.debug(
                    "MemoryService: drained %d in-flight graph "
                    "indexing task(s) on shutdown.", drained,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "MemoryService: graph task drain failed: %s", exc,
            )

        if self._owns_graph_repository and (
            self._graph_repository is not None
        ):
            try:
                self._graph_repository.close_all()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "MemoryService: graph repository close failed: %s",
                    exc,
                )

        self._graph_repository = None
        self._owns_graph_repository = False
        self._graph_store = None
        self._graph_extractor = None
        self._graph_indexer = None
        self._graph_search = None

    @property
    def graph_search(self) -> Optional["GraphSearch"]:
        """The live graph searcher, or ``None``.

        ``None`` means graph search will contribute nothing: either
        ``retrieval.graph_search_enabled`` is off, or the graph was
        never initialized. Exposed for diagnostics and for the
        ``/api/memory/search`` response's ``search_mode`` reporting.
        """
        return self._graph_search

    @property
    def graph_enabled(self) -> bool:
        """True when knowledge-graph INDEXING is initialized."""
        return self._graph_indexer is not None

    @property
    def graph_store(self) -> Optional["GraphStore"]:
        """The graph store, or ``None`` when indexing is off."""
        return self._graph_store

    @property
    def graph_indexer(self) -> Optional["GraphIndexer"]:
        """The graph indexer, or ``None`` when indexing is off."""
        return self._graph_indexer

    # ------------------------------------------------------------------
    # Internal: memory-LLM concurrency
    # ------------------------------------------------------------------

    def _apply_llm_concurrency(self) -> None:
        """Push ``ai.llm_max_concurrency`` into the process-wide gate.

        Called on initialize and on every settings reload. The gate
        lives in ``peldrun.memory.llm_contracts`` because it must cover
        entity extraction, query rewriting, gap analysis, AND evidence
        synthesis together: all four are ``/chat/completions`` calls to
        the same model, and a single-slot local runtime interleaves
        overlapping requests into unparseable output.

        Never raises: a malformed setting must not break startup.
        """
        try:
            from peldrun.memory.llm_contracts import (
                configure_llm_concurrency,
            )

            ai_settings = getattr(
                self._lifecycle.settings, "ai", None
            )
            limit = int(
                getattr(ai_settings, "llm_max_concurrency", 1) or 1
            )
            configure_llm_concurrency(limit)
            if limit > 1:
                logger.info(
                    "MemoryService: memory LLM concurrency set to %d "
                    "(safe only for a provider with real concurrency "
                    "headroom; a local runtime serves one at a time).",
                    limit,
                )
            else:
                logger.debug(
                    "MemoryService: memory LLM calls serialized "
                    "(llm_max_concurrency=1)."
                )
        except Exception as exc:  # noqa: BLE001 -- never fatal
            logger.warning(
                "MemoryService: could not apply llm_max_concurrency "
                "(defaulting to serialized calls): %s", exc,
            )

    async def _close_extraction(self) -> None:
        """Tear down the extraction subsystem. Idempotent.

        Shutdown ordering matters here:

          1. Stop the worker FIRST. ``worker.stop()`` waits for the
             current job to finish (up to its own timeout), ensuring
             no job attempts to write to the outbox or the provider
             after this method returns.

          2. Only THEN null out the state fields and close the
             outbox. If we closed the outbox first, a still-running
             worker would hit "outbox is not open" errors on every
             heartbeat / complete() call until its lease expired.
        """
        # --- Step 1: stop the worker ---
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_running:
            try:
                await worker.stop(timeout_seconds=30)
                logger.info(
                    "MemoryService: extraction worker stopped."
                )
            except Exception as exc:
                logger.warning(
                    "MemoryService: worker stop failed "
                    "(non-fatal): %s", exc,
                )

        # Clear orchestrator + batch service alongside worker --
        # all three depend on outbox + router, and leaving any
        # of them reachable after teardown would produce
        # "RuntimeError: outbox is not open" on the next call.
        self._orchestrator = None
        self._batch_service = None
        self._has_real_transport = False

        # --- Step 2: stop the rest of the pipeline ---
        # Capture and null-out first so a re-entrant call
        # (should never happen, but defense in depth) sees the
        # fields as already-cleared.
        watcher = self._status_watcher
        router = self._trigger_router
        scanner = self._scanner
        outbox = self._outbox

        self._status_watcher = None
        self._trigger_router = None
        self._scanner = None
        self._outbox = None

        # Watcher / router / scanner are stateless; nothing to close.
        # Only the outbox holds a live SQLite connection.
        if outbox is not None:
            try:
                await outbox.close()
                logger.info("MemoryService: extraction outbox closed.")
            except Exception as exc:
                logger.warning(
                    "MemoryService: extraction outbox close "
                    "failed (non-fatal): %s", exc,
                )

    async def _close_embedding(self) -> None:
        """Tear down the embedding pipeline. Idempotent.

        Shutdown ordering mirrors ``_close_extraction``:

          1. Stop the worker FIRST (waits for the current job).
          2. Close the outbox (the worker's transition methods --
             complete/fail/retry -- require the outbox to remain
             open until after the worker stops).
          3. Close the SQLite repositories. Their ``close_all()``
             is synchronous; failure here is logged and swallowed
             so that a broken repository cannot prevent the
             lifecycle provider from closing.

        Repositories are closed LAST because other pipelines may
        share the same DB file (WAL permits concurrent access), but
        nothing else owns references to these specific instances.
        """
        # ---- 0a. Cancel in-flight hybrid build task ------
        #
        # [FIX-BOOT-HANG] If a hybrid build is still running
        # when shutdown starts (the operator quit the server
        # before the background probe finished), cancel it.
        # Waiting is unnecessary -- the task's only side effect
        # is setting ``_hybrid_provider``, which we are about
        # to null anyway. Cancelling first (before nulling in
        # 0b) prevents a race where the in-flight task sets
        # ``_hybrid_provider`` back to a value AFTER we null it.
        task = self._hybrid_build_task
        self._hybrid_build_task = None
        if task is not None and not task.done():
            try:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass

        # ---- 0b. Hybrid + vec provider --------------------
        #
        # Null the hybrid wrapper BEFORE tearing down the rest
        # of the chain. The hybrid holds references to BOTH
        # ``self._lifecycle.provider`` (FTS) and
        # ``self._vec_provider``. If a concurrent caller reads
        # ``self.provider`` mid-teardown, they would otherwise
        # see a hybrid whose vec side is already closing. Nulling
        # the wrapper first makes the caller fall through to the
        # FTS provider, which remains open until lifecycle
        # shutdown runs (after this method).
        self._hybrid_provider = None
        self._vec_provider = None

        # ---- 1. Worker ------------------------------------
        worker = self._embedding_worker
        self._embedding_worker = None
        if worker is not None and worker.is_running:
            try:
                await worker.stop(timeout_seconds=30)
                logger.info("MemoryService: embedding worker stopped.")
            except Exception as exc:
                logger.warning(
                    "MemoryService: embedding worker stop "
                    "failed (non-fatal): %s", exc,
                )

        # ---- 2. Resolver + active model key ---------------
        self._embedding_resolver = None
        self._embedding_active_model_key = None

        # ---- 3. Outbox ------------------------------------
        outbox = self._embedding_outbox
        self._embedding_outbox = None
        if outbox is not None:
            try:
                await outbox.close()
                logger.info(
                    "MemoryService: embedding outbox closed."
                )
            except Exception as exc:
                logger.warning(
                    "MemoryService: embedding outbox close "
                    "failed (non-fatal): %s", exc,
                )

        # ---- 4. Repositories (sync close) -----------------
        vec_repo = self._vec_repository
        self._vec_repository = None
        if vec_repo is not None:
            try:
                vec_repo.close_all()
            except Exception as exc:
                logger.warning(
                    "MemoryService: vec repository close "
                    "failed (non-fatal): %s", exc,
                )

        mem_repo = self._memory_repository
        self._memory_repository = None
        if mem_repo is not None:
            try:
                mem_repo.close_all()
            except Exception as exc:
                logger.warning(
                    "MemoryService: memory repository close "
                    "failed (non-fatal): %s", exc,
                )

    # ------------------------------------------------------------------
    # Internal: hybrid retrieval build (Phase 5)
    # ------------------------------------------------------------------

    async def _build_hybrid_provider(
        self,
        *,
        resolver: "EmbeddingModelResolver",
        model_key: str,
        mem_repo: "SqliteMemoryRepository",
        vec_repo: "SqliteVecRepository",
        db_path: Path,
    ) -> bool:
        """Build the hybrid retrieval pipeline. Returns True on success.

        Steps (each may fail -- the whole method is best-effort):

          1. Resolve the model in the resolver (should never fail
             given ``_initialize_embedding`` already validated
             that the model_key is in the snapshot, but a
             race-free resolver can still raise).
          2. Determine the vector dimension:
             a. ``resolved.known_dimensions`` (from metadata),
             b. ``vec_repo.vec0_dimension`` (in-session cache --
                set if the worker already ran this process),
             c. read from disk via ``sqlite_master`` (persistent
                cache across boots),
             d. probe the model endpoint (slow, only on first
                boot with embedding enabled).
          3. Build the embedder, hydration adapter,
             ``SqliteVecProvider``, and ``HybridMemoryProvider``.
          4. Commit ``self._vec_provider`` and
             ``self._hybrid_provider``.

        On any failure: log a warning, null out both fields (so
        the ``provider`` property falls through to FTS), and
        return ``False``. The caller (``_initialize_embedding``)
        continues normally -- the worker still writes embeddings;
        only the read path stays on FTS until the operator fixes
        the underlying issue and restarts.

        See also
        --------
        * ``HybridMemoryProvider`` (``omweb.memory.manager.hybrid_provider``)
        * ``SqliteVecProvider``   (``peldrun.memory.retrieval.vector_search``)
        * ``SqliteVectorHydration`` (``omweb.memory.storage.vector_hydration``)
        """
        # ---- 1. Resolve the model in the resolver ------------
        try:
            resolved = resolver.resolve(model_key)
        except Exception as exc:
            logger.warning(
                "MemoryService._build_hybrid_provider: resolver "
                "cannot resolve model_key %r: %s",
                model_key,
                exc,
            )
            return False

        # ---- 2. Determine dimensions -------------------------
        dimensions = await self._resolve_embedding_dimensions(
            resolved=resolved,
            vec_repo=vec_repo,
            db_path=db_path,
        )
        if dimensions is None or dimensions <= 0:
            logger.info(
                "MemoryService: hybrid retrieval DISABLED -- "
                "vector dimensions for model %r could not be "
                "resolved (probe failed or model unreachable). "
                "Retrieval will remain FTS-only until the next "
                "restart succeeds in probing. The embedding "
                "worker still runs and writes vectors -- this "
                "only affects the READ path.",
                model_key,
            )
            return False

        # ---- 2b. Ensure vec0 schema + auto-reindex -----------
        #
        # Two things happen here:
        #
        #   1. ``ensure_vec0_table(dimensions)`` guarantees the
        #      table is at the correct dimension AND uses cosine
        #      metric. If either check fails -- dimension drift
        #      after a model switch, or a legacy L2 table from
        #      before the FIX-VEC0-COSINE patch -- the table is
        #      dropped and recreated. This is safe: the source
        #      of truth is ``memory_embeddings``, and vec0 is a
        #      pure index.
        #
        #   2. If the vec0 table is EMPTY after the ensure call
        #      (the state right after a fresh rebuild, OR on a
        #      brand-new deployment), reindex from
        #      ``memory_embeddings`` for the ACTIVE model. This
        #      is what turns an empty index back into a
        #      populated one WITHOUT requiring the operator to
        #      click Reindex manually. Typical cost: <1s for
        #      thousands of rows.
        #
        # Both steps are best-effort: a failure here logs a
        # warning but does NOT abort the hybrid build (search
        # would degrade to FTS-only, which is still better than
        # no search at all).
        try:
            await vec_repo.ensure_vec0_table(dimensions)
            existing_count = await vec_repo.count_vec0_rows()
            if existing_count == 0:
                logger.info(
                    "MemoryService: vec0 is empty after schema "
                    "check (fresh build or cosine migration); "
                    "reindexing from memory_embeddings for "
                    "model %r/%r at dim %d.",
                    resolved.model_id,
                    resolved.model_version,
                    dimensions,
                )
                indexed = await vec_repo.reindex_namespace(
                    model_id=resolved.model_id,
                    model_version=resolved.model_version,
                    dimension=dimensions,
                )
                logger.info(
                    "MemoryService: auto-reindex wrote %d "
                    "vector(s) into vec0.",
                    indexed,
                )
            else:
                logger.debug(
                    "MemoryService: vec0 already populated "
                    "(%d rows); skipping auto-reindex.",
                    existing_count,
                )
        except Exception as reindex_err:
            logger.warning(
                "MemoryService: vec0 schema check / auto-reindex "
                "failed (search will be FTS-only until manual "
                "reindex): %s",
                reindex_err,
                exc_info=True,
            )

        # ---- 3. Build the collaborator chain -----------------
        embedder = None
        try:
            from omweb.memory.storage.vector_hydration import (
                SqliteVectorHydration,
            )
            from peldrun.memory.retrieval.vector_search import (
                SqliteVecProvider,
            )
            from peldrun.memory.embeddings.provider import (
                OpenAICompatEmbeddingProvider,
            )
            from omweb.memory.manager.hybrid_provider import (
                HybridMemoryProvider,
            )

            # Hydration: converts vector-search ids into full
            # MemoryEntry instances, filtering by scope. Reuses
            # the SAME SqliteMemoryRepository instance the
            # embedding worker holds -- no second connection
            # stack, no duplicated schema setup.
            hydration = SqliteVectorHydration(mem_repo)

            # Long-lived embedder for the READ path. The worker
            # creates its own short-lived embedder per job (see
            # its invariant #1); this one lives for the lifetime
            # of the pipeline and is closed in
            # ``_close_embedding`` when ``_vec_provider`` is
            # nulled.
            embedder = OpenAICompatEmbeddingProvider(
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                model_id=resolved.model_id,
                model_version=resolved.model_version,
                dimensions=dimensions,
                extra_headers=resolved.extra_headers or None,
            )

            # Vector search provider: composes embedder +
            # backend + hydration. Implements the ``search``
            # surface HybridMemoryProvider consumes.
            vec_provider = SqliteVecProvider(
                embedding_provider=embedder,
                vector_backend=vec_repo,
                hydration_backend=hydration,
                model_id=resolved.model_id,
                model_version=resolved.model_version,
                dimensions=dimensions,
            )

            # Phase 5A: build the optional LLM reranker.
            #
            # Returns None ONLY when no LLM model is resolvable
            # at all (no extraction model AND no fallback marked
            # memory model). When a model IS configured, the
            # reranker is ALWAYS built -- the runtime gate for
            # ``retrieval.rerank_enabled`` happens INSIDE
            # ``CrossEncoderReranker.rerank()`` via
            # ``rerank_enabled_provider``. This is why
            # ``reload_settings`` toggling the flag takes effect
            # on the NEXT search WITHOUT a rebuild: the reranker
            # instance already exists; only the runtime gate
            # needs to flip. See ``_build_reranker_if_configured``
            # for the full rationale.
            reranker = self._build_reranker_if_configured()

            # Hybrid: wraps the FTS provider (from lifecycle)
            # and the vector provider. Delegates ALL non-search
            # methods to FTS via ``__getattr__``.
            hybrid = HybridMemoryProvider(
                fts_provider=self._lifecycle.provider,
                vec_provider=vec_provider,
                reranker=reranker,
                # Phase 6B: a LATE-BINDING proxy, not the searcher
                # itself -- see ``_LazyGraphSearch`` for why the
                # graph is not yet built at this point and why the
                # flag must be re-read on every search.
                graph_search=_LazyGraphSearch(self),
            )

            # ---- 4. Commit state -------------------------
            self._vec_provider = vec_provider
            self._hybrid_provider = hybrid

            if reranker is not None:
                rerank_status = (
                    "ON"
                    if reranker.is_active
                    else "NO-OP (model unresolved)"
                )
                rerank_suffix = " and rerank via LLM"
            else:
                rerank_status = "off (no model configured)"
                rerank_suffix = ""

            logger.info(
                "MemoryService: hybrid retrieval ENABLED "
                "(dimensions=%d, model=%r, rrf_k=%d, "
                "reranker=%s). Search will fuse FTS (BM25) with "
                "vector similarity%s.",
                dimensions,
                resolved.model_id,
                hybrid.rrf_k,
                rerank_status,
                rerank_suffix,
            )
            return True

        except Exception as exc:
            logger.warning(
                "MemoryService: hybrid provider construction "
                "failed -- retrieval will remain FTS-only: %s",
                exc,
                exc_info=True,
            )
            self._hybrid_provider = None
            self._vec_provider = None
            # Best-effort cleanup of the orphaned embedder
            # (its HTTP client should be released explicitly;
            # relying on GC is fragile when the process keeps
            # running for a long time).
            if embedder is not None:
                try:
                    await embedder.close()
                except Exception:
                    pass
            return False

    def _build_reranker_if_configured(self) -> Optional[Any]:
        """Build the Phase 5A reranker if an LLM model is available.

        Returns
        -------
        Optional[Any]
            A ``CrossEncoderReranker`` instance, or ``None``.

            Returns ``None`` when:
              * no ``settings.ai`` is available at all (the
                extraction subsystem is completely unconfigured);
              * no ``settings.ai.extraction`` model is configured
                AND no fallback marked-memory model exists;
              * ``CrossEncoderReranker`` construction raises
                (logged at WARNING; search remains hybrid-only).

            Returns a reranker INSTANCE whenever a model IS
            resolvable -- regardless of the CURRENT value of
            ``retrieval.rerank_enabled``. The runtime gate for
            that flag lives INSIDE ``rerank()`` via
            ``rerank_enabled_provider``, so flipping the flag
            at runtime takes effect on the NEXT search WITHOUT
            a rebuild. This method therefore does NOT check
            ``rerank_enabled`` itself -- the check is deferred
            to the runtime gate.

        Design rationale
        ----------------
        Building the reranker eagerly (when a model is
        configured) rather than lazily-on-first-search has
        three benefits:

          1. **Reload-safe without rebuild**: flipping
             ``rerank_enabled`` from False to True in
             ``reload_settings`` takes effect immediately.
             The instance already exists; only the runtime
             gate needs to flip. No hybrid rebuild, no
             restart.

          2. **Boot-time diagnostics**: the log line emitted
             here shows the operator exactly what is running,
             including whether reranking is currently enabled.

          3. **Fail-fast on misconfiguration**: a broken
             model config surfaces at construction time
             (logged) rather than at the first search (silent
             degradation).

        The cost is trivial: object construction is pure
        allocation (no I/O). The LLM gateway is lazy -- it
        connects only on the first ``generate_structured``
        call. Even when ``rerank_enabled=False``, building the
        reranker costs microseconds and consumes zero network
        resources.

        Never raises. All failure modes log at WARNING and
        return ``None``.
        """
        try:
            # ---- 1. Settings gate -------------------------
            settings = self._lifecycle.settings
            ai = getattr(settings, "ai", None)
            if ai is None:
                logger.debug(
                    "MemoryService: no AI settings; reranker "
                    "will not be built (search stays hybrid-only)."
                )
                return None

            extraction = getattr(ai, "extraction", None)
            if extraction is None:
                logger.debug(
                    "MemoryService: no extraction model config; "
                    "reranker will not be built."
                )
                return None

            provider_id = getattr(extraction, "provider_id", None)
            model_id = getattr(extraction, "model_id", None)

            # ---- 2. Resolve provider via fallbacks --------
            # Same inference path as ``_model_config_provider``
            # in ``_initialize_extraction`` -- reuse the
            # existing static helpers so no duplicate logic.
            if model_id and not provider_id:
                inferred = self._resolve_model_provider(
                    str(model_id)
                )
                if inferred is not None:
                    provider_id = inferred
                    logger.debug(
                        "MemoryService: inferred provider %r "
                        "for reranker model %r.",
                        provider_id, model_id,
                    )

            if not model_id:
                fallback = self._resolve_first_memory_model()
                if fallback is not None:
                    provider_id, model_id = fallback
                    logger.debug(
                        "MemoryService: using first marked memory "
                        "model %r::%r for reranker.",
                        provider_id, model_id,
                    )

            if not provider_id or not model_id:
                logger.info(
                    "MemoryService: no model resolved for "
                    "reranker (provider=%r, model=%r); search "
                    "remains hybrid-only. Configure a model in "
                    "Settings -> Memory -> AI to enable.",
                    provider_id, model_id,
                )
                return None

            # ---- 3. Build the gateway ---------------------
            # Same pattern as _initialize_extraction, but
            # simpler: no "has_real_transport" gate. The
            # reranker is a READ-side feature -- a broken
            # transport degrades to RRF ordering on the first
            # search; it never leaves stuck jobs or a corrupt
            # cursor.
            from omweb.memory.extraction.gateway import (
                MemoryModelGateway,
            )

            transport = None
            if self._transport_provider is not None:
                try:
                    transport = self._transport_provider()
                except Exception as exc:
                    logger.warning(
                        "MemoryService: transport_provider "
                        "raised during reranker construction; "
                        "falling through to default transport: "
                        "%s", exc,
                    )
                    transport = None

            extra_body = dict(
                getattr(extraction, "extraction_extra_body", {})
                or {}
            )

            gateway = MemoryModelGateway(
                transport=transport,
                force_openai_compat=True,
                extraction_extra_body=extra_body,
            )

            # ---- 4. Runtime enabled-flag reader -----------
            # Reads the LIVE settings on every rerank() call.
            # ``self._lifecycle`` is stable (its ``settings``
            # field is atomically swapped by reload_settings),
            # so this closure always sees the current flag --
            # no rebuild, no restart, no race.
            def _rerank_enabled_provider() -> bool:
                try:
                    cur = self._lifecycle.settings
                    cur_ret = getattr(cur, "retrieval", None)
                    if cur_ret is None:
                        return False
                    return bool(
                        getattr(cur_ret, "rerank_enabled", False)
                    )
                except Exception:
                    # Fail-closed: a broken settings reader
                    # must not accidentally trigger LLM
                    # calls on every search (cost risk).
                    return False

            # ---- 5. Construct the reranker -----------------
            from peldrun.memory.retrieval.reranker import (
                CrossEncoderReranker,
            )

            retrieval = getattr(settings, "retrieval", None)
            reranker = CrossEncoderReranker(
                gateway=gateway,
                provider_id=str(provider_id),
                model_id=str(model_id),
                rerank_max_candidates=int(
                    getattr(
                        retrieval, "rerank_max_candidates", 20,
                    )
                    if retrieval is not None else 20
                ),
                rerank_batch_size=int(
                    getattr(retrieval, "rerank_batch_size", 5)
                    if retrieval is not None else 5
                ),
                timeout_seconds=float(
                    getattr(
                        retrieval, "rerank_timeout_seconds", 3.0,
                    )
                    if retrieval is not None else 3.0
                ),
                rerank_enabled_provider=_rerank_enabled_provider,
                connection_id="memory_rerank",
            )

            # ---- 6. Report status --------------------------
            runtime_enabled = _rerank_enabled_provider()
            logger.info(
                "MemoryService: reranker constructed "
                "(provider=%r, model=%r, "
                "rerank_enabled=%s, batch_size=%d, "
                "max_candidates=%d, timeout=%.1fs).",
                provider_id, model_id,
                runtime_enabled,
                reranker.rerank_batch_size,
                reranker.rerank_max_candidates,
                reranker.timeout_seconds,
            )
            return reranker
        except Exception as exc:
            logger.warning(
                "MemoryService: reranker construction failed -- "
                "search will remain hybrid-only: %s",
                exc,
                exc_info=True,
            )
            return None

    async def _build_hybrid_provider_in_background(
        self,
        *,
        resolver: "EmbeddingModelResolver",
        model_key: str,
        mem_repo: "SqliteMemoryRepository",
        vec_repo: "SqliteVecRepository",
        db_path: Path,
    ) -> None:
        """Background wrapper around ``_build_hybrid_provider``.

        [FIX-BOOT-HANG] Why this exists as a separate method:

          * ``_build_hybrid_provider`` itself is unchanged --
            it still contains the full build logic and its own
            try/except. This method only wraps it in a
            background task with clear log prefixes.

          * ``asyncio.CancelledError`` is caught and re-raised
            so shutdown (which cancels the task) does not log
            as a failure -- the operator sees "cancelled
            (server shutting down)" not "raised unexpectedly".

          * All log lines start with ``[hybrid-build]`` so an
            operator grepping the boot log can see exactly
            when the background task started, succeeded, or
            failed, separately from the synchronous startup
            messages.

        Never raises. ``asyncio.create_task`` has no exception
        handler attached, so this method MUST absorb everything
        except CancelledError (which must propagate for
        correct task cleanup).
        """
        try:
            logger.info(
                "[hybrid-build] Starting background hybrid build "
                "(model_key=%r). Server startup is NOT blocked; "
                "retrieval uses FTS-only until this completes.",
                model_key,
            )
            success = await self._build_hybrid_provider(
                resolver=resolver,
                model_key=model_key,
                mem_repo=mem_repo,
                vec_repo=vec_repo,
                db_path=db_path,
            )
            if success:
                logger.info(
                    "[hybrid-build] Hybrid provider READY. All "
                    "subsequent searches use FTS + Vector fusion."
                )
            else:
                logger.warning(
                    "[hybrid-build] Hybrid build returned False "
                    "(probe failed or pipeline unavailable). "
                    "Retrieval remains FTS-only until the next "
                    "successful boot."
                )
        except asyncio.CancelledError:
            logger.info(
                "[hybrid-build] Background build cancelled "
                "(server shutting down)."
            )
            raise
        except Exception as exc:
            logger.error(
                "[hybrid-build] Background build raised "
                "unexpectedly: %s. Retrieval remains FTS-only.",
                exc,
                exc_info=True,
            )

    async def _resolve_embedding_dimensions(
        self,
        *,
        resolved,
        vec_repo: "SqliteVecRepository",
        db_path: Path,
    ) -> Optional[int]:
        """Return the vector dimension for the active model, or ``None``.

        Resolution order (fastest / cheapest first):

          1. ``resolved.known_dimensions`` -- persisted in
             ``models_metadata.json`` by a previous successful
             probe (future optimisation -- currently ``None``
             because the worker does not persist, but the
             plumbing is there).
          2. ``vec_repo.vec0_dimension`` -- in-session cache. Set
             if the worker already ran in THIS process (e.g.
             ``reload_settings`` triggered a rebuild after the
             worker processed jobs).
          3. Read from disk via ``sqlite_master`` -- survives
             process restarts. Skips the probe entirely on
             subsequent boots after the table has been created.
          4. Probe the model endpoint (HTTP call). SLOW path,
             used only on truly first boot with embedding enabled
             AND no prior vec0 table.

        Every step is wrapped in its own try/except -- a failure
        in one step falls through to the next, never aborts the
        chain with an exception.
        """
        if resolved.known_dimensions is not None:
            return resolved.known_dimensions

        try:
            if vec_repo.vec0_dimension > 0:
                return vec_repo.vec0_dimension
        except Exception:
            pass

        try:
            existing = await asyncio.to_thread(
                self._probe_existing_dimension, db_path,
            )
            if existing > 0:
                return existing
        except Exception:
            pass

        if resolved.base_url is None:
            logger.debug(
                "MemoryService._resolve_embedding_dimensions: no "
                "base_url for %r; cannot probe.",
                resolved.model_id,
            )
            return None

        from peldrun.memory.embeddings.provider import (
            OpenAICompatEmbeddingProvider,
        )

        # [FIX-PROBE-TIMEOUT] Hard 3-second cap on the probe.
        #
        # Without this cap, an unreachable provider (LM Studio
        # not yet started, network down, wrong base_url) can
        # block the probe for the provider's default socket
        # timeout -- commonly 30s or more. Even though the
        # hybrid build now runs in a background task (see
        # ``_build_hybrid_provider_in_background``), a long
        # hang there keeps ``service.provider`` on FTS-only
        # for the whole duration, which is confusing when the
        # provider is genuinely just slow to warm up.
        #
        # 3 seconds is the right cap:
        #   * sufficient for a healthy embedding endpoint on
        #     a local network to respond (typical: 50-300ms),
        #   * short enough that an unreachable endpoint does
        #     NOT appear as a freeze -- the operator sees
        #     "[hybrid-build] Hybrid build returned False"
        #     within seconds,
        #   * does not prevent recovery -- once LM Studio
        #     starts, the operator restarts PELDRUN and the
        #     probe succeeds normally.
        #
        # ``asyncio.wait_for`` raises ``asyncio.TimeoutError``
        # on expiry; caught by the explicit branch below so the
        # log message is precise ("timed out") rather than the
        # generic "failed: " message.
        _PROBE_TIMEOUT_SECONDS = 3.0
        try:
            probed = await asyncio.wait_for(
                OpenAICompatEmbeddingProvider.probe_dimensions(
                    base_url=resolved.base_url,
                    api_key=resolved.api_key,
                    model_id=resolved.model_id,
                ),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            if isinstance(probed, int) and probed > 0:
                logger.info(
                    "MemoryService: probed %r dimensions = %d "
                    "(first boot with embedding enabled).",
                    resolved.model_id,
                    probed,
                )
                return probed
            logger.warning(
                "MemoryService: probe_dimensions for %r returned "
                "an invalid value: %r",
                resolved.model_id,
                probed,
            )
            return None
        except asyncio.TimeoutError:
            logger.warning(
                "MemoryService: probe_dimensions for %r timed out "
                "after %.1fs -- model provider is unreachable. "
                "Hybrid retrieval remains FTS-only. Once the "
                "provider is available, restart the server to "
                "enable hybrid.",
                resolved.model_id,
                _PROBE_TIMEOUT_SECONDS,
            )
            return None
        except Exception as exc:
            logger.warning(
                "MemoryService: probe_dimensions for %r failed: %s",
                resolved.model_id,
                exc,
            )
            return None

    @staticmethod
    def _probe_existing_dimension(db_path: Path) -> int:
        """Read the vec0 dimension from disk, if the table exists.

        Opens a short-lived connection, reads ``sqlite_master.sql``
        for ``memory_vectors``, parses ``FLOAT[N]``. Does NOT load
        the sqlite-vec extension (not needed -- ``sqlite_master``
        is plain SQLite).

        Returns 0 when the file, the table, or the parse fails.
        Callers (``_resolve_embedding_dimensions``) treat 0 as
        "no cached dimension" and move to the probe step.
        """
        import sqlite3

        from omweb.memory.storage.vector_repository import (
            SqliteVecRepository,
        )

        if not db_path.exists():
            return 0
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                return SqliteVecRepository._read_existing_vec0_dimension(
                    conn
                )
            finally:
                conn.close()
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Retrieval failure observability (audit R5)
    # ------------------------------------------------------------------

    def record_retrieval_failure(self, exc: Exception) -> None:
        """Record a retrieval failure for diagnostics (audit R5).

        Called by the dispatcher's retrieval hook when ``retrieve``
        raises. Never raises itself: observability must not create a
        new failure path. The counter and last error are exposed via
        ``retrieval_failure_stats`` for the diagnostics endpoint.
        """
        try:
            self._retrieval_failure_count += 1
            self._last_retrieval_error = f"{type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001 -- observability is best-effort
            pass

    @property
    def retrieval_failure_stats(self) -> dict:
        """Retrieval failure counter and last error (for diagnostics)."""
        return {
            "retrieval_failure_count": getattr(
                self, "_retrieval_failure_count", 0
            ),
            "last_retrieval_error": getattr(
                self, "_last_retrieval_error", None
            ),
        }

    # ------------------------------------------------------------------
    # Internal: usage recording
    # ------------------------------------------------------------------

    async def _record_usage(
        self,
        *,
        run_id: Optional[str],
        context: MemoryContext,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        actual_tokens: Optional[int] = None,
    ) -> None:
        """Record one retrieval's usage to the persistent ledger.

        Logs the usage at INFO level (operators rely on it for
        real-time visibility) AND writes a row to
        ``memory_usage_ledger`` via ``MemoryUsageTracker``. The
        persistent write is best-effort: any failure (DB locked,
        disk full, tracker import error) is logged at WARNING and
        swallowed -- memory is an ENHANCEMENT, not a critical path,
        and a monitoring write failure must never abort the run.

        Parameters
        ----------
        run_id
            The run this retrieval belongs to. May be ``None`` in
            early-bootstrap / test scenarios; when ``None`` the
            persistent write is SKIPPED (the ledger's
            ``UNIQUE(run_id)`` constraint requires a non-empty id).
        context
            The ``MemoryContext`` produced by the pipeline.
        chat_id, project_id
            Copied from ``retrieve``'s arguments so the ledger can
            filter by chat or project without a join.
        actual_tokens
            Reserved for the future token-accounting bridge
            (``MemoryAccountingBridge``). Currently unused -- the
            tracker computes ``context_tokens`` from the context.
        """
        # Always emit the log line first -- operators rely on it
        # even when the persistent write below is skipped or fails.
        try:
            mode_value = self._lifecycle.settings.mode.value
        except RuntimeError:
            mode_value = "<closed>"

        logger.info(
            "Memory retrieval: run_id=%s, entries=%d, used_tokens=%d, "
            "dropped=%d, mode=%s, router=%s",
            run_id or "<unknown>",
            len(context.memories),
            context.used_tokens,
            context.dropped_for_budget,
            mode_value,
            context.router_decision,
        )

        # Persistent write. Requires a non-empty run_id (the
        # ledger's UNIQUE constraint is on run_id, and an empty
        # string would collide across runs).
        if not run_id or not run_id.strip():
            return

        try:
            from omweb.memory.monitoring import (
                get_memory_accounting_bridge,
            )

            # Use the bridge (NOT the tracker) so that the
            # memory token count is BOTH written to the
            # memory_usage_ledger AND registered as a
            # one-shot pending value for the next
            # LLMInvocationRecord (see
            # ``MemoryAccountingBridge.record_retrieval``).
            #
            # Using ``tracker.record()`` directly would write
            # the ledger row but leave
            # ``LLMInvocationRecord.memory_tokens_used`` at
            # ``None`` for every run -- which defeats the
            # per-invocation memory attribution that Phase 2
            # task 2.3 requires.
            bridge = get_memory_accounting_bridge()
            await bridge.record_retrieval(
                run_id=run_id.strip(),
                context=context,
                chat_id=chat_id,
                project_id=project_id,
            )
        except Exception as exc:
            # Best-effort. Log at WARNING (not ERROR) so that
            # missing/disabled tracking does not pollute the
            # error stream of an otherwise healthy run.
            logger.warning(
                "Memory usage ledger write skipped for run %r: %s",
                run_id,
                exc,
            )


__all__ = ["MemoryService"]
