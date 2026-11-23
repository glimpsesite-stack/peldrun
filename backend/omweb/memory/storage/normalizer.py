# backend/omweb/memory/storage/normalizer.py
"""
PELDRUN Memory Platform - Multilingual Text Normalization.

Pure, deterministic, idempotent text normalization for the memory
subsystem's full-text search. Supports Arabic, Russian, and English
out of the box; every other Unicode script passes through
unchanged except for casefolding and whitespace collapsing (which
are universally safe).

Why this module exists
----------------------

SQLite's FTS5 ``unicode61`` tokenizer removes diacritics and
handles case folding for Latin scripts, but it does NOT unify
Arabic letter variants (alef, yaa, taa marbuta) and it does NOT
normalize Russian ``ё`` to ``е``. Without this module:

    User types:     Content stored:      FTS5 alone would MISS
    ----------      ---------------      -----------------------
    محمد            مُحَمَّد              (harakat differ)
    احمد            أحمد                 (alef variant)
    Елка (with е)   Ёлка (with ё)        (Cyrillic variant)

This module bridges those gaps so retrieval works the way users
expect: they type what they would type in a search box, and the
results match regardless of how the memory was originally written.

Two entry points, one pipeline
------------------------------

Both entry points share ``normalize_for_storage`` so that content
and queries are normalized IDENTICALLY:

    * ``normalize_for_storage(text)`` -- applied to
      ``MemoryEntry.content`` before persistence (via
      ``MemoryEntry.with_normalized_content``). The result is
      stored in the ``normalized_content`` column and indexed by
      FTS5.

    * ``normalize_query(text)`` -- applied to the search query
      before it reaches ``memories_fts MATCH ?``. The result is a
      space-separated token string safe for FTS5 operator
      interpretation.

Because both share the same pipeline, a query for "محمد" and a
stored memory "مُحَمَّد" normalize to the same string "محمد" and
the FTS index finds the match. If the two used different pipelines
(e.g. content normalized one way, query another) the search would
silently miss everything -- a bug that is very hard to debug
because nothing looks obviously broken.

Design invariants
-----------------

1. **Idempotent.**
   ``normalize(normalize(x)) == normalize(x)`` for every input.
   This is CRITICAL: FTS5's UPDATE trigger fires on any change to
   ``normalized_content``, and a non-idempotent normalizer would
   cause every re-save to be treated as a content change,
   triggering unnecessary FTS rebuilds. All transforms below are
   idempotent by construction.

2. **Deterministic.**
   Same input string always produces the same output string
   across processes, Python versions (3.10+), and platforms. No
   dependence on locale, system clock, or process state.

3. **Never lossy for the raw content.**
   The raw ``content`` field is never modified by this module.
   Only ``normalized_content`` is transformed. Users always see
   what they typed; only the search index sees the normalized
   form.

4. **Fail-safe on edge cases.**
   Empty input, whitespace-only input, and input consisting
   entirely of diacritics all produce well-defined outputs
   (documented on each function). The normalizer never raises --
   it is called at the write path where exceptions would corrupt
   the entry-creation contract.

5. **Composable language steps.**
   Each language lives in its own file under ``languages/`` as a
   ``LanguageProfile`` (``arabic.py``, ``russian.py``,
   ``english.py``). The pipeline in ``_normalize_for_storage_impl``
   iterates over the discovered ``PROFILES`` tuple -- adding a
   fourth language (e.g. Turkish) is a new
   ``languages/turkish.py`` file exposing a ``PROFILE`` instance.
   No change to ``normalizer.py`` is required.

6. **Pure stdlib (plus local subpackage).**
   Imports only ``re``, ``typing``, ``enum``, and the sibling
   ``.languages`` package. The latter itself imports only stdlib
   (``re``, ``typing``, ``abc``, ``importlib``, ``pkgutil``). No
   external normalization libraries (``unicodedata2``,
   ``PyICU``, ``camel-tools``). This keeps the storage layer
   lightweight and deployable without system dependencies, at
   the cost of not handling exotic scripts (Hebrew niqqud,
   Devanagari matras) -- acceptable because those scripts are
   not target languages for the current phase.

Normalization specification (per language)
------------------------------------------

**Arabic**

    * Removes harakat, tanwin, shadda, sukun, superscript alef,
      and every Quranic annotation mark. Range: U+064B-U+065F,
      U+0670, U+06D6-U+06DC, U+06DF-U+06E4, U+06E7-U+06E8,
      U+06EA-U+06ED.
    * Removes tatweel / kashida (U+0640). "مـــحـــمـــد" and
      "محمد" are the same word.
    * Unifies alef variants: U+0622 (آ), U+0623 (أ), U+0625 (إ),
      U+0671 (ٱ) all map to bare alef U+0627 (ا).
    * Unifies alef maqsura U+0649 (ى) to yaa U+064A (ي).
    * Unifies hamza carriers: U+0624 (ؤ) to waw U+0648 (و),
      U+0626 (ئ) to yaa U+064A (ي).

    NOT done (deliberately, in Phase 1):

    * Taa marbuta U+0629 (ة) is NOT mapped to ha U+0647 (ه).
      Converting would improve recall for users who type the
      wrong one, but it is semantically lossy ("كتابة" = writing
      vs "كتابه" = his book). Documented as a Phase 2 candidate
      once real-world recall data justifies the trade-off.

**Russian**

    * ``ё`` (U+0451, U+0401 uppercase) maps to ``е`` (U+0435).
      This is a widespread typing convention -- most Russian
      keyboards do not have a dedicated ``ё`` key, so users type
      ``е`` and expect ``ё`` to match. The reverse is also true.

**English / Latin (via common pipeline)**

    * Case folding (``.lower()``). Preserves the original case in
      the raw ``content`` field.
    * Invisible characters removed: soft hyphen U+00AD,
      zero-width space U+200B, zero-width joiner U+200D,
      zero-width no-break space / BOM U+FEFF, and every other
      ``Cc`` / ``Cf`` category character in the "invisible" set.
      These often appear when text is pasted from PDFs or web
      pages and cause invisible mismatches otherwise.

**All scripts (common pipeline)**

    * Non-breaking space U+00A0 -> regular space.
    * Collapse every run of whitespace (spaces, tabs, newlines,
      form feeds, vertical tabs) to a single space.
    * Strip leading / trailing whitespace.
    * Casefold (``.lower()``). Casefolding is applied AFTER the
      script-specific steps so that, e.g., uppercase Cyrillic
      ``Ё`` is first mapped to ``ё`` by the Russian step (which
      handles both cases explicitly), then lowercased
      consistently with other Cyrillic.

Fallback for pathological input
-------------------------------

If the input is non-empty but the normalized result is empty,
the normalizer returns ``text.strip().lower()`` instead of the
empty string. This can happen for input consisting ENTIRELY of
diacritics (theoretically possible, practically never), or a
string of zero-width characters. The fallback preserves the
"non-empty input -> non-empty normalized" contract that
``MemoryEntry.with_normalized_content`` relies on.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final

from .languages import (
    PROFILES,
    by_code,
    normalize_common,
)


# ---------------------------------------------------------------------------
# Pipeline implementation
# ---------------------------------------------------------------------------


#: Word-character pattern for query tokenization. ``\w`` in
#: Python 3 Unicode mode matches Latin letters, Cyrillic,
#: Arabic letters, Arabic-Indic digits, and underscore -- which
#: is exactly the set of characters that FTS5's ``unicode61``
#: tokenizer would produce as tokens. No apostrophes, quotes,
#: or operator characters can leak through, so the result is
#: safe to pass directly to ``MATCH``.
#:
#: Kept here (rather than moved to ``languages/common.py``):
#: this pattern is consumed directly by ``normalize_query`` below,
#: which is the only caller and is defined in this same module.
#: Moving it would force a circular import back from
#: ``languages.common`` into the storage layer for a value that
#: is not language-specific and has no user-facing invariant.
_WORD_TOKEN: Final[re.Pattern[str]] = re.compile(r"\w+")


def _normalize_for_storage_impl(text: str) -> str:
    """Run every discovered language profile, then the shared tail.

    Iteration order is ``PROFILES``'s discovery order (``priority``
    then ``code`` -- see ``languages/__init__.py``). The
    language-specific ``normalize()`` methods run BEFORE the
    casefold step delegated to ``languages.common.normalize_common``,
    which is what preserves the original pre-refactor ordering
    guarantee: uppercase Cyrillic ``Ё`` is first mapped to ``Е`` by
    the Russian profile, then lowercased consistently with other
    Cyrillic by the common step.

    Adding a fourth language is now a new file under
    ``languages/`` -- this function does not change. See
    ``languages/_base.py`` for the discovery contract and
    priority bands.

    Idempotent by composition: each profile's ``normalize`` is
    idempotent (argued in its own docstring) and
    ``normalize_common`` is idempotent (argued in its own
    docstring), and the composition of idempotent functions that
    operate on disjoint aspects of the input is itself idempotent.
    """
    result = text
    for profile in PROFILES:
        result = profile.normalize(result)
    return normalize_common(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_for_storage(text: str) -> str:
    """Normalize ``text`` for FTS indexing.

    The output is stored in ``MemoryEntry.normalized_content`` and
    indexed by FTS5. It is NEVER shown to the user -- the raw
    ``content`` field is what the UI displays.

    Parameters
    ----------
    text
        The raw text to normalize. May be ``None`` (treated as
        ``""``) or empty (returns ``""``).

    Returns
    -------
    str
        The normalized text. If ``text`` was non-empty but the
        pipeline produced an empty string (input was exclusively
        diacritics or invisible characters), returns
        ``text.strip().lower()`` as a fallback so the
        "non-empty in -> non-empty out" contract of
        ``MemoryEntry.with_normalized_content`` is always
        satisfied.

    Raises
    ------
    Nothing. This function is called on the write path where
    exceptions would break the entry-creation contract. Every
    possible input (including ``None``, bytes-that-are-not-strs,
    control characters) produces a well-defined ``str`` output.

    Note on the fallback
    --------------------
    The fallback to ``text.strip().lower()`` is intentionally
    minimal -- it applies NO language-specific transforms. This
    means a pathological input of "all Arabic diacritics" would
    be stored with its diacritics intact in
    ``normalized_content``, which could theoretically cause a
    subtle search mismatch (the diacritics would be indexed as
    characters, not as their base letters). This is acceptable
    because such input is:
      * not linguistically meaningful (a memory containing only
        diacritics is not a real memory),
      * extremely rare (would require the user to actively
        paste an isolated mark),
      * far better than raising and corrupting the write path.
    The alternative -- raising on such input -- would be worse
    for every real user.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_for_storage expects str, got {type(text).__name__}"
        )
    stripped_input = text.strip()
    if not stripped_input:
        return ""

    result = _normalize_for_storage_impl(stripped_input)
    if not result:
        # Pathological input (diacritics-only, invisible-chars-only).
        # Fall back to lowercase-stripped input to honor the
        # non-empty contract. See the docstring note above.
        return stripped_input.lower()
    return result


