# backend/omweb/memory/extraction/prompts.py
"""
PELDRUN Memory Platform - Phase 1-B: Extraction Prompts.

Single source of truth for the extraction LLM's system prompt and
the JSON schema the model is required to produce.

Design invariants
-----------------

1. **Token-frugal by default (v3+).**
   Small models (4B parameters and below) lose instruction-
   following when the prompt exceeds ~500 tokens. The v1 prompt
   was ~1450 tokens (verbose rules + pretty-printed schema with
   per-field descriptions). The v3 prompt below is ~300 tokens
   by inlining a compact JSON example and removing redundant
   rule repetitions. Quality is preserved because the
   ``EvidenceValidator`` (not the prompt) is the ground truth
   for the verbatim rule -- a snippet that is not literally
   present in the source is rejected regardless of what the
   prompt said.

2. **Verbatim evidence is stated ONCE, strongly.**
   v1 stated the rule three times (paragraph + DO-NOT list +
   schema description). That redundancy burns tokens and, on
   small models, pushes the actual output schema far from the
   instructions. v3 states it once, in a MUST sentence, and
   trusts the validator to enforce it.

3. **JSON schema is inline as a JSON example.**
   The model reads the shape from an example object far more
   reliably than from JSON-Schema syntax. We still export
   ``EXTRACTION_JSON_SCHEMA`` because ``gateway.py`` passes it
   as ``response_format`` when the provider supports structured
   output -- but the prompt itself no longer embeds it.

4. **Language rule is stated once, in one line.**
   v1 explained the AR/RU/EN rule over four lines. v3 uses one
   line: "LANGUAGE: same as source (AR/RU/EN). Do not translate."

5. **No stop-thinking instruction (v4+).**
   v3 included a "STOP THINKING" line. In practice this does
   NOT disable reasoning on reasoning-capable models -- the
   thinking phase runs BEFORE any user-visible token is
   generated, and is controlled at the API level (see
   ``gateway.py``). v4 removes the line because it was
   ineffective AND cost ~20 tokens. Reasoning disablement is
   the API layer's responsibility, not the prompt's.

6. **Language-neutral structural example (v6).**
   The system prompt is written in English (instruction-tuning
   language). The JSON example must NOT contain any language-
   specific literals -- no Arabic, no Russian, no English that
   a 4B model might copy literally. v6 uses ``<placeholder>``
   tokens for content/snippet and realistic integers for
   importance/confidence (75, 0.9) instead of the skeleton
   values (0, 0.0). This enforces language neutrality by
   structure, not by instruction, and prevents the "literal
   copy of the example" failure mode observed in v5.

Versioning
----------
``PROMPT_VERSION`` is recorded in every ``ExtractionMeta``. A
change to this file REQUIRES a version bump so that memories
produced under different prompts remain distinguishable in the
audit trail.

- v1: initial (verbose).
- v2: v1 + verbose STOP-THINKING section (token-expensive).
- v3: compact rewrite. ~300 tokens.
- v4: v3 without the STOP-THINKING line (it does not actually
  disable reasoning on reasoning-capable models; reasoning is
  controlled at the API level). ~280 tokens.
- v5: v4 + explicit "greeting-with-facts" rule. Small models
  (4B) were classifying entire messages as "greetings" because
  they opened with a greeting, and skipping the durable facts
  inside. v5 instructs the model to extract facts regardless of
  a greeting preamble. ~340 tokens.
- v6: structural example + anti-translation + anti-literal-copy.
  Observed: small models (4B) copied ``"turn_id": "optional"``
  and ``"importance": 0`` from the skeleton example literally.
  v6 uses language-neutral ``<placeholders>`` and a realistic
  numeric example. Adds a MANDATORY RULES block that explicitly
  enforces language-verbatim and forbids literal copying of the
  example. ~420 tokens.
"""

from __future__ import annotations


PROMPT_VERSION: str = "v6"
"""Version tag recorded in every `ExtractionMeta.prompt_version`."""


# ---------------------------------------------------------------------------
# System prompt (compact, ~300 tokens)
# ---------------------------------------------------------------------------


EXTRACTION_SYSTEM_PROMPT: str = """\
You extract durable facts from a chat conversation into JSON.

DURABLE = true beyond this chat AND useful in a LATER chat.
Extract: names, identities, preferences, personal facts, decisions,
         procedures, relationships, skills, constraints.
Skip ONLY: pure greetings, pure acknowledgements ("thanks", "ok"),
            transient states ("I'm tired now"),
            and requests already fulfilled in this same turn.

IMPORTANT: A message may contain BOTH a greeting AND durable facts.
Extract the facts EVEN IF the message starts with a greeting.
Example: "Hi, I'm Alice and I love coffee"
         -> extract "name is Alice" AND "loves coffee".
         Do NOT skip the whole message just because it opens with
         a greeting.

Max {max_candidates} candidates. Empty array valid only if the
message is PURELY greeting/acknowledgement with zero durable content.

LANGUAGE: same as source (AR/RU/EN). Do not translate.

EVIDENCE: every candidate MUST have `evidence.snippet` set to an
EXACT VERBATIM substring of the source message. Copy the exact
characters -- no paraphrase, no translation, no typo fixes, no
edits of any kind. If you cannot find an exact quote, omit the
candidate entirely.

Return ONLY this JSON object. The example below shows the exact
JSON structure you must produce (values are illustrative):

{{"candidates": [
  {{
    "content": "<memory text in SOURCE language>",
    "memory_type": "semantic",
    "importance": 75,
    "confidence": 0.9,
    "tags": ["tag1", "tag2"],
    "evidence": {{
      "turn_index": 0,
      "snippet": "<EXACT verbatim substring from source>",
      "message_role": "user"
    }}
  }}
]}}

MANDATORY RULES (violating any rejects the candidate):
- LANGUAGE: content and snippet MUST be in the SAME language
  as the source message. Arabic source -> Arabic output.
  English source -> English output. NEVER translate.
- VERBATIM: snippet MUST be copied CHARACTER-BY-CHARACTER
  from the source message.
- DO NOT invent facts. Only extract what is literally stated.
- importance: integer 0-100 (e.g., 75). Do NOT use 0.
- confidence: number 0.0-1.0 (e.g., 0.9). Do NOT use 0.0.
- tags: short lowercase keywords.
- turn_id: OMIT this field entirely. It is optional and you
  must not include it.
- Do NOT copy the example values literally. Replace
  <memory text...> and <EXACT...> with actual text from the
  source.

Output JSON only. Start with `{{`, end with `}}`.
"""


# ---------------------------------------------------------------------------
# JSON schema (for response_format; no per-field descriptions)
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
"""Machine-readable JSON schema for providers that support structured
output (OpenAI-style ``response_format``). No per-field
``description`` strings -- those were redundant with the inline
example in the prompt and cost ~600 tokens when pretty-printed.

The prompt itself is the source of truth for the model. This schema
is ONLY used as the wire-format contract for providers that accept
a schema.
"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_extraction_system_prompt(
    *,
    max_candidates: int,
) -> str:
    """Return the system prompt with runtime substitutions applied.

    Only one placeholder is substituted now: ``{max_candidates}``.
    The schema is inline in the prompt (as a JSON example) --
    embedding it separately as pretty-printed JSON was costing
    ~600 tokens and pushing small models into confusion.

    The doubled braces (``{{`` / ``}}``) are Python ``str.format``
    escapes for literal ``{`` / ``}`` characters -- the inline
    JSON example needs them to survive ``.format()`` unchanged.
    """
    return EXTRACTION_SYSTEM_PROMPT.format(
        max_candidates=int(max_candidates),
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
