# backend/omweb/memory/extraction/validator.py
"""
PELDRUN Memory Platform - Phase 1-B: Evidence Validation.

Verifies that every ``MemoryCandidate.evidence.snippet`` is a
VERBATIM substring of the source message it claims to cite.
Rejects paraphrased, hallucinated, or mistranslated snippets
before they can become a persisted memory.

Why this is the single most important file in the extraction pipeline
---------------------------------------------------------------------

LLMs hallucinate. In the extraction context, a hallucination is a
candidate that "quotes" text the user never wrote. Every hallucinated
snippet that survives validation becomes a persisted memory that
the audit trail cannot justify -- and when the user asks "why do you
think that?", the UI shows evidence that does not match the source.
The trust of the entire memory subsystem depends on this file.

The validator implements the matching rules from §21.5 of the plan:

  1. Snippet length: 3..200 chars.
  2. Whitespace normalization: any run of whitespace is treated as
     a single space for comparison.
  3. Case normalization: comparison is case-insensitive.
  4. Exact content: after normalization, the snippet MUST be a
     substring of the source text. No fuzzy matching, no synonyms,
     no partial-word matches.

Anything not explicitly allowed by these four rules is REJECTED.
The validator does not attempt to "guess what the LLM meant".

Two evidence sources
--------------------

The source of truth depends on the session mode:

  * ``session_json`` -- regular chat. The evidence is validated
    against ``session.json``'s ``turns[]`` array. Each turn has
    ``user`` and ``assistant`` fields; ``message_role`` in the
    evidence selects which one (or the validator checks both
    when the role is absent).

  * ``session_context_json`` -- agent mode. The evidence is
    validated against ``session.context.json``'s
    ``conversation[]`` array, which is a FLAT list of user/
    assistant messages (filtered, no tool steps). For a turn
    index T, the user message is at index ``2*T`` and the
    assistant message at ``2*T + 1``.

The caller (`ExtractionOrchestrator`) decides which source to
use based on `SessionCandidate.recommended_evidence_source`
(Phase 1-C). This module only validates against whatever source
it is handed.

Design invariants
-----------------

1. **Validation is cheap.**
   No LLM calls, no token counting, no disk I/O. The source
   dict is already in memory when `validate` is called; the
   only work is dict lookup and regex-based normalization.

2. **Rejection is granular.**
   Each rejection returns a `ValidationResult` with:
     * a boolean `ok`,
     * a short `reason` code (used by the decisions log),
     * a human-readable `detail` (used in logs and, later, in
       the review queue).

   The caller logs all three, so a surge of rejections has an
   obvious diagnostic signal.

3. **No exceptions leak.**
   A malformed source (missing `turns`, non-dict turn entry,
   non-string content) is caught and returned as a rejection
   with ``reason="source_malformed"``. The orchestrator never
   has to wrap `validate` in a try/except.

4. **`message_role=None` is checked against BOTH sides of a turn.**
   When the LLM does not report which speaker the snippet came
   from, the validator accepts the evidence if the snippet
   matches EITHER the user OR the assistant content of the turn.
   This is more forgiving than requiring the LLM to always
   report the role correctly, while still requiring the snippet
   to be a literal substring of one of the two actual messages.

5. **`turn_id` cross-check is OPTIONAL.**
   When the evidence includes a `turn_id` AND the source turn
   has an id-like field (`id` or `turn_id` -- both appear in the
   current session format), the validator enforces equality.
   When either side is missing, the check is skipped. This
   avoids failing validation on sessions written before
   turn_ids were recorded.

6. **Chat-mode fallback MUST mirror the orchestrator.**
   The orchestrator (``_prepare_from_session_json``) and the
   validator both apply a fallback to session-level
   ``prompt`` / ``result`` when the turn's per-turn fields
   are empty AND the turn is the last one. This fallback
   exists because ``dispatcher.run_direct_chat`` writes the
   user prompt and assistant reply to the SESSION top level,
   not to ``turns[]``.

   **The two fallbacks MUST stay byte-for-byte identical.**
   If the orchestrator uses the fallback and the validator
   does not, the LLM sees text the validator cannot locate
   -> every candidate is rejected with
   ``turn_index_out_of_range``. If the validator uses a
   DIFFERENT fallback (e.g. checks every turn, not just the
   last), it accepts snippets the orchestrator never showed
   the LLM, defeating the audit trail.

   Any change to the fallback logic in one file REQUIRES the
   same change in the other. The Phase 1-C acceptance test
   ``test_T5_mock_gateway_success`` exercises this path
   end-to-end and will fail loudly if the two drift apart.

   Phase 2+ migration path: eliminate the session-level
   fallback entirely by writing per-turn user/assistant
   content to ``session.json["turns"][i]["user"]`` at
   dispatch time. When that lands, remove the fallback here
   AND in the orchestrator in the same commit.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from omweb.memory.extraction.contracts import Evidence


logger = logging.getLogger("omweb.memory.extraction.validator")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_SNIPPET_CHARS: int = 3
_MAX_SNIPPET_CHARS: int = 200

#: Sentences that assert the ASSISTANT's ignorance about the user.
#: See the self-poisoning guard in ``validate`` for the failure loop
#: these prevent.
#:
#: Every alternative is anchored on knowledge-of-the-user phrasing,
#: never on a bare "I don't know" -- "I don't know Rust" (the user
#: speaking about a skill) is legitimate memory content.
_SELF_REFUSAL_PATTERN = re.compile(
    r"(?:"
    # English: "I don't have any other personal information about you"
    r"(?:i\s+)?(?:do\s+not|don'?t)\s+(?:have|know)\s+(?:any\s+)?"
    r"(?:other\s+|further\s+|more\s+)?"
    r"(?:personal\s+)?(?:information|info|details|data|knowledge)"
    r"|"
    # English: "I have no information about you"
    r"(?:i\s+)?have\s+no\s+(?:other\s+|further\s+|more\s+)?"
    r"(?:personal\s+)?(?:information|info|details|knowledge)"
    r"|"
    # English: "I don't know you" / "I do not know who you are"
    r"(?:i\s+)?(?:do\s+not|don'?t)\s+know\s+(?:you|who\s+you\s+are)"
    r"|"
    # English: "as an AI ... I cannot ..."
    r"\bas\s+an?\s+(?:ai|artificial\s+intelligence|language\s+model)\b"
    r"|"
    # Arabic: "ليس لدي معلومات" / "لا اعرف" / "لا املك معلومات"
    r"ل(?:ا|يس)\s+(?:لدي|عندي)\s+(?:اي\s+)?(?:معلومات|بيانات|تفاصيل)"
    r"|"
    r"لا\s+(?:اعرف|أعرف)\s+(?:من\s+)?(?:انت|أنت|عنت|عنك)"
    r")",
    re.IGNORECASE,
)
"""Pre-compiled self-refusal detector.

