# backend/tests/test_normalizer_compat.py
"""
PELDRUN Memory Platform - Normalizer Behavioural Compatibility Lock.

This module pins the byte-for-byte output of every public function
in ``omweb.memory.storage.normalizer`` to the values produced by the
pre-refactor monolithic implementation. It exists to guarantee that
the language-profile extraction (moving Arabic/Russian/English data
out of ``normalizer.py`` into ``languages/*.py``) did not change any
observable behaviour, and to catch any future regression that would
silently alter search results.

Two classes of test
-------------------

1. **Snapshot tests.**
   Each ``(input, expected)`` pair below was derived by running the
   input through the pre-refactor module. Those values are FROZEN.
   Changing an expected value is a deliberate behaviour change and
   must be accompanied by:

     * a migration plan for existing FTS-index content
       (``memories_fts`` was built with the old normalizer, so any
       change in storage normalization invalidates the index and
       requires a rebuild -- see ``content_hash`` in the entry
       model), and
     * a corresponding commit message that explicitly notes which
       search results are affected.

2. **Property tests.**
   Idempotence, the non-empty-input contract, and type-error
   behaviour are properties, not snapshots. They hold for every
   future implementation that satisfies the documented contract,
   so they do not need to be updated when the input corpus
   expands.

Running
-------

From ``backend/``::

    python -m pytest tests/test_normalizer_compat.py -v

The file has no external dependencies beyond ``pytest``. It does
not touch the database, the file system, or any environment
variable, so it is safe to run in any CI configuration.
"""

from __future__ import annotations

import pytest

from omweb.memory.storage.normalizer import (
    QueryIntent,
    arabic_variants,
    build_search_terms,
    build_variant_query,
    classify_query,
    coverage_score,
    fold_for_search,
    normalize_for_storage,
    normalize_query,
    query_term_sets,
)


# ---------------------------------------------------------------------------
# Storage normalization corpus
# ---------------------------------------------------------------------------
#
# Every expected value matches the pre-refactor implementation
# byte-for-byte. Groups are ordered by concern: empty input, English,
# invisible-character removal, Arabic by transform, Russian, mixed.

STORAGE_CORPUS: list[tuple[str, str]] = [
    # --- Empty / whitespace-only ---
    ("", ""),
    (" ", ""),
    ("\t\n\r", ""),
    ("\u00A0", ""),                 # NBSP is whitespace, strip() empties it
    # --- Plain English ---
    ("Hello", "hello"),
    ("Hello World", "hello world"),
    ("  Hello   World  ", "hello world"),
    ("HELLO", "hello"),
    ("Mixed Case TeXt", "mixed case text"),
    # --- NBSP folding (not stripped, replaced mid-string) ---
    ("\u00A0Hello\u00A0World\u00A0", "hello world"),
    # --- Invisible-character removal ---
    ("Hello\u200BWorld", "helloworld"),
    ("Hello\u200DWorld", "helloworld"),
    ("Hello\uFEFFWorld", "helloworld"),
    # --- Pathological: all-invisible input falls back to lowercased input ---
    ("\u200B", "\u200B"),
    ("\u200B\u200B\u200B", "\u200B\u200B\u200B"),
    # --- Arabic: diacritics removal ---
    ("مُحَمَّد", "محمد"),
    ("كِتَاب", "كتاب"),
    ("مُحَمَّدٌ", "محمد"),
    # --- Arabic: tatweel removal ---
    ("مـــحـــمـــد", "محمد"),
    # --- Arabic: alef unification ---
    ("أحمد", "احمد"),
    ("إسلام", "اسلام"),
    ("آمن", "امن"),
    ("ٱلْحَمْدُ", "الحمد"),
    # --- Arabic: alef maqsura -> yaa ---
    ("مصطفى", "مصطفي"),
    # --- Arabic: hamza carriers -> plain letter ---
    ("مؤمن", "مومن"),
    ("سائل", "سايل"),
    # --- Arabic: taa marbuta is preserved in storage (deliberate) ---
    ("كتابة", "كتابة"),
    # --- Russian ---
    ("Ёлка", "елка"),
    ("ёлка", "елка"),
    ("ЁЛКА", "елка"),
    ("Москва", "москва"),
    ("ёж", "еж"),
    # --- Mixed scripts ---
    ("Hello محمد", "hello محمد"),
    ("Test Ёлка", "test елка"),
    ("محمد hello", "محمد hello"),
]


@pytest.mark.parametrize("text,expected", STORAGE_CORPUS)
def test_normalize_for_storage_snapshot(text: str, expected: str) -> None:
    assert normalize_for_storage(text) == expected


# ---------------------------------------------------------------------------
# Query normalization corpus -- match="all"
# ---------------------------------------------------------------------------
#
# "all" mode keeps every token (no stopword filtering). It is the
# historical keyword-search mode for the ``memory_search`` tool.

