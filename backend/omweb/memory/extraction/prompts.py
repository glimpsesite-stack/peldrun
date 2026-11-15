# backend/omweb/memory/extraction/prompts.py
"""
PELDRUN Memory Platform - Phase 1-B: Extraction Prompts.

Single source of truth for the extraction LLM's system prompt and
the JSON schema the model is required to produce. Changing either
of these REQUIRES bumping ``PROMPT_VERSION`` below -- the version
is recorded in ``ExtractionMeta.prompt_version`` for every memory
the pipeline produces, so an A/B comparison of prompt changes is
always possible from the audit trail.

Why the prompt is in a dedicated module
---------------------------------------

1. **Testability.** `tests/memory/test_phase1b_extraction.py`
   asserts specific prompt phrases exist (e.g. the verbatim
   snippet rule). Hard-coding the string inside `gateway.py`
   would require re-implementing the test logic against that
   file's internals.

2. **Versioning.** `PROMPT_VERSION` is read by
   `ExtractionOrchestrator` when it builds `ExtractionMeta`.
   Keeping it next to the prompt ensures they can never drift.

3. **Multilingual clarity.** The prompt is written in English
   (the LLM's instruction-tuning is overwhelmingly English), but
   it EXPLICITLY instructs the model to extract content in the
   SOURCE language (AR/RU/EN) -- so an Arabic conversation
   produces Arabic memories, an English one produces English
   memories, and a mixed one produces mixed-language memories.

Design invariants
-----------------

1. **The verbatim snippet rule is non-negotiable (§21.5).**
   The system prompt repeats the rule in THREE places: an
   instruction paragraph, a "DO NOT" list, and a per-field
   description inside the JSON schema. This redundancy is
   deliberate -- prompt-following degrades with distance from
   the instruction, so a rule stated once is often ignored.

2. **JSON-only output.**
   ``ResponseFormat.JSON`` is set by the gateway, but the prompt
   ALSO instructs "Return ONLY the JSON object, no prose before
   or after" because some providers silently wrap JSON responses
   in markdown fences even when asked for JSON.

3. **The schema is machine-readable.**
   ``EXTRACTION_JSON_SCHEMA`` is a plain Python dict. The
   gateway passes it as ``response_format`` when the provider
   supports structured output; when the provider does NOT
   support it (older local models), the gateway falls back to
   "prompt-only JSON" and relies on a parsing helper that
   tolerates markdown fences.
"""

from __future__ import annotations


PROMPT_VERSION: str = "v1"
"""Version tag recorded in every `ExtractionMeta.prompt_version`.

Bump this on ANY substantive change to
``EXTRACTION_SYSTEM_PROMPT`` or ``EXTRACTION_JSON_SCHEMA``. A
memory produced under v1 must be distinguishable from one
produced under v2 -- a change in extraction quality MUST be
attributable to a version, not to an untracked edit.
"""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