Case-insensitive; Arabic diacritics are NOT matched here because
``normalize_for_storage`` has already stripped them by the time a
snippet reaches the source text. Both ``ا`` alef forms are listed for
the same reason: the normalizer maps them, but a snippet echoed
straight from a model reply may not have been through it.
"""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_comparison(text: str) -> str:
    """Return the canonical form used for snippet comparison.

    Three transformations (§21.5):

      1. Every run of whitespace is collapsed to a single space.
         So ``"hello    world"`` matches ``"hello world"``.
      2. Leading/trailing whitespace is stripped.
      3. The result is lowercased. So ``"Hello World"`` matches
         ``"hello world"``.

    What is NOT transformed:

      * Diacritics are preserved. Arabic ``حَرف`` and ``حرف`` are
        NOT treated as equal -- the LLM is required to reproduce
        the exact vowel marks it saw. (If this becomes a real
        source of false rejections, a future version of the
        normalizer can add diacritic folding; it is deliberately
        NOT done in Phase 1-B to keep the rule simple and
        auditable.)
      * Punctuation is preserved. ``"Hello, world"`` and
        ``"Hello world"`` are different.
      * Character set is preserved. No transliteration between
        Arabic, Russian, and Latin scripts.

    The result is deterministic and idempotent: applying it to
    its own output produces the same string.
    """
    if not text or not isinstance(text, str):
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    """Outcome of one ``EvidenceValidator.validate`` call.

    Carries everything the caller needs to log the decision and,
    in a future phase, to display a rejection reason in the
    review queue.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    ok: bool = Field(
        ...,
        description=(
            "True when the evidence was accepted; False when it "
            "was rejected."
        ),
    )
    reason: str = Field(
        ...,
        description=(
            "Machine-readable rejection code. Empty when "
            "`ok` is True. Values: 'snippet_too_short', "
            "'snippet_too_long', 'turn_index_out_of_range', "
            "'turn_id_mismatch', 'snippet_not_verbatim', "
            "'source_malformed'."
        ),
    )
    detail: str = Field(
        default="",
        description=(
            "Human-readable explanation. Empty when `ok` is True."
        ),
    )

    @classmethod
    def accept(cls) -> "ValidationResult":
        """Construct a successful result."""
        return cls(ok=True, reason="", detail="")

    @classmethod
    def reject(cls, reason: str, detail: str = "") -> "ValidationResult":
        """Construct a rejection result."""
        return cls(ok=False, reason=reason, detail=detail)


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def _get_turn_field(
    turn: Dict[str, Any],
    role: Optional[str],
) -> Tuple[str, str]:
    """Return (text, actual_role) for a turn, given the claimed role.

    * When ``role == "user"``       -> (turn["user"], "user")
    * When ``role == "assistant"``  -> (turn["assistant"], "assistant")
    * When ``role is None``         -> concatenation of both
                                       (for snippet-in check),
                                       role="user+assistant"

    A missing field is treated as an empty string. The caller
    handles the empty-text case by treating the snippet as a
    non-match.
    """
    if role == "user":
        return str(turn.get("user") or ""), "user"
    if role == "assistant":
        return str(turn.get("assistant") or ""), "assistant"
    # Unknown role -- concatenate both sides. The separator is a
    # newline so a snippet cannot accidentally match across the
    # user/assistant boundary.
    user_text = str(turn.get("user") or "")
    assistant_text = str(turn.get("assistant") or "")
    combined = f"{user_text}\n{assistant_text}"
    return combined, "user+assistant"


