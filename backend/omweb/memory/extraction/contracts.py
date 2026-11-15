# backend/omweb/memory/extraction/contracts.py
"""
PELDRUN Memory Platform - Phase 1-B: Extraction Contracts.

The DTOs that flow between the extraction pipeline stages:

    LLM response (raw JSON)
       |
       v
    [parse] -> List[MemoryCandidate]   (candidate.py in this module)
       |
       v
    [validate evidence] -> List[MemoryCandidate]  (validator.py)
       |
       v
    [write pipeline] -> List[MemoryEntry]         (write_pipeline.py)
       |
       v
    ExtractionResult  (what ExtractionOrchestrator.extract() returns)

Three classes
-------------

    * ``Evidence``        -- points at the exact text in the
                             original messages that justifies a
                             candidate. Its ``snippet`` MUST be a
                             verbatim substring of the source
                             content (see ``validator.py`` for the
                             matching rule).

    * ``MemoryCandidate`` -- a DRAFT memory produced by the LLM,
                             not yet persisted. Carries the
                             proposed content, classification, and
                             the evidence that supports it.

    * ``ExtractionResult``-- the complete output of one extraction
                             run: the candidates that survived
                             validation, the per-candidate decision
                             log, and the LLM cost.

Design invariants
-----------------

1. **Verbatim evidence, no paraphrase (§21.5).**
   ``Evidence.snippet`` MUST be a substring of the source message
   content. The extraction prompt enforces this; ``validator.py``
   verifies it. Paraphrased or translated snippets are REJECTED.

2. **All candidates are immutable-by-convention.**
   The list manipulation happens in the orchestrator (filter,
   sort, dedupe). Individual candidates are not mutated after
   construction -- a new candidate is produced instead.

3. **ExtractionResult is a SUMMARY, not a state machine.**
   It carries what the orchestrator decided; it does NOT carry
   intermediate state (which candidates were rejected and why --
   that lives in ``decisions``). The distinction matters because
   ``ExtractionResult`` is converted to ``JobResult`` for
   ``outbox.complete()``, and ``JobResult`` only needs the
   summary.

4. **No memory-entry coupling.**
   This module does NOT import ``MemoryEntry``. The orchestrator
   converts candidates to entries via the write pipeline; that
   conversion belongs to the pipeline, not to this DTO module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from peldrun.memory.domain.enums import (
    MemoryScope,
    MemoryType,
)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


_MAX_SNIPPET_CHARS: int = 200
"""Hard ceiling on ``Evidence.snippet`` length (§21.5).

