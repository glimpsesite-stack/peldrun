# backend/omweb/memory/storage/languages/_base.py
"""
PELDRUN Memory Platform - Multilingual Language Profile Contract.

This module defines the two things every language plugin file needs
to implement in order to participate in the normalizer pipeline:

  * ``LanguageProfile`` -- a ``Protocol`` describing the *structural*
    contract. Used only for type-checking and ``isinstance`` guards.
    Nothing in the runtime pipeline depends on inheritance from it.

  * ``BaseLanguageProfile`` -- an ``ABC`` that ships safe, minimal
    defaults for every optional hook. Concrete language modules
    subclass this and override only the methods they actually need.
    This keeps a new language file (e.g. ``turkish.py``) down to
    roughly 30 lines.

Why both?

If we shipped only the ``Protocol``, every language would have to
implement every method -- including the ten hooks it doesn't care
about -- just to satisfy structural typing. If we shipped only the
``ABC``, we would lose duck-typing: a test double or an externally
provided profile could not participate without importing from this
package. The combination gives us both: zero-boilerplate subclasses
*and* structural compatibility.

Discovery contract
------------------

``languages/__init__.py`` discovers profiles by importing every
``*.py`` module in this package whose name does not start with an
underscore, and reading its module-level ``PROFILE`` attribute.
Each ``PROFILE`` must satisfy ``LanguageProfile`` -- either by
subclassing ``BaseLanguageProfile`` (the common case) or by
implementing every method by hand (rare, e.g. generated proxies).

Pipeline contract
-----------------

The normalizer applies profiles in the order determined by
``(priority, code)``. Profiles with equal priority are ordered
lexicographically by ``code`` so the result is deterministic across
processes, Python versions, and platforms. The recommended priority
bands are:

  10-19   scripts with their own character range
          (Arabic, Russian, Hebrew, Greek, ...)
  20-29   scripts sharing a range with another language
          (Turkish vs Latin, Persian vs Arabic -- see those files)
  30+     general-purpose linguistic post-processing
          (stemming-adjacent fixes, exceptional casefold rules)

The reserved band ``1000+`` is used internally by ``normalizer.py``
for the final language-agnostic pipeline step (whitespace, NBSP,
casefold). Language modules must stay below 1000.

Idempotence invariant
---------------------

Every ``normalize()`` implementation MUST be idempotent:

    normalize(normalize(x)) == normalize(x)   for every x

The full pipeline is also idempotent by composition *as long as*
each profile is idempotent AND each profile's outputs do not feed
into a *later* profile's inputs in a way that changes the result.
In practice this holds trivially because each profile only touches
its own script range, and unknown scripts pass through unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Structural contract
# ---------------------------------------------------------------------------


@runtime_checkable
class LanguageProfile(Protocol):
    """Structural typing contract every language module must satisfy.

    Declared as a ``Protocol`` so that third-party profiles (test
    doubles, generated proxies, plugin distributions) do not need to
    import from this package to be accepted by the discovery layer.

    All return-type annotations describe the *fully defaulted* output:
    hooks that return "no data" return an empty collection, never
    ``None``. This eliminates ``Optional[...]`` from every call site
    in ``normalizer.py``.
    """

    #: ISO 639-1 (two-letter) language code, e.g. ``"ar"``, ``"ru"``.
    #: Used for tie-breaking in the discovery ordering and for lookup
    #: via ``languages.by_code``.
    code: ClassVar[str]

    #: Integer priority. Lower runs first. See the module docstring
    #: for the recommended bands.
    priority: ClassVar[int]

    # ------------------------------------------------------------------
    # Storage-side hook (the only REQUIRED method)
    # ------------------------------------------------------------------

    def normalize(self, text: str) -> str:
        """Apply this language's storage-normalization steps.

        MUST be idempotent and MUST NOT raise. Empty input MUST pass
        through unchanged (returning the same empty string).

        Called once per profile per ``normalize_for_storage`` call,
        in ascending ``(priority, code)`` order. The final output of
        the whole chain is then passed to the shared
        ``common.normalize_common`` step.
        """
        ...

    # ------------------------------------------------------------------
    # Query-side optional hooks (all have safe defaults)
    # ------------------------------------------------------------------

    def query_prefixes(self) -> tuple[str, ...]:
        """Multi-character prefixes the query builder should strip.

        Used by ``_build_any_expression`` in ``normalizer.py`` to
        emit additional OR terms. Returning an empty tuple disables
        prefix-variant expansion for this language.
        """
        ...

    def query_suffixes(self) -> tuple[str, ...]:
        """Multi-character suffixes the search index should expand.

        Used by ``build_search_terms`` (R2/R3 pipeline). The search
        index emits, for every word, one entry per
        ``search_variants`` form; suffix stripping widens recall
        without touching ``normalize_for_storage``.
        """
        ...

    def stopwords(self) -> frozenset[str]:
        """Function words that must NOT become OR terms in ``any`` mode.

        The union of every profile's stopwords is subtracted from
        query tokens. A token is only dropped when at least one
        content token survives, so a query composed entirely of
        function words correctly yields zero matches.
        """
        ...

    def generic_terms(self) -> frozenset[str]:
        """Low-information words used by the coverage scorer (R2).

        Terms returned here receive a lower weight in
        ``coverage_score`` so that an incidental generic token
        cannot promote a weak match to a high score. See the
        ``coverage_score`` docstring in ``normalizer.py``.
        """
        ...

    def query_variants(self, token: str) -> list[str]:
        """Additional query-side surface forms for ``token``.

        The original token is ALWAYS preserved by the caller; this
        hook returns only the *extras*. Used to emit OR terms for
        languages with productive clitics (Arabic article prefixes,
        Turkish possessive suffixes, ...). Order matters: variants
        are appended to the OR expression in the order returned.
        """
        ...

    def search_variants(self, token: str) -> set[str]:
        """All search-index forms for ``token``, including the token.

        The default implementation returns ``{token}``. Languages
        that want to widen index-side recall (Arabic clitic +
        suffix variants) return the full set, INCLUDING the
        original token, so callers can union results from multiple
        profiles without special-casing.
        """
        ...

    def search_fold(self, text: str) -> str:
        """Search-only extra folding, applied AFTER normalize_for_storage.

        Deliberately separated from ``normalize()`` so that
        ``content_hash`` and the stored ``normalized_content`` are
        never altered by search-side experiments (e.g. Arabic
        taa-marbuta -> haa). The default is the identity function.
        """
        ...

    def social_patterns(self) -> tuple[str, ...]:
        """Regex fragments for social / conversational phrases.

        Concatenated with ``|`` into a single anchored pattern used
        by ``classify_query`` to detect "no retrieval intent"
        inputs like greetings. Fragments are matched against
        already-normalized text, so they must be lowercase and use
        ``\\s+`` for internal whitespace.
        """
        ...


# ---------------------------------------------------------------------------
# Concrete base with defaults
# ---------------------------------------------------------------------------


class BaseLanguageProfile(ABC):
    """Concrete base class for language profiles.

    Provides safe defaults for every optional hook so subclasses can
    override only what they need. The only abstract member is
    ``normalize``, because that is the one behaviour that
    fundamentally defines a language profile -- a profile that does
    not normalize its own script is not a profile.

    Subclasses MUST set ``code`` and SHOULD set ``priority``.
    Omitting ``priority`` inherits the conservative default of 100,
    which places the profile after every recommendation-band entry
    -- safe, but likely not what the author intended. See the
    module docstring for the recommended bands.
    """

    code: ClassVar[str] = ""
    priority: ClassVar[int] = 100

    # ------------------------------------------------------------------
    # Storage-side (abstract)
    # ------------------------------------------------------------------

    @abstractmethod
    def normalize(self, text: str) -> str:
        """Apply this language's storage normalization. Idempotent."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Query-side (concrete defaults)
    # ------------------------------------------------------------------

    def query_prefixes(self) -> tuple[str, ...]:
        return ()

    def query_suffixes(self) -> tuple[str, ...]:
        return ()

    def stopwords(self) -> frozenset[str]:
        return frozenset()

    def generic_terms(self) -> frozenset[str]:
        return frozenset()

    def query_variants(self, token: str) -> list[str]:
        return []

    def search_variants(self, token: str) -> set[str]:
        return {token}

    def search_fold(self, text: str) -> str:
        return text

    def social_patterns(self) -> tuple[str, ...]:
        return ()


__all__ = [
    "LanguageProfile",
    "BaseLanguageProfile",
]
