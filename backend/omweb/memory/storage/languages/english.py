# backend/omweb/memory/storage/languages/english.py
"""
PELDRUN Memory Platform - English / Latin Language Profile.

Unlike ``arabic.py`` and ``russian.py``, this profile contributes NO
storage-side transform. Latin-script content is fully handled by the
shared ``languages/common.py`` step (casefold + whitespace collapse
+ invisible-character removal), which the original pre-refactor
pipeline applied to English content directly with no intermediate
language step.

So why does this profile exist at all?

Because everything that IS English-specific in the original
``normalizer.py`` -- the ~60 stopwords, the generic-term list, and
the two social-phrase regex fragments -- lived inline alongside the
Arabic data in module-level constants. Relocating them to their own
file is what makes the "add a language by adding a file" promise
true: without this profile, the stopword union computed in
``normalizer._QUERY_STOPWORDS`` would silently lose every English
entry, and ``classify_query`` would stop recognising
``"how are you"`` / ``"good morning"`` as no-intent inputs.

Concretely, without this file:

  * ``normalize_query("what is my name", match="any")`` would emit
    OR terms for ``what``, ``is``, ``my``, and ``name`` -- four
    terms instead of one, because the English stopword list that
    currently filters three of them would be gone.

  * ``classify_query("how are you")`` would return ``LOOKUP``
    instead of ``NO_INTENT``, causing the chat retrieval path to
    search the memory store for "how", "are", and "you" and inject
    whatever weak matches BM25 happened to find.

  * ``coverage_score`` would weigh ``name`` and ``like`` as
    high-signal terms instead of 0.3, promoting incidental generic
    matches to confident ones.

Behavioural compatibility
-------------------------

Every value exported here is a verbatim copy of the corresponding
entry in the original ``normalizer.py``. The compat corpus in
``tests/test_normalizer_compat.py`` pins the combined pipeline's
output byte-for-byte, so any accidental drift fails CI.

Why ``normalize`` is the identity function
------------------------------------------

The base class marks ``normalize`` as abstract, so this profile
must implement it -- but English content needs no script-specific
transform. The temptation is to make ``normalize`` casefold the
text (``.lower()``) since English is case-sensitive for indexing.
That would be wrong for two reasons:

  1. **Double-lowercasing is not the issue** -- it is harmless in
     isolation. The issue is that any transform here runs BEFORE
     ``common.normalize_common``. If ``normalize`` lowercased, and
     a future common-pipeline change introduced, say, upper-case
     handling for a specific edge case, the two steps would fight
     each other over who owns the case. Keeping the profile as
     identity makes the ownership boundary explicit: all Latin
     casefolding belongs to common, full stop.

  2. **Consistency with the discovery contract** -- every profile
     runs in ``(priority, code)`` order. English (``"en"``, priority
     10) sits between Arabic (``"ar"``) and Russian (``"ru"``).
     Because its ``normalize`` is identity, position is
     irrelevant -- which is the correct behaviour for a language
     whose only pipeline contribution is *data* (stopwords) rather
     than *transform*.

  In short: identity here is not laziness, it is the correct
  contract for a "data-only" profile. The docstring below explains
  this again at the method level so the intent survives refactors.

Idempotence
-----------

Identity is trivially idempotent. The stopword and generic-term
sets are ``frozenset`` constants -- immutable by construction. The
social-pattern tuple is likewise immutable.
"""

from __future__ import annotations

from typing import ClassVar, Final

from ._base import BaseLanguageProfile


#: English function words. Verbatim copy of the Latin-script
#: entries from the original ``_QUERY_STOPWORDS`` set.
#:
#: Grouped semantically so future maintainers can extend the list
#: without having to cross-reference the original file:
#:
#:   articles / determiners
#:   conjunctions
#:   prepositions
#:   copulas / auxiliaries / modals
#:   personal pronouns
#:   demonstratives
#:   interrogatives
#:   interjections / social words
#:   quantifiers and adverbs
_ENGLISH_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # --- articles / determiners ---
        "a", "an", "the",
        # --- conjunctions ---
        "and", "or", "but", "if",
        # --- prepositions ---
        "of", "to", "in", "on", "at", "for", "with", "from", "by",
        "as", "about",
        # --- copulas / auxiliaries / modals ---
        "is", "are", "was", "were", "be", "been", "am",
        "do", "does", "did",
        "will", "would", "can", "could", "should", "shall",
        "may", "might",
        # --- personal pronouns ---
        "i", "me", "my", "mine",
        "you", "your", "yours",
        "he", "she", "it",
        "we", "us",
        "they", "them",
        # --- demonstratives ---
        "this", "that", "these", "those",
        "there", "here",
        # --- interrogatives ---
        "what", "who", "whom", "which",
        "when", "where", "why", "how",
        # --- interjections / social words ---
        "yes", "ok", "okay",
        "hi", "hello", "hey",
        "thanks", "thank", "please",
        # --- quantifiers and adverbs ---
        "so", "not", "no", "just", "very", "too", "also",
        "any", "all", "some", "more", "most", "such",
        "than", "then", "now",
    }
)


#: English low-information terms used by the R2 coverage scorer.
#: Verbatim copy of the Latin-script entries from the original
#: ``_GENERIC_TERMS`` set.
#:
#: These words appear in so many memories that their presence alone
#: should not promote a weak partial match to a high coverage
#: score. Example: ``"what is my favorite color"`` -- the word
#: ``"favorite"`` matches hundreds of preference statements, so it
#: must not outweigh a distinctive term like ``"color"``.
_ENGLISH_GENERIC_TERMS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "like",
        "have",
        "favorite",
        "prefer",
        "pick",
        "chose",
    }
)


#: Regex fragments for English social / conversational phrases.
#: Joined with ``|`` into the anchored pattern built by
#: ``classify_query``. Fragments run against already-normalized
#: text (lowercase, single-space whitespace, no invisible chars),
#: which is why ``\s+`` is used instead of a literal space -- the
#: normalized form always has exactly one space, but using ``\s+``
#: keeps the fragment robust against a future relaxation of the
#: collapse rule.
_ENGLISH_SOCIAL_PATTERNS: Final[tuple[str, ...]] = (
    r"how\s+are\s+you",
    r"good\s+(?:morning|evening)",
)


class EnglishProfile(BaseLanguageProfile):
    """English / Latin-script language profile.

    Data-only: contributes stopwords, generic terms, and social
    patterns. The ``normalize`` step is the identity function --
    see the module docstring for why this is correct and not an
    oversight.
    """

    code: ClassVar[str] = "en"
    priority: ClassVar[int] = 10

    def normalize(self, text: str) -> str:
        """Identity -- Latin casefolding belongs to the common step.

        See the module docstring, section "Why ``normalize`` is the
        identity function", for the full rationale. The short
        version: all Latin transforms that this codebase performs
        live in ``languages/common.py``, and duplicating even a
        subset of them here would split ownership of casefolding
        across two modules.
        """
        return text

    def stopwords(self) -> frozenset[str]:
        return _ENGLISH_STOPWORDS

    def generic_terms(self) -> frozenset[str]:
        return _ENGLISH_GENERIC_TERMS

    def social_patterns(self) -> tuple[str, ...]:
        return _ENGLISH_SOCIAL_PATTERNS


#: Module-level singleton consumed by ``languages/__init__.py``.
PROFILE: Final[EnglishProfile] = EnglishProfile()


__all__ = ["PROFILE", "EnglishProfile"]