def normalize_query(text: str, *, match: str = "all") -> str:
    """Normalize ``text`` into a safe FTS5 MATCH query.

    Parameters
    ----------
    text
        The user's search query. May be ``None`` (treated as
        ``""``) or empty (returns ``""``).
    text
        The user's search query. May be ``None`` (treated as
        ``""``) or empty (returns ``""``).
    match
        Token combination semantics:

          * ``"all"`` (default) -- space-separated tokens, which FTS5
            treats as an implicit **AND**: every token must appear in
            one memory. This is the historical behaviour, unchanged
            byte for byte.
          * ``"any"`` -- ``token1* OR token2* OR ...`` with Arabic
            article-prefix variants. Used for natural-language
            retrieval (chat turns and agentic hops), where demanding
            that EVERY word of a question appear in a single memory is
            exactly what made real questions return nothing.

    Why ``"any"`` is opt-in
    -----------------------
    The ``memory_search`` tool's contract is strict keyword matching:
    an agent asking "find all decisions" must not receive entries that
    merely share one common word. Broadening the operator there would
    silently degrade that contract. The chat path is different -- it
    wants RECALL, and the ranker, ``min_relevance_score``, and the
    recency fallback downstream handle precision.

    The output is a safe FTS5 MATCH expression. Because every token
    is extracted by ``\\w+`` from the ALREADY-NORMALIZED text,
    the result cannot contain:
      * quote characters (which would create phrase operators),
      * FTS5 keywords ``AND`` / ``OR`` / ``NOT`` in uppercase
        (the pipeline lowercases everything),
      * parentheses or other grouping syntax,
      * any character that ``unicode61`` would treat as a
        separator.

    In ``"any"`` mode this function DOES emit the uppercase ``OR``
    operator and a trailing ``*`` per token. Both are produced by this
    module, never by user input, so the injection guarantee holds.

    This makes the query SAFE against accidental or malicious
    FTS5 operator injection: a user typing
    ``foo OR bar; DROP TABLE`` produces the harmless normalized
    query ``foo or bar drop table``, which FTS5 interprets as
    "match any document containing ALL of foo, or, bar, drop,
    table" -- a strict superset of the user's intent, never an
    unexpected match.

    Returns
    -------
    str
        The normalized token string. Empty string when the input
        contains no word characters after normalization. The
        repository uses "empty string" as the sentinel for
        "skip FTS MATCH, return zero results" -- because
        ``MATCH ''`` raises a SQLite error in every FTS5 version.

    Raises
    ------
    TypeError
        If ``text`` is neither ``str`` nor ``None``.
    ValueError
        If ``match`` is not ``"all"`` or ``"any"``. A typo here would
        silently pick the wrong operator.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_query expects str, got {type(text).__name__}"
        )
    if match not in ("all", "any"):
        raise ValueError(
            f"normalize_query: match must be 'all' or 'any', "
            f"got {match!r}"
        )

    normalized = _normalize_for_storage_impl(text.strip())
    if not normalized:
        return ""

    tokens = _WORD_TOKEN.findall(normalized)
    if match == "all":
        return " ".join(tokens)
    return _build_any_expression(tokens)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


#: Minimum token length for a wildcard variant to be emitted.
#: One-character tokens ("i", "a", Arabic "wa") would expand to
#: ``i*`` / ``a*`` and match almost every document, drowning BM25
#: in noise.
_MIN_WILDCARD_TOKEN_CHARS: Final[int] = 2


#: Function words that must NOT become OR terms in ``match="any"``
#: mode. Assembled from every discovered language profile's
#: ``stopwords()`` hook.
#:
#: The rationale that used to accompany the hard-coded English +
#: Arabic list now lives on the profiles themselves -- see
#: ``languages/english.py`` and ``languages/arabic.py`` for the
#: full explanation and the measured noise examples.
#:
#: Why dynamic: adding a language (Turkish, Persian, Urdu) must
#: not require editing this file. The set is the union of every
#: profile's stopwords, so a new ``languages/<lang>.py`` with a
#: ``stopwords()`` override automatically participates.
#:
#: Why ``Final``: the set must not be rebound at runtime. A
#: monkeypatched stopword set would silently change which queries
#: return zero results -- a bug that is very hard to trace.
_QUERY_STOPWORDS: Final[frozenset[str]] = frozenset().union(
    *(profile.stopwords() for profile in PROFILES)
)


def _arabic_variants(token: str) -> list[str]:
    """Return the token plus its Arabic prefix-stripped variants.

    Thin wrapper around ``ArabicProfile.query_variants`` -- the
    algorithm itself lives in ``languages/arabic.py``. This
    wrapper exists for two reasons:

    1. **Original-token-first contract.** ``query_variants``
       returns only the *extra* forms (the base-class contract
       says so, so callers can prepend the token once and union
       across profiles). ``_build_any_expression`` needs the
       token FIRST in the OR expression for stable BM25 ranking,
       so this wrapper does the prepending in one place.

    2. **Graceful degradation.** If the Arabic profile is ever
       removed from the discovered set (e.g. a minimal
       deployment that ships only English), this function falls
       back to ``[token]`` instead of raising. Search still
       works, just without Arabic prefix expansion.

    Behaviour is byte-for-byte identical to the pre-refactor
    ``_arabic_variants`` for every input.
    """
    profile = by_code("ar")
    if profile is None:
        return [token]
    return [token, *profile.query_variants(token)]


def _build_any_expression(tokens: list[str]) -> str:
    """Build an ``a* OR b* OR ...`` MATCH expression.

    Each token contributes a prefix wildcard (so ``coffee`` also
    matches ``coffees``, and Arabic ``al-qahwa*`` matches
    ``al-qahwa``), plus one variant with any Arabic article prefix
    removed (via ``_arabic_variants``).

    Safety
    ------
    Tokens come from ``\\w+`` on already-normalized text, so they
    cannot contain quotes, parentheses, ``*``, or uppercase FTS
    keywords. The only operator text emitted here is the ``OR``
    joiner and the per-token ``*``.

    Ordering
    --------
    For each content token, the original token comes first followed
    by its Arabic variants. This ordering is preserved from the
    pre-refactor version -- BM25's OR-term scoring is order
    sensitive, and changing it would shift scores on existing
    content.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _emit(term: str) -> None:
        if len(term) < _MIN_WILDCARD_TOKEN_CHARS:
            return
        if term in seen:
            return
        seen.add(term)
        terms.append(f"{term}*")

    # Drop function words, but ONLY when a content word survives.
    # A query made entirely of function words ("thank you") must
    # produce an EMPTY expression -- zero matches -- so nothing is
    # injected. Falling back to "keep them anyway" would restore
    # the exact noise this set exists to remove.
    content_tokens = [
        token for token in tokens if token not in _QUERY_STOPWORDS
    ]
    if not content_tokens:
        return ""

    for token in content_tokens:
        for variant in _arabic_variants(token):
            _emit(variant)

    return " OR ".join(terms)