QUERY_ALL_CORPUS: list[tuple[str, str]] = [
    ("", ""),
    ("   ", ""),
    ("محمد", "محمد"),
    ("مُحَمَّد", "محمد"),
    ("Hello World", "hello world"),
    ("Hello   World", "hello world"),
    ("Ёлка", "елка"),
    # Stopwords are NOT filtered in "all" mode.
    ("how are you", "how are you"),
    ("thank you", "thank you"),
    ("بالقهوه", "بالقهوه"),
    ("what is my name", "what is my name"),
]


@pytest.mark.parametrize("text,expected", QUERY_ALL_CORPUS)
def test_normalize_query_all_snapshot(text: str, expected: str) -> None:
    assert normalize_query(text, match="all") == expected


# ---------------------------------------------------------------------------
# Query normalization corpus -- match="any"
# ---------------------------------------------------------------------------
#
# "any" mode filters stopwords and emits ``token*`` OR variants.

QUERY_ANY_CORPUS: list[tuple[str, str]] = [
    # --- Empty / stopword-only -> empty expression ---
    ("", ""),
    ("   ", ""),
    ("thank you", ""),
    ("شكرا", ""),
    # --- Single content token ---
    ("محمد", "محمد*"),
    ("Ёлка", "елка*"),
    ("world", "world*"),
    # --- Multiple content tokens ---
    ("world peace", "world* OR peace*"),
    ("coffee and tea", "coffee* OR tea*"),
    # --- Mixed stopword + content ---
    ("hello world", "world*"),          # 'hello' is a stopword
    ("ok i will see you", "see*"),      # 'see' is the only content word
    ("what is my name", "name*"),       # only 'name' survives
    # --- Arabic article-prefix expansion (three variants) ---
    ("بالقهوه", "بالقهوه* OR قهوه* OR القهوه*"),
    # --- Arabic with diacritics ---
    ("مُحَمَّد", "محمد*"),
]


@pytest.mark.parametrize("text,expected", QUERY_ANY_CORPUS)
def test_normalize_query_any_snapshot(text: str, expected: str) -> None:
    assert normalize_query(text, match="any") == expected


def test_normalize_query_invalid_match_mode() -> None:
    with pytest.raises(ValueError, match="match must be 'all' or 'any'"):
        normalize_query("test", match="bogus")


def test_normalize_query_type_error() -> None:
    with pytest.raises(TypeError):
        normalize_query(123)  # type: ignore[arg-type]


