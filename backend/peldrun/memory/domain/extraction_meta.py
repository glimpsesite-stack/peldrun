# backend/peldrun/memory/domain/extraction_meta.py
"""
PELDRUN Memory Platform - Phase 1-B: ExtractionMeta.

Defines ``ExtractionMeta``: the provenance record attached to every
``MemoryEntry`` whose ``source_type == MemorySourceType.EXTRACTION``.

Scope and rationale
-------------------

``ExtractionMeta`` answers the question:

    "Which extraction job produced this memory, from which turns,
     using which model, and how confident was it?"

This is the foundation of PELDRUN's explainability guarantee: every
memory produced automatically by the extraction pipeline is fully
traceable to the exact session, turn range, model, and prompt
version that generated it. A user who sees a memory in the UI can
follow the chain back to the original messages.

Design invariants
-----------------

1. **Mandatory for EXTRACTION-sourced entries; forbidden otherwise.**
   Enforced by a model validator on ``MemoryEntry`` (not here). This
   class is just the record; the entry-level policy lives with the
   entry.

2. **Serializable to/from JSON without loss.**
   The repository stores this as ``extraction_meta_json`` (a TEXT
   column on ``memories``, added by Migration v2). Every field must
   round-trip through ``model_dump_json()`` / ``model_validate_json()``
   without conversion.

3. **No content duplication.**
   ``source_turn_indexes`` records WHICH turns, not their text.
   The text lives in the session file, which the user can open
   independently. Storing the text here would bloat every memory
   row by the size of its source conversation.

4. **Length-bounded identifiers, defensive defaults.**
   All ids are required non-empty strings. All lists default to
   empty. ``confidence_at_creation`` defaults to 0.0 (never None) so
   that a "record exists but confidence unknown" case cannot
   silently appear as "confidence is 1.0".
"""

from __future__ import annotations

from typing import List

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from peldrun.memory.domain.enums import ExtractionTrigger


_MAX_ID_CHARS: int = 256


class ExtractionMeta(BaseModel):
    """Provenance record for an extraction-sourced memory entry.

    Attached to ``MemoryEntry.extraction_meta`` when
    ``source_type == EXTRACTION``. Persisted as JSON in the
    ``extraction_meta_json`` column.

    See the module docstring for the full rationale.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- job reference -------------------------------

    job_id: str = Field(
        ...,
        description=(
            "The `memory_jobs.job_id` that produced this entry. "
            "Primary link from a memory back to the extraction run."
        ),
    )
    trigger_type: ExtractionTrigger = Field(
        ...,
        description=(
            "The extraction trigger that produced this entry. "
            "Mirrors `MemoryJob.trigger_type`; useful when reading "
            "the memory in isolation from its job row."
        ),
    )

    # ------------------------- source session ------------------------------

    source_chat_id: str = Field(
        ...,
        description=(
            "The chat the extraction read from. Equal to the "
            "producing job's `chat_id`."
        ),
    )
    source_turn_indexes: List[int] = Field(
        ...,
        description=(
            "Which turns of `source_chat_id` the evidence came "
            "from. Indices match the `turns[]` array in "
            "`session.json`, 0-based. Non-empty (a memory with "
            "no source turns cannot be justified)."
        ),
    )

    # ------------------------- model ---------------------------------------

    model_provider_id: str = Field(
        ...,
        description="Provider id of the extraction LLM.",
    )
    model_id: str = Field(
        ...,
        description="Model id of the extraction LLM.",
    )

    # ------------------------- extraction quality --------------------------

    prompt_version: str = Field(
        ...,
        description=(
            "Version tag of the extraction prompt that produced "
            "this candidate. Enables A/B testing of prompt "
            "changes without losing attribution of historical "
            "memories."
        ),
    )
    confidence_at_creation: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "The candidate's self-reported confidence at the time "
            "this memory was created. Defaults to 0.0 (safe) "
            "rather than None -- a 'record exists but confidence "
            "unknown' state is more dangerous than 'confidence "
            "was reported as zero'."
        ),
    )

    # ------------------------- evidence ------------------------------------

    evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Identifiers of the Evidence objects supporting this "
            "memory (format decided by the ExtractionOrchestrator "
            "-- currently '<job_id>:ev:<index>'). Empty only when "
            "the extraction produced no supporting evidence "
            "(allowed but strongly discouraged)."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator(
        "job_id",
        "source_chat_id",
        "model_provider_id",
        "model_id",
        "prompt_version",
        mode="after",
    )
    @classmethod
    def _validate_required_strings(cls, v: str, info) -> str:
        """Trim and non-empty-check every required identifier."""
        if not isinstance(v, str):
            raise ValueError(
                f"{info.field_name} must be a string, "
                f"got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                f"{info.field_name} must not be empty"
            )
        if len(stripped) > _MAX_ID_CHARS:
            raise ValueError(
                f"{info.field_name} exceeds {_MAX_ID_CHARS} chars "
                f"(got {len(stripped)})"
            )
        return stripped

    @field_validator("source_turn_indexes", mode="after")
    @classmethod
    def _validate_turn_indexes(cls, v: List[int]) -> List[int]:
        """Non-empty, non-negative, deduplicated, sorted."""
        if not v:
            raise ValueError(
                "source_turn_indexes must not be empty"
            )
        cleaned: List[int] = []
        seen = set()
        for raw in v:
            if not isinstance(raw, int):
                raise ValueError(
                    f"source_turn_indexes entries must be ints, "
                    f"got {type(raw).__name__}"
                )
            if raw < 0:
                raise ValueError(
                    f"source_turn_indexes entries must be >= 0, "
                    f"got {raw}"
                )
            if raw not in seen:
                seen.add(raw)
                cleaned.append(raw)
        return sorted(cleaned)

    @field_validator("evidence_ids", mode="after")
    @classmethod
    def _validate_evidence_ids(cls, v: List[str]) -> List[str]:
        """Deduplicate and trim; empty strings dropped."""
        cleaned: List[str] = []
        seen = set()
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError(
                    f"evidence_ids entries must be strings, "
                    f"got {type(raw).__name__}"
                )
            stripped = raw.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            cleaned.append(stripped)
        return cleaned


__all__ = ["ExtractionMeta"]
