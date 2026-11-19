# backend/peldrun/memory/graph/contracts.py
"""
PELDRUN Memory Platform - Knowledge Graph Domain Contracts.

The PURE value objects that describe the knowledge graph: what an
entity is, what a typed relation is, and how a name is reduced to a
canonical form. This module is the contract boundary between the
``peldrun`` core (which reasons over entities) and ``omweb`` (which
persists them in SQLite).

Design invariants
-----------------

1. **No ``omweb`` import, ever.**
   This is the same rule ``peldrun.memory.domain`` follows: the core
   must not know where graph data is stored. ``omweb`` imports these
   types; these types import nothing from ``omweb``. A future
   storage backend (Postgres, an external graph DB, an in-memory
   test double) implements the storage protocol without touching
   this file.

2. **Frozen value objects with self-normalizing constructors.**
   ``Entity`` and ``Relation`` are ``frozen=True``, so a caller that
   mutates a returned object gets an immediate error instead of a
   silent aliasing bug between the extractor's list and the store's
   cache. Construction NORMALIZES its inputs (entity type casing,
   canonical name folding, alias cleanup, confidence clamping) so
   that no downstream component has to defend against the LLM
   emitting ``"Person"``, ``" person"``, or ``"0.95 "``.

3. **Closed vocabularies with tolerant normalization.**
   The LLM is asked for one of six entity types and a small set of
   relation types, but it will sometimes answer ``"ORGANIZATION"``,
   ``"Company"``, or ``"EMPLOYMENT"``. ``normalize_entity_type`` and
   ``normalize_relation_type`` map the observed spelling onto the
   closed vocabulary via an explicit alias table rather than
   rejecting the candidate and losing a real entity. Anything
   genuinely unknown degrades to ``TOPIC`` / ``related_to`` -- the
   correct failure mode for a graph: a vaguely-typed node is still
   a useful node, whereas a dropped node is a permanent recall hole.

4. **Deterministic identity for relations.**
   ``make_relation_id`` derives the id from the triple
   ``(source, target, type)`` with SHA-1. ``GraphStore.add_relation``
   can therefore use a plain ``INSERT OR IGNORE`` on the primary key
   and get idempotency for free: re-indexing the same memory
   produces the SAME id, so the row is skipped instead of duplicated.
   A caller may still supply its own id; the store honors it, and
   the UNIQUE index from migration v6 backstops the invariant.

5. **Validation is for programmer errors only.**
   ``__post_init__`` raises only on genuinely unusable input -- an
   empty entity name, an empty relation endpoint, confidence outside
   ``[0, 1]``. Every condition that an LLM can plausibly violate is
   NORMALIZED rather than raised, because the extractor's contract
   is "return ``[]`` on failure", not "crash the write pipeline".

6. **Aliases are deduplicated and canonical-free.**
   ``aliases`` holds the OTHER surface forms observed for an entity
   ("Ahmed", "Ahmed Ali"). The canonical form itself is excluded --
   storing it twice would make alias-count-based heuristics
   (confidence boosting) lie about how many alternates exist.

See also
--------

    * ``peldrun/memory/graph/entity_extractor.py``
        -- produces ``Entity`` objects from memory content
    * ``peldrun/memory/graph/graph_search.py``
        -- consumes ``RelationHop`` results from a traversal
    * ``omweb/memory/storage/graph_store.py``
        -- persists these values in the v6 tables
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


ENTITY_TYPES: Tuple[str, ...] = (
    "PERSON",
    "ORG",
    "PROJECT",
    "PLACE",
    "TOPIC",
    "DATE",
)
"""The six entity types the extractor may produce.

Chosen to cover the questions Agentic RAG must answer for a personal
memory platform:

  * ``PERSON``  -- who said / manages / knows something,
  * ``ORG``     -- employers, vendors, institutions,
  * ``PROJECT`` -- named work the user refers to by name,
  * ``PLACE``   -- locations ("moved to Cairo"),
  * ``TOPIC``   -- recurring concepts, technologies, preferences,
  * ``DATE``    -- dates and periods ("December 2024").

Kept deliberately small. Every extra type multiplies the LLM's
opportunity to disagree with itself across calls, which fragments
the graph: the same real-world thing ends up as two nodes under two
types. Six types are enough to disambiguate the multi-hop questions
Phase 6 targets.
"""


RELATION_TYPES: Tuple[str, ...] = (
    "works_at",
    "manages",
    "uses",
    "lives_in",
    "part_of",
    "knows",
    "related_to",
)
"""The relation types the extractor may produce.

