# backend/peldrun/memory/agentic/contracts.py
"""
PELDRUN Memory Platform - Agentic Retrieval Contracts.

The value objects and the storage-facing Protocol that the Phase 6
agentic pipeline is built from:

    * ``MemorySearchProtocol`` -- the ONE search surface the
      multi-hop retriever needs. Satisfied by
      ``MemoryService.provider`` (FTS or ``HybridMemoryProvider``),
      so the pipeline depends on an interface rather than on
      ``omweb`` internals.
    * ``SynthesizedAnswer``   -- an answer + confidence + citations.
    * ``GapAnalysis``         -- "is the evidence enough, and what
                                 should the next hop search for?".
    * ``MultiHopResult``      -- everything one multi-hop run produced.
    * ``AgenticRetrievalResult`` -- the PUBLIC result of
      ``MemoryService.retrieve_agentic``, including the fallback
      path's ``MemoryContext``.

Design invariants
-----------------

1. **No ``omweb`` import.**
   Same rule as ``peldrun.memory.graph``: the core owns the
   contracts and the algorithms, ``omweb`` owns the wiring. The
   search backend arrives as a Protocol, which is what lets
   ``MultiHopRetriever`` be unit-tested against a three-line double.

2. **Frozen, fully-typed results.**
   Every result object is a frozen dataclass. An agentic run crosses
   several async boundaries (rewrite, N hops, gap analysis,
   synthesis); a caller that mutates a half-built result between
   stages would produce an answer whose citations no longer match its
   evidence. Freezing makes that impossible and makes the API layer's
   serialization a pure read.

3. **Confidence is clamped at construction.**
   ``SynthesizedAnswer.confidence`` is normalized into ``[0, 1]`` in
   ``__post_init__``. The threshold comparison
   (``agentic_confidence_threshold``) is a plain numeric comparison,
   so an out-of-range value from the LLM would silently invert the
   "is this answer good enough?" decision.

4. **An unavailable feature is a RESULT, not an exception.**
   ``AgenticRetrievalResult`` carries ``is_available`` plus an
   optional ``fallback_context``. When agentic retrieval is
   disabled, misconfigured, or fails, the caller receives a
   well-formed object holding the STANDARD Phase 4/5 context --
   never an exception. The endpoint then always returns 200 with
   whatever the best available answer was, which is the same
   degradation contract ``retrieve`` follows.

5. **Citations are indices into the evidence list.**
   An LLM asked to cite "sources" will happily invent identifiers.
   Numbering the evidence ``[1]..[N]`` in the prompt and accepting
   only integer indices that fall inside that range makes an invalid
   citation detectable and droppable, instead of a dangling
   reference the UI cannot render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Tuple

from peldrun.memory.domain.context import MemoryContext
from peldrun.memory.domain.entry import MemoryEntry


logger = logging.getLogger(
    "peldrun.memory.agentic.contracts"
)


# ---------------------------------------------------------------------------
# Search protocol
# ---------------------------------------------------------------------------


class MemorySearchProtocol(Protocol):
    """The single search surface the agentic pipeline depends on.

    ``MemoryService.provider`` satisfies this structurally in BOTH
    of its shapes:

      * ``SqliteFtsProvider`` -- lexical only,
      * ``HybridMemoryProvider`` -- FTS + vector + graph fused.

    Declaring it here (rather than importing either class) means the
    agentic pipeline automatically inherits whatever improvement is
    made to the provider stack -- including the Phase 6 graph source
    -- without a single change in this package.
    """

    async def search_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[Tuple[MemoryEntry, float]]: ...


# ---------------------------------------------------------------------------
# Shared coercion
# ---------------------------------------------------------------------------


def coerce_confidence(value: Any) -> float:
    """Coerce an LLM-reported confidence into ``[0, 1]``.

    Two model mistakes are common enough to normalize rather than
    reject:

      * a 0-100 percentage instead of a 0-1 fraction
        (``95`` meaning ``0.95``),
      * rounding slop just above 1 (``1.04``).

    Bands (deliberately identical to
    ``peldrun.memory.graph.contracts._clamp_confidence`` so the two
    subsystems cannot disagree about what ``95`` means):

      * ``< 0``            -> ``0.0``
      * ``[0, 1]``         -> unchanged
      * ``(1, 2)``         -> ``1.0`` (rounding slop)
      * ``[2, 100]``       -> divided by 100 (percentage)
      * ``> 100``          -> ``1.0``
      * NaN / non-numeric  -> ``0.0``

    A trailing ``%`` on a string is honored. Booleans yield ``0.0``:
    ``True`` as a confidence is a schema mistake, and treating it as
    certainty would make a broken call look like a confident answer.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        try:
            numeric = float(text.rstrip("%"))
        except (TypeError, ValueError):
            return 0.0
        if text.endswith("%"):
            numeric = numeric / 100.0
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
    if numeric != numeric:  # NaN
        return 0.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        if 2.0 <= numeric <= 100.0:
            return round(numeric / 100.0, 4)
        return 1.0
    return numeric


