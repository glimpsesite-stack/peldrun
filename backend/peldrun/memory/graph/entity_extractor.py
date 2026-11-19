# backend/peldrun/memory/graph/entity_extractor.py
"""
PELDRUN Memory Platform - LLM-Backed Named Entity Extraction.

Turns the free-text ``content`` of a memory entry into the
``Entity`` nodes that the knowledge graph indexes. This is the ONLY
place in Phase 6 where unstructured text becomes graph structure.

Design invariants
-----------------

1. **Never raises for an LLM failure.**
   The extractor runs on the WRITE path (backgrounded, see
   ``omweb.memory.graph.indexer``). Throwing would either kill the
   indexing task or, worse, tempt a caller to await it inline and
   slow down ``save_many``. Every failure mode -- transport error,
   timeout, unparseable JSON, malformed item, provider not
   configured -- maps to an empty list plus a WARNING log. The
   memory is already persisted at this point; a missing graph node
   degrades retrieval quality, it never corrupts data.

2. **``CancelledError`` propagates.**
   The single exception that is re-raised. Swallowing cancellation
   breaks asyncio's shutdown semantics and leaves the task in a
   zombie state that ``stop_embedding_worker``-style timeouts cannot
   clear.

3. **Deterministic, order-preserving output.**
   Entities come back in first-mention order with duplicates
   collapsed by canonical name. Determinism matters because the
   downstream ``GraphIndexer`` derives relation ids from the entity
   ORDER it pairs; a non-deterministic list would produce different
   edges across identical re-index runs and defeat the graph's
   idempotency guarantee.

4. **Aliases absorb spelling variation, never guess identity.**
   Two mentions collapse into ONE entity only when they share the
   same ``(canonical_name, entity_type)`` pair -- the exact key the
   v6 index ``idx_entities_canonical`` is built on. So "Ahmed",
   "AHMED", and " ahmed " become one node, while "Ahmed" and "Ahmed
   Ali" stay SEPARATE nodes.

   Because the dedupe key IS the canonical form, a folded re-mention
   yields the same node with no NEW alias -- the alias set only grows
   when a surface form folds differently, which the current
   canonicalization (case + whitespace + NFKC) never does. So
   ``aliases`` is empty for entities produced by extraction alone;
   it is populated by ``GraphStore.add_entity`` when the same person
   is observed in a DIFFERENT memory under a spelling the operator
   or a later resolution pass supplies. The fold call is kept here so
   that tightening the canonical rule later automatically starts
   producing aliases instead of silently dropping the information.

   Not merging "Ahmed" / "Ahmed Ali" is deliberate. Doing so needs a
   fuzzy "probably the same person" heuristic, and a wrong merge is
   far more damaging than a duplicate node: it makes two real people
   share one set of facts, permanently, with no trace of the
   mistake. A duplicate node costs one extra traversal hop. Entity
   resolution across surface forms is a later, explicitly labelled
   pass -- not a side effect of extraction.

   Keeping the type in the key also preserves a real distinction:
   "Ahmed" as a PERSON and "Ahmed" as a PLACE are different nodes,
   exactly as the storage index allows.

5. **Bounded cost and bounded output.**
   ``max_content_chars`` bounds the prompt; ``max_output_tokens``
   bounds the completion; ``max_entities`` bounds the node count.
   Without the last one, a model that decides every noun is an
   entity could add hundreds of nodes per memory and make the graph
   traversal explode (see the ``traverse`` caps in ``graph_store``).

6. **No ``omweb`` import.**
   The gateway arrives through ``StructuredGatewayProtocol``
   (``peldrun.memory.llm_contracts``). ``omweb``'s
   ``MemoryModelGateway`` satisfies it structurally, so wiring
   happens in ``omweb`` while the algorithm stays in the core.

See also
--------

    * ``peldrun/memory/graph/contracts.py``
        -- ``Entity`` and the normalization rules
    * ``peldrun/memory/llm_contracts.py``
        -- the gateway Protocol and response parser
    * ``omweb/memory/graph/indexer.py``
        -- the write-time caller
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.graph.contracts import (
    Entity,
    canonicalize_name,
)
from peldrun.memory.llm_contracts import (
    StructuredGatewayProtocol,
    call_structured,
    first_present,
)


logger = logging.getLogger(
    "peldrun.memory.graph.entity_extractor"
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

ENTITY_PROMPT: str = """Extract the salient entities AND topics from the text.

