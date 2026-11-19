# backend/peldrun/memory/domain/settings.py
"""
PELDRUN Memory Platform - Configuration & Token Budget Contracts.

Defines:

    * ``MemoryTokenBudget`` -- per-turn token allocation limits
      (max, user-scope reservation, warning threshold).
    * ``MemorySettings``    -- the complete configuration snapshot
      for the memory subsystem: mode, provider, retrieval tuning,
      budget, extraction policy, and per-project overrides.

Relationship to ``RunRequest``
------------------------------

**These settings are NOT part of any run.** They are loaded ONCE
during FastAPI ``lifespan`` initialization (P1.7) and held by the
``MemoryService`` singleton. A given run therefore observes the
settings that were current when the run started; a settings change
mid-run affects only the NEXT run. This is the design the plan's
section 0 mandates ("MemoryService... does NOT hold conversation
state"), and it is what acceptance test #9 verifies.

Because settings are decoupled from individual runs, they must be
a **pure value object**: no I/O, no repository references, no
callbacks. The whole model can be serialized to JSON, compared for
equality, copied, and cached without side effects. Persistence
lives entirely in ``MemorySettingsManager`` (P1.7).

``project_overrides`` semantics
-------------------------------

``project_overrides: Dict[str, MemorySettings]`` supports per-project
customization. The nested instances are DELTA-like: a field is
considered "overridden" if and only if the caller explicitly
supplied it. Fields the override did not set inherit from the parent.

This is exposed via ``effective_for_project(project_id)``, which:

  1. returns ``self`` when the project has no override,
  2. otherwise starts from ``self``'s values and overlays the
     override's EXPLICITLY-SET fields on top (using Pydantic's
     ``model_fields_set`` / ``model_dump(exclude_unset=True)``),
  3. returns a NEW ``MemorySettings`` instance with empty
     ``project_overrides`` (preventing any possibility of recursion).

An override MUST NOT itself contain ``project_overrides``. The
``_reject_nested_overrides`` model validator enforces this at
construction time -- nested overrides would invite infinite
recursion and, worse, make "which settings actually apply?"
undecidable without walking an unbounded tree.

Mode escalation across override levels
--------------------------------------

A project override's ``mode`` is applied as-is. This schema does
NOT enforce "override can only restrict, never escalate" -- that
is a policy the ``MemoryService`` can layer on top in P1.7 if
needed, based on real user feedback. The schema trusts the caller
that produced the settings: the settings are always written by an
authorized user (via the UI or the settings file), so the schema
does not need to second-guess them.

The rationale for keeping the schema permissive:

  * "Global RW, project X disabled" is the common case (sensitive
    project). It works.
  * "Global disabled, project X RW" is a maintenance-window case
    (memory off for most of the deployment, on for one project).
    It also works. Forbidding it would require awkward two-step
    edits.
  * Escalation surprises ("I disabled globally, why is project X
    still remembering?") are visible in the UI: the project's
    settings page shows its actual mode and highlights the
    divergence from the global default.

Design invariants
-----------------

1. **Pure domain, no I/O.**
   This module imports only stdlib + Pydantic + the sibling
   ``enums`` module. It never imports the repository, the
   settings manager, or any framework module.

2. **Complete defaults, round-trip safe.**
   ``model_dump(mode="json")`` produces a dict that
   ``MemorySettings.model_validate(...)`` accepts unchanged.
   ``MemorySettingsManager`` (P1.7) persists this dict directly to
   ``memory_settings.json``; loading is a single
   ``model_validate`` call. No custom (de)serialization logic.

3. **All limits enforced at construction.**
   Every numeric field has explicit ``ge`` / ``le`` bounds chosen
   to reject obvious misconfigurations (negative budgets, zero
   limits, warn thresholds outside ``[0, 1]``) while leaving room
   for legitimate extremes. A misconfigured settings file fails
   fast at startup, not silently at run time.

4. **Defaults are stable.**
   Every default below matches the plan's specification. Changing
   a default is a behavioral change that must be paired with an
   update to ``peldrun-memory-package`` format-version
   compatibility (P1.6), because imported packages were produced
   under the previous defaults.

5. **No mutable module-level singletons.**
   ``default_settings()`` returns a FRESH instance every call so
   that a caller who mutates the returned object cannot corrupt a
   shared state. The plan's "MemoryService singleton" pattern is
   preserved -- it is the SERVICE that is a singleton, not its
   configuration object.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from peldrun.memory.domain.enums import MemoryMode


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_PROJECT_OVERRIDES: int = 1_000
"""Hard ceiling on the number of project overrides in a single settings
instance.

A deployment with more than 1000 projects is either using the
override mechanism wrongly (overrides should be exceptional, not
per-project boilerplate) or has grown beyond what the settings file
is designed for. Capping here surfaces the situation early with a
clear error instead of allowing the JSON file to bloat to an
unmanageable size.
"""

_DEFAULT_PROVIDER_ID: str = "sqlite_fts"
"""The P1.3 provider that ships as the default backend."""


# ---------------------------------------------------------------------------
# MemoryTokenBudget
# ---------------------------------------------------------------------------


class MemoryTokenBudget(BaseModel):
    """Per-turn token allocation limits for the memory block.

    This budget is applied by ``MemoryContextBuilder`` (P1.4) when it
    assembles a ``MemoryContext`` from the ranked candidates. The
    total token count that reaches the LLM in the ``[RETRIEVED
    MEMORY]`` block is guaranteed to be ``<= max_tokens``, and the
    portion attributable to USER-scope entries is guaranteed to be
    ``<= reserved_for_user_scope`` (see the reservation rationale
    below).

    Reservation rationale
    ---------------------
    Without a reservation, a burst of high-scoring project-scope
    matches could starve user-scope entries out of the budget,
    silently hiding the user's stated preferences (e.g. "always use
    dark mode"). The ``reserved_for_user_scope`` field guarantees a
    slice of every turn's memory budget is available for user-scope
    entries, even if no user-scope match is above the relevance
    threshold in a given turn. The behavior of the reservation when
    unused is documented on ``MemoryContextBuilder`` in P1.4, not
    here.

    Warning threshold
    -----------------
    ``warn_threshold`` is the fraction of ``max_tokens`` at which
    the monitoring layer (P1.9) emits a "budget is getting tight"
    signal. It is a pure monitoring hint -- exceeding it does not
    drop entries or fail the run. It exists so that operators can
    see "the memory budget is 80% full on average" and raise
    ``max_tokens`` before it becomes a functional problem.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_tokens: int = Field(
        default=1024,
        ge=64,
        le=32_768,
        description=(
            "Absolute ceiling on tokens consumed by the memory block "
            "in a single turn. Must be at least 64 (a smaller cap "
            "cannot fit even one short entry plus the header) and "
            "at most 32768 (beyond that, memory is competing with "
            "the actual conversation for the context window)."
        ),
    )
    reserved_for_user_scope: int = Field(
        default=300,
        ge=0,
        description=(
            "Minimum tokens guaranteed to USER-scope entries within "
            "every turn's memory block. Zero disables the guarantee "
            "(user-scope entries compete with everyone else). Must "
            "not exceed ``max_tokens`` (enforced by the model "
            "validator)."
        ),
    )
    warn_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of ``max_tokens`` at which the monitoring "
            "layer flags the budget as 'tight'. 0.0 = warn always, "
            "1.0 = warn only at exactly 100% usage. Must be in "
            "[0.0, 1.0]."
        ),
    )

    @model_validator(mode="after")
    def _check_reservation_fits(self) -> "MemoryTokenBudget":
        """Enforce ``reserved_for_user_scope <= max_tokens``.

        A reservation larger than the total budget is meaningless
        (you cannot reserve 500 of a 300-token budget) and, more
        importantly, breaks the ``MemoryContext._check_budget``
        invariant downstream: the builder would be forced to
        consume more than ``max_tokens`` to honor the reservation.
        Rejecting the configuration at construction is the only
        safe response.
        """
        if self.reserved_for_user_scope > self.max_tokens:
            raise ValueError(
                f"reserved_for_user_scope ({self.reserved_for_user_scope}) "
                f"must not exceed max_tokens ({self.max_tokens}); a "
                f"reservation larger than the total budget cannot be "
                f"honored"
            )
        return self


