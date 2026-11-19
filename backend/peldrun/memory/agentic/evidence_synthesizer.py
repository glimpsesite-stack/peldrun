# backend/peldrun/memory/agentic/evidence_synthesizer.py
"""
PELDRUN Memory Platform - Agentic Evidence Synthesizer.

Two LLM-backed judgements that bracket every hop of an agentic
retrieval run:

    * :meth:`EvidenceSynthesizer.identify_gaps` -- "is the evidence
      I have enough, and if not, what should I search for next?".
      Called BEFORE each additional hop.
    * :meth:`EvidenceSynthesizer.synthesize` -- "answer the question
      from this evidence, and tell me how sure you are". Called
      ONCE, after the last hop.

Why a separate class from the retriever
---------------------------------------
The retriever owns the LOOP (how many hops, which queries, what
constitutes progress). The synthesizer owns the JUDGEMENTS the loop
branches on. Keeping them apart means the loop can be tested against
scripted judgements (``GapAnalysis(needed=True, ...)`` then
``needed=False``) with no LLM at all, and the prompts can be revised
without touching control flow.

Design invariants
-----------------

1. **Evidence is numbered, and citations are indices.**
   The prompt renders evidence as ``[1] ... [2] ...``. The model is
   asked for the indices it used. An LLM asked to cite document ids
   will invent plausible-looking ones; an integer range is
   mechanically verifiable, so an out-of-range citation is dropped
   instead of surfacing as a broken reference. See
   ``SynthesizedAnswer`` invariant #5.

2. **Honest insufficiency beats confident invention.**
   The prompt explicitly authorises the model to say the evidence is
   insufficient, and the fallback answer for a failed call is EMPTY
   with confidence 0.0. An agentic pipeline whose failure mode is
   "made something up" is worse than one that returns nothing --
   the caller can always fall back to raw evidence (which is still
   returned alongside the answer), but cannot detect a fabrication.

3. **Every failure is a well-formed default.**
   ``synthesize`` -> ``SynthesizedAnswer()`` (empty, 0.0, no
   citations). ``identify_gaps`` -> ``GapAnalysis(needed=False)``,
   which STOPS the loop. Looping forever because the model is down
   would multiply cost with no chance of progress.

4. **The evidence prompt is bounded.**
   Only ``max_evidence`` entries, each truncated to
   ``max_evidence_chars``, are rendered. A question that matched 200
   memories must not produce a 100k-token synthesis prompt.

See also
--------

    * ``peldrun/memory/agentic/contracts.py`` -- ``SynthesizedAnswer``,
      ``GapAnalysis``
    * ``peldrun/memory/agentic/multi_hop_retriever.py`` -- the loop
      that calls both
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence, Tuple

from peldrun.memory.agentic.contracts import (
    GapAnalysis,
    SynthesizedAnswer,
    coerce_confidence,
)
from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.llm_contracts import (
    StructuredGatewayProtocol,
    call_structured,
    first_present,
)


logger = logging.getLogger(
    "peldrun.memory.agentic.evidence_synthesizer"
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYNTHESIZE_PROMPT: str = """Answer the question using ONLY the evidence below.

Question: {query}

Evidence:
{evidence}

Return ONLY JSON, in this shape (replace the placeholders, do not copy them):
{{"answer": <the answer text>, "confidence": <0.0 to 1.0>, "citations": [<evidence numbers used>]}}

Rules:
- Every claim must be supported by the evidence. Do not add outside knowledge.
- "citations" lists the evidence numbers you actually used.
- confidence is 0.0 (no idea) to 1.0 (certain).
- ANSWER IF YOU CAN. If ANY evidence item is related to the question, state what
  it says, even partially. One supported sentence is a valid answer.
- An empty "answer" is ONLY acceptable when the evidence is entirely unrelated
  to the question. Do not return "" merely because the match is imperfect or
  the evidence is brief.