# ---------------------------------------------------------------------------
# Synthesized answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesizedAnswer:
    """An answer produced by the evidence synthesizer.

    Attributes
    ----------
    answer
        The natural-language answer, grounded in the evidence. An
        EMPTY string is the documented failure value -- see
        :attr:`is_substantive`.
    confidence
        Model-reported confidence, clamped into ``[0, 1]``.
        ``0.0`` is also the failure value, so "the LLM failed" and
        "the LLM is certain there is no answer" are distinguishable
        only by :attr:`answer` being empty.
    citations
        1-BASED indices into the evidence list the synthesizer was
        given. Out-of-range indices are dropped at construction
        (invariant #5). Stored 1-based because that is how they are
        numbered in the prompt AND how a user reads them in the UI.
    """

    answer: str = ""
    confidence: float = 0.0
    citations: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """Normalize the answer text, confidence, and citations."""
        if not isinstance(self.answer, str):
            object.__setattr__(self, "answer", str(self.answer))

        numeric = coerce_confidence(self.confidence)
        if numeric != self.confidence:
            object.__setattr__(self, "confidence", numeric)

        if self.citations:
            cleaned: List[int] = []
            seen: set = set()
            for item in self.citations:
                if isinstance(item, bool):
                    continue
                try:
                    index = int(item)
                except (TypeError, ValueError):
                    continue
                if index < 1 or index in seen:
                    continue
                seen.add(index)
                cleaned.append(index)
            if tuple(cleaned) != tuple(self.citations):
                object.__setattr__(self, "citations", tuple(cleaned))

    @property
    def is_substantive(self) -> bool:
        """True when the synthesizer produced actual text.

        The single predicate callers should branch on: an empty
        answer means "nothing usable was synthesized", regardless of
        the confidence value.
        """
        return bool(self.answer.strip())

    def __repr__(self) -> str:
        preview = self.answer[:60].replace("\n", " ")
        return (
            f"SynthesizedAnswer(confidence={self.confidence:.2f}, "
            f"citations={len(self.citations)}, "
            f"answer={preview!r})"
        )


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapAnalysis:
    """The result of asking "is more evidence needed?".

    Attributes
    ----------
    needed
        True when the evidence gathered so far cannot answer the
        question and another hop is worthwhile.
    next_queries
        Concrete standalone queries to run on the next hop.
        Ignored when ``needed`` is False, and empty means "no idea
        what to look for" -- which the retriever treats exactly like
        ``needed=False`` (spinning on an empty query set would just
        burn a hop).

    The DEFAULT for a failed gap-analysis call is
    ``GapAnalysis(needed=False, next_queries=())``: when the model
    cannot tell us what is missing, the correct behaviour is to stop
    and answer with what we have, not to loop.
    """

    needed: bool = False
    next_queries: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.needed, bool):
            object.__setattr__(self, "needed", bool(self.needed))
        if self.next_queries:
            cleaned: List[str] = []
            seen: set = set()
            for item in self.next_queries:
                if not isinstance(item, str):
                    continue
                text = " ".join(item.split())
                if not text:
                    continue
                # Casefolded: "Who is Ahmed" and "who is ahmed" are
                # one query -- re-running a case variant returns the
                # same rows and wastes a hop.
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append(text)
            if tuple(cleaned) != tuple(self.next_queries):
                object.__setattr__(
                    self, "next_queries", tuple(cleaned)
                )
        # A "needed" analysis with no queries is a contradiction;
        # normalize it to "not needed" so the retriever's loop
        # condition has ONE meaning.
        if self.needed and not self.next_queries:
            object.__setattr__(self, "needed", False)

    @property
    def should_continue(self) -> bool:
        """True only when another hop would actually do something."""
        return self.needed and bool(self.next_queries)


