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

import logging
from pathlib import Path
from typing import Callable, List, Optional

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

        logger.info(
            "MemoryService initialized (mode=%s, provider=%s, "
            "extraction=%s, embedding=%s).",
            self._lifecycle.settings.mode.value,
            self._lifecycle.settings.provider_id,
            "enabled" if self._outbox is not None else "disabled",
            "enabled" if self._embedding_worker is not None else "disabled",
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
        """
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
            context = await build_memory_context(
                query=query,
                scope_refs=scope_refs,
                route=route,
                provider=self._lifecycle.provider,
                settings=settings,
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
            return MemoryWritePipeline(
                port=self._lifecycle.provider,
                settings=self._lifecycle.settings,
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

        # ---- 8. Commit state ------------------------------
        self._embedding_outbox = embedding_outbox
        self._embedding_resolver = resolver
        self._embedding_worker = worker
        self._vec_repository = vec_repo
        self._memory_repository = mem_repo
        self._embedding_active_model_key = model_key.strip()

        logger.info(
            "MemoryService: embedding pipeline initialized "
            "(model_key=%r, models_in_snapshot=%d, outbox_db=%s, "
            "worker_id=%s). Worker will start when "
            "start_embedding_worker() is called.",
            self._embedding_active_model_key,
            len(resolver),
            db_path,
            worker.worker_id,
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