- Never return an empty answer while also citing evidence numbers -- that is
  self-contradictory. If you cite, you answer.
- Be concise: 1-4 sentences.
"""


GAP_PROMPT: str = """Decide whether the evidence below is enough to answer the question.

Question: {query}

Evidence:
{evidence}

Return ONLY JSON, in this shape (replace the placeholders, do not copy them):
{{"needed": <true or false>, "next_queries": [<up to 2 standalone search queries>]}}

Rules:
- "needed" is true ONLY if a further memory search could plausibly add the missing facts.
- Each entry in "next_queries" must be a real, standalone, searchable query.
- NEVER introduce a topic that does not appear in the question. Every next
  query must be a restatement or a narrower refinement of the question
  itself -- not a new subject taken from the evidence.
- Never emit placeholder text such as "query 1" or "sub-query 2".
- Maximum 2 next queries.
- If the evidence already answers the question, return {{"needed": false, "next_queries": []}}.
- If the question is conversational (a greeting, a thank-you, an
  acknowledgement, a farewell) and asks for no fact, return
  {{"needed": false, "next_queries": []}}.
"""


SYNTHESIZE_SYSTEM_PROMPT: str = (
    "You answer questions strictly from provided evidence and "
    "return only JSON."
)


GAP_SYSTEM_PROMPT: str = (
    "You decide whether more evidence is needed and return only JSON."
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_EVIDENCE: int = 20
"""Evidence entries rendered into a prompt.

20 is roughly ``default_limit * 4`` -- the window the ranker and
budget fitter already consider -- and keeps the synthesis prompt
under ~2k tokens for typical memories.
"""


DEFAULT_MAX_EVIDENCE_CHARS: int = 400
"""Per-entry truncation inside the evidence block.

The identity-bearing part of a memory is its opening. 400 chars
keeps 20 entries at ~8k characters (~2k tokens) while rarely losing
the fact that answers a question.
"""


DEFAULT_MAX_NEXT_QUERIES: int = 2
"""Cap on gap-analysis next queries.