# ---------------------------------------------------------------------------
# Multi-hop result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiHopResult:
    """Everything one multi-hop retrieval run produced.

    Attributes
    ----------
    evidence
        ``(MemoryEntry, score)`` pairs, best-first, deduplicated by
        ``memory_id`` across all hops. Scores come from whatever
        search backend ran, so they are only comparable WITHIN one
        result set (see the router/fusion notes on score scale).
    hops_used
        Number of retrieval rounds actually executed (>= 1 when at
        least one hop ran, 0 when the query was empty).
    sub_queries_used
        Every distinct sub-query the run searched for, in order.
        Exposed so the API response can show the operator what the
        rewriter and gap analysis decided -- the "why did it search
        that?" affordance.
    synthesized_answer
        The synthesized answer, or ``None`` when synthesis was
        skipped or failed.
    """

    evidence: Tuple[Tuple[MemoryEntry, float], ...] = ()
    hops_used: int = 0
    sub_queries_used: Tuple[str, ...] = ()
    synthesized_answer: Optional[SynthesizedAnswer] = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            object.__setattr__(
                self, "evidence", tuple(self.evidence or ())
            )
        if not isinstance(self.sub_queries_used, tuple):
            object.__setattr__(
                self,
                "sub_queries_used",
                tuple(self.sub_queries_used or ()),
            )
        if not isinstance(self.hops_used, int) or self.hops_used < 0:
            object.__setattr__(
                self, "hops_used", max(0, int(self.hops_used or 0))
            )

    @property
    def memory_ids(self) -> List[str]:
        """Memory ids in evidence order (diagnostics/tests)."""
        return [entry.memory_id for entry, _score in self.evidence]

    @property
    def is_empty(self) -> bool:
        """True when no evidence was found at all."""
        return not self.evidence

    def __repr__(self) -> str:
        return (
            f"MultiHopResult(hops={self.hops_used}, "
            f"evidence={len(self.evidence)}, "
            f"queries={len(self.sub_queries_used)}, "
            f"answer={'yes' if self.synthesized_answer else 'no'})"
        )


# ---------------------------------------------------------------------------
# Public agentic result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgenticRetrievalResult:
    """The public result of ``MemoryService.retrieve_agentic``.

    Always well-formed. See invariant #4: a disabled, misconfigured,
    timed-out, or failed agentic pipeline yields an instance with
    ``is_available=False``, ``error`` set, and ``fallback_context``
    holding the standard Phase 4/5 context -- never an exception.

    Attributes
    ----------
    is_available
        True when the agentic pipeline actually ran to completion.
    query
        The original question, echoed for logging and correlation.
    evidence
        Supporting ``(MemoryEntry, score)`` pairs. Empty on the
        fallback path (the caller should then read
        ``fallback_context``).
    synthesized_answer
        The answer, when one was produced. ``None`` on the fallback
        path and whenever synthesis failed while still returning
        evidence.
    hops_used, sub_queries_used
        Diagnostic breadcrumbs -- see ``MultiHopResult``.
    search_mode
        ``"agentic"`` for a completed run,
        ``"agentic+fallback"`` when the standard path was used
        instead.
    error
        A short machine-readable reason when the agentic path did
        not run (``"disabled"``, ``"timeout"``, ``"failed"``), else
        ``None``. Kept as a string rather than an exception so the
        result stays JSON-serializable for the HTTP layer.
    fallback_context
        The standard ``MemoryContext`` for the fallback path.
        ``None`` on a successful agentic run.
    confidence_threshold_met
        Whether the synthesized answer's confidence reached
        ``retrieval.agentic_confidence_threshold``. Surfaced so the
        caller can label a low-confidence answer rather than
        silently trusting it.
    elapsed_ms
        Wall-clock duration of the agentic run, in milliseconds.
        Useful in the API response and for cost/latency dashboards.
    """

    is_available: bool = False
    query: str = ""
    evidence: Tuple[Tuple[MemoryEntry, float], ...] = ()
    synthesized_answer: Optional[SynthesizedAnswer] = None
    hops_used: int = 0
    sub_queries_used: Tuple[str, ...] = ()
    search_mode: str = "agentic+fallback"
    error: Optional[str] = None
    fallback_context: Optional[MemoryContext] = None
    confidence_threshold_met: bool = False
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            object.__setattr__(
                self, "evidence", tuple(self.evidence or ())
            )
        if not isinstance(self.sub_queries_used, tuple):
            object.__setattr__(
                self,
                "sub_queries_used",
                tuple(self.sub_queries_used or ()),
            )

    @property
    def memories(self) -> List[MemoryEntry]:
        """The evidence entries only (no scores)."""
        return [entry for entry, _score in self.evidence]

    @property
    def memory_ids(self) -> List[str]:
        """Memory ids in evidence order."""
        return [entry.memory_id for entry, _score in self.evidence]

    @property
    def answer_text(self) -> str:
        """The synthesized answer text, or ``""``."""
        if self.synthesized_answer is None:
            return ""
        return self.synthesized_answer.answer

    @property
    def confidence(self) -> float:
        """The synthesized confidence, or ``0.0``."""
        if self.synthesized_answer is None:
            return 0.0
        return self.synthesized_answer.confidence

    @property
    def is_empty(self) -> bool:
        """True when the result carries neither answer nor memories."""
        if self.fallback_context is not None:
            return self.fallback_context.is_empty
        return not self.evidence and not self.answer_text

    def __repr__(self) -> str:
        return (
            f"AgenticRetrievalResult(available={self.is_available}, "
            f"mode={self.search_mode!r}, "
            f"evidence={len(self.evidence)}, "
            f"hops={self.hops_used}, "
            f"error={self.error!r})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "AgenticRetrievalResult",
    "GapAnalysis",
    "MemorySearchProtocol",
    "MultiHopResult",
    "SynthesizedAnswer",
]