A TOPIC is what the text is ABOUT: a preference, a food or drink, a tool,
a technology, an activity, a recurring subject. Do NOT restrict yourself
to proper nouns -- a lowercase common noun that the sentence is about IS
an entity for this purpose.

Return ONLY a JSON object with this exact shape:
{{"entities": [{{"type": "PERSON|ORG|PROJECT|PLACE|TOPIC|DATE", "name": "..."}}]}}

Rules:
- PERSON: a person's name (Mohamed, Ahmed Ali).
- ORG: a company, team, or institution (AcmeCorp, Google).
- PROJECT: a named project or product (PELDRUN Memory, ProjectX).
- PLACE: a city, country, or location (Cairo, London).
- TOPIC: a subject, preference, food/drink, tool, technology or activity
  the text is about (Python, vector search, black coffee, black tea).
- DATE: a date or time period (December 2024, next week).
- Use the surface form exactly as it appears in the text.
- If the sentence states a preference, habit, fact or activity, then the
  thing it is about is a TOPIC. "I love drinking black coffee" yields
  TOPIC "black coffee".
- Do NOT invent entities that are not mentioned.
- Do NOT return pronouns, bare articles, or function words.
- Return {{"entities": []}} ONLY when the text is empty or carries no
  content at all. A single ordinary noun phrase is still an entity.

Example:
TEXT: I love drinking black coffee and green tea.
{{"entities": [{{"type": "TOPIC", "name": "black coffee"}}, {{"type": "TOPIC", "name": "green tea"}}]}}

TEXT:
{content}
"""
"""Extraction prompt.

Notes on the shape of this template:

  * Every literal brace is DOUBLED (``{{`` / ``}}``) because the
    template is consumed by ``str.format(content=...)``. A single
    brace would be read as a replacement field and raise
    ``KeyError: '"entities"'``.
  * The type list is spelled out in the schema line AND in the
    rules. Instruction-tuned models weight the schema line most
    heavily, but the rules disambiguate the near-miss cases
    (PROJECT vs TOPIC, ORG vs TEAM) that otherwise fragment the
    graph.
  * The explicit "Do NOT invent" and "Do NOT return pronouns" lines
    exist because the default failure mode of a small model on this
    task is over-extraction: returning "user", "project", "it".
    Those produce junk nodes that then dominate traversal fan-out.
"""


SYSTEM_PROMPT: str = (
    "You extract entities and topics from text and return only JSON."
)
"""System message.

Kept short on purpose: the extraction gateway is also used by the
Phase 1 extractor, whose system prompt is long and
schema-heavy. A short, single-purpose system message measurably
improves JSON-only compliance on small local models.
"""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_MAX_CONTENT_CHARS: int = 2000
"""Per-memory content truncation fed to the LLM.

A memory entry is capped at 10 000 chars by the domain
(``_MAX_CONTENT_CHARS`` in ``entry.py``), but 2000 is the useful
prefix for entity extraction: the opening of a memory carries its
subject, while the tail is usually elaboration, examples, or
transcript noise. 2000 chars is ~500 tokens -- a cheap prompt that
still covers a full paragraph.
"""


_DEFAULT_MAX_ENTITIES: int = 30
"""Cap on entities returned for ONE memory.

Counted AFTER canonical deduplication. 30 is generous for a single
memory (a dense paragraph mentions 5-15) while bounding the
worst case: a model that decides every capitalized token is an
entity. The cap is the first line of defence against graph
fan-out; ``GraphStore.traverse`` applies the second.
"""


_MIN_CANONICAL_CHARS: int = 2
"""Minimum canonical length for a candidate to be kept.

Single characters are noise in every language this platform targets:
a stray "I", "a", or a bullet-point letter. Two characters still
admits real short names ("Li", "Al", "R2").
"""


_SINGLE_TOKEN_STOPWORDS: frozenset = frozenset(
    {
        # English function words. Models routinely emit these as
        # "entities" despite the prompt forbidding generic nouns;
        # each one that leaks in becomes a hub node adjacent to
        # everything, which then dominates every traversal.
        "the", "a", "an", "and", "or", "but", "if", "of", "to",
        "in", "on", "at", "by", "for", "with", "from", "as",
        "is", "are", "was", "were", "be", "been", "being",
        "this", "that", "these", "those", "it", "its",
        "i", "me", "my", "we", "our", "you", "your",
        "he", "she", "they", "them", "his", "her", "their",
        "not", "no", "yes", "do", "does", "did",
    }
)
"""Function words that can never BE an entity on their own.