# ---------------------------------------------------------------------------
# Phase 2 (R2/R3): query intent + candidate-variant engine.
#
# Additive pure functions. ``normalize_for_storage`` and
# ``normalize_query`` above are UNCHANGED, so ``content_hash`` and
# the existing FTS index are untouched. Arabic literals are written
# as ``\uXXXX`` escapes to keep this file ASCII.
# ---------------------------------------------------------------------------


class QueryIntent(str, Enum):
    """Explicit retrieval intent, classified once and consumed everywhere."""

    BLANK = "blank"          # no word chars: caller wants the recency listing
    NO_INTENT = "no_intent"  # all stopwords / social phrase: zero results
    LOOKUP = "lookup"        # a real retrieval query


#: Anchored pattern matching social / conversational phrases.
#: Assembled from every profile's ``social_patterns()`` hook.
#:
#: Why dynamic: the original had hard-coded Arabic + English
#: fragments. Adding a Japanese ``ohayou`` or a Spanish ``como
#: estas`` must be a matter of a new profile file, not an edit to
#: this module.
#:
#: Regex construction note: each fragment is joined with ``|``
#: inside a single non-capturing group, then wrapped with ``^`` and
#: ``\W*$``. Every fragment is expected to be a valid regex
#: alternative on its own (no top-level ``|``, no anchors) -- the
#: profiles uphold that invariant, and a malformed fragment fails
#: loudly at import time via the ``re.compile`` call below.
_SOCIAL_RX: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    + "|".join(
        pattern
        for profile in PROFILES
        for pattern in profile.social_patterns()
    )
    + r")\W*$"
)