The extraction prompt also caps snippets at 200 chars. This
validator is the SECOND line of defence: a misbehaving LLM that
returns a longer snippet gets its candidate rejected cleanly
instead of landing a 10KB blob in the review queue.
"""


class Evidence(BaseModel):
    """A pointer to the exact source text that supports a candidate.

    The snippet MUST be a verbatim substring of the referenced
    message's content. Whitespace and case are normalized for
    comparison (see ``validator._normalize_for_comparison``), but
    the text itself must be IDENTICAL to the source -- no
    paraphrase, no translation, no typo correction.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    turn_index: int = Field(
        ...,
        ge=0,
        description=(
            "0-based index into the session's `turns[]` array. "
            "Identifies WHICH turn the snippet came from."
        ),
    )
    turn_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional stable turn id (`turn_<n>_<ts>` in the "
            "current session format). When provided, the "
            "validator cross-checks it against the session to "
            "catch turn reordering."
        ),
    )
    snippet: str = Field(
        ...,
        description=(
            "The exact substring of the source message content "
            f"that justifies this candidate. Max "
            f"{_MAX_SNIPPET_CHARS} chars. No paraphrase."
        ),
    )
    message_role: Optional[str] = Field(
        default=None,
        description=(
            "Which message in the turn the snippet came from: "
            "'user' or 'assistant'. Optional -- the validator "
            "accepts snippets from either role, but the UI shows "
            "the role as context."
        ),
    )

    @field_validator("snippet", mode="after")
    @classmethod
    def _validate_snippet(cls, v: str) -> str:
        """Trim outer whitespace, enforce non-empty and length cap."""
        if not isinstance(v, str):
            raise ValueError(
                f"snippet must be a string, got {type(v).__name__}"
            )
        # Trim only OUTER whitespace -- inner whitespace is part
        # of the verbatim contract and is preserved as-is.
        stripped = v.strip()
        if not stripped:
            raise ValueError("snippet must not be empty")
        if len(stripped) > _MAX_SNIPPET_CHARS:
            raise ValueError(
                f"snippet exceeds {_MAX_SNIPPET_CHARS} chars "
                f"(got {len(stripped)})"
            )
        return stripped

    @field_validator("turn_id", "message_role", mode="after")
    @classmethod
    def _strip_optional(
        cls, v: Optional[str], info
    ) -> Optional[str]:
        """Trim optional strings; empty-after-trim -> None."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# MemoryCandidate
# ---------------------------------------------------------------------------


class MemoryCandidate(BaseModel):
    """A DRAFT memory produced by the LLM, not yet persisted.

    Carries everything needed to construct a ``MemoryEntry`` via
    the write pipeline, plus the evidence that justifies it.
    Validation (evidence check, confidence threshold, dedupe)
    happens AFTER construction -- this class is purely a data
    container.

    Instances are added to ``ExtractionResult.candidates`` only
    after they have passed validation. Rejected candidates go to
    ``ExtractionResult.decisions`` with a rejection reason.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- content -------------------------------------

    content: str = Field(
        ...,
        description=(
            "The proposed memory text, written by the LLM from "
            "the source turns. This is what will be persisted as "
            "`MemoryEntry.content` if the candidate is accepted."
        ),
    )
    memory_type: MemoryType = Field(
        ...,
        description="Semantic classification (semantic/episodic/...).",
    )

    # ------------------------- scope ---------------------------------------

    scope_type: Optional[MemoryScope] = Field(
        default=None,
        description=(
            "Proposed scope. When None, the orchestrator applies "
            "the job's default scope. LLMs generally should NOT "
            "choose the scope -- the job knows better."
        ),
    )
    scope_id: Optional[str] = Field(
        default=None,
        description=(
            "Proposed scope id. When None, the orchestrator "
            "applies the job's default scope_id."
        ),
    )

    # ------------------------- quality signals -----------------------------

    importance: int = Field(
        default=50,
        ge=0,
        le=100,
        description=(
            "Self-reported importance. Passed through to "
            "`MemoryEntry.importance` unchanged."
        ),
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Self-reported confidence. Below "
            "`MemorySettings.min_confidence` -> the candidate is "
            "sent to the review queue (or rejected, depending on "
            "the settings)."
        ),
    )
    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Optional free-form tags. Normalized by "
            "`MemoryEntry`'s validator when the entry is built."
        ),
    )

    # ------------------------- evidence ------------------------------------

    evidence: Optional[Evidence] = Field(
        default=None,
        description=(
            "The single strongest piece of source evidence "
            "supporting this candidate. A future Phase may "
            "support multiple evidence items; Phase 1 keeps it "
            "to one to minimize LLM output size."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator("content", mode="after")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        """Trim outer whitespace; require non-empty."""
        if not isinstance(v, str):
            raise ValueError(
                f"content must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("content must not be empty")
        return stripped


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------


class ExtractionResult(BaseModel):
    """The output of one ``ExtractionOrchestrator.extract()`` call.

    Summary of what happened during one job. The full candidate
    list (including rejected ones) lives in ``candidates`` --
    accepted candidates only, with their final classification.
    Rejected candidates are recorded in ``decisions`` with their
    rejection reason.

    The orchestrator's caller (the worker) converts this to a
    ``JobResult`` before calling ``outbox.complete()``. The
    ``ExtractionResult`` carries more detail (the accepted
    candidates themselves) for debugging and Phase-5 review-queue
    integration.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    candidates: List[MemoryCandidate] = Field(
        default_factory=list,
        description=(
            "Candidates that passed all validations and are ready "
            "for the write pipeline. Their order is the order the "
            "pipeline will process them."
        ),
    )
    decisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-candidate decision log. Each entry has at least "
            "'candidate_index', 'action' ('added' | 'rejected' | "
            "'merged' | 'skipped'), and 'reason'. Empty when the "
            "LLM produced zero candidates."
        ),
    )
    cost_nano_usd: int = Field(
        default=0,
        ge=0,
        description=(
            "LLM cost of the extraction call, in nano-USD."
        ),
    )

    @property
    def candidates_count(self) -> int:
        """Number of candidates that passed validation."""
        return len(self.candidates)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "Evidence",
    "MemoryCandidate",
    "ExtractionResult",
]