Applied ONLY to single-token candidates. Two reasons for that
restriction:

  * A multi-token span may legitimately start with a function word
    ("The Beatles", "A Clockwork Orange"), so filtering by first
    token would destroy real names.
  * The list is English-only; restricting it to exact single-token
    matches means it produces NO false negatives in any other
    language -- an Arabic or Japanese candidate simply never matches
    a key here and is kept.

This is a language-specific SAFETY NET, not a content heuristic:
there is no rule anywhere in this module that looks for a
particular word to decide an entity exists.
"""


_CONNECTION_ID: str = "memory_entity_extraction"
"""Diagnostic identifier passed to the gateway for log attribution
and cost bucketing. Distinct per component so an operator can tell
WHICH Phase 6 component spent the tokens."""


# ---------------------------------------------------------------------------
# EntityExtractor
# ---------------------------------------------------------------------------


class EntityExtractor:
    """Extracts ``Entity`` nodes from memory content via an LLM.

    Stateless after construction (no mutable diagnostic fields), so
    concurrent ``extract`` calls are safe. The only state is the
    configuration captured at construction time.

    Example
    -------
    ::

        extractor = EntityExtractor(
            gateway,
            provider_id="lmstudio",
            model_id="qwen2.5-7b-instruct",
        )
        entities = await extractor.extract(
            "I work as a programmer at AcmeCorp for Ahmed."
        )
        # [Entity(type=PERSON, name='Ahmed'),
        #  Entity(type=ORG,   name='AcmeCorp'),
        #  Entity(type=TOPIC, name='programmer')]
    """

    def __init__(
        self,
        gateway: Optional[StructuredGatewayProtocol],
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        max_output_tokens: int = 800,
        timeout_seconds: float = 5.0,
        max_content_chars: int = _DEFAULT_MAX_CONTENT_CHARS,
        max_entities: int = _DEFAULT_MAX_ENTITIES,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        connection_id: str = _CONNECTION_ID,
    ) -> None:
        """Construct the extractor.

        Parameters
        ----------
        gateway
            Anything satisfying ``StructuredGatewayProtocol``.
            ``None`` is tolerated: every ``extract`` call then
            returns ``[]`` without an LLM attempt. This is what lets
            ``MemoryService`` build the graph pipeline optimistically
            and have it degrade to "graph disabled" when no transport
            is wired -- the same contract
            ``start_extraction_worker`` uses.
        provider_id, model_id
            The model that performs extraction. Either being falsy
            (``None`` / empty) disables the LLM call: an unresolved
            model would produce a job that cannot succeed, so the
            extractor refuses early instead of failing per call.
        max_output_tokens
            Completion cap. 800 fits ~30 entities with JSON
            scaffolding (the §7.5 reference value).
        timeout_seconds
            The extractor's OWN authoritative deadline, enforced with
            ``asyncio.wait_for``. The gateway receives
            ``timeout_seconds + slack`` so this deadline always wins
            (see ``DEFAULT_GATEWAY_TIMEOUT_SLACK_SECONDS``).
        max_content_chars
            Input truncation.
        max_entities
            Output cap, applied after deduplication.
        api_key, base_url
            Optional overrides forwarded verbatim. ``None`` lets the
            gateway resolve credentials from the model vault -- the
            production path.
        connection_id
            Cost/log attribution label.

        Non-positive numeric arguments are corrected to their
        defaults rather than raising: the values come from a
        settings file that an operator edits by hand, and an
        unparseable setting must not take down the write path.
        """
        self._gateway = gateway
        self._provider_id = (provider_id or "").strip()
        self._model_id = (model_id or "").strip()
        self._max_output_tokens = _positive_int(
            max_output_tokens, default=800
        )
        self._timeout_seconds = _positive_float(
            timeout_seconds, default=5.0
        )
        self._max_content_chars = _positive_int(
            max_content_chars, default=_DEFAULT_MAX_CONTENT_CHARS
        )
        self._max_entities = _positive_int(
            max_entities, default=_DEFAULT_MAX_ENTITIES
        )
        self._api_key = api_key
        self._base_url = base_url
        self._connection_id = (
            connection_id or _CONNECTION_ID
        ).strip() or _CONNECTION_ID

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(self, content: str) -> List[Entity]:
        """Extract named entities from ``content``.

        Returns
        -------
        List[Entity]
            Entities in first-mention order, deduplicated by
            canonical name, each with ``entity_id == ""`` (the store
            assigns ids). Empty list on EVERY failure path:
            no content, no gateway, unresolved model, transport
            error, timeout, unparseable JSON, malformed items.

        Notes
        -----
        This method never raises (except ``CancelledError``). The
        caller on the write path depends on that: it runs inside an
        ``asyncio.create_task`` whose exception would otherwise be
        logged as "Task exception was never retrieved".
        """
        text = self._prepare_content(content)
        if not text:
            return []
        if self._gateway is None:
            logger.debug(
                "EntityExtractor: no gateway configured; skipping "
                "extraction (graph indexing is inactive)."
            )
            return []
        if not self._provider_id or not self._model_id:
            logger.debug(
                "EntityExtractor: model not configured "
                "(provider_id=%r, model_id=%r); skipping "
                "extraction.",
                self._provider_id,
                self._model_id,
            )
            return []

        # Delegated to the shared executor: it owns the per-call
        # deadline, the CancelledError discipline, the
        # never-raise contract, and -- critically -- the PROCESS-WIDE
        # concurrency gate. Calling the gateway directly here (as this
        # module originally did) bypassed that gate, so a batch write
        # opened one chat completion per memory in parallel and a
        # single-slot local runtime interleaved them into garbage.
        parsed = await call_structured(
            self._gateway,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=ENTITY_PROMPT.format(content=text),
            provider_id=self._provider_id,
            model_id=self._model_id,
            api_key=self._api_key,
            base_url=self._base_url,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            connection_id=self._connection_id,
        )
        if parsed is None:
            logger.warning(
                "EntityExtractor: extraction produced no parsable "
                "JSON (provider=%r, model=%r).",
                self._provider_id,
                self._model_id,
            )
            return []

        raw_items = first_present(parsed, "entities", "results")
        if raw_items is None:
            # A well-formed response with no "entities" key means
            # "nothing found" only if the key is ABSENT; a model
            # that answered {"foo": 1} is a schema violation, but
            # the correct reaction is still an empty result -- there
            # is nothing to salvage and no data is lost.
            logger.debug(
                "EntityExtractor: response contained no 'entities' "
                "key (keys=%s).",
                sorted(parsed.keys())[:8],
            )
            return []
        if not isinstance(raw_items, list):
            logger.warning(
                "EntityExtractor: 'entities' is %s, expected a "
                "list. Returning no entities.",
                type(raw_items).__name__,
            )
            return []

        return self._build_entities(raw_items)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prepare_content(self, content: Any) -> str:
        """Coerce and truncate the input text.

        Non-strings are coerced via ``str()`` rather than rejected:
        the caller is a memory entry, and ``content`` is always a
        string in practice. Defensive coercion keeps a future caller
        that passes a buffer from crashing the write path.
        """
        if content is None:
            return ""
        text = content if isinstance(content, str) else str(content)
        text = text.strip()
        if not text:
            return ""
        if len(text) > self._max_content_chars:
            return text[: self._max_content_chars]
        return text

    def _build_entities(
        self, raw_items: List[Any]
    ) -> List[Entity]:
        """Convert raw JSON items into deduplicated ``Entity`` objects.

        Deduplication rule
        ------------------
        Items are keyed by ``(canonical_name, entity_type)`` -- the
        same pair the v6 index ``idx_entities_canonical`` uses. The
        FIRST occurrence supplies the display ``name`` and the type;
        every later item with the same key contributes its surface
        form as an ALIAS (when it differs) and is otherwise dropped.

        So "Ahmed", "AHMED", and " ahmed " are one node with two
        aliases, while "Ahmed" and "Ahmed Ali" are two nodes. See
        design invariant #4 for why fuzzy identity merging is
        intentionally NOT done here.

        Rejection rule
        --------------
        An item is skipped (not fatal) when:

          * it is not a dict,
          * it has no usable name (missing / non-string /
            whitespace-only),
          * its canonical form is shorter than
            ``_MIN_CANONICAL_CHARS``,
          * it contains no letter or digit at all (pure punctuation
            or emoji),
          * ``Entity.build`` rejects it.

        Processing stops once ``_max_entities`` unique entities are
        collected -- later items are ignored rather than truncated
        from the middle, keeping the earliest mentions (which are
        the most salient).
        """
        ordered: List[Entity] = []
        by_key: Dict[Tuple[str, str], int] = {}
        rejected = 0

        for item in raw_items:
            if len(ordered) >= self._max_entities:
                break
            if not isinstance(item, dict):
                rejected += 1
                continue

            raw_name = first_present(
                item, "name", "text", "entity", "value", "surface"
            )
            raw_type = first_present(
                item, "type", "entity_type", "label", "category"
            )

            if not isinstance(raw_name, str):
                rejected += 1
                continue

            display = " ".join(raw_name.split())
            if not display:
                rejected += 1
                continue

            canonical = canonicalize_name(display)
            if len(canonical) < _MIN_CANONICAL_CHARS:
                rejected += 1
                continue
            if not _has_alnum(canonical):
                rejected += 1
                continue
            # Single-token function words are never entities. See
            # ``_SINGLE_TOKEN_STOPWORDS`` for why this applies ONLY
            # to single tokens.
            if (
                " " not in canonical
                and canonical in _SINGLE_TOKEN_STOPWORDS
            ):
                rejected += 1
                continue

            try:
                entity = Entity.build(raw_type, display)
            except ValueError:
                rejected += 1
                continue

            # Key on the SAME pair the storage index uses, so two
            # mentions dedupe here exactly when the store would
            # resolve them to one row.
            key = (canonical, entity.entity_type)
            existing_index = by_key.get(key)
            if existing_index is not None:
                # Same entity, different surface form: fold the new
                # spelling in as an alias (a no-op when it matches
                # what the entity already carries).
                ordered[existing_index] = ordered[
                    existing_index
                ].with_alias(display)
                continue

            by_key[key] = len(ordered)
            ordered.append(entity)

        if rejected:
            logger.debug(
                "EntityExtractor: kept %d entity/entities, skipped "
                "%d unusable item(s).",
                len(ordered),
                rejected,
            )
        return ordered

    def _canonicalize(self, name: str) -> str:
        """Expose the canonical-key rule for callers and tests.

        Delegates to ``contracts.canonicalize_name`` so there is
        exactly ONE folding rule in the system. Exposed as a method
        because the §7.5 contract lists it on the extractor, and a
        tests/diagnostics caller finds it where it expects.
        """
        return canonicalize_name(name)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when a gateway AND a resolvable model are present.

        ``False`` means every ``extract`` call is a no-op. Exposed so
        ``MemoryService`` can log an honest "graph indexing is
        inactive because ..." line at startup instead of leaving the
        operator to infer it from absent data.
        """
        return (
            self._gateway is not None
            and bool(self._provider_id)
            and bool(self._model_id)
        )

    @property
    def provider_id(self) -> str:
        """The resolved extraction provider id (empty when unset)."""
        return self._provider_id

    @property
    def model_id(self) -> str:
        """The resolved extraction model id (empty when unset)."""
        return self._model_id

    @property
    def timeout_seconds(self) -> float:
        """The authoritative per-call deadline."""
        return self._timeout_seconds

    @property
    def max_entities(self) -> int:
        """The per-memory entity cap."""
        return self._max_entities

    def __repr__(self) -> str:
        return (
            f"EntityExtractor(provider={self._provider_id!r}, "
            f"model={self._model_id!r}, "
            f"max_entities={self._max_entities}, "
            f"timeout={self._timeout_seconds}s, "
            f"configured={self.is_configured})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_alnum(text: str) -> bool:
    """True when ``text`` contains at least one letter or digit.

    Filters pure-punctuation candidates ("--", "**", "…") that the
    model sometimes emits for bullet formatting. Implemented with
    ``str.isalnum`` so it is unicode-aware (Arabic, Cyrillic, CJK
    names all pass).
    """
    for char in text:
        if char.isalnum():
            return True
    return False


def _positive_int(value: Any, *, default: int) -> int:
    """Coerce to a positive int, falling back to ``default``.

    Booleans are rejected (``True`` as a token count is a schema
    mistake, not a value of 1). Zero and negatives fall back rather
    than raising, because these arrive from a hand-edited settings
    file.
    """
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


def _positive_float(value: Any, *, default: float) -> float:
    """Coerce to a positive float, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if numeric != numeric or numeric <= 0.0:  # NaN or non-positive
        return default
    return numeric


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ENTITY_PROMPT",
    "SYSTEM_PROMPT",
    "EntityExtractor",
]