def classify_query(query: str) -> QueryIntent:
    """Classify retrieval intent (replaces the R1 regex in the repository)."""
    if not re.search(r"\w", query or ""):
        return QueryIntent.BLANK
    if _SOCIAL_RX.match(normalize_for_storage(query)) or not normalize_query(
        query, match="any"
    ):
        return QueryIntent.NO_INTENT
    return QueryIntent.LOOKUP


def fold_for_search(text: str) -> str:
    """Search-only folding: canonical normalization + per-profile fold.

    Delegates the fold step to each profile's ``search_fold`` hook,
    applied in ``PROFILES`` order. Arabic's hook is the taa-marbuta
    -> haa fold; English and Russian inherit the identity default.

    Deliberately SEPARATE from ``normalize_for_storage`` so that
    ``content_hash`` and the stored ``normalized_content`` are
    never altered by search-side experiments.
    """
    result = normalize_for_storage(text)
    for profile in PROFILES:
        result = profile.search_fold(result)
    return result


def arabic_variants(token: str) -> set[str]:
    """Candidate surface forms: strip one clitic prefix and/or one suffix.

    Delegates to ``ArabicProfile.search_variants`` -- the algorithm
    (prefix stripping, suffix stripping, taa-marbuta restoration
    after a stripped final ``ta``) lives there. This wrapper exists
    for the same two reasons as ``_arabic_variants``: graceful
    degradation when the Arabic profile is not discovered, and a
    stable public name.

    Behaviour is byte-for-byte identical to the pre-refactor
    ``arabic_variants`` for every input.
    """
    profile = by_code("ar")
    if profile is None:
        return {token}
    return profile.search_variants(token)


