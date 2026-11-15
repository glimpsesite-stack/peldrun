# backend/omweb/memory/extraction/orchestrator.py
"""
PELDRUN Memory Platform - Phase 1-B: Extraction Orchestrator.

The pipeline that glues together everything from Phase 1-B:

    session_turns
        |
        v
    [range filter]              -- only turns in job.start..end
        |
        v
    [DataMinimizer]             -- strip secrets (T11)
        |
        v
    [user_prompt builder]       -- format turns with turn_index
        |
        v
    [prompts.build_extraction_system_prompt]
        |
        v
    [MemoryModelGateway]        -- call LLM, parse JSON
        |
        v
    [per-candidate validation]
        |-- malformed       -> rejected (decision)
        |-- low confidence  -> rejected (decision)
        |-- duplicate       -> rejected (decision)
        |-- no evidence     -> rejected (decision)
        |-- bad evidence    -> rejected (decision, T13)
        |-- good            -> accepted -> MemoryCandidate
        |
        v
    ExtractionResult(candidates, decisions, cost_nano_usd)

Design invariants
-----------------

1. **Pure pipeline, no persistence.**
   The orchestrator produces ``ExtractionResult`` (in-memory).
   The WRITE PIPELINE (Phase 1-C) turns candidates into
   ``MemoryEntry`` rows. Keeping orchestration and persistence
   separate means the orchestrator is fully testable without a
   database.

2. **Decisions are complete.**
   Every raw candidate from the LLM appears in ``decisions``
   exactly once, either ``accepted`` or ``rejected`` with a
   ``reason``. Consumers can therefore reconstruct the full
   LLM-output -> final-candidates filter without inspecting
   intermediate state. This is what makes T5 and T13
   diagnosable from the job's ``decisions_json`` column alone.

3. **Turn indexes are PRESERVED, not renumbered.**
   When a job covers turns 3..7, the LLM sees turn indexes
   3,4,5,6,7 (NOT 0,1,2,3,4). This is critical: the
   ``EvidenceValidator`` looks up ``session["turns"][turn_index]``,
   which must match the original session index, not the
   excerpt's index. Renumbering would silently break evidence
   validation for every non-zero start_turn_index.

4. **Dependencies are injectable.**
   Gateway, minimizer, validator are constructor-injected. Tests
   inject mocks; Phase 1-C injects real implementations. The
   orchestrator never imports a concrete transport.

5. **Empty result is a first-class outcome.**
   When the LLM returns zero candidates (common -- most turns
   contain nothing durable), ``extract()`` returns an empty
   ``ExtractionResult`` with zero cost. The caller (worker)
   treats this as success, advances the cursor, and moves on.
   There is NO exception for "nothing extracted".

6. **The LLM never sees the raw session.**
   Every message text passes through ``DataMinimizer`` BEFORE
   appearing in the user prompt. This is the LAST opportunity
   to redact secrets before they leave the process; a regression
   in the minimizer is caught by T11, not by an incident.

Why no write pipeline here
--------------------------

Converting ``MemoryCandidate`` to ``MemoryEntry`` involves:
  * building ``MemoryProvenance`` (source_type=EXTRACTION,
    source_chat_id, source_turn_id, source_run_id),
  * building ``ExtractionMeta`` (job_id, prompt_version,
    evidence_ids, confidence_at_creation),
  * resolving the entry's scope (job.scope_type / job.scope_id),
  * running the actual ``MemoryWritePipeline``.

That is substantial integration work and belongs to Phase 1-C,
where it can be tested against a real SQLite repository. The
orchestrator's contract stops at "candidate is valid and ready
to be written".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from omweb.memory.extraction.contracts import (
    Evidence,
    ExtractionResult,
    MemoryCandidate,
)
from omweb.memory.extraction.data_minimizer import MemoryDataMinimizer
from omweb.memory.extraction.gateway import (
    LLMParseError,
    LLMTransportError,
    MemoryModelGateway,
)
from omweb.memory.extraction.prompts import (
    build_extraction_system_prompt,
)
from omweb.memory.extraction.validator import EvidenceValidator
from omweb.memory.jobs.models import MemoryJob


logger = logging.getLogger("omweb.memory.extraction.orchestrator")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_MAX_CANDIDATES: int = 20
_DEFAULT_MAX_OUTPUT_TOKENS: int = 1200
_DEFAULT_TIMEOUT_SECONDS: int = 45


# ---------------------------------------------------------------------------
# Internal turn representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TurnData:
    """A single turn prepared for inclusion in the user prompt.

    ``turn_index`` is the ORIGINAL index in the session (see
    design invariant #3 in the module docstring). Never renumbered.
    """

    turn_index: int
    turn_id: Optional[str]
    user_text: str
    assistant_text: str


# ---------------------------------------------------------------------------
# ExtractionOrchestrator
# ---------------------------------------------------------------------------


class ExtractionOrchestrator:
    """Runs one extraction job end-to-end.

    See the module docstring for the pipeline overview and
    design invariants.

    Usage::

        orchestrator = ExtractionOrchestrator(
            gateway=MemoryModelGateway(transport=real_transport),
        )
        result = await orchestrator.extract(
            job=job,
            session=session_dict,
            recommended_source="session_json",
        )
        # `result.candidates` are ready for the write pipeline.
    """

    def __init__(
        self,
        *,
        gateway: Optional[MemoryModelGateway] = None,
        minimizer: Optional[MemoryDataMinimizer] = None,
        validator: Optional[EvidenceValidator] = None,
        max_candidates_per_job: int = _DEFAULT_MAX_CANDIDATES,
        min_confidence: float = 0.0,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Parameters
        ----------
        gateway
            LLM gateway. When ``None``, a default instance is
            constructed. In tests, pass
            ``MemoryModelGateway(transport=mock)``.
        minimizer
            Secret-redaction component. Defaults to
            ``MemoryDataMinimizer()``.
        validator
            Evidence validator. Defaults to ``EvidenceValidator()``.
        max_candidates_per_job
            Hard cap on candidates per job. Substituted into the
            system prompt via ``{max_candidates}``.
        min_confidence
            Candidates with ``confidence < min_confidence`` are
            rejected. Default 0.0 (permissive -- Phase 1-B does
            not gate on confidence). Phase 5 will raise this to
            route low-confidence candidates to a review queue.
        max_output_tokens
            Passed to the gateway as the completion token cap.
        timeout_seconds
            Passed to the gateway as the per-call timeout.
        """
        self._gateway = gateway or MemoryModelGateway()
        self._minimizer = minimizer or MemoryDataMinimizer()
        self._validator = validator or EvidenceValidator()
        self._max_candidates = int(max_candidates_per_job)
        self._min_confidence = float(min_confidence)
        self._max_output_tokens = int(max_output_tokens)
        self._timeout_seconds = int(timeout_seconds)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def extract(
        self,
        *,
        job: MemoryJob,
        session: Dict[str, Any],
        recommended_source: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> ExtractionResult:
        """Run one extraction job against one session.

        Parameters
        ----------
        job
            The ``MemoryJob`` being processed. Its
            ``start_turn_index`` / ``end_turn_index`` bound the
            range that is read; its ``model_provider_id`` /
            ``model_id`` select the LLM.
        session
            The parsed session dict. Must be the format indicated
            by ``recommended_source``:
              * ``"session_json"`` -> contains ``turns[]``
              * ``"session_context_json"`` -> contains ``conversation[]``
        recommended_source
            Which of the two session formats this is.
        api_key
            Optional override; the transport resolves credentials
            from the model vault when this is None.
        base_url
            Optional override for the provider endpoint.

        Returns
        -------
        ExtractionResult
            Accepted candidates, per-candidate decision log, LLM
            cost in nano-USD.

        Raises
        ------
        LLMTransportError
            The gateway could not obtain an LLM response. The
            worker treats this as retryable.
        LLMParseError
            The gateway obtained a response but it could not be
            parsed as a JSON object. Also retryable -- a second
            sampling attempt often succeeds.
        """
        # --- 1. Prepare turns (filter by range, extract text) ------
        turns_data = self._prepare_turns(
            job=job,
            session=session,
            recommended_source=recommended_source,
        )

        if not turns_data:
            logger.debug(
                "Job %s: no turns in range [%s..%s]; returning "
                "empty result.",
                job.job_id,
                job.start_turn_index,
                job.end_turn_index,
            )
            return ExtractionResult(
                candidates=[],
                decisions=[],
                cost_nano_usd=0,
            )

        # --- 2. Minimize secrets BEFORE anything leaves process ----
        minimized_turns = self._minimize_turns(turns_data)

        # --- 3. Build prompts --------------------------------------
        user_prompt = self._build_user_prompt(minimized_turns)
        system_prompt = build_extraction_system_prompt(
            max_candidates=self._max_candidates,
        )

        # --- 4. Call LLM via gateway -------------------------------
        logger.debug(
            "Job %s: invoking LLM (%s/%s) with %d turn(s).",
            job.job_id,
            job.model_provider_id,
            job.model_id,
            len(minimized_turns),
        )
        response = await self._gateway.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider_id=job.model_provider_id,
            model_id=job.model_id,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            connection_id=job.job_id,
        )

        # --- 5. Parse, validate, build candidates ------------------
        return self._process_response(
            response=response,
            session=session,
            recommended_source=recommended_source,
            job=job,
        )

    # ------------------------------------------------------------------
    # Turn preparation
    # ------------------------------------------------------------------

    def _prepare_turns(
        self,
        *,
        job: MemoryJob,
        session: Dict[str, Any],
        recommended_source: str,
    ) -> List[_TurnData]:
        """Extract the job's turn range from the session.

        Delegates to the format-specific helper. Both helpers
        preserve the ORIGINAL turn_index (see invariant #3).
        """
        if recommended_source == "session_json":
            return self._prepare_from_session_json(job, session)
        if recommended_source == "session_context_json":
            return self._prepare_from_context_json(job, session)
        raise ValueError(
            f"Unknown recommended_source: {recommended_source!r}; "
            f"expected 'session_json' or 'session_context_json'"
        )

    def _prepare_from_session_json(
        self,
        job: MemoryJob,
        session: Dict[str, Any],
    ) -> List[_TurnData]:
        """Extract turns from a `session.json` document.

        `session["turns"]` is a list of
        ``{id, user, assistant, timestamp, ...}`` objects.
        """
        turns = session.get("turns")
        if not isinstance(turns, list):
            return []

        total = len(turns)
        if total == 0:
            return []

        start = (
            job.start_turn_index
            if job.start_turn_index is not None else 0
        )
        end = (
            job.end_turn_index
            if job.end_turn_index is not None else total - 1
        )
        start = max(0, start)
        end = min(end, total - 1)

        result: List[_TurnData] = []
        for idx in range(start, end + 1):
            turn = turns[idx]
            if not isinstance(turn, dict):
                continue
            user_text = str(turn.get("user") or "").strip()
            assistant_text = str(turn.get("assistant") or "").strip()
            if not user_text and not assistant_text:
                continue
            raw_id = turn.get("turn_id") or turn.get("id")
            turn_id = (
                raw_id.strip()
                if isinstance(raw_id, str) and raw_id.strip()
                else None
            )
            result.append(_TurnData(
                turn_index=idx,
                turn_id=turn_id,
                user_text=user_text,
                assistant_text=assistant_text,
            ))
        return result

    def _prepare_from_context_json(
        self,
        job: MemoryJob,
        session: Dict[str, Any],
    ) -> List[_TurnData]:
        """Extract turns from a `session.context.json` sidecar.

        `session["conversation"]` is a FLAT list of
        ``{role, content}`` messages that alternate
        user/assistant. Turn T is at indices ``2*T`` and
        ``2*T+1``.
        """
        conversation = session.get("conversation")
        if not isinstance(conversation, list):
            return []

        total_turns = len(conversation) // 2
        if total_turns == 0:
            return []

        start = (
            job.start_turn_index
            if job.start_turn_index is not None else 0
        )
        end = (
            job.end_turn_index
            if job.end_turn_index is not None
            else total_turns - 1
        )
        start = max(0, start)
        end = min(end, total_turns - 1)

        result: List[_TurnData] = []
        for idx in range(start, end + 1):
            user_idx = 2 * idx
            assistant_idx = 2 * idx + 1
            user_text = ""
            assistant_text = ""
            if user_idx < len(conversation):
                msg = conversation[user_idx]
                if isinstance(msg, dict) and msg.get("role") == "user":
                    user_text = str(msg.get("content") or "").strip()
            if assistant_idx < len(conversation):
                msg = conversation[assistant_idx]
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "assistant"
                ):
                    assistant_text = str(msg.get("content") or "").strip()
            if not user_text and not assistant_text:
                continue
            result.append(_TurnData(
                turn_index=idx,
                turn_id=None,  # context.json carries no turn_ids
                user_text=user_text,
                assistant_text=assistant_text,
            ))
        return result

    # ------------------------------------------------------------------
    # Minimization
    # ------------------------------------------------------------------

    def _minimize_turns(
        self, turns: List[_TurnData]
    ) -> List[_TurnData]:
        """Apply the minimizer to every turn's content.

        Preserves turn_index and turn_id. Applies the minimizer
        to user and assistant text independently.
        """
        result: List[_TurnData] = []
        for t in turns:
            result.append(_TurnData(
                turn_index=t.turn_index,
                turn_id=t.turn_id,
                user_text=self._minimizer.minimize(t.user_text),
                assistant_text=self._minimizer.minimize(
                    t.assistant_text
                ),
            ))
        return result

    # ------------------------------------------------------------------
    # User prompt builder
    # ------------------------------------------------------------------

    def _build_user_prompt(
        self, turns: List[_TurnData]
    ) -> str:
        """Render turns into the user-facing prompt.

        Format is deliberately simple:

            === Turn 3 ===
            [user]: ...
            [assistant]: ...

            === Turn 4 ===
            ...

        The turn index is visible so the LLM can cite it in
        evidence. Roles are labeled because most chat logs
        interleave user/assistant and the LLM must know which
        side said what to categorize (preferences vs. answers).
        """
        parts: List[str] = []
        for t in turns:
            parts.append(f"=== Turn {t.turn_index} ===")
            if t.user_text:
                parts.append(f"[user]: {t.user_text}")
            if t.assistant_text:
                parts.append(f"[assistant]: {t.assistant_text}")
            parts.append("")  # blank line between turns
        return "\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Response processing
    # ------------------------------------------------------------------

    def _process_response(
        self,
        *,
        response,  # LLMStructuredResponse
        session: Dict[str, Any],
        recommended_source: str,
        job: MemoryJob,
    ) -> ExtractionResult:
        """Parse + validate + build the final ExtractionResult."""
        decisions: List[Dict[str, Any]] = []
        accepted: List[MemoryCandidate] = []

        raw_candidates = response.parsed.get("candidates")
        if raw_candidates is None:
            # Schema allows "candidates": [] but not omitting the
            # key entirely. A model that omits the key is either
            # confused or its output was truncated -- treat as
            # empty rather than as an error (retrying rarely
            # helps and the worker's retry policy will handle
            # persistent empties).
            return ExtractionResult(
                candidates=[],
                decisions=[{
                    "action": "rejected",
                    "reason": "malformed_response",
                    "detail": (
                        "response.parsed is missing 'candidates' key"
                    ),
                }],
                cost_nano_usd=response.cost_nano_usd,
            )

        if not isinstance(raw_candidates, list):
            return ExtractionResult(
                candidates=[],
                decisions=[{
                    "action": "rejected",
                    "reason": "malformed_response",
                    "detail": (
                        f"'candidates' is not a list "
                        f"(got {type(raw_candidates).__name__})"
                    ),
                }],
                cost_nano_usd=response.cost_nano_usd,
            )

        seen_contents: set[tuple[str, str]] = set()

        for idx, raw in enumerate(raw_candidates):
            decision = self._validate_single_candidate(
                idx=idx,
                raw=raw,
                session=session,
                recommended_source=recommended_source,
                seen_contents=seen_contents,
            )
            if decision[0] == "accepted":
                accepted.append(decision[1])
                decisions.append({
                    "candidate_index": idx,
                    "action": "accepted",
                    "reason": "ok",
                    "detail": "",
                })
            else:
                decisions.append({
                    "candidate_index": idx,
                    "action": "rejected",
                    "reason": decision[1],
                    "detail": decision[2],
                })

        logger.info(
            "Job %s: %d/%d candidate(s) accepted.",
            job.job_id,
            len(accepted),
            len(raw_candidates),
        )

        return ExtractionResult(
            candidates=accepted,
            decisions=decisions,
            cost_nano_usd=response.cost_nano_usd,
        )

    def _validate_single_candidate(
        self,
        *,
        idx: int,
        raw: Any,
        session: Dict[str, Any],
        recommended_source: str,
        seen_contents: set[tuple[str, str]],
    ) -> tuple[str, Any, str]:
        """Validate one raw candidate from the LLM.

        Returns
        -------
        tuple[str, Any, str]
            ``("accepted", MemoryCandidate, "")`` on success.
            ``("rejected", reason_code, detail)`` on rejection.
        """
        # --- Structural check: dict -------------------------------
        if not isinstance(raw, dict):
            return (
                "rejected",
                "malformed_candidate",
                f"not a dict (got {type(raw).__name__})",
            )

        # --- Build Evidence ---------------------------------------
        evidence_obj: Optional[Evidence] = None
        raw_evidence = raw.get("evidence")
        if raw_evidence is not None:
            if not isinstance(raw_evidence, dict):
                return (
                    "rejected",
                    "malformed_evidence",
                    f"evidence not a dict "
                    f"(got {type(raw_evidence).__name__})",
                )
            try:
                evidence_obj = Evidence(**raw_evidence)
            except (TypeError, ValueError) as exc:
                return (
                    "rejected",
                    "invalid_evidence",
                    str(exc)[:200],
                )

        # --- Build MemoryCandidate --------------------------------
        try:
            candidate = MemoryCandidate(
                content=raw.get("content", ""),
                memory_type=raw.get("memory_type", ""),
                importance=raw.get("importance", 50),
                confidence=raw.get("confidence", 1.0),
                tags=raw.get("tags", []),
                evidence=evidence_obj,
            )
        except (TypeError, ValueError) as exc:
            return (
                "rejected",
                "invalid_candidate",
                str(exc)[:200],
            )

        # --- Confidence threshold ---------------------------------
        if candidate.confidence < self._min_confidence:
            return (
                "rejected",
                "below_min_confidence",
                f"confidence={candidate.confidence:.3f} < "
                f"{self._min_confidence:.3f}",
            )

        # --- Deduplication within this job ------------------------
        dedup_key = (
            candidate.content.lower(),
            candidate.memory_type.value,
        )
        if dedup_key in seen_contents:
            return (
                "rejected",
                "duplicate_candidate",
                "same content+type as an earlier candidate",
            )
        seen_contents.add(dedup_key)

        # --- Evidence is mandatory --------------------------------
        if candidate.evidence is None:
            return (
                "rejected",
                "no_evidence",
                "candidate has no evidence object",
            )

        # --- Verbatim evidence validation (T13) -------------------
        validation = self._validator.validate(
            candidate.evidence,
            source=session,
            recommended_source=recommended_source,
        )
        if not validation.ok:
            return (
                "rejected",
                validation.reason,
                validation.detail,
            )

        return ("accepted", candidate, "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["ExtractionOrchestrator"]