Two keeps the NEXT hop's search count bounded (2 queries x N hops);
a model that proposes five refinements is usually padding.
"""


_SYNTHESIZE_CONNECTION_ID: str = "memory_evidence_synthesis"
_GAP_CONNECTION_ID: str = "memory_gap_analysis"


# ---------------------------------------------------------------------------
# EvidenceSynthesizer
# ---------------------------------------------------------------------------


class EvidenceSynthesizer:
    """Produces grounded answers and next-hop gap analyses.

    Stateless; safe to share across concurrent requests.
    """

    def __init__(
        self,
        gateway: Optional[StructuredGatewayProtocol],
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        max_output_tokens: int = 900,
        timeout_seconds: float = 10.0,
        max_evidence: int = DEFAULT_MAX_EVIDENCE,
        max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS,
        max_next_queries: int = DEFAULT_MAX_NEXT_QUERIES,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        synthesize_connection_id: str = _SYNTHESIZE_CONNECTION_ID,
        gap_connection_id: str = _GAP_CONNECTION_ID,
    ) -> None:
        """Construct the synthesizer.

        Parameters
        ----------
        gateway
            Anything satisfying ``StructuredGatewayProtocol``.
            ``None`` makes both methods return their documented
            defaults.
        provider_id, model_id
            The synthesis model. This is the component where answer
            QUALITY matters most, so an operator may deliberately
            point it at a stronger model than the rewriter uses --
            hence the separate instances ``MemoryService`` builds.
        max_output_tokens
            900 fits a four-sentence answer plus citations.
        timeout_seconds
            Deadline for ONE call. Looser than the rewriter's 5s
            because the answer is what the user actually reads.
        max_evidence, max_evidence_chars, max_next_queries
            Prompt-size and fan-out bounds.
        api_key, base_url
            Optional overrides.
        synthesize_connection_id, gap_connection_id
            Distinct cost/log labels so an operator can tell which
            judgement spent the tokens.
        """
        self._gateway = gateway
        self._provider_id = (provider_id or "").strip()
        self._model_id = (model_id or "").strip()
        self._max_output_tokens = _positive_int(
            max_output_tokens, 900
        )
        self._timeout_seconds = _positive_float(
            timeout_seconds, 10.0
        )
        self._max_evidence = _positive_int(
            max_evidence, DEFAULT_MAX_EVIDENCE
        )
        self._max_evidence_chars = _positive_int(
            max_evidence_chars, DEFAULT_MAX_EVIDENCE_CHARS
        )
        self._max_next_queries = _positive_int(
            max_next_queries, DEFAULT_MAX_NEXT_QUERIES
        )
        self._api_key = api_key
        self._base_url = base_url
        self._synthesize_connection_id = (
            synthesize_connection_id or _SYNTHESIZE_CONNECTION_ID
        ).strip() or _SYNTHESIZE_CONNECTION_ID
        self._gap_connection_id = (
            gap_connection_id or _GAP_CONNECTION_ID
        ).strip() or _GAP_CONNECTION_ID

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        query: str,
        evidence: Sequence[Tuple[MemoryEntry, float]],
    ) -> SynthesizedAnswer:
        """Answer ``query`` from ``evidence``.

        Returns
        -------
        SynthesizedAnswer
            ``SynthesizedAnswer()`` -- empty text, confidence 0.0, no
            citations -- for EVERY failure mode, including the case
            where there is no evidence at all (nothing to
            synthesize; calling the LLM would only invite
            hallucination).

        Never raises (``CancelledError`` excepted).
        """
        text = query.strip() if isinstance(query, str) else ""
        if not text or not evidence:
            return SynthesizedAnswer()
        if not self.is_configured:
            logger.debug(
                "EvidenceSynthesizer: not configured; returning an "
                "empty answer (evidence is still available to the "
                "caller)."
            )
            return SynthesizedAnswer()

        block, count = self._render_evidence(evidence)
        if not block:
            return SynthesizedAnswer()

        parsed = await call_structured(
            self._gateway,
            system_prompt=SYNTHESIZE_SYSTEM_PROMPT,
            user_prompt=SYNTHESIZE_PROMPT.format(
                query=text, evidence=block
            ),
            provider_id=self._provider_id,
            model_id=self._model_id,
            api_key=self._api_key,
            base_url=self._base_url,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            connection_id=self._synthesize_connection_id,
        )
        if parsed is None:
            return SynthesizedAnswer()

        raw_answer = first_present(parsed, "answer", "response", "text")
        if not isinstance(raw_answer, str):
            return SynthesizedAnswer()

        answer_text = raw_answer.strip()

        # ----------------------------------------------------------
        # Empty answer WITH evidence present
        # ----------------------------------------------------------
        #
        # A small model asked to be honest about insufficiency takes
        # the escape hatch far too eagerly: it returns
        # ``{"answer": "", "confidence": 0.0, "citations": [1, 2]}``
        # -- an answer that cites two sources it declined to use.
        # The UI then shows "No supported answer in memory" while the
        # citations prove otherwise, which is worse than a terse
        # answer: the user cannot tell a retrieval failure from a
        # model failure.
        #
        # So: when the model cites evidence but writes nothing,
        # fall back to an EXTRACTIVE answer built from the very
        # evidence it cited. It is grounded by construction (the text
        # is copied from stored memories), it keeps citations honest,
        # and confidence stays LOW because no synthesis was performed.
        # The flag lets the API/UI label it as extractive.
        if not answer_text:
            extractive = self._extractive_answer(evidence, parsed)
            if extractive is None:
                return SynthesizedAnswer()
            answer_text, fallback_citations = extractive
            return SynthesizedAnswer(
                answer=answer_text,
                # Deliberately low: this is a quotation, not reasoning.
                confidence=0.25,
                citations=fallback_citations,
            )

        raw_confidence = first_present(
            parsed, "confidence", "score", "certainty"
        )
        raw_citations = first_present(
            parsed, "citations", "sources", "indices"
        )

        return SynthesizedAnswer(
            answer=answer_text,
            confidence=coerce_confidence(raw_confidence),
            citations=self._clean_citations(raw_citations, count),
        )

    @staticmethod
    def _extractive_answer(
        evidence: Sequence[Tuple[MemoryEntry, float]],
        parsed: Any,
    ) -> Optional[Tuple[str, List[int]]]:
        """Build a grounded answer from the evidence itself.

        Used when the model produced NO answer text but DID cite
        indices. Returns ``None`` when the model cited nothing (a
        genuine "no support" verdict we must respect) or when there
        is no usable evidence text.

        The result quotes stored memory content verbatim -- nothing is
        generated, so every sentence is supported by definition.
        """
        if not evidence:
            return None

        raw_citations = first_present(
            parsed, "citations", "sources", "indices"
        )

        indices: List[int] = []
        if isinstance(raw_citations, (list, tuple)):
            for item in raw_citations:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                # 1-based, as rendered in the prompt.
                if 1 <= value <= len(evidence):
                    indices.append(value)

        if not indices:
            return None

        snippets: List[str] = []
        for index in indices[:3]:
            content = getattr(
                evidence[index - 1][0], "content", ""
            )
            text = str(content).strip()
            if text:
                snippets.append(text)

        if not snippets:
            return None

        if len(snippets) == 1:
            body = f"Based on your saved memories: {snippets[0]}"
        else:
            joined = " ".join(
                f"({position}) {snippet}"
                for position, snippet in enumerate(snippets, start=1)
            )
            body = f"Based on your saved memories: {joined}"

        return body, indices[:3]

    async def identify_gaps(
        self,
        query: str,
        evidence: Sequence[Tuple[MemoryEntry, float]],
    ) -> GapAnalysis:
        """Decide whether another hop is worthwhile.

        Returns
        -------
        GapAnalysis
            ``GapAnalysis(needed=False)`` -- STOP the loop -- for
            every failure mode. The asymmetry with ``synthesize`` is
            deliberate: synthesizing with partial evidence still
            produces something useful, whereas looping without a
            coherent plan only multiplies cost.

        Never raises (``CancelledError`` excepted).
        """
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            return GapAnalysis()
        if not evidence:
            # No evidence at all: the FIRST hop already came back
            # empty. Rather than ask the model what is missing, stop
            # -- a second identical search is the most likely
            # outcome, and the caller reports "nothing found".
            return GapAnalysis()
        if not self.is_configured:
            return GapAnalysis()

        block, _count = self._render_evidence(evidence)
        if not block:
            return GapAnalysis()

        parsed = await call_structured(
            self._gateway,
            system_prompt=GAP_SYSTEM_PROMPT,
            user_prompt=GAP_PROMPT.format(query=text, evidence=block),
            provider_id=self._provider_id,
            model_id=self._model_id,
            api_key=self._api_key,
            base_url=self._base_url,
            max_output_tokens=min(self._max_output_tokens, 400),
            timeout_seconds=self._timeout_seconds,
            connection_id=self._gap_connection_id,
        )
        if parsed is None:
            return GapAnalysis()

        raw_needed = first_present(parsed, "needed", "required", "more")
        needed = _coerce_bool(raw_needed)

        raw_queries = first_present(
            parsed, "next_queries", "queries", "missing"
        )
        next_queries = self._clean_queries(raw_queries)

        # ``GapAnalysis`` normalizes the contradiction
        # needed=True with no queries into needed=False.
        return GapAnalysis(needed=needed, next_queries=next_queries)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _render_evidence(
        self,
        evidence: Sequence[Tuple[MemoryEntry, float]],
    ) -> Tuple[str, int]:
        """Render the numbered evidence block.

        Returns ``(block, count)`` where ``count`` is the number of
        entries actually rendered -- the valid citation range is
        ``1..count``, which is what makes range-checking possible.

        Entries are numbered in the ORDER GIVEN (best-first), because
        the ranker already decided what is most relevant and the
        model should see that prioritization.
        """
        lines: List[str] = []
        count = 0
        for index, item in enumerate(evidence, start=1):
            if count >= self._max_evidence:
                break
            entry = item[0] if isinstance(item, tuple) else item
            if entry is None:
                continue
            content = getattr(entry, "content", "")
            if not isinstance(content, str):
                content = str(content)
            content = " ".join(content.split())
            if not content:
                continue
            if len(content) > self._max_evidence_chars:
                content = (
                    content[: self._max_evidence_chars].rstrip()
                    + "..."
                )
            count += 1
            lines.append(f"[{count}] {content}")
        return "\n".join(lines), count

    def _clean_citations(
        self,
        raw: Any,
        evidence_count: int,
    ) -> Tuple[int, ...]:
        """Keep only in-range, deduplicated 1-based citation indices."""
        if not isinstance(raw, list):
            return ()
        kept: List[int] = []
        seen: set = set()
        for item in raw:
            if isinstance(item, bool):
                continue
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if index < 1 or index > evidence_count:
                continue
            if index in seen:
                continue
            seen.add(index)
            kept.append(index)
        return tuple(kept)

    def _clean_queries(self, raw: Any) -> Tuple[str, ...]:
        """Normalize and bound the gap analysis' next queries."""
        if not isinstance(raw, list):
            return ()
        cleaned: List[str] = []
        seen: set = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            text = " ".join(item.split())
            if not text:
                continue
            if len(text) > 300:
                text = text[:300].rstrip()
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
            if len(cleaned) >= self._max_next_queries:
                break
        return tuple(cleaned)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when a gateway and a resolvable model are present."""
        return (
            self._gateway is not None
            and bool(self._provider_id)
            and bool(self._model_id)
        )

    @property
    def provider_id(self) -> str:
        """The resolved provider id (empty string when unset).

        Public because ``MemoryService.reload_settings`` needs it to
        detect a model change and rebuild the pipeline. Reading the
        private field from outside would have been a silent failure
        mode: an ``AttributeError`` raised inside the reload block is
        swallowed by its ``except`` and the whole re-evaluation is
        skipped.
        """
        return self._provider_id

    @property
    def model_id(self) -> str:
        """The resolved model id (empty string when unset)."""
        return self._model_id

    @property
    def timeout_seconds(self) -> float:
        """The per-call deadline."""
        return self._timeout_seconds

    @property
    def max_evidence(self) -> int:
        """Evidence entries rendered per prompt."""
        return self._max_evidence

    def __repr__(self) -> str:
        return (
            f"EvidenceSynthesizer(provider={self._provider_id!r}, "
            f"model={self._model_id!r}, "
            f"max_evidence={self._max_evidence}, "
            f"timeout={self._timeout_seconds}s, "
            f"configured={self.is_configured})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    """Coerce an LLM-reported boolean.

    Models answer ``true``, ``"true"``, ``"yes"``, and ``1``
    interchangeably; anything unrecognized is ``False`` (the safe
    choice -- it stops the loop rather than extending it).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {
            "true", "yes", "1", "y", "needed", "required",
        }
    return False


def _positive_int(value: Any, default: int) -> int:
    """Coerce to a positive int, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


def _positive_float(value: Any, default: float) -> float:
    """Coerce to a positive float, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if numeric != numeric or numeric <= 0.0:
        return default
    return numeric


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_MAX_EVIDENCE_CHARS",
    "DEFAULT_MAX_NEXT_QUERIES",
    "GAP_PROMPT",
    "SYNTHESIZE_PROMPT",
    "EvidenceSynthesizer",
]
