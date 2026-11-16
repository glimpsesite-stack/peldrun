# backend/peldrun/memory/domain/embedding.py
"""
PELDRUN Memory Platform - Memory Embedding Domain Model.

Value object representing one embedding vector for one memory entry
under one specific model version.

Storage model
-------------

The companion table ``memory_embeddings`` (migration v4) keys rows by
the composite primary key::

    PRIMARY KEY (memory_id, model_id, model_version)

This triple is what makes the vector index **model-versioned**:

  * Switching the user's embedding model does NOT corrupt existing
    vectors — the new model writes rows under a new
    ``(model_id, model_version)`` namespace, and the search layer
    queries only the rows for the currently-active model. Old rows
    remain in the table until an explicit reindex deletes them.

  * Rolling back to a previous model re-uses its existing rows, so
    rollback is instant (no re-embedding cost).

  * A model upgrade (same ``model_id``, new ``model_version``) is
    treated identically to a model change — the namespace shifts,
    and the vector space is not silently mixed with the old one.

Design invariants
-----------------

1. **Pure domain, no I/O.**
   This module imports only stdlib + Pydantic. It has no knowledge
   of SQLite, ``sqlite-vec``, ``struct.pack``, or any concrete
   storage format. Conversion between the in-memory ``List[float]``
   and the on-disk ``BLOB`` is the repository's responsibility
   (``omweb/memory/storage/vector_repository.py``). This is the
   same boundary ``MemoryEntry`` maintains: the domain object is
   storage-agnostic.

2. **Immutable.**
   ``frozen=True`` for the same reason ``MemoryEntry`` is frozen:
   an embedding is a fact about a specific (entry, model) pair at a
   specific point in time. Changing the vector in place would be
   meaningless — you cannot "modify" a vector into a different one;
   you produce a new embedding. Callers that need "the same but
   different" create a new instance.

3. **Vector sanity enforced at construction.**
   Every instance is guaranteed to carry a non-empty vector of
   finite floats within a reasonable dimension range
   (``MIN_DIMENSIONS``..``MAX_DIMENSIONS``). The repository layer
   relies on this invariant — it does NOT re-validate before
   packing. A malformed vector reaching the vec0 table would
   either raise an opaque SQLite error or, worse, silently pollute
   the index with a wrong-dimension vector and break every
   subsequent search.

4. **``dimensions`` is a property, not a field.**
   The dimension is ``len(vector)`` — a derivation, not an
   independent fact. Storing it as a separate field would create a
   second source of truth that could drift from the vector itself
   (a bug we'd have to prevent rather than design out).

5. **``created_at`` is aware datetime (UTC).**
   Same convention as ``MemoryEntry``: the domain object carries
   timezone-aware timestamps; the repository converts to epoch
   seconds when persisting. Naive datetimes are coerced to UTC
   at construction time (with a warning-free assumption matching
   ``MemoryEntry``).

6. **No ``content_version`` derivation here.**
   The field exists on the model because the storage schema
   requires it, but **its derivation is not this module's
   responsibility.** In Phase 4 it is always ``1``; in Phase 5
   (consolidation), the consolidation pipeline increments it when
   it rewrites an entry's content. Whether the caller supplies
   ``1``, ``2``, or anything else, this class trusts the value —
   it does not attempt to reconcile the embedding against
   ``memories.content`` (that comparison requires the repository,
   not the value object).

Why no ``model_key`` / ``provider_id`` fields
---------------------------------------------

The embedding row does NOT know the provider that produced it, or
the composite key the UI used to identify the model. Those are
configuration concerns that live in the resolver snapshot
(``peldrun/memory/embeddings/model_resolver.py``) and are looked
up by ``model_id`` at query time. Storing them per-row would
duplicate state, break when the resolver changes, and require a
data migration for something that is ultimately derived data.

The rule of thumb: **the row stores what makes the vector
searchable; everything else is looked up.**

See also
--------

    * ``omweb/memory/storage/migrations.py::_apply_v4``
                                  — the ``memory_embeddings`` schema
    * ``omweb/memory/storage/vector_repository.py`` (Task 4.4)
                                  — BLOB <-> List[float] conversion
    * ``peldrun/memory/retrieval/vector_search.py`` (Task 4.5)
                                  — consumes embeddings at search time
    * ``peldrun/memory/embeddings/contracts.py``
                                  — ``EmbeddingResult`` (the
                                    transient transport value; this
                                    is the persisted value)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MIN_DIMENSIONS: int = 1
"""Lowest allowed vector dimension.