# ---------------------------------------------------------------------------
# MemoryAIExtractionSettings
# ---------------------------------------------------------------------------


class MemoryAIExtractionSettings(BaseModel):
    """LLM configuration for one extraction or consolidation task.

    Consumed by ``MemoryModelGateway.generate_structured``
    (``omweb/memory/extraction/gateway.py``) to resolve credentials
    from ``models_vault.json`` via ``provider_id``, dispatch to the
    correct model via ``model_id``, bound cost via
    ``max_output_tokens``, and bound wall-clock latency via
    ``timeout_seconds``.

    All fields are operator-tunable. No field is a secret -- the
    actual API key lives in the model vault, keyed by
    ``provider_id``, and is resolved lazily by the gateway at call
    time. Storing a key here would leak it into
    ``memory_settings.json``, which is not the vault.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    provider_id: Optional[str] = Field(
        default=None,
        description=(
            "Provider id (e.g. 'openai', 'anthropic', 'lmstudio', "
            "'ollama'). MUST match a provider entry in "
            "models_vault.json. None means 'not yet configured' "
            "-- the frontend populates this from a dropdown of "
            "models the user marked as 'memory' in "
            "Settings -> Models. At runtime, MemoryService falls "
            "back to the first marked memory model "
            "(get_memory_models()[0]) when this is None."
        ),
    )
    model_id: Optional[str] = Field(
        default=None,
        description=(
            "Model id understood by the provider (e.g. "
            "'gpt-4o-mini', 'claude-3-haiku-20240307'). None "
            "means 'not yet configured' -- same fallback "
            "semantics as provider_id. Both fields must be set "
            "together; setting only one is caught at runtime "
            "(no jobs are enqueued until both are non-None)."
        ),
    )
    max_output_tokens: int = Field(
        default=1200,
        ge=128,
        le=32_000,
        description=(
            "Hard cap on completion tokens per extraction call. "
            "1200 fits ~20 candidate memories with room for "
            "JSON scaffolding; raising it increases cost but may "
            "improve coverage on dense sessions."
        ),
    )
    timeout_seconds: int = Field(
        default=45,
        ge=5,
        le=180,
        description=(
            "Wall-clock timeout for one extraction LLM call, "
            "enforced by the gateway's default transport via "
            "``asyncio.wait_for``. 45s matches the plan's "
            "default; the range allows 5s (very fast local "
            "models) up to 180s (slow reasoning models)."
        ),
    )

    # ------------------------- reasoning control ------------------------
    #
    # Provider-neutral reasoning-disablement payload forwarded verbatim
    # as ``extra_body`` to the OpenAI-compatible transport used by the
    # extraction gateway. Reasoning-capable models (Nemotron, Qwen-QwQ,
    # DeepSeek-R1, OpenAI o-series, ...) emit a hidden "thinking" phase
    # BEFORE any visible token -- and each provider spells the disable
    # switch differently. This field is the OPERATOR-VISIBLE place to
    # declare which spelling their provider expects, so no provider
    # name is ever hard-coded inside the gateway.
    #
    # Defaults to the vLLM/LM Studio convention (the most common way to
    # host reasoning models locally, and honored by vLLM, SGLang, TGI,
    # LM Studio, and most OpenAI-compatible proxies). Operators using a
    # different provider change this via ``PATCH /api/memory/settings``:
    #
    #   OpenAI o-series:    {"reasoning": {"effort": "none"}}
    #   DeepSeek:           {"thinking": {"type": "disabled"}}
    #   SiliconFlow/AliYun: {"enable_thinking": false}
    #   OpenRouter:         {"reasoning": {"effort": "none"}}
    #
    # An empty dict disables the extra_body entirely (useful for
    # non-reasoning models, so the SDK sends nothing the provider
    # might reject).
    extraction_extra_body: Dict[str, Any] = Field(
        default_factory=lambda: {
            "chat_template_kwargs": {"enable_thinking": False},
        },
        description=(
            "Provider-specific extra_body forwarded verbatim to the "
            "extraction LLM call. Used to DISABLE the reasoning / "
            "thinking phase on reasoning-capable models (e.g. "
            "Nemotron, Qwen-QwQ, DeepSeek-R1, OpenAI o-series). "
            "Defaults to the vLLM/LM Studio convention "
            "(``chat_template_kwargs.enable_thinking=false``). "
            "Change the default via the settings API to match your "
            "provider's spelling. Empty dict disables the extra_body."
        ),
    )

    @field_validator("provider_id", "model_id", mode="after")
    @classmethod
    def _validate_non_empty(cls, v: Optional[str]) -> Optional[str]:
        """Allow None ('not configured'); reject empty strings.

        Empty string is rejected because it is ambiguous with
        None -- a caller who means "not configured" should send
        None, not "". Allowing "" would create two
        indistinguishable representations of the same state.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(
                f"must be a string or None, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "must not be empty (use None to leave unset)"
            )
        return stripped