def _extract_turn_id(turn: Dict[str, Any]) -> Optional[str]:
    """Return the turn's id, if present under either name.

    The current session format writes the turn id under the key
    ``"id"`` (see `chat_storage_engine.record_turn_and_sync`),
    while some older or alternate paths use ``"turn_id"``. We
    accept both so the validator works on historical data too.
    """
    for key in ("turn_id", "id"):
        value = turn.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _get_session_context_message(
    conversation: List[Dict[str, Any]],
    turn_index: int,
    role: Optional[str],
) -> Tuple[str, str]:
    """Return (text, actual_role) for a context.json message.

    The ``conversation[]`` array is a FLAT list of user/assistant
    messages, filtered from any tool steps, in strict alternation
    starting with user. For a given ``turn_index`` T:

      * user message      is at ``2*T``
      * assistant message is at ``2*T + 1``

    A missing message (shorter conversation) yields an empty
    string, which the caller treats as a non-match.
    """
    if role == "user":
        idx = 2 * turn_index
        if 0 <= idx < len(conversation):
            msg = conversation[idx]
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or ""), "user"
        return "", "user"

    if role == "assistant":
        idx = 2 * turn_index + 1
        if 0 <= idx < len(conversation):
            msg = conversation[idx]
            if (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
            ):
                return str(msg.get("content") or ""), "assistant"
        return "", "assistant"

    # Unknown role -- check both slots.
    user_idx = 2 * turn_index
    assistant_idx = 2 * turn_index + 1
    user_text = ""
    assistant_text = ""
    if 0 <= user_idx < len(conversation):
        msg = conversation[user_idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            user_text = str(msg.get("content") or "")
    if 0 <= assistant_idx < len(conversation):
        msg = conversation[assistant_idx]
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            assistant_text = str(msg.get("content") or "")
    return f"{user_text}\n{assistant_text}", "user+assistant"


# ---------------------------------------------------------------------------
# EvidenceValidator
# ---------------------------------------------------------------------------


class EvidenceValidator:
    """Validates ``Evidence`` against a source session/context dict.

    Stateless. A single instance can be shared across the process.

    Usage::

        validator = EvidenceValidator()
        result = validator.validate(
            evidence=candidate.evidence,
            source=session_dict,
            recommended_source="session_json",
        )
        if not result.ok:
            logger.info(
                "Rejected candidate evidence: %s (%s)",
                result.reason, result.detail,
            )
    """

    def validate(
        self,
        evidence: Optional[Evidence],
        *,
        source: Dict[str, Any],
        recommended_source: str,
    ) -> ValidationResult:
        """Validate one ``Evidence`` against the given source.

        Parameters
        ----------
        evidence
            The evidence to validate. ``None`` is always rejected
            -- a candidate with no evidence is not auditable.
        source
            The parsed `session.json` or `session.context.json`
            dict (matching ``recommended_source``).
        recommended_source
            Either ``"session_json"`` or ``"session_context_json"``,
            matching the values of
            `SessionCandidate.recommended_evidence_source`.

        Returns
        -------
        ValidationResult
            ``.ok == True`` on success; ``.ok == False`` with a
            ``reason`` code otherwise.
        """
        if evidence is None:
            return ValidationResult.reject(
                "source_malformed",
                "No evidence provided; every candidate must "
                "cite a source snippet.",
            )

        # --- Snippet shape checks (independent of source) ----------
        snippet = evidence.snippet
        if len(snippet) < _MIN_SNIPPET_CHARS:
            return ValidationResult.reject(
                "snippet_too_short",
                f"snippet is {len(snippet)} chars; minimum "
                f"is {_MIN_SNIPPET_CHARS}.",
            )
        if len(snippet) > _MAX_SNIPPET_CHARS:
            return ValidationResult.reject(
                "snippet_too_long",
                f"snippet is {len(snippet)} chars; maximum "
                f"is {_MAX_SNIPPET_CHARS}.",
            )

        # --- Self-poisoning guard ----------------------------------
        #
        # An assistant REPLY is a valid evidence source: "your manager
        # is Sara" is a fact worth remembering even when the assistant
        # said it. But a reply that asserts the assistant's OWN
        # ignorance is not a fact about the user -- and storing it
        # creates a feedback loop:
        #
        #   turn N     user: "do you know me?"
        #              assistant: "I don't have any other personal
        #                          information about you."
        #   extraction stores that sentence as a user memory
        #   turn N+1   retrieval injects it with relevance 1.00,
        #              ABOVE the real memories, because it literally
        #              contains the words of the question
        #   model      reads "I don't have any personal information
        #              about you" and answers "No, I don't know you"
        #
        # The memory becomes self-fulfilling: the more the assistant
        # says it knows nothing, the stronger the evidence that it
        # knows nothing. Observed exactly this on the live database
        # ("don't have any other personal information about you" sat
        # at rank 1 with score 1.00, outranking "User is Mohamed").
        #
        # The guard is deliberately NARROW: it matches sentences about
        # the assistant's lack of knowledge ABOUT THE USER, not any
        # occurrence of "I don't know" (a user saying "I don't know
        # Rust" is a perfectly good memory).
        if _SELF_REFUSAL_PATTERN.search(snippet):
            return ValidationResult.reject(
                "self_referential_refusal",
                "snippet asserts the assistant's own ignorance "
                "about the user; storing it would reinforce that "
                "claim on every later retrieval.",
            )

        # --- Locate the source text --------------------------------
        try:
            text, _actual_role = self._resolve_source_text(
                evidence=evidence,
                source=source,
                recommended_source=recommended_source,
            )
        except Exception as exc:
            # Defensive: any malformed source shape -> rejection.
            logger.debug(
                "Evidence source resolution failed: %s", exc
            )
            return ValidationResult.reject(
                "source_malformed",
                f"Could not resolve source for evidence: {exc}",
            )

        if not text:
            return ValidationResult.reject(
                "turn_index_out_of_range",
                f"turn_index={evidence.turn_index} yields no "
                f"source text in {recommended_source}.",
            )

        # --- Optional turn_id cross-check -------------------------
        if evidence.turn_id is not None:
            actual_turn_id = self._resolve_turn_id(
                evidence=evidence,
                source=source,
                recommended_source=recommended_source,
            )
            if (
                actual_turn_id is not None
                and actual_turn_id != evidence.turn_id
            ):
                return ValidationResult.reject(
                    "turn_id_mismatch",
                    f"evidence.turn_id={evidence.turn_id!r} "
                    f"does not match source turn id="
                    f"{actual_turn_id!r}.",
                )

        # --- The verbatim check -----------------------------------
        norm_snippet = normalize_for_comparison(snippet)
        norm_text = normalize_for_comparison(text)
        if not norm_snippet:
            # Whitespace-only snippet: this cannot happen because
            # the Pydantic validator on `Evidence` already strips
            # outer whitespace, but be defensive.
            return ValidationResult.reject(
                "snippet_too_short",
                "snippet normalizes to an empty string.",
            )
        if norm_snippet not in norm_text:
            # Do NOT include the raw snippet in the log message at
            # INFO level -- it may contain content the user does
            # not expect to see in logs. The length + a marker is
            # enough for the decisions log.
            return ValidationResult.reject(
                "snippet_not_verbatim",
                f"snippet (len={len(snippet)}) is not a "
                f"substring of the source text.",
            )

        return ValidationResult.accept()

    # ------------------------------------------------------------------
    # Source resolution helpers
    # ------------------------------------------------------------------

    def _resolve_source_text(
        self,
        *,
        evidence: Evidence,
        source: Dict[str, Any],
        recommended_source: str,
    ) -> Tuple[str, str]:
        """Return the raw text the snippet should be checked against."""
        if recommended_source == "session_json":
            turns = source.get("turns")
            if not isinstance(turns, list):
                raise ValueError("source has no 'turns' list")
            if not (0 <= evidence.turn_index < len(turns)):
                return "", ""
            turn = turns[evidence.turn_index]
            if not isinstance(turn, dict):
                raise ValueError(
                    f"turn at index {evidence.turn_index} is not a dict"
                )

            # First attempt: canonical per-turn fields.
            text, actual_role = _get_turn_field(
                turn, evidence.message_role
            )
            if text:
                return text, actual_role

            # Fallback: chat-mode sessions written by
            # ``dispatcher.run_direct_chat`` (and its ancestors)
            # store the most recent user/assistant exchange at the
            # SESSION level (``session["prompt"]`` /
            # ``session["result"]``), while ``turns[]`` carries
            # ONLY metadata (turn_id, timestamp, usage). Without
            # this fallback, the validator would report
            # ``turn_index_out_of_range`` for every candidate the
            # LLM produces, even though the LLM SAW the text
            # (``ExtractionOrchestrator._prepare_from_session_json``
            # uses the same fallback when building the prompt).
            #
            # The fallback fires ONLY for the LAST turn (matching
            # the orchestrator's exact behavior). Earlier turns
            # have no accessible content until the write path
            # stores per-turn user/assistant text (a future fix
            # tracked separately).
            is_last_turn = evidence.turn_index == len(turns) - 1
            if not is_last_turn:
                return "", actual_role

            fallback_user = str(source.get("prompt") or "")
            fallback_assistant = str(source.get("result") or "")

            if evidence.message_role == "user":
                return fallback_user, "user"
            if evidence.message_role == "assistant":
                return fallback_assistant, "assistant"
            # Unknown role: combine both, same separator as
            # ``_get_turn_field``.
            combined = f"{fallback_user}\n{fallback_assistant}"
            return combined, "user+assistant"

        if recommended_source == "session_context_json":
            conversation = source.get("conversation")
            if not isinstance(conversation, list):
                raise ValueError("source has no 'conversation' list")
            return _get_session_context_message(
                conversation,
                evidence.turn_index,
                evidence.message_role,
            )

        raise ValueError(
            f"Unknown recommended_source: {recommended_source!r}"
        )

    def _resolve_turn_id(
        self,
        *,
        evidence: Evidence,
        source: Dict[str, Any],
        recommended_source: str,
    ) -> Optional[str]:
        """Return the source turn id at the evidence's turn_index.

        Returns ``None`` when the source format has no per-turn id
        (context.json's flat conversation has no ids), which tells
        the caller to skip the cross-check.
        """
        if recommended_source != "session_json":
            # context.json messages have no stable turn id.
            return None
        turns = source.get("turns")
        if not isinstance(turns, list):
            return None
        if not (0 <= evidence.turn_index < len(turns)):
            return None
        turn = turns[evidence.turn_index]
        if not isinstance(turn, dict):
            return None
        return _extract_turn_id(turn)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EvidenceValidator",
    "ValidationResult",
    "normalize_for_comparison",
]