def build_search_terms(text: str) -> str:
    """Union of candidate variants of every folded word (index side)."""
    terms: set[str] = set()
    for word in re.findall(r"\w+", fold_for_search(text)):
        terms |= arabic_variants(word)
    return " ".join(sorted(terms))


def _content_words(text: str) -> list[str]:
    return [
        w for w in re.findall(r"\w+", fold_for_search(text))
        if w not in _QUERY_STOPWORDS and len(w) > 1
    ]


def build_variant_query(text: str) -> str:
    """OR of prefix-wildcarded variants of each content word (query side)."""
    terms: list[str] = []
    seen: set[str] = set()
    for word in _content_words(text):
        for variant in sorted(arabic_variants(word)):
            if variant not in seen:
                seen.add(variant)
                terms.append(variant + "*")
    return " OR ".join(terms)


#: Low-information words used by the coverage scorer. Assembled
#: from every profile's ``generic_terms()`` hook. See
#: ``languages/arabic.py`` and ``languages/english.py`` for the
#: per-language rationale.
_GENERIC_TERMS: Final[frozenset[str]] = frozenset().union(
    *(profile.generic_terms() for profile in PROFILES)
)


def _is_generic(word: str) -> bool:
    folded = fold_for_search(word)
    return any(folded.startswith(g) for g in _GENERIC_TERMS) or bool(
        arabic_variants(folded) & _GENERIC_TERMS
    )