# ---------------------------------------------------------------------------
# MemoryAISettings
# ---------------------------------------------------------------------------


class MemoryAISettings(BaseModel):
    """AI extraction subsystem configuration (Phase 1).

    Controls whether and how the LLM-powered extraction pipeline
    runs. Distinct from the legacy ``auto_extraction_enabled`` /
    ``auto_extraction_min_turns`` fields on ``MemorySettings``:
    those were Phase-0 placeholders for a heuristic extractor.
    This class gates the LLM extractor.

    Enabling the pipeline
    ---------------------
    All three conditions must hold for ``MemoryService`` to bring
    up the extraction subsystem:

      1. ``enabled`` is True,
      2. ``extraction_enabled`` is True,
      3. the current memory mode permits writes
         (``mode.can_write`` -- extraction writes memories and
         would fail on every persistence attempt in READ_ONLY or
         DISABLED mode).

    When any condition fails, the extraction pipeline is absent:
    no outbox, no worker, no jobs. The rest of the memory
    subsystem (retrieval, save, search) operates exactly as
    Phase 0 -- no behavioral regression.

    Two opt-in flags (not one)
    --------------------------
    ``enabled`` and ``extraction_enabled`` are intentionally
    separate so an operator can pre-configure the model
    (provider, model_id, limits) BEFORE turning extraction on.
    With a single flag, saving ``provider_id='openai'`` and
    ``enabled=True`` in the same request would risk the extractor
    firing before the operator finished configuring sibling
    fields. The two-step opt-in gives a clean "configure, then
    arm" workflow without an artificial delay.

    Trigger patterns
    ----------------
    See ``extraction_trigger`` for the three supported modes.
    ``after_turn`` fires on every completed turn (with optional
    batching); ``after_session`` fires only when the session's
    status becomes 'completed'; ``both`` fires on either. The
    actual routing decisions are made by ``TriggerRouter``
    (``omweb/memory/extraction/trigger_router.py``).

    Field ordering
    --------------
    Fields are grouped by concern for readability:

      * toggles (enabled, extraction_enabled, consolidation_enabled)
      * trigger policy (extraction_trigger, after_turn_batching)
      * per-task LLM config (extraction, consolidation)
      * cost and size bounds (max_*, min_confidence)
      * batch scheduler (batch_*)
      * worker heartbeat (snapshot_heartbeat_interval_seconds)
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------- toggles --------------------------------------

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the AI extraction subsystem. "
            "See the class docstring for the three-condition "
            "gating contract."
        ),
    )
    extraction_enabled: bool = Field(
        default=False,
        description=(
            "Second opt-in flag. Both ``enabled`` and "
            "``extraction_enabled`` must be True for the "
            "pipeline to initialize -- see the class docstring."
        ),
    )
    consolidation_enabled: bool = Field(
        default=False,
        description=(
            "Enable the consolidation subsystem (Phase 5). "
            "Consolidation merges similar memories into one "
            "target and archives the sources. Runs in the same "
            "worker as extraction; when both are enabled the "
            "worker alternates between the two job types. When "
            "disabled, consolidation jobs are rejected at the "
            "orchestrator boundary."
        ),
    )

    # ------------------------- trigger policy -------------------------------

    extraction_trigger: Literal[
        "after_turn", "after_session", "both"
    ] = Field(
        default="after_session",
        description=(
            "Which trigger pattern fires the extractor. "
            "'after_turn' = every completed turn (batched by "
            "default, see ``after_turn_batching``); "
            "'after_session' = only when session.json.status "
            "becomes 'completed'; 'both' = both."
        ),
    )
    after_turn_batching: bool = Field(
        default=True,
        description=(
            "When True, ``extraction_trigger='after_turn'`` "
            "batches all pending turns into ONE job per "
            "notification instead of one job per turn. "
            "Batching is cheaper (1 LLM call instead of N) but "
            "delays per-turn visibility. Only consulted when "
            "the trigger is 'after_turn' or 'both'."
        ),
    )

    # ------------------------- per-task LLM config --------------------------

    extraction: MemoryAIExtractionSettings = Field(
        default_factory=MemoryAIExtractionSettings,
        description=(
            "Model selection and per-call limits for the "
            "extraction task. See ``MemoryAIExtractionSettings``."
        ),
    )
    consolidation: MemoryAIExtractionSettings = Field(
        default_factory=MemoryAIExtractionSettings,
        description=(
            "Model selection and per-call limits for the "
            "consolidation task (Phase 5). Independent from "
            "``extraction`` so an operator can use a stronger "
            "(or cheaper) model for merging without changing "
            "the extraction model."
        ),
    )
    agentic: MemoryAIExtractionSettings = Field(
        default_factory=MemoryAIExtractionSettings,
        description=(
            "Model selection and per-call limits for the Phase 6 "
            "agentic components: the entity extractor (graph "
            "indexing), the query rewriter, and the evidence "
            "synthesizer. Kept SEPARATE from ``extraction`` on "
            "purpose: agentic retrieval makes several LLM calls "
            "per user question, so an operator will usually want a "
            "cheaper or faster model here than the one used for "
            "deep conversation extraction."
        ),
    )

    # ------------------------- Phase 6: graph indexing ----------------------

    graph_indexing_enabled: bool = Field(
        default=False,
        description=(
            "Enable write-time knowledge-graph indexing (Phase 6). "
            "When True, every successfully written memory schedules "
            "a BACKGROUND entity-extraction task; the write itself "
            "never waits for it. Requires ``enabled=True`` plus a "
            "resolvable model in ``agentic``. When False (the "
            "default), no graph work happens at all and the write "
            "path is identical to Phase 5."
        ),
    )
    graph_backfill_interval_seconds: int = Field(
        default=900,
        ge=60,
        le=86_400,
        description=(
            "How often the background backfill scans for active "
            "memories that have no graph links yet and indexes "
            "them. This is what covers writes that bypassed "
            "``MemoryWritePipeline`` (the ``memory_save`` tool "
            "writes straight through the provider) and any "
            "indexing task that failed. 900s (15 min) keeps the "
            "graph converging without competing with real writes."
        ),
    )
    graph_backfill_batch_size: int = Field(
        default=20,
        ge=1,
        le=200,
        description=(
            "Maximum memories the backfill indexes per tick. "
            "Bounded so a large un-indexed backlog is worked "
            "through gradually instead of burning the whole "
            "day's LLM budget in one burst."
        ),
    )
    llm_max_concurrency: int = Field(
        default=1,
        ge=1,
        le=4,
        description=(
            "Maximum SIMULTANEOUS memory LLM calls, process-wide. "
            "Covers every Phase 6 chat-completion call: entity "
            "extraction (graph indexing), query rewriting, gap "
            "analysis, and evidence synthesis. "
            "Why this matters: the write pipeline dispatches one "
            "background indexing task per written memory, and the "
            "composer can start a new agentic run while a previous one "
            "is still generating, so the default would otherwise be "
            "several overlapping /chat/completions requests. "
            "Most LOCAL runtimes (LM Studio, Ollama, llama.cpp) serve "
            "one generation at a time: they do NOT queue overlapping "
            "requests, they interleave the token streams and both "
            "responses become unparseable JSON. "
            "Keep this at 1 (default) for a local model. Raise it to "
            "2-4 ONLY for a hosted API with real concurrency headroom."
        ),
    )

    # ------------------------- size and confidence bounds -------------------

    max_messages_per_job: int = Field(
        default=40,
        ge=1,
        le=200,
        description=(
            "Maximum messages sent to the LLM per extraction "
            "job. A longer conversation is truncated to the "
            "LAST N messages (most recent context wins). "
            "Bounds the per-job cost of the LLM call."
        ),
    )
    max_input_tokens: int = Field(
        default=12_000,
        ge=512,
        le=128_000,
        description=(
            "Maximum input tokens per extraction job. A second "
            "cost bound applied AFTER ``max_messages_per_job``. "
            "If the truncated message set still exceeds this, "
            "the oldest messages are dropped."
        ),
    )
    max_candidates_per_job: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Ceiling on candidate memories the LLM may propose "
            "per job. Applied post-parse by "
            "``ExtractionOrchestrator`` as a cost bound on "
            "acceptance."
        ),
    )
    min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum LLM-reported confidence for a candidate "
            "to be accepted WITHOUT human review. Candidates "
            "below the threshold are queued for Pending Review "
            "(Phase 5), not discarded -- the orchestrator "
            "makes the accept/reject decision on the review "
            "queue. 0.0 = accept everything (not recommended); "
            "0.7 = the plan's default; 1.0 = accept only "
            "maximum confidence."
        ),
    )

    # ------------------------- cost ceiling ---------------------------------

    max_cost_nano_usd_per_day: int = Field(
        default=50_000_000,
        ge=0,
        le=10_000_000_000,
        description=(
            "Daily cost ceiling for extraction, in nano-USD. "
            "When the sum of ``memory_jobs.cost_nano_usd`` for "
            "the current UTC day exceeds this value, new jobs "
            "are paused until the next UTC midnight. "
            "50,000,000 nano = $0.05/day (the plan's default). "
            "Set to 0 to disable extraction entirely "
            "(equivalent to ``enabled=False`` but leaves the "
            "configuration visible for diagnostics)."
        ),
    )

    # ------------------------- batch scheduler ------------------------------

    batch_enabled: bool = Field(
        default=True,
        description=(
            "Enable the batch retroactive extraction subsystem "
            "(Phase 3). Manual batch runs use the "
            "``/api/memory/extract/batch`` endpoint; automatic "
            "runs are driven by ``batch_auto_schedule_hours``. "
            "When False, both the UI button and the scheduler "
            "refuse to start a new batch."
        ),
    )
    batch_max_concurrent_jobs: int = Field(
        default=1,
        ge=1,
        le=4,
        description=(
            "Maximum concurrent batch jobs. The current "
            "default (1) serializes batch work to avoid SQLite "
            "lock contention on the shared ``memory_jobs`` "
            "table. Raising this is only safe with a "
            "non-SQLite provider or a properly-configured WAL "
            "mode."
        ),
    )
    batch_auto_schedule_hours: Optional[int] = Field(
        default=None,
        ge=1,
        le=168,
        description=(
            "Auto-schedule a batch every N hours. ``None`` = "
            "manual trigger only (the 'Run batch now' button "
            "on ``/memory/batch``). Range: 1 hour (aggressive) "
            ".. 168 hours (one week)."
        ),
    )

    # ------------------------- worker heartbeat -----------------------------

    snapshot_heartbeat_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description=(
            "Heartbeat interval for the worker's session-"
            "snapshot re-check. Lower values detect mid-flight "
            "session changes faster but consume more IO."
        ),
    )


# ---------------------------------------------------------------------------
# MemoryEmbeddingSettings (Phase 4)
# ---------------------------------------------------------------------------


class MemoryEmbeddingSettings(BaseModel):
    """Embedding subsystem configuration (Phase 4).

    Kept minimal on purpose: only the two fields that any code
    path actually reads. Everything else a caller might expect
    (``provider_id``, ``base_url``, ``api_key``,
    ``model_id``, ``model_version``, ``dimensions``) is
    **derived** from ``model_key`` via
    ``EmbeddingModelResolver.resolve()``. Storing any of them
    here would create a second source of truth that could
    drift from the resolver snapshot.

    Master switch
    -------------
    ``enabled`` controls whether the Phase 4 embedding layer
    runs at all:

      * ``False`` (default) -- no jobs are enqueued, the
        embedding worker is not started, and retrieval falls
        back to lexical-only mode. This is the current
        behavior for every deployment that has not opted into
        Phase 4.
      * ``True`` AND ``model_key`` set -- jobs are enqueued
        when memories are created/updated, the embedding worker
        processes them, and retrieval can use vector / hybrid
        mode.

    Why no ``dimensions`` field
    ---------------------------
    The vector dimension is a fact about the model, not a user
    preference. It is:

      * discovered by
        ``OpenAICompatEmbeddingProvider.probe_dimensions`` the
        first time a model is used,
      * cached in ``models_metadata.json`` (the canonical
        location for model facts, written by
        ``config_rtr._parse_adapter_model_list``),
      * re-read by the embedding worker via
        ``ResolvedEmbeddingModel.known_dimensions``.

    Persisting it in settings would duplicate state that
    already has a home and could disagree with the metadata
    cache after a re-scan. Keeping it out of settings ensures
    the metadata cache remains the single source of truth.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the Phase 4 embedding subsystem. "
            "When False, no embedding jobs are enqueued, the "
            "embedding worker is not started, and retrieval "
            "stays on lexical mode. When True and "
            "``model_key`` is set, the embedding pipeline "
            "activates."
        ),
    )
    model_key: Optional[str] = Field(
        default=None,
        description=(
            "Composite key that identifies the embedding model "
            "to use (e.g. ``'openai::text-embedding-3-small'``). "
            "Must match a key in the resolver snapshot built by "
            "``build_embedding_model_snapshot``. The key "
            "resolves to the provider credentials, model_id, "
            "model_version, and dimensions -- none of which are "
            "stored separately in this settings object."
        ),
    )

    @field_validator("model_key", mode="after")
    @classmethod
    def _validate_model_key(cls, v: Optional[str]) -> Optional[str]:
        """Allow None ('not configured'); reject empty/whitespace strings."""
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(
                f"model_key must be a string or None, got "
                f"{type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "model_key must not be empty (use None to leave "
                "unconfigured)"
            )
        return stripped


# ---------------------------------------------------------------------------
# MemoryRetrievalSettings (Phase 4)
# ---------------------------------------------------------------------------


class MemoryRetrievalSettings(BaseModel):
    """Retrieval strategy and tuning (Phase 4).

    DISTINCT from ``MemorySettings.mode``:

      * ``MemorySettings.mode`` is a memory-subsystem state
        (DISABLED / READ_ONLY / READ_WRITE) -- "is memory on
        at all, and can it write?"

      * ``MemoryRetrievalSettings.mode`` is a search strategy
        choice -- "how should enabled memory be searched?"
        The four values (lexical / vector / hybrid /
        automatic) are orthogonal to the memory state: any
        can be combined with any ``MemorySettings.mode``.

    Modes
    -----
    ``lexical`` (default)
        FTS5/BM25 only. Fast, deterministic, no API calls. The
        current pre-Phase-4 behavior.

    ``vector``
        Embedding cosine similarity only. Requires a working
        embedding configuration (``embedding.enabled=True``
        plus a valid ``model_key``) and a populated
        ``memory_vectors`` index. Falls back to lexical when
        either precondition fails.

    ``hybrid``
        Runs both lexical and vector, then fuses the results
        via ``reciprocal_rank_fusion`` (when ``rrf_k`` is
        set) or ``weighted_fusion`` (when ``vector_alpha``
        drives the mix). The recommended mode when both
        preconditions hold.

    ``automatic``
        Short queries (fewer than 3 words) use lexical;
        longer queries use hybrid. A pragmatic default for
        deployments where vector calls are expensive and the
        query distribution is mixed. The threshold is
        implemented in ``MemoryRetrievalRouter``, not here.

    Tuning fields
    -------------
    ``vector_alpha``
        Weight of the vector side in fusion. ``0.0`` = pure
        lexical, ``1.0`` = pure vector, ``0.5`` = balanced.
        Translated to ``weighted_fusion(alpha=1 - vector_alpha)``
        by the router (note: ``weighted_fusion``'s ``alpha``
        is the weight of the LEXICAL side, so the translation
        keeps the naming intuitive for operators).

    ``min_lexical_score`` / ``min_vector_similarity``
        Per-source thresholds applied before fusion. Values
        below these are dropped from their respective lists
        before the merge. Both default to values that discard
        obvious noise without over-filtering.

    ``rrf_k``
        RRF smoothing constant. Default 60 (Cormack 2009).
        Only consulted when ``mode="hybrid"`` AND
        ``vector_alpha`` is at its default of 0.5 (the router
        interprets non-default ``vector_alpha`` as "user wants
        weighted fusion").

    ``rerank_enabled``
        Phase 5 stub. When True, the router will call
        ``CrossEncoderReranker.rerank`` on the fused top-K.
        In Phase 4 this field is present but unused; a future
        Phase 5 deployment flips it on without any settings
        migration.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    mode: Literal["lexical", "vector", "hybrid", "automatic"] = Field(
        default="lexical",
        description=(
            "Retrieval strategy. See the class docstring for "
            "the semantics of each value. Defaults to lexical "
            "so existing deployments keep their current "
            "behavior when Phase 4 is rolled out."
        ),
    )
    vector_alpha: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Weight of the vector side in hybrid fusion. "
            "0.0 = pure lexical, 1.0 = pure vector, 0.5 = "
            "balanced. Only consulted when mode is 'hybrid' "
            "or 'automatic'. The router translates this to "
            "``weighted_fusion(alpha=1 - vector_alpha)`` so "
            "the operator-facing name matches the intuitive "
            "meaning."
        ),
    )
    min_lexical_score: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum normalized BM25 score for a lexical hit "
            "to survive into fusion. Applied by "
            "``MemoryRanker`` before the fusion step. Lower "
            "values increase recall; higher values reduce "
            "noise. 0.2 is a conservative floor that keeps "
            "weak-but-valid matches."
        ),
    )
    min_vector_similarity: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum cosine similarity for a vector hit to "
            "survive into fusion. Applied by "
            "``SqliteVecProvider.search``. Cosine similarity "
            "of 0.5 is roughly the boundary between 'somewhat "
            "related' and 'unrelated' for most embedding "
            "models; raise this for stricter semantic "
            "matching."
        ),
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        le=100,
        description=(
            "RRF smoothing constant (Cormack 2009). Larger "
            "values flatten the rank-reward curve (rank "
            "differences matter less). 60 is optimal across a "
            "wide range of datasets in the original paper. "
            "Only consulted when ``mode='hybrid'`` AND "
            "``vector_alpha`` is at the default 0.5 (otherwise "
            "the router uses weighted fusion)."
        ),
    )
    rerank_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the Phase 5 reranking step. "
            "When True AND an extraction model is configured "
            "AND ``rerank_max_candidates > 0``, "
            "``HybridMemoryProvider.search_ranked`` invokes "
            "``CrossEncoderReranker.rerank`` on the fused "
            "top-K, re-ordering candidates by LLM-scored "
            "query relevance. When False (the default), the "
            "search path is identical to Phase 4 -- no LLM "
            "call, no added latency, no change to "
            "``search_mode``. Any reranker failure (timeout, "
            "parse error, provider down) degrades gracefully "
            "to RRF ordering; the search endpoint never "
            "returns an error because of the reranker."
        ),
    )
    rerank_max_candidates: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Upper bound on candidates sent to the reranker "
            "LLM per search. Candidates beyond this cap "
            "retain their RRF position and are appended "
            "AFTER the reranked set, so ``top_k`` larger "
            "than this cap still returns the full count "
            "(reranked head + RRF-ordered tail). 20 covers "
            "the typical ``default_limit * 4`` window the "
            "ranker/budget fitter downstream considers, so "
            "virtually all retrievable entries get scored "
            "at negligible extra cost. Increase only if you "
            "routinely search with ``default_limit > 10``."
        ),
    )
    rerank_timeout_seconds: float = Field(
        default=3.0,
        ge=1.0,
        le=30.0,
        description=(
            "Per-batch wall-clock timeout for one reranker "
            "LLM call. Applied via ``asyncio.wait_for`` "
            "around each ``generate_structured`` call. "
            "3.0s matches the LLM's typical latency "
            "(sub-500ms when healthy) with headroom for "
            "reasoning-capable models. A timeout degrades "
            "to RRF ordering rather than failing the "
            "search. Do NOT raise this above 10s -- the "
            "user is waiting for a search response, and a "
            "slow reranker is worse than no reranker."
        ),
    )
    rerank_batch_size: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Candidates per reranker LLM call. Controls the "
            "number of round-trips per search: with "
            "``rerank_max_candidates=20`` and "
            "``rerank_batch_size=5``, a full rerank uses 4 "
            "calls. Smaller batches parse more reliably "
            "(shorter prompts are less likely to trigger "
            "the model's 'ignore the schema' failure mode), "
            "larger batches reduce per-call overhead. Set "
            "to ``rerank_max_candidates`` (e.g. 20) for "
            "single-call behavior -- the reference §6.3 "
            "suggestion. Invalid values are clamped to the "
            "reranker's own defaults at construction time."
        ),
    )

    # ------------------------- Phase 6: graph search ------------------------

    # ------------------------- Phase 6: always retrieve ---------------------

    always_retrieve: bool = Field(
        default=True,
        description=(
            "Search the user's memories on EVERY non-empty chat turn, "
            "instead of only when the query happens to look like an "
            "explicit recall request. "
            "``classify_route`` is a heuristic gate whose patterns are "
            "English-centric; with it active, a normal question (or any "
            "question in another language) classifies as NO_NEED and "
            "``build_memory_context`` returns an empty context WITHOUT "
            "searching -- so stored facts are silently withheld from "
            "the model even though the search itself works. "
            "When True (default), every non-empty turn retrieves from "
            "the user's memories in BOTH exec modes (chat and agent). "
            "Scope is unchanged: ``user:<id>`` is always included, plus "
            "project/chat when known -- this changes WHEN retrieval "
            "runs, never WHAT is visible (scope isolation is a security "
            "boundary). "
            "Cost: one FTS query (~ms) per turn, no LLM call. "
            "Set to False to restore the strict intent-gated behaviour."
        ),
    )

    graph_search_enabled: bool = Field(
        default=False,
        description=(
            "Add the knowledge graph as a THIRD ranked source in "
            "``HybridMemoryProvider.search_ranked`` (Phase 6, "
            "sub-phase 6B). When True, a query that mentions a "
            "known entity also returns the memories linked to that "
            "entity and its neighbours, fused with the FTS and "
            "vector lists via three-way RRF. ``search_mode`` "
            "reports ``hybrid+graph`` (or ``hybrid+rerank+graph``). "
            "When False (the default) the search path is identical "
            "to Phase 5 -- no graph lookup, no extra latency. "
            "Requires graph indexing to have populated data; with "
            "an empty graph the lookup returns nothing and the "
            "result is unchanged."
        ),
    )
    graph_search_max_depth: int = Field(
        default=2,
        ge=0,
        le=3,
        description=(
            "Traversal depth for graph search, counted in HOPS from a "
            "resolved query entity. 0 = only memories linked "
            "DIRECTLY to that entity (score 1.0). 1 adds the "
            "entity's direct neighbours (score 0.5). 2 (default) "
            "adds two-hop discoveries (score 0.33), which is what "
            "resolves 'what did my manager say about the project' "
            "(person -> org -> memories). 3 is allowed but "
            "expensive: reachable edges grow multiplicatively with "
            "depth and the traversal caps start truncating "
            "aggressively."
        ),
    )
    graph_search_max_relations: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Maximum edges expanded per entity during traversal. "
            "Hub entities (a broad TOPIC mentioned in every memory) "
            "can have hundreds of neighbours; without this cap one "
            "hub consumes the entire traversal budget and the "
            "query returns hub noise only."
        ),
    )
    graph_search_min_query_entities: int = Field(
        default=1,
        ge=1,
        le=5,
        description=(
            "How many known entities a query must resolve before "
            "the graph contributes. 1 (default) means any known "
            "entity is enough. Raise this on a sparse or noisy "
            "graph, where a single incidental token match adds "
            "more noise than signal."
        ),
    )
    graph_search_memories_per_entity: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Memories hydrated per entity during graph search. "
            "Bounds the work for an entity linked to thousands of "
            "memories; the fusion only needs a handful per entity "
            "to influence the ranking."
        ),
    )

    # ------------------------- Phase 6: agentic RAG -------------------------

    agentic_enabled: bool = Field(
        default=False,
        description=(
            "Enable agentic retrieval (Phase 6, sub-phases 6C/6D): "
            "query decomposition, multi-hop retrieval, gap analysis, "
            "and evidence synthesis. Reachable ONLY through "
            "``POST /api/memory/search/agentic`` and "
            "``MemoryService.retrieve_agentic`` -- never through the "
            "dispatcher's per-turn ``retrieve``, because an agentic "
            "run costs several LLM calls. Requires a resolvable "
            "model in ``ai.agentic`` (falling back to "
            "``ai.extraction``) and ``ai.enabled=True``."
        ),
    )
    agentic_max_hops: int = Field(
        default=3,
        ge=1,
        le=5,
        description=(
            "Maximum retrieval rounds in one agentic run. Hop 1 "
            "searches the rewritten question; hops 2..N chase the "
            "gap analysis' refinements. Each hop costs "
            "``len(sub_queries)`` searches plus one LLM call, so "
            "raising this multiplies both latency and cost."
        ),
    )
    agentic_timeout_seconds: float = Field(
        default=90.0,
        ge=5.0,
        le=300.0,
        description=(
            "Hard wall-clock ceiling for one agentic run, enforced "
            "with ``asyncio.wait_for`` around the whole pipeline. "
            "On timeout the run returns the evidence gathered so far "
            "plus a partial answer if one was produced. "
            "Budget guidance: a run makes one rewrite, one gap "
            "analysis per hop, and one synthesis -- 5 chats plus a "
            "handful of searches. Those calls are SERIALIZED by the "
            "memory-LLM gate (ai.llm_max_concurrency, default 1), "
            "because a local runtime interleaves overlapping "
            "generations instead of queueing them. At ~1.5s per chat "
            "call that is roughly 10s, but a 3-hop run that also "
            "waits on an unreachable embedding provider or a slow "
            "reranker needs several times that. 30s -- the original "
            "budget, sized for UNSERIALIZED calls -- cut real runs "
            "off mid-flight and returned 0 evidence. Raise it for a "
            "slow local model; lower it for a hosted API."
        ),
    )
    agentic_confidence_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence at or above which a synthesized answer is "
            "reported as meeting the threshold "
            "(``confidence_threshold_met`` in the response). This "
            "does NOT discard low-confidence answers -- it labels "
            "them, so the caller can decide whether to surface the "
            "raw evidence instead. Discarding would hide the partial "
            "progress the run paid for."
        ),
    )


# ---------------------------------------------------------------------------
# MemorySettings
# ---------------------------------------------------------------------------


class MemorySettings(BaseModel):
    """Complete configuration snapshot for the memory subsystem.

    See the module docstring for the relationship to ``RunRequest``,
    the ``project_overrides`` semantics, and the mode-escalation
    policy. The field-level documentation below focuses on HOW each
    knob is consumed by the surrounding layers, not on repeating
    the plan's high-level design.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- global ---------------------------------------

    mode: MemoryMode = Field(
        default=MemoryMode.READ_WRITE,
        description=(
            "Global operating mode. DISABLED returns empty contexts "
            "on every retrieval and rejects every non-import write. "
            "READ_ONLY rejects writes EXCEPT ``IMPORT`` (the "
            "operator has explicitly confirmed an import via the "
            "preview → apply flow, which counts as user consent). "
            "READ_WRITE is the normal operating mode."
        ),
    )
    provider_id: str = Field(
        default=_DEFAULT_PROVIDER_ID,
        description=(
            "Identifier of the storage/retrieval backend provider "
            "(P1.3 registry). The default ``sqlite_fts`` ships with "
            "the platform. The registry validates the id at "
            "``MemoryService`` startup (P1.7); this field only "
            "requires a non-empty string."
        ),
    )

    # ------------------------- retrieval ------------------------------------

    default_limit: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "Maximum number of entries the ranker returns BEFORE "
            "budget fitting. The budget fitter (P1.4) may drop "
            "entries that exceed the token budget; this limit "
            "bounds the candidate set the fitter must consider. "
            "Typical values: 5 (default), 3 (memory-constrained "
            "deployments), 10-20 (memory-heavy applications)."
        ),
    )
    min_relevance_score: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum NORMALIZED relevance score for an entry to be "
            "considered. Scores returned by the FTS backend are "
            "raw BM25 values; the ranker (P1.4) normalizes them to "
            "[0, 1] before comparing against this threshold. "
            "Raising the value reduces noise but can filter out "
            "legitimate matches; lowering it increases recall but "
            "risks injecting irrelevant memories."
        ),
    )
    boost_pinned: float = Field(
        default=1.5,
        ge=1.0,
        le=10.0,
        description=(
            "Multiplier applied to the score of entries with "
            "``is_pinned=True``. Values below 1.0 would be a "
            "DE-boost, which contradicts the field's name and is "
            "therefore rejected. Typical range: 1.2 (mild nudge) "
            "to 3.0 (pinned entries dominate). The default 1.5 "
            "matches the plan's specification."
        ),
    )

    # ------------------------- budget ---------------------------------------

    token_budget: MemoryTokenBudget = Field(
        default_factory=MemoryTokenBudget,
        description=(
            "Per-turn token allocation. See ``MemoryTokenBudget`` "
            "for the reservation and warning-threshold semantics."
        ),
    )

    # ------------------------- writing --------------------------------------

    auto_extraction_enabled: bool = Field(
        default=False,
        description=(
            "When True, the post-run outbox processor (P1.4/P1.7) "
            "extracts candidate memories from each completed run. "
            "Phase 1 defaults to False: extraction is opt-in and "
            "requires explicit enablement after the operator has "
            "validated the extraction quality on their data. The "
            "field is present in Phase 1 so the pipeline wiring can "
            "exist before the feature is turned on."
        ),
    )
    auto_extraction_min_turns: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Minimum number of completed conversation turns before "
            "the extractor (P1.4) considers running. Prevents the "
            "extractor from wasting CPU on trivial one-shot chats. "
            "Only consulted when ``auto_extraction_enabled`` is "
            "True."
        ),
    )

    # ------------------------- ai extraction --------------------------------

    ai: MemoryAISettings = Field(
        default_factory=MemoryAISettings,
        description=(
            "AI extraction configuration (Phase 1). When "
            "``ai.enabled`` and ``ai.extraction_enabled`` are "
            "both True AND the current mode permits writes, "
            "``MemoryService`` brings up the extraction pipeline "
            "(outbox, scanner, trigger router, status watcher, "
            "orchestrator, worker). When either flag is False "
            "or the mode forbids writes, the pipeline is "
            "entirely absent. See ``MemoryAISettings`` for the "
            "full gating contract."
        ),
    )

    # ------------------------- phase 4: embedding --------------------------

    embedding: MemoryEmbeddingSettings = Field(
        default_factory=MemoryEmbeddingSettings,
        description=(
            "Embedding subsystem configuration (Phase 4). See "
            "``MemoryEmbeddingSettings`` for the gating contract: "
            "``enabled=True`` and ``model_key`` set are both "
            "required for the embedding pipeline to activate. "
            "When either condition fails, no embedding jobs are "
            "enqueued and retrieval falls back to lexical mode."
        ),
    )

    # ------------------------- phase 4: retrieval --------------------------

    retrieval: MemoryRetrievalSettings = Field(
        default_factory=MemoryRetrievalSettings,
        description=(
            "Retrieval strategy configuration (Phase 4). See "
            "``MemoryRetrievalSettings`` for the semantics of "
            "each mode. DISTINCT from ``MemorySettings.mode``: "
            "the latter controls whether memory is enabled at "
            "all (DISABLED / READ_ONLY / READ_WRITE); this field "
            "controls HOW enabled memory is searched. Both are "
            "orthogonal -- any ``retrieval.mode`` can combine "
            "with any ``MemorySettings.mode``."
        ),
    )

    # ------------------------- per-project overrides ------------------------

    project_overrides: Dict[str, "MemorySettings"] = Field(
        default_factory=dict,
        description=(
            "Per-project configuration deltas. Each entry maps a "
            "project id to a ``MemorySettings`` instance whose "
            "EXPLICITLY-SET fields override the corresponding "
            "global fields for that project. See "
            "``effective_for_project(project_id)`` for the merge "
            "semantics. Nested overrides (overrides that themselves "
            "carry ``project_overrides``) are rejected -- see "
            "``_reject_nested_overrides``."
        ),
    )

    # ------------------------- validators -----------------------------------

    @field_validator("provider_id", mode="after")
    @classmethod
    def _validate_provider_id(cls, v: str) -> str:
        """Trim and require non-empty."""
        if not isinstance(v, str):
            raise ValueError(
                f"provider_id must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("provider_id must not be empty")
        return stripped

    @model_validator(mode="after")
    def _reject_nested_overrides(self) -> "MemorySettings":
        """Reject ``project_overrides[*].project_overrides``.

        Nested overrides invite infinite recursion (an override can
        override an override, ad infinitum) and, more
        pragmatically, make "which settings actually apply to
        project X?" undecidable without walking an unbounded tree.
        The plan does not require nesting; the settings manager
        (P1.7) has no API path that would produce it. Enforcing the
        flat structure here turns any accidental producer into a
        fast, precise error instead of a silent deep-tree bug.
        """
        for project_id, override in self.project_overrides.items():
            if override.project_overrides:
                raise ValueError(
                    f"project_overrides[{project_id!r}] must not "
                    f"itself contain project_overrides; nested "
                    f"overrides are not supported. Edit the parent "
                    f"settings directly instead."
                )
        return self

    @field_validator("project_overrides", mode="after")
    @classmethod
    def _validate_override_count(
        cls, v: Dict[str, "MemorySettings"]
    ) -> Dict[str, "MemorySettings"]:
        """Cap the number of project overrides to prevent unbounded growth."""
        if len(v) > _MAX_PROJECT_OVERRIDES:
            raise ValueError(
                f"project_overrides exceeds the maximum of "
                f"{_MAX_PROJECT_OVERRIDES} entries "
                f"(got {len(v)}); consider using a smaller number "
                f"of shared project configuration templates"
            )
        return v

    # ------------------------- project resolution --------------------------

    def effective_for_project(self, project_id: str) -> "MemorySettings":
        """Resolve the effective settings for a specific project.

        Merge semantics
        ---------------
        If ``project_id`` has no override, this method returns
        ``self`` (fast path; no allocation, no field copying).

        If an override exists, the returned instance's field values
        are computed as follows, per field:

          * ``field in override.model_fields_set``
                → the override's value wins (the user
                  explicitly configured this field for the
                  project)
          * ``field not in override.model_fields_set``
                → the base (``self``) value is inherited

        This is Pydantic's standard "delta merge" semantics,
        exposed through ``model_dump(exclude_unset=True)``. It
        matches what a user means when they edit a single field on
        the project settings page: "change THIS field for THIS
        project, keep everything else from the defaults".

        Recursion prevention
        --------------------
        The returned instance always has ``project_overrides={}``.
        Even if the override somehow carried a nested
        ``project_overrides`` (which the validator rejects), this
        method would not propagate it. Belt-and-suspenders against
        a future relaxation of the validator.
        """
        if not isinstance(project_id, str):
            raise TypeError(
                f"project_id must be a string, got "
                f"{type(project_id).__name__}"
            )
        clean_pid = project_id.strip()
        if not clean_pid:
            # Empty project_id: no override can match. Return base.
            # Fail-fast was considered and rejected -- callers that
            # ask "what are the settings for project ''?" are often
            # in a fallback path and need a working settings object.
            return self

        override = self.project_overrides.get(clean_pid)
        if override is None:
            return self

        # Delta merge: base first, then explicitly-set override fields.
        base_data = self.model_dump(mode="json")
        override_delta = override.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"project_overrides"},
        )
        merged_data = {**base_data, **override_delta}
        # Force an empty overrides map to prevent any accidental
        # propagation of the original overrides into the resolved
        # instance.
        merged_data["project_overrides"] = {}
        return MemorySettings.model_validate(merged_data)

    # ------------------------- override editing ----------------------------

    def with_project_override(
        self,
        project_id: str,
        override: "MemorySettings",
    ) -> "MemorySettings":
        """Return a copy with ``project_id`` bound to ``override``.

        Convenience wrapper used by ``MemorySettingsManager`` (P1.7)
        when the operator edits a project's settings via the API.
        Returns a NEW ``MemorySettings`` instance; ``self`` is
        untouched. The new override REPLACES any previous one for
        the same project id.
        """
        clean_pid = self._clean_project_id(project_id)
        if override.project_overrides:
            raise ValueError(
                "override passed to with_project_override must not "
                "itself contain project_overrides; nested overrides "
                "are not supported"
            )
        new_overrides = dict(self.project_overrides)
        new_overrides[clean_pid] = override
        return self.model_copy(update={"project_overrides": new_overrides})

    def without_project_override(self, project_id: str) -> "MemorySettings":
        """Return a copy with ``project_id``'s override removed.

        Removes the override (reverting the project to global
        settings). Idempotent: removing a non-existent override is
        a no-op that still returns a valid new instance.
        """
        clean_pid = self._clean_project_id(project_id)
        if clean_pid not in self.project_overrides:
            return self
        new_overrides = dict(self.project_overrides)
        del new_overrides[clean_pid]
        return self.model_copy(update={"project_overrides": new_overrides})

    # ------------------------- internal helpers ----------------------------

    @staticmethod
    def _clean_project_id(project_id: str) -> str:
        """Trim and validate a project id for the override helpers."""
        if not isinstance(project_id, str):
            raise TypeError(
                f"project_id must be a string, got "
                f"{type(project_id).__name__}"
            )
        clean = project_id.strip()
        if not clean:
            raise ValueError("project_id must not be empty")
        return clean


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def default_settings() -> MemorySettings:
    """Return a fresh ``MemorySettings`` with plan-default values.

    Returns a NEW instance on every call. This is deliberate: the
    ``MemoryService`` singleton in P1.7 holds its OWN instance, and
    a caller that mutates a ``default_settings()`` result must not
    affect anyone else's view of "the defaults".

    Useful in tests, in the ``MemorySettingsManager.load()`` fallback
    path when no settings file exists, and in the API's "reset to
    defaults" endpoint (P1.7).
    """
    return MemorySettings()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryAIExtractionSettings",
    "MemoryAISettings",
    "MemoryEmbeddingSettings",
    "MemoryRetrievalSettings",
    "MemorySettings",
    "MemoryTokenBudget",
    "default_settings",
]