def test_normalize_for_storage_type_error() -> None:
    with pytest.raises(TypeError):
        normalize_for_storage(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classify_query corpus
# ---------------------------------------------------------------------------

CLASSIFY_CORPUS: list[tuple[str, QueryIntent]] = [
    # --- BLANK: no word characters at all ---
    ("", QueryIntent.BLANK),
    ("   ", QueryIntent.BLANK),
    ("\t\n", QueryIntent.BLANK),
    # --- NO_INTENT: social phrases and stopword-only queries ---
    ("how are you", QueryIntent.NO_INTENT),
    ("good morning", QueryIntent.NO_INTENT),
    ("thank you", QueryIntent.NO_INTENT),
    ("كيف حالك", QueryIntent.NO_INTENT),
    ("صباح الخير", QueryIntent.NO_INTENT),
    ("من فضلك", QueryIntent.NO_INTENT),
    # --- LOOKUP: real retrieval queries ---
    ("محمد", QueryIntent.LOOKUP),
    ("قهوه", QueryIntent.LOOKUP),
    ("what is my name", QueryIntent.LOOKUP),
    ("hello world", QueryIntent.LOOKUP),
    ("ok i will see you", QueryIntent.LOOKUP),  # 'see' is a content word
]


@pytest.mark.parametrize("text,expected", CLASSIFY_CORPUS)
def test_classify_query_snapshot(text: str, expected: QueryIntent) -> None:
    assert classify_query(text) == expected


# ---------------------------------------------------------------------------
# Secondary API snapshots
# ---------------------------------------------------------------------------

def test_fold_for_search_folds_taa_marbuta() -> None:
    # Storage preserves taa marbuta; search folds it to haa.
    assert normalize_for_storage("كتابة") == "كتابة"
    assert fold_for_search("كتابة") == "كتابه"


def test_fold_for_search_is_none_for_non_arabic() -> None:
    # English and Russian pass through the fold unchanged.
    assert fold_for_search("hello") == "hello"
    assert fold_for_search("Ёлка") == "елка"


def test_arabic_variants_public_includes_original() -> None:
    """Every returned set contains the input token verbatim."""
    for token in ("بالقهوه", "قهوه", "القهوه", "محمد"):
        result = arabic_variants(token)
        assert token in result


def test_arabic_variants_expands_prefixed_word() -> None:
    """Prefix + suffix stripping must yield the base noun."""
    result = arabic_variants("بالقهوه")
    assert "قهوه" in result
    assert "القهوه" in result
    assert len(result) > 1  # expansion actually happened


def test_build_search_terms_returns_sorted_space_joined() -> None:
    terms = build_search_terms("بالقهوه")
    parts = terms.split()
    assert parts == sorted(parts)
    assert "قهوه" in parts


def test_build_variant_query_uses_prefix_wildcards() -> None:
    result = build_variant_query("بالقهوه")
    assert "قهوه*" in result
    assert "القهوه*" in result
    assert result.count(" OR ") >= 1


def test_query_term_sets_generic_weight() -> None:
    qsets = query_term_sets("what is my name")
    assert len(qsets) == 1
    word, _, weight = qsets[0]
    assert word == "name"
    assert weight == 0.3  # generic word gets reduced weight


def test_query_term_sets_non_generic_weight() -> None:
    qsets = query_term_sets("coffee")
    assert len(qsets) == 1
    word, _, weight = qsets[0]
    assert word == "coffee"
    assert weight == 1.0


def test_coverage_score_full_match() -> None:
    qsets = query_term_sets("coffee")
    assert coverage_score(qsets, {"coffee"}) == 1.0


def test_coverage_score_no_match() -> None:
    qsets = query_term_sets("coffee")
    assert coverage_score(qsets, {"tea"}) == 0.0


def test_coverage_score_empty_qsets() -> None:
    assert coverage_score([], {"anything"}) == 0.0


def test_coverage_score_generic_weight_applied() -> None:
    # A single generic word alone must not score 1.0 on an unrelated
    # entry -- but it DOES score 1.0 on a matching entry because the
    # weight cancels out in the ratio. The point of the 0.3 weight is
    # visible when combined with a non-generic word.
    qsets = query_term_sets("favorite coffee")
    # 'favorite' is generic (0.3), 'coffee' is not (1.0)
    # Entry that matches only 'favorite' -> 0.3 / 1.3 ~= 0.231
    partial = coverage_score(qsets, {"favorite"})
    assert 0.0 < partial < 0.5


# ---------------------------------------------------------------------------
# Property tests -- invariants that must hold for every input
# ---------------------------------------------------------------------------

IDEMPOTENCE_CORPUS: list[str] = [
    "",
    "   ",
    "Hello",
    "  Hello   World  ",
    "مُحَمَّد",
    "Ёлка",
    "بالقهوه",
    "كتابة",
    "\u200B",
    "\u200B\u200B",
    "\u00A0",
    "Hello محمد",
]


@pytest.mark.parametrize("text", IDEMPOTENCE_CORPUS)
def test_normalize_for_storage_idempotent(text: str) -> None:
    once = normalize_for_storage(text)
    twice = normalize_for_storage(once)
    assert once == twice


NON_EMPTY_INPUT_CORPUS: list[str] = [
    "مُحَمَّد",
    "Ёлка",
    "Hello",
    "بالقهوه",
    "كتابة",
    "\u200B",
    "\u200B\u200B",
    "test",
    "مرحبا",
]


@pytest.mark.parametrize("text", NON_EMPTY_INPUT_CORPUS)
def test_normalize_for_storage_non_empty_contract(text: str) -> None:
    """Non-empty non-whitespace input must produce non-empty output."""
    assert normalize_for_storage(text) != ""


# ---------------------------------------------------------------------------
# Discovery invariants
# ---------------------------------------------------------------------------

def test_profiles_discovered_in_deterministic_order() -> None:
    """The discovered profiles must be sorted by ``(priority, code)``.

    This locks the current three-profile set and their order. Adding
    a new language to ``languages/`` will require updating this test
    -- that is deliberate: a new language changes the pipeline and
    should be a visible diff, not a silent addition.
    """
    from omweb.memory.storage.languages import PROFILES

    assert [(p.code, p.priority) for p in PROFILES] == [
        ("ar", 10),
        ("en", 10),
        ("ru", 10),
    ]


def test_by_code_lookup() -> None:
    from omweb.memory.storage.languages import by_code

    assert by_code("ar") is not None
    assert by_code("en") is not None
    assert by_code("ru") is not None
    assert by_code("zz") is None  # unknown code returns None, not raising


def test_arabic_profile_is_used_by_public_helpers() -> None:
    """``arabic_variants`` must delegate to the Arabic profile.

    Sanity check that the wrapper is wired correctly. If the Arabic
    profile were removed or its ``search_variants`` broken, this
    test would fail loudly rather than silently returning ``{token}``.
    """
    result = arabic_variants("بالقهوه")
    assert len(result) > 1            # expansion actually happened
    assert "بالقهوه" in result        # original always preserved
    assert "قهوه" in result           # stripped form present


__all__ = [
    "STORAGE_CORPUS",
    "QUERY_ALL_CORPUS",
    "QUERY_ANY_CORPUS",
    "CLASSIFY_CORPUS",
    "IDEMPOTENCE_CORPUS",
    "NON_EMPTY_INPUT_CORPUS",
]