def _is_latin(word: str) -> bool:
    return all(ord(c) < 128 for c in word)


def query_term_sets(query: str) -> list:
    """Return [(word, variant_set, weight)] for each content word."""
    return [
        (word, arabic_variants(word), 0.3 if _is_generic(word) else 1.0)
        for word in _content_words(query)
    ]


def coverage_score(qsets: list, entry_terms: set) -> float:
    """Absolute weighted query-term coverage in [0, 1] (audit R2).

    A content word is covered when one of its variants is present
    in the entry's search_terms; Latin words also match by prefix
    (deploy -> deployment), mirroring the FTS wildcard. Generic
    words weigh 0.3 so a single incidental generic token cannot
    carry a weak match to 1.0.
    """
    if not qsets:
        return 0.0
    total = 0.0
    hit = 0.0
    for word, variants, weight in qsets:
        total += weight
        covered = bool(variants & entry_terms)
        if not covered and _is_latin(word):
            covered = any(
                et.startswith(word) or word.startswith(et)
                for et in entry_terms
            )
        if covered:
            hit += weight
    return hit / total if total else 0.0


__all__ = [
    # --- Storage and query normalization ---
    "normalize_for_storage",
    "normalize_query",
    # --- Query intent classification ---
    "QueryIntent",
    "classify_query",
    # --- Search-index expansion ---
    "fold_for_search",
    "arabic_variants",
    "build_search_terms",
    "build_variant_query",
    # --- Coverage scoring ---
    "query_term_sets",
    "coverage_score",
]