``related_to`` is the designated FALLBACK: any edge the extractor
believes exists but cannot type confidently is recorded as
``related_to`` rather than dropped. An untyped edge still supports
two-hop reachability ("who is connected to Ahmed?"); a dropped edge
supports nothing.
"""


_DEFAULT_ENTITY_TYPE: str = "TOPIC"
_DEFAULT_RELATION_TYPE: str = "related_to"


# ---------------------------------------------------------------------------
# Alias tables
# ---------------------------------------------------------------------------
#
# Keys are NORMALIZED (upper-case, collapsed whitespace, stripped).
# Values are members of the closed vocabulary above. The tables are
# explicit rather than fuzzy-matched on purpose: a substring rule
# ("contains ORG") would map "ORGANIC" to ORG and quietly pollute the
# graph. Every alias below is a spelling actually observed from
# instruction-tuned models in production-style prompts.

_ENTITY_TYPE_ALIASES: Dict[str, str] = {
    # PERSON
    "PERSON": "PERSON",
    "PEOPLE": "PERSON",
    "PER": "PERSON",
    "HUMAN": "PERSON",
    "NAME": "PERSON",
    # ORG
    "ORG": "ORG",
    "ORGANIZATION": "ORG",
    "ORGANISATION": "ORG",
    "COMPANY": "ORG",
    "CORPORATION": "ORG",
    "EMPLOYER": "ORG",
    "TEAM": "ORG",
    "INSTITUTION": "ORG",
    # PROJECT
    "PROJECT": "PROJECT",
    "PRODUCT": "PROJECT",
    "REPO": "PROJECT",
    "REPOSITORY": "PROJECT",
    "CODEBASE": "PROJECT",
    # PLACE
    "PLACE": "PLACE",
    "LOCATION": "PLACE",
    "LOC": "PLACE",
    "CITY": "PLACE",
    "COUNTRY": "PLACE",
    "GPE": "PLACE",
    # DATE
    "DATE": "DATE",
    "TIME": "DATE",
    "PERIOD": "DATE",
    "TEMPORAL": "DATE",
    # TOPIC
    "TOPIC": "TOPIC",
    "CONCEPT": "TOPIC",
    "SUBJECT": "TOPIC",
    "TECH": "TOPIC",
    "TECHNOLOGY": "TOPIC",
    "THING": "TOPIC",
    "OTHER": "TOPIC",
    "MISC": "TOPIC",
}


_RELATION_TYPE_ALIASES: Dict[str, str] = {
    # employment
    "WORKS_AT": "works_at",
    "WORKS_FOR": "works_at",
    "WORK_AT": "works_at",
    "EMPLOYED_AT": "works_at",
    "EMPLOYED_BY": "works_at",
    # management
    "MANAGES": "manages",
    "MANAGER_OF": "manages",
    "LEADS": "manages",
    "REPORTS_TO": "manages",
    "SUPERVISES": "manages",
    # tooling
    "USES": "uses",
    "USING": "uses",
    "BUILT_WITH": "uses",
    "DEPENDS_ON": "uses",
    # location
    "LIVES_IN": "lives_in",
    "LIVES_AT": "lives_in",
    "LOCATED_IN": "lives_in",
    "BASED_IN": "lives_in",
    # composition
    "PART_OF": "part_of",
    "BELONGS_TO": "part_of",
    "MEMBER_OF": "part_of",
    # social
    "KNOWS": "knows",
    "WORKS_WITH": "knows",
    "COLLEAGUE_OF": "knows",
    "ACQUAINTED_WITH": "knows",
    # fallback
    "RELATED_TO": "related_to",
    "RELATED": "related_to",
    "ASSOCIATED_WITH": "related_to",
    "MENTIONS": "related_to",
    "CONNECTED_TO": "related_to",
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")

#: Maximum canonical name length, in characters. Matches the
#: ``_MAX_SCOPE_ID_CHARS`` philosophy from the memory domain: bound
#: the value at the boundary so a pathological "entity" (a whole
#: paragraph the LLM mistook for a name) cannot bloat a row or a
#: prompt. 128 comfortably fits any real person / org / project name.
_MAX_CANONICAL_NAME_CHARS: int = 128


def canonicalize_name(name: str) -> str:
    """Reduce a surface form to its canonical, comparison-safe key.

    The transformation chain is:

      1. **NFKC unicode normalization** -- folds compatibility
         characters so full-width or ligature spellings ("Ａｈｍｅｄ")
         compare equal to their ASCII form.
      2. **Casefold** (not ``lower()``) -- correct for the German
         sharp s and Turkish dotted-i edge cases, and the standard
         choice for case-insensitive comparison.
      3. **Whitespace collapse** -- ``"Ahmed   Ali"`` and
         ``"Ahmed Ali"`` become the same key, and leading/trailing
         whitespace disappears.
      4. **Length cap** at ``_MAX_CANONICAL_NAME_CHARS``.

    Deliberately NOT done here:

      * **Punctuation stripping.** ``"AcmeCorp"`` and ``"Acme Corp"`
        are different strings, but stripping punctuation would also
        merge ``"C++"`` into ``"c"`` and ``"node.js"`` into
        ``"nodejs"`` -- destroying distinguishing signal for exactly
        the technology topics this graph cares about. Alias matching
        handles punctuation variants explicitly instead.
      * **Arabic diacritic removal.** The memory subsystem's
        ``normalizer.py`` already deals with that for FTS; the graph
        key must match what ``graph_entities.canonical_name`` stores,
        and a single folding rule (this function) is used on BOTH
        the extraction and the lookup side, so consistency is
        guaranteed without duplicating the language logic here.

    Parameters
    ----------
    name
        The raw surface form. May contain surrounding whitespace,
        mixed case, or non-ASCII characters.

    Returns
    -------
    str
        The canonical key. Empty string when ``name`` is empty or
        whitespace-only (the caller decides whether that is an
        error).
    """
    if not isinstance(name, str):
        return ""
    folded = unicodedata.normalize("NFKC", name)
    folded = _WHITESPACE_RE.sub(" ", folded).strip().casefold()
    return folded[:_MAX_CANONICAL_NAME_CHARS]


def normalize_entity_type(
    raw: object,
    *,
    default: str = _DEFAULT_ENTITY_TYPE,
) -> str:
    """Map an LLM-reported entity type onto the closed vocabulary.

    Accepts any spelling the model produced (``"Person"``,
    ``"organization"``, ``"GPE"``), upper-cases and trims it, then
    looks it up in ``_ENTITY_TYPE_ALIASES``. Unknown values return
    ``default`` (``TOPIC``) instead of raising.

    Why ``TOPIC`` is the right fallback (rather than dropping the
    entity): a mis-typed node still participates in traversal and
    retrieval. Losing it creates a permanent hole in the graph that
    no later query can discover -- the failure is silent and
    unrecoverable, which is far worse than an imperfect label.

    Parameters
    ----------
    raw
        Whatever the model emitted. Non-strings are handled (they
        yield ``default``).
    default
        The fallback type. Must be a member of ``ENTITY_TYPES``;
        a caller passing something else is a programmer error and
        is corrected defensively rather than honored.

    Returns
    -------
    str
        A member of ``ENTITY_TYPES``.
    """
    fallback = (
        default if default in ENTITY_TYPES else _DEFAULT_ENTITY_TYPE
    )
    if not isinstance(raw, str):
        return fallback
    key = _WHITESPACE_RE.sub(" ", raw).strip().upper().replace(" ", "_")
    if not key:
        return fallback
    mapped = _ENTITY_TYPE_ALIASES.get(key)
    if mapped is not None:
        return mapped
    # Tolerate a trailing/leading underscore left by the space
    # replacement (e.g. "PERSON " -> "PERSON" already handled, but
    # " PERSON" -> "_PERSON" would not be).
    mapped = _ENTITY_TYPE_ALIASES.get(key.strip("_"))
    return mapped if mapped is not None else fallback


def normalize_relation_type(
    raw: object,
    *,
    default: str = _DEFAULT_RELATION_TYPE,
) -> str:
    """Map an LLM-reported relation type onto the closed vocabulary.

    Same contract as :func:`normalize_entity_type`. Unknown relations
    degrade to ``related_to`` so that the EDGE survives even when its
    label does not -- preserving reachability, which is what multi-hop
    retrieval actually consumes.
    """
    fallback = (
        default
        if default in RELATION_TYPES
        else _DEFAULT_RELATION_TYPE
    )
    if not isinstance(raw, str):
        return fallback
    key = _WHITESPACE_RE.sub(" ", raw).strip().lower()
    key = key.replace(" ", "_").replace("-", "_")
    if not key:
        return fallback
    mapped = _RELATION_TYPE_ALIASES.get(key.upper())
    if mapped is not None:
        return mapped
    mapped = _RELATION_TYPE_ALIASES.get(key.strip("_").upper())
    return mapped if mapped is not None else fallback


def make_entity_id(entity_type: str, canonical_name: str) -> str:
    """Derive a deterministic entity id from the identity key.

    ``entity_id = "ent_" + sha1("<TYPE>|<canonical>")[:24]``

    The identity of an entity in this graph is the PAIR
    ``(entity_type, canonical_name)`` -- the same pair the v6 index
    ``idx_entities_canonical`` is built on. Deriving the primary key
    from that pair makes ``GraphStore.add_entity`` idempotent by
    construction: observing "AcmeCorp" in two different memories
    computes the same id, so the second insert is a primary-key
    collision that ``INSERT OR IGNORE`` skips. A resolution step is
    unnecessary, and a race between two concurrent indexers cannot
    create two rows for one entity.

    Including the TYPE in the hash is what keeps "Ahmed" the PERSON
    separate from "Ahmed" the PLACE, matching the storage index.

    Parameters
    ----------
    entity_type
        Any spelling; normalized through ``normalize_entity_type``
        first so ``"person"`` and ``"PERSON"`` hash identically.
    canonical_name
        The folded key from ``canonicalize_name``. A raw surface form
        works too (it is re-folded here), but passing the canonical
        form avoids doing the work twice.

    Returns
    -------
    str
        A stable id, safe to use as a primary key.

    Raises
    ------
    ValueError
        When the canonical name folds to empty. An entity with no
        name has no identity, and hashing it would map every nameless
        entity onto the SAME id -- silently merging unrelated data.
    """
    entity_type_norm = normalize_entity_type(entity_type)
    canonical = canonicalize_name(canonical_name)
    if not canonical:
        raise ValueError(
            "make_entity_id requires a non-empty canonical name; "
            f"got {canonical_name!r}. A nameless entity has no "
            "identity (every one would hash to the same id)."
        )
    payload = f"{entity_type_norm}|{canonical}"
    digest = hashlib.sha1(
        payload.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"ent_{digest[:24]}"


def make_relation_id(
    source_entity_id: str,
    target_entity_id: str,
    relation_type: str,
) -> str:
    """Derive a deterministic relation id from the triple.

    ``relation_id = "rel_" + sha1("<source>|<target>|<type>")[:32]``

    Determinism is the mechanism behind ``GraphStore.add_relation``'s
    idempotency: indexing the same memory twice derives the same id,
    so ``INSERT OR IGNORE`` skips the second write instead of
    creating a duplicate edge. Without it, "the user worked at Acme"
    observed in two memories would become two parallel edges and
    inflate every degree-based score.

    Deliberately excludes ``source_memory_id`` and ``confidence``
    from the hash: those are attributes of the observation, not of
    the fact. Two memories asserting the same relation should
    converge on ONE edge (the first observation wins, which is why
    the migration also declares a UNIQUE index on the triple).

    Parameters
    ----------
    source_entity_id, target_entity_id
        Persisted entity ids (assigned by the store, so non-empty).
    relation_type
        A member of ``RELATION_TYPES``. Normalized defensively.

    Returns
    -------
    str
        A stable, collision-resistant id string.
    """
    rel_type = normalize_relation_type(relation_type)
    payload = f"{source_entity_id}|{target_entity_id}|{rel_type}"
    digest = hashlib.sha1(
        payload.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"rel_{digest[:32]}"


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """One node in the knowledge graph.

    Attributes
    ----------
    entity_id
        Persisted primary key. **Empty until the store assigns one**:
        the extractor has no way to know whether "Ahmed" already
        exists, so it emits ``""`` and ``GraphStore.add_entity``
        either resolves the existing row or mints a new id. An entity
        that has never been persisted therefore compares unequal to
        its own persisted form -- use ``with_entity_id`` (or the
        store's return value) to get the authoritative instance.
    entity_type
        A member of ``ENTITY_TYPES``. Always upper-case; the
        constructor normalizes.
    name
        The display surface form, as observed ("Ahmed Ali").
    canonical_name
        The comparison key, always ``canonicalize_name(name)`` when
        not supplied explicitly. Auto-derived in ``__post_init__``,
        so callers cannot accidentally persist a row whose name and
        canonical form disagree.
    aliases
        Other observed surface forms, order-preserving and
        deduplicated. Excludes the canonical form.

    Mutating helpers
    ----------------
    ``with_entity_id`` and ``with_alias`` return COPIES (frozen
    dataclass + ``replace``), so an extractor can hold one instance
    while the store derives its persisted twin without either side
    observing the other's change.
    """

    entity_id: str = ""
    entity_type: str = _DEFAULT_ENTITY_TYPE
    name: str = ""
    canonical_name: str = ""
    aliases: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalize every field, raising only on unusable input.

        The only hard errors are:

          * ``name`` not a non-empty string -- an entity with no name
            cannot be looked up, linked, or displayed; persisting it
            would be pure noise,
          * ``entity_id`` / ``canonical_name`` not strings.

        Everything else (type casing, alias hygiene, a missing
        canonical name) is corrected in place via
        ``object.__setattr__``, which is the documented way to
        normalize a ``frozen`` dataclass during construction.
        """
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                "Entity.name must be a non-empty string; got "
                f"{self.name!r}. An unnamed entity cannot be "
                "resolved, linked, or displayed."
            )
        if not isinstance(self.entity_id, str):
            raise ValueError(
                "Entity.entity_id must be a string, got "
                f"{type(self.entity_id).__name__}"
            )
        if not isinstance(self.canonical_name, str):
            raise ValueError(
                "Entity.canonical_name must be a string, got "
                f"{type(self.canonical_name).__name__}"
            )

        clean_name = _WHITESPACE_RE.sub(" ", self.name).strip()
        object.__setattr__(self, "name", clean_name)

        canonical = canonicalize_name(self.canonical_name)
        if not canonical:
            canonical = canonicalize_name(clean_name)
        if canonical != self.canonical_name:
            object.__setattr__(self, "canonical_name", canonical)

        entity_type = normalize_entity_type(self.entity_type)
        if entity_type != self.entity_type:
            object.__setattr__(self, "entity_type", entity_type)

        cleaned_aliases = _clean_aliases(self.aliases, canonical)
        if cleaned_aliases != tuple(self.aliases):
            object.__setattr__(self, "aliases", cleaned_aliases)

    # -- constructors --------------------------------------------------

    @classmethod
    def build(
        cls,
        entity_type: object,
        name: object,
        *,
        aliases: Tuple[str, ...] = (),
    ) -> "Entity":
        """Build an UNPERSISTED entity from raw extractor output.

        This is the constructor the entity extractor uses: it takes
        whatever the model emitted, coerces it, and returns an entity
        whose ``entity_id`` is ``""`` (to be assigned by the store).

        Raises
        ------
        ValueError
            Only when ``name`` is unusable. The extractor catches it
            and drops that one candidate.
        """
        return cls(
            entity_id="",
            entity_type=str(entity_type) if entity_type else "",
            name=str(name) if name is not None else "",
            canonical_name="",
            aliases=tuple(aliases or ()),
        )

    # -- immutable updates ---------------------------------------------

    def with_entity_id(self, entity_id: str) -> "Entity":
        """Return a copy bound to the persisted id."""
        return replace(self, entity_id=str(entity_id))

    def with_alias(self, *aliases: str) -> "Entity":
        """Return a copy with additional aliases merged in.

        Idempotent: merging an alias the entity already carries (or
        the canonical form itself) leaves the entity unchanged.
        Used by ``GraphStore.add_entity`` when the same real-world
        entity is observed under a new surface form.
        """
        merged = _clean_aliases(
            tuple(self.aliases) + tuple(a or "" for a in aliases),
            self.canonical_name,
        )
        return replace(self, aliases=merged)

    # -- domain predicates ---------------------------------------------

    @property
    def is_persisted(self) -> bool:
        """True once the store has assigned an ``entity_id``."""
        return bool(self.entity_id)

    def matches(self, other_name: str) -> bool:
        """True when ``other_name`` resolves to this entity's key."""
        return canonicalize_name(other_name) == self.canonical_name

    def __repr__(self) -> str:
        return (
            f"Entity(id={self.entity_id or '<unpersisted>'!r}, "
            f"type={self.entity_type}, name={self.name!r}, "
            f"aliases={len(self.aliases)})"
        )


# ---------------------------------------------------------------------------
# Relation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    """One typed, directed edge between two persisted entities.

    Attributes
    ----------
    relation_id
        Primary key. When empty, ``__post_init__`` derives it with
        :func:`make_relation_id`, which is what makes repeated
        indexing idempotent.
    source_entity_id, target_entity_id
        Both MUST be persisted entity ids (non-empty). The direction
        is meaningful: ``(Ahmed) --manages--> (PELDRUN)`` and
        ``(PELDRUN) --manages--> (Ahmed)`` are different facts.
    relation_type
        A member of ``RELATION_TYPES``; normalized by the
        constructor.
    source_memory_id
        The memory that produced this edge, for provenance and for
        the UI's "why is this connected?" affordance. ``None`` for
        edges synthesized without a backing memory (e.g. a future
        co-occurrence pass over an import).
    confidence
        Extractor confidence in ``[0, 1]``. Clamped, never raised:
        a wildly-out-of-range value from the LLM is a unit mistake,
        not a reason to lose the edge. See ``_clamp_confidence`` for
        the exact bands: ``[2, 100]`` is read as a percentage,
        ``(1, 2)`` as rounding slop clamped to ``1.0``.
    """

    relation_id: str = ""
    source_entity_id: str = ""
    target_entity_id: str = ""
    relation_type: str = _DEFAULT_RELATION_TYPE
    source_memory_id: Optional[str] = None
    confidence: float = 0.8

    def __post_init__(self) -> None:
        """Normalize and validate; raise only on unusable input."""
        for label, value in (
            ("source_entity_id", self.source_entity_id),
            ("target_entity_id", self.target_entity_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"Relation.{label} must be a non-empty persisted "
                    f"entity id; got {value!r}. A relation with a "
                    f"missing endpoint is not a fact -- the v6 "
                    f"foreign key would reject it anyway."
                )
        if not isinstance(self.relation_id, str):
            raise ValueError(
                "Relation.relation_id must be a string, got "
                f"{type(self.relation_id).__name__}"
            )
        if self.source_memory_id is not None and not isinstance(
            self.source_memory_id, str
        ):
            raise ValueError(
                "Relation.source_memory_id must be a string or None, "
                f"got {type(self.source_memory_id).__name__}"
            )

        rel_type = normalize_relation_type(self.relation_type)
        if rel_type != self.relation_type:
            object.__setattr__(self, "relation_type", rel_type)

        if not self.relation_id:
            object.__setattr__(
                self,
                "relation_id",
                make_relation_id(
                    self.source_entity_id,
                    self.target_entity_id,
                    rel_type,
                ),
            )

        clamped = _clamp_confidence(self.confidence)
        if clamped != self.confidence:
            object.__setattr__(self, "confidence", clamped)

        if self.source_entity_id == self.target_entity_id:
            # Self-loops carry no reachability information and
            # would let a traversal "discover" its own start node
            # at distance 0, corrupting hop-distance scores.
            raise ValueError(
                "Relation must connect two DISTINCT entities; "
                f"got a self-loop on {self.source_entity_id!r}."
            )

    @classmethod
    def build(
        cls,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: object,
        *,
        source_memory_id: Optional[str] = None,
        confidence: object = 0.8,
    ) -> "Relation":
        """Build a relation, deriving its id and normalizing fields."""
        return cls(
            relation_id="",
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relation_type=(
                str(relation_type) if relation_type else ""
            ),
            source_memory_id=source_memory_id,
            confidence=_coerce_confidence(confidence),
        )

    def reversed(self) -> "Relation":
        """Return the inverse direction of this edge.

        The relation TYPE is preserved (``manages`` stays
        ``manages``) -- this is a direction flip, not a semantic
        inversion. Traversal uses it to walk a bidirectional view of
        the graph without inventing inverse vocabulary.
        """
        return Relation(
            relation_id="",
            source_entity_id=self.target_entity_id,
            target_entity_id=self.source_entity_id,
            relation_type=self.relation_type,
            source_memory_id=self.source_memory_id,
            confidence=self.confidence,
        )

    def __repr__(self) -> str:
        return (
            f"Relation({self.source_entity_id}"
            f" -[{self.relation_type}, {self.confidence:.2f}]-> "
            f"{self.target_entity_id})"
        )


# ---------------------------------------------------------------------------
# Traversal result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationHop:
    """A relation discovered during a BFS, plus its distance.

    ``distance`` is the number of edges from the traversal's START
    entity to the FAR endpoint of ``relation``:

      * ``1`` -- the neighbour was reached in one hop,
      * ``2`` -- two hops (the multi-hop case Phase 6 exists for).

    Graph search converts this into a relevance score with
    ``1 / (1 + distance)``, so a direct neighbour (1.0 / 0.5) always
    outranks a two-hop discovery (1.0 / 0.333) for the same memory.

    Keeping the distance OUT of ``Relation`` is deliberate: distance
    is a property of one traversal, not of the stored fact. The same
    edge is distance 1 in one query and distance 2 in another.
    """

    relation: Relation
    distance: int

    def __post_init__(self) -> None:
        if not isinstance(self.relation, Relation):
            raise ValueError(
                "RelationHop.relation must be a Relation instance, "
                f"got {type(self.relation).__name__}"
            )
        if (
            not isinstance(self.distance, int)
            or isinstance(self.distance, bool)
            or self.distance < 1
        ):
            raise ValueError(
                "RelationHop.distance must be an int >= 1 (0 would "
                "describe the start node itself, which is not a "
                f"hop); got {self.distance!r}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clean_aliases(
    aliases: object,
    canonical_name: str,
) -> Tuple[str, ...]:
    """Normalize an alias collection.

    Rules:

      * non-strings and blank strings are dropped,
      * whitespace is collapsed and the display form is preserved
        (aliases keep their original casing for UI display; they are
        NOT canonicalized to the folded key, otherwise the graph
        would render "ahmed" instead of "Ahmed"),
      * duplicates are removed case-insensitively,
      * the entity's OWN name (== canonical form) is excluded, so
        alias counts reflect genuinely distinct surface forms.

    Returns a tuple in first-seen order, so the result is
    deterministic for a given input list.
    """
    cleaned: list = []
    seen: set = set()
    if not isinstance(aliases, (list, tuple)):
        return ()
    for raw in aliases:
        if not isinstance(raw, str):
            continue
        display = _WHITESPACE_RE.sub(" ", raw).strip()
        if not display:
            continue
        key = canonicalize_name(display)
        if not key or key == canonical_name or key in seen:
            continue
        seen.add(key)
        cleaned.append(display)
    return tuple(cleaned)


def _coerce_confidence(value: object, *, default: float = 0.8) -> float:
    """Coerce extractor output into a float confidence.

    Strings are parsed (the LLM often emits ``"0.9"``); booleans are
    rejected because ``True`` as "1.0" is almost certainly a schema
    mistake; unparseable values fall back to ``default`` rather than
    dropping the edge.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def _clamp_confidence(value: float) -> float:
    """Clamp a confidence into ``[0.0, 1.0]``, rescaling percentages.

    The LLM occasionally answers on a 0-100 scale even when asked for
    0-1. Treating ``95`` as an out-of-range value and clamping to
    ``1.0`` would be indistinguishable from a perfectly confident
    edge AND would collapse the ordering between a 95 and a 60 edge;
    rescaling preserves it.

    Disambiguation rule
    -------------------
    ``> 1.0`` is ambiguous on its own, so two bands are used:

      * ``(1.0, 2.0)``  -- treated as ROUNDING SLOP from a 0-1
        scale and clamped to ``1.0``. No realistic 0-100 confidence
        is expressed as ``1.4``: a model scoring an edge at ~1%
        emits ``1`` or ``2``, not a fractional percentage. A
        fractional value just above 1 is therefore a 0-1 model
        being sloppy, and ``1.0`` is the honest reading.
      * ``[2.0, 100.0]`` -- treated as a 0-100 PERCENTAGE and
        divided by 100.
      * ``> 100.0``     -- not a confidence in any scale; clamped
        to ``1.0``.

    Getting this wrong in the other direction (dividing ``1.4`` by
    100) would score a maximally confident edge below an average
    one, inverting the ranking -- the failure this rule exists to
    prevent.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.8
    if numeric != numeric:  # NaN -- must never reach a score
        return 0.8
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        if 2.0 <= numeric <= 100.0:
            return round(numeric / 100.0, 4)
        return 1.0
    return numeric


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ENTITY_TYPES",
    "RELATION_TYPES",
    "Entity",
    "Relation",
    "RelationHop",
    "canonicalize_name",
    "make_entity_id",
    "make_relation_id",
    "normalize_entity_type",
    "normalize_relation_type",
]