EXTRACTION_SYSTEM_PROMPT: str = """\
You are a memory extraction assistant for a chat application.

Your task is to read a short conversation excerpt and identify any
DURABLE FACTS that are worth remembering across future sessions.
A durable fact is any piece of information that:

  * will remain true beyond this conversation, AND
  * would be useful to recall in a LATER conversation.

Do NOT extract:
  * transient states ("I'm hungry right now"),
  * greetings or small talk,
  * requests that were fulfilled in this turn (unless the request
    itself encodes a durable preference),
  * content that only makes sense within this specific turn.

Extract at most {max_candidates} candidates. Prefer FEWER, HIGHER
quality candidates over many marginal ones.

Language
--------
Extract the memory content in the SAME LANGUAGE as the source
message (Arabic, Russian, or English). If the source mixes
languages, use the dominant one. Do NOT translate.

Memory types (pick exactly one per candidate)
---------------------------------------------
  * "semantic"    -- timeless facts about the world or the user
  * "episodic"    -- events with a specific time reference
  * "procedural"  -- reusable procedures or workflows
  * "preference"  -- user tastes, defaults, or constraints
  * "decision"    -- accepted decisions with their rationale

Evidence -- MANDATORY AND VERBATIM
----------------------------------
Every candidate MUST include an `evidence` object pointing at the
EXACT substring of the source message that justifies the memory.

evidence.snippet MUST be a VERBATIM substring of the original
message content:
  * Do NOT paraphrase.
  * Do NOT summarize.
  * Do NOT translate.
  * Do NOT correct typos.
  * Copy-paste the EXACT text, whitespace preserved.
  * Keep the snippet UNDER 200 characters.
  * If you cannot find an exact quote, OMIT the evidence (the
    candidate will be rejected).

The evidence object also records:
  * "turn_index"    -- 0-based index of the turn in the input
  * "message_role"  -- "user" or "assistant" (which side said it)
  * "turn_id"       -- optional; include if the input provides it

DO NOT:
  * invent quotes that are not present verbatim in the source,
  * cite multiple messages in a single evidence snippet,
  * return anything except a JSON object.

Output format
-------------
Return ONLY a JSON object matching this exact schema:

{json_schema}

The top-level object MUST have a single key, "candidates", whose
value is a JSON array. An empty array means "nothing worth
extracting from this excerpt" -- this is a VALID and common
result. Do NOT pad the array with low-confidence guesses.
"""


# ---------------------------------------------------------------------------
# JSON schema
# ---------------------------------------------------------------------------


EXTRACTION_JSON_SCHEMA: dict = {
    "name": "extraction_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "content",
                        "memory_type",
                        "importance",
                        "confidence",
                        "evidence",
                    ],
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": (
                                "The memory text, in the source "
                                "language. One self-contained "
                                "sentence or short paragraph."
                            ),
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": [
                                "semantic",
                                "episodic",
                                "procedural",
                                "preference",
                                "decision",
                            ],
                        },
                        "importance": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "tags": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                            "maxItems": 8,
                        },
                        "evidence": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["turn_index", "snippet"],
                            "properties": {
                                "turn_index": {
                                    "type": "integer",
                                    "minimum": 0,
                                },
                                "turn_id": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "snippet": {
                                    "type": "string",
                                    "minLength": 3,
                                    "maxLength": 200,
                                    "description": (
                                        "VERBATIM substring of "
                                        "the source message. "
                                        "No paraphrase."
                                    ),
                                },
                                "message_role": {
                                    "type": "string",
                                    "enum": ["user", "assistant"],
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}
"""Machine-readable JSON schema for the extraction response.

The shape is STRICT on the LLM's output so that:

  * every required field is guaranteed present (so the parser
    never has to invent defaults),
  * the `memory_type` enum is enforced at the model level
    (never trust the LLM to spell "preference" correctly),
  * the `evidence.snippet` length cap is declared as a schema
    constraint so capable models respect it during generation
    (belt-and-suspenders with the validator's own cap).

The `name` and `strict` fields match OpenAI's structured-output
API convention. Other providers ignore them; the gateway decides
whether to pass the schema as `response_format` or embed it into
the prompt.
"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_extraction_system_prompt(
    *,
    max_candidates: int,
) -> str:
    """Return the system prompt with runtime substitutions applied.

    The prompt contains two format placeholders:

      * ``{max_candidates}`` -- the hard cap on output candidates
        (from ``MemoryAISettings.max_candidates_per_job``),
      * ``{json_schema}`` -- the schema, rendered as pretty JSON.

    Both are substituted here in ONE place so the prompt itself
    stays a literal string. A future change to the schema
    automatically flows into the prompt; a future change to the
    cap flows in from settings.
    """
    import json as _json
    schema_str = _json.dumps(
        EXTRACTION_JSON_SCHEMA["schema"], indent=2
    )
    return EXTRACTION_SYSTEM_PROMPT.format(
        max_candidates=int(max_candidates),
        json_schema=schema_str,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "PROMPT_VERSION",
    "EXTRACTION_SYSTEM_PROMPT",
    "EXTRACTION_JSON_SCHEMA",
    "build_extraction_system_prompt",
]
