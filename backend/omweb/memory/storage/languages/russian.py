# backend/omweb/memory/storage/languages/russian.py
"""
PELDRUN Memory Platform - Russian Language Profile.

The only storage-normalization transform Russian needs is the
``yo`` -> ``ye`` fold: ``U+0451`` (lowercase yo) and ``U+0401``
(uppercase YO) both map to their ``ye`` counterparts ``U+0435``
and ``U+0415``.

Why this matters
----------------

Most Russian keyboards do not have a dedicated ``yo`` key. Users
routinely type ``ye`` when they mean ``yo``, and vice versa when
autocomplete substitutes the wrong letter. A memory stored as
``"<yo>lka"`` (with yo) would be unreachable by a query typed
``"<ye>lka"`` (with ye) under FTS5 alone -- the two letters have
distinct code points and ``unicode61`` does not fold them.

Folding both directions to ``ye`` makes the two spellings
interchangeable for search, which matches user expectations: if
you cannot type the letter, you should still find the word.

Why both cases are mapped explicitly
------------------------------------

The common pipeline (``languages/common.py``) applies
``.lower()`` AFTER every profile's ``normalize()`` has run. If
this file only mapped the lowercase ``yo``, an uppercase ``YO``
at the start of a sentence would survive as ``U+0401``, then be
lowercased to ``U+0451`` by the common step -- which is a
different code point from the ``U+0435`` we want. Mapping both
cases here keeps the profile independent of pipeline order and
makes the transform trivially verifiable in isolation.

Idempotence
-----------

``str.maketrans`` maps are one-way by construction: ``U+0451``
and ``U+0401`` are the sources, ``U+0435`` and ``U+0415`` are
the targets, and neither target is a source of any mapping.
Therefore applying ``translate`` twice yields the same string as
applying it once.

No query-side hooks
-------------------

Russian has no productive clitics, no article-prefix system, and
no search-only folding requirement beyond the storage fold
already performed by ``normalize``. The base class's defaults
(empty prefix/suffix tuples, empty stopword/generic sets, identity
``search_fold``, ``{token}`` ``search_variants``) are correct for
Russian as-is.

Adding Russian stopwords later, if needed, is a one-line override:

    def stopwords(self) -> frozenset[str]:
        return frozenset({ "и", "в", "на", ... })
"""

from __future__ import annotations

from typing import ClassVar, Final

from ._base import BaseLanguageProfile


#: Cyrillic ``yo`` -> ``ye``, both cases.
#:
#:   U+0401  CYRILLIC CAPITAL LETTER IO         ->  U+0415  CYRILLIC CAPITAL LETTER IE
#:   U+0451  CYRILLIC SMALL LETTER IO           ->  U+0435  CYRILLIC SMALL LETTER IE
#:
#: Written as ``\\uXXXX`` escapes to keep this file pure ASCII and
#: to avoid any dependence on the editor's handling of Cyrillic
#: literals. The comments above give the transliteration.
_RUSSIAN_MAP: Final = str.maketrans(
    {
        "\u0401": "\u0415",
        "\u0451": "\u0435",
    }
)


class RussianProfile(BaseLanguageProfile):
    """Russian language profile."""

    code: ClassVar[str] = "ru"
    priority: ClassVar[int] = 10

    def normalize(self, text: str) -> str:
        """Fold ``yo`` -> ``ye``, both cases.

        See module docstring for the two-case rationale and the
        idempotence argument.
        """
        if not text:
            return text
        return text.translate(_RUSSIAN_MAP)


#: Module-level singleton consumed by ``languages/__init__.py``.
PROFILE: Final[RussianProfile] = RussianProfile()


__all__ = ["PROFILE", "RussianProfile"]