Set to 1 (not to a "meaningful" floor like 32) because:

  * a test fixture may legitimately use a 1-dimensional vector to
    verify the wiring without paying the cost of a real model's
    vector;
  * some exotic future model (not currently on the market) could
    produce a very small vector.

Any non-empty vector is a technically valid embedding. The
practical floor is enforced by the model itself, not by the
storage layer.
"""


MAX_DIMENSIONS: int = 8192
"""Highest allowed vector dimension.

OpenAI's largest embedding model (``text-embedding-3-large``)
produces 3072-dimensional vectors. Google's ``text-embedding-004``
produces 768. Cohere v3 produces 1024. LM Studio's local models
range from 384 (``nomic-embed-text``) to 1024 (``bge-m3``).

8192 provides ~2.5x headroom over the largest known model — enough
to accommodate a future model without a code change, while still
catching a programming bug (e.g. "we accidentally embedded an
array of length 100,000") with a fast, clear error rather than a
multi-megabyte BLOB insertion that would slow the whole table.
"""


# ---------------------------------------------------------------------------
# MemoryEmbedding
# ---------------------------------------------------------------------------


class MemoryEmbedding(BaseModel):
    """One embedding vector for one memory entry under one model version.

    Frozen, validated, storage-agnostic. Built by the embedding
    worker after a successful ``embed_batch`` call, persisted by the
    repository, and consumed by the vector search layer.

    Example
    -------
    ::

        embedding = MemoryEmbedding(
            memory_id="mem_abc123",
            model_id="text-embedding-3-small",
            model_version="text-embedding-3-small",
            content_version=1,
            vector=[0.12, -0.45, ...],  # 1536 floats
            created_at=datetime.now(timezone.utc),
        )
        embedding.dimensions  # -> 1536

    Validation contract
    -------------------
    Construction raises ``pydantic.ValidationError`` (a subclass of
    ``ValueError``) for any of the following:

      * an empty ``memory_id`` / ``model_id`` / ``model_version``;
      * ``content_version < 1``;
      * a ``vector`` that is empty, or longer than
        ``MAX_DIMENSIONS``;
      * a ``vector`` containing a non-finite float
        (``NaN``, ``+Inf``, ``-Inf``);
      * a ``created_at`` that is not a ``datetime``.

    All other Pydantic coercion (int -> float, string -> datetime
    where applicable) follows the standard rules. ``extra="forbid"``
    ensures a typo in a field name does not silently produce a
    partially-populated instance.

    Thread-safety
    -------------
    Instances are immutable after construction. Reading any field
    (including ``dimensions``) from any number of threads is safe
    without locking.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    # ------------------------- identity ------------------------------------

    memory_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the memory entry this vector belongs to. "
            "Foreign-key-like reference to ``memories.memory_id``. "
            "Not declared as a SQL FOREIGN KEY because embedding "
            "rows are deleted before the memory row on reindex "
            "— a FK ON DELETE CASCADE would work, but the manual "
            "ordering is more auditable and matches the rest of "
            "the schema (which deliberately avoids cross-table "
            "FKs to keep migrations simple)."
        ),
    )
    model_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-reported model identifier that produced this "
            "vector (e.g. ``'text-embedding-3-small'``). "
            "Combined with ``model_version``, it defines the "
            "vector space: two vectors from different "
            "``(model_id, model_version)`` pairs cannot be "
            "compared by cosine distance."
        ),
    )
    model_version: str = Field(
        ...,
        min_length=1,
        description=(
            "Opaque version string for the model. For most "
            "providers this equals ``model_id`` (they do not "
            "expose a separate version dimension). Providers "
            "that DO version models independently (rare) "
            "supply a distinct value here, allowing rolling "
            "upgrades to build a new vector namespace without "
            "destroying the old one."
        ),
    )

    # ------------------------- content linkage -----------------------------

    content_version: int = Field(
        default=1,
        ge=1,
        description=(
            "Monotonic counter for the content this vector was "
            "generated from. Starts at 1. Incremented only when "
            "the source memory's content is rewritten by a "
            "process that intends the embedding to be "
            "regenerated (Phase 5 consolidation). A vector "
            "whose ``content_version`` is lower than the "
            "memory's current version is stale — the search "
            "layer may still return it, but a background "
            "re-embed job will replace it. In Phase 4 this "
            "value is always ``1``."
        ),
    )

    # ------------------------- vector --------------------------------------

    vector: List[float] = Field(
        ...,
        description=(
            "The embedding itself, as a list of floats in "
            "provider-canonical order. Length is the vector "
            "dimension; validated to be in "
            "``[MIN_DIMENSIONS, MAX_DIMENSIONS]`` and to "
            "contain only finite values. The repository packs "
            "this into a ``BLOB`` via ``array('f', ...).tobytes`` "
            "(or ``struct.pack``) on write, and unpacks on "
            "read — the domain object never sees the binary "
            "representation."
        ),
    )

    # ------------------------- timestamps ----------------------------------

    created_at: datetime = Field(
        ...,
        description=(
            "When this embedding was generated. Used by the "
            "reindex path to decide which vectors are stale, "
            "and by the operational dashboard to display "
            "'last reindex' times. Persisted as epoch seconds; "
            "always exposed to consumers as a timezone-aware "
            "``datetime`` (UTC)."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator("memory_id", "model_id", "model_version", mode="after")
    @classmethod
    def _validate_non_empty_id(cls, v: str) -> str:
        """Reject whitespace-only identifiers.

        ``min_length=1`` on the field catches ``""``, but not
        ``"   "`` (which is length 3 but semantically empty).
        ``str_strip_whitespace=True`` on the model config strips
        the value first, so by the time this validator runs the
        value is already trimmed — but we check explicitly
        anyway, because the invariant is semantic
        ("identifies something") and a future config change to
        ``str_strip_whitespace`` should not silently weaken it.
        """
        if not v:
            raise ValueError(
                "identifier fields must not be empty or "
                "whitespace-only"
            )
        return v

    @field_validator("created_at", mode="after")
    @classmethod
    def _coerce_naive_to_utc(cls, v: datetime) -> datetime:
        """Assume naive datetimes are UTC.

        Matches ``MemoryEntry``'s handling. A naive datetime is
        ambiguous — silently treating it as UTC is the least
        surprising choice for a system whose storage layer is
        UTC-only (epoch seconds). Callers that legitimately
        have a different timezone attach ``tzinfo`` before
        constructing the instance.
        """
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def _validate_vector(self) -> "MemoryEmbedding":
        """Enforce the vector shape invariants.

        Runs once per instance (the model is frozen). Three
        checks, in increasing cost order:

          1. Non-empty (cheapest).
          2. Bounded length (cheap).
          3. Every element is a finite float (O(N)).

        The finiteness check is the last line of defense against a
        provider that returns ``NaN`` for an empty input, or a bug
        that packs a numpy array containing ``Inf`` (some
        tokenizers produce it on pathological inputs). Either
        would corrupt the ``vec0`` index — cosine distance
        against ``NaN`` returns ``NaN``, which SQLite-vec treats
        as "distance undefined" and drops from the result set,
        silently hiding every match for that memory forever.
        Catching it here is far better than debugging a
        "disappeared memory" months later.
        """
        n = len(self.vector)
        if n < MIN_DIMENSIONS:
            raise ValueError(
                f"vector must contain at least {MIN_DIMENSIONS} "
                f"element(s); got {n}. An empty vector cannot "
                f"be searched or compared."
            )
        if n > MAX_DIMENSIONS:
            raise ValueError(
                f"vector dimension {n} exceeds the maximum of "
                f"{MAX_DIMENSIONS}. This usually indicates a bug "
                f"(e.g. an array of the wrong shape was passed). "
                f"Raise MAX_DIMENSIONS in "
                f"peldrun.memory.domain.embedding only after "
                f"confirming the intended model actually "
                f"produces vectors that long."
            )
        for i, component in enumerate(self.vector):
            # ``math.isfinite`` covers NaN, +Inf, -Inf in one check.
            # ``not isinstance(component, (int, float))`` was
            # deliberately NOT added: Pydantic already coerced the
            # list to floats during field validation, so every
            # element here IS a float. The isfinite check is the
            # only one that remains meaningful at this point.
            if not math.isfinite(component):
                raise ValueError(
                    f"vector contains a non-finite value at "
                    f"index {i}: {component!r}. NaN and infinity "
                    f"break cosine-distance queries in "
                    f"sqlite-vec and must be rejected at "
                    f"construction."
                )
        return self

    # ------------------------- derived properties --------------------------

    @property
    def dimensions(self) -> int:
        """Length of the vector.

        Derived property, not a stored field. See the module
        docstring's design invariant #4 for the rationale.
        """
        return len(self.vector)

    # ------------------------- convenience --------------------------------

    def matches_namespace(self, model_id: str, model_version: str) -> bool:
        """Return True when this embedding belongs to a given vector space.

        A "namespace" is the pair ``(model_id, model_version)``.
        Two embeddings in different namespaces are not comparable
        by cosine distance, even if their dimensions happen to
        match (different models trained on different data can
        produce orthogonal bases with the same length). The
        search layer MUST filter by namespace before running any
        distance computation.

        This helper exists so that check has one canonical
        spelling. Downstream code that needs the same check
        (retrieval, reindex, consolidation) calls it here
        instead of re-implementing the comparison — which is
        exactly the kind of low-level test that can silently
        drift if duplicated.
        """
        return (
            self.model_id == model_id
            and self.model_version == model_version
        )

    def to_vector_list(self) -> List[float]:
        """Return a shallow copy of the vector as a plain list.

        Callers that need to pass the vector to an API (numpy,
        sqlite-vec, a JSON serializer) SHOULD use this method
        rather than reading ``self.vector`` directly. The
        returned list is a new list (the elements are floats,
        which are immutable, so no deep copy is required) — a
        caller that mutates the returned list cannot affect
        the frozen domain object.

        This is the storage-agnostic path. The repository
        (Task 4.4) may pack the same floats into a BLOB for
        SQLite storage, but it MUST read them via this method
        so the returned list is a clean, mutable copy that
        the packing routine can safely hold on to.
        """
        return list(self.vector)

    # ------------------------- factory -------------------------------------

    @classmethod
    def from_result_and_entry(
        cls,
        *,
        memory_id: str,
        model_id: str,
        model_version: str,
        vector: Sequence[float],
        content_version: int = 1,
    ) -> "MemoryEmbedding":
        """Convenience factory used by the embedding worker.

        Builds an instance with ``created_at=now()`` and the
        supplied vector. The caller (``embedding_worker.py``,
        Task 4.4) already has the ``memory_id`` from the job
        context, the ``model_id``/``model_version`` from the
        resolved model, and the ``vector`` from the
        ``EmbeddingResult`` — this factory keeps the
        construction site free of the timestamp boilerplate.

        ``content_version`` defaults to 1 (Phase 4's only value).
        Phase 5's consolidation pipeline passes an explicit
        counter when it rewrites content.
        """
        return cls(
            memory_id=memory_id,
            model_id=model_id,
            model_version=model_version,
            content_version=content_version,
            vector=list(vector),
            created_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MAX_DIMENSIONS",
    "MIN_DIMENSIONS",
    "MemoryEmbedding",
]
