# backend/omweb/memory/storage/languages/common.py
"""
PELDRUN Memory Platform - Shared Language-Agnostic Normalization Step.

This module is the *final* stage of the storage-normalization pipeline
for every language. It is not a ``LanguageProfile`` -- it is invoked
directly by ``normalizer._normalize_for_storage_impl`` AFTER every
discovered profile has run, so its behaviour is applied exactly once
per call, in a fixed position that cannot be reordered by profile
priority.

Why a separate module (and not a profile with priority=1000)
------------------------------------------------------------

Two reasons:

1. **It has no ``code``.** Every ``LanguageProfile`` is keyed by ISO
   639-1, and this step is not a language -- it is the tail of the
   pipeline that every language shares. Reserving a fake code
   (``"xx"``, ``"und"``) would violate the discovery contract and
   leak into ``languages.by_code`` lookups.

2. **It must run *last*, unconditionally.** A profile with
   ``priority=1000`` is *currently* last, but a future contributor
   could add a profile with ``priority=2000`` and silently move the
   casefold step before their language-specific work, breaking the
   documented ordering guarantee in ``normalizer``'s module docstring:

       "Casefolding is applied AFTER the script-specific steps so
        that, e.g., uppercase Cyrillic ``Ё`` is first mapped to
        ``ё`` by the Russian step, then lowercased consistently
        with other Cyrillic."

   Hard-coding the call site in ``normalizer.py`` makes that
   ordering a structural invariant rather than a numeric convention.

Ordering contract within this module
------------------------------------

The five sub-steps below MUST execute in this exact order. Each
ordering constraint is load-bearing and documented inline. Changing
the order produces observable behaviour differences in real corpora
(the compat test corpus in ``tests/test_normalizer_compat.py``
pins the current output byte-for-byte, so any accidental reorder
fails CI rather than silently changing search results):

    1. Remove invisible characters.
    2. Replace NBSP with a regular space.
    3. Casefold (``.lower()``).
    4. Collapse whitespace runs to a single space.
    5. Strip leading / trailing whitespace.

Idempotence
-----------

The step is idempotent by construction:

    * ``_INVISIBLE_CHARS.sub`` -- removing zero-width/formatting
      characters is a no-op on already-clean text.
    * ``.replace(NBSP, " ")`` -- the replacement target (``" "``) is
      not itself an NBSP, so a second pass changes nothing.
    * ``.lower()`` -- ``.lower()`` is idempotent for every Unicode
      code point that Python's ``str.lower`` handles; applying it
      twice yields the same string.
    * ``_WHITESPACE_RUN.sub(" ")`` -- a single space is not a
      *run* of whitespace, so the pattern does not match it again.
    * ``.strip()`` -- the output has no leading/trailing whitespace,
      so a second call is a no-op.

Therefore ``normalize_common(normalize_common(x)) == normalize_common(x)``
for every ``x``, which is the invariant the FTS5 UPDATE trigger
depends on (see ``normalizer``'s module docstring, invariant #1).
"""

from __future__ import annotations

import re
from typing import Final


#: Invisible / formatting characters that often sneak into text from
#: copy-paste operations and cause search mismatches with no visible
#: cause. Removing all of them is universally safe -- they carry zero
#: semantic content in any target language, present or future.
#:
#:   U+00AD  SOFT HYPHEN
#:   U+200B  ZERO WIDTH SPACE
#:   U+200C  ZERO WIDTH NON-JOINER
#:   U+200D  ZERO WIDTH JOINER
#:   U+200E  LEFT-TO-RIGHT MARK
#:   U+200F  RIGHT-TO-LEFT MARK
#:   U+202A  LEFT-TO-RIGHT EMBEDDING
#:   U+202B  RIGHT-TO-LEFT EMBEDDING
#:   U+202C  POP DIRECTIONAL FORMATTING
#:   U+202D  LEFT-TO-RIGHT OVERRIDE
#:   U+202E  RIGHT-TO-LEFT OVERRIDE
#:   U+2060  WORD JOINER
#:   U+FEFF  ZERO WIDTH NO-BREAK SPACE / BOM
#:
#: Kept as an explicit character class rather than ``[\p{Cf}\p{Cc}]``
#: because Python's ``re`` does not support Unicode general categories
#: without the third-party ``regex`` module -- and the project's
#: "pure stdlib" invariant (module docstring of ``normalizer``,
#: invariant #6) forbids that dependency. The set above is exhaustive
#: for the practical cases we have seen in real pasted content; the
#: remaining Cf code points (bidi isolates U+2066-U+2069, tag
#: characters U+E0000-E007F) are extremely rare in user input and are
#: left in the text deliberately so that any future requirement to
#: preserve them for forensic reasons has a natural home.
_INVISIBLE_CHARS: Final[re.Pattern[str]] = re.compile(
    r"[\u00AD\u200B-\u200F\u202A-\u202E\u2060\uFEFF]"
)

#: Non-breaking space. Common in Arabic (justified text) and Russian
#: (typography conventions). Folded to a regular space *before*
#: whitespace collapsing so that ``"hello\u00A0world"`` and
#: ``"hello world"`` normalize to the same token stream. If this
#: replacement happened *after* ``_WHITESPACE_RUN``, the NBSP would
#: already have survived the pattern (``\s`` in Python 3 Unicode mode
#: DOES match NBSP -- but only when the ``re.UNICODE`` flag or the
#: default str pattern is used, which is our case; keeping the
#: explicit replace makes the behaviour independent of that detail
#: and preserves the historical output for any future refactor that
#: switches to ``re.ASCII`` for performance).
_NBSP: Final[str] = "\u00A0"

#: Any run of one or more whitespace characters. Collapsed to a
#: single ASCII space. In Python 3 Unicode mode, ``\s`` matches:
#: ``\t \n \v \f \r``, ASCII space, NBSP, the EN/EM QUAD family
#: (U+2000-U+200A), LINE SEPARATOR (U+2028), PARAGRAPH SEPARATOR
#: (U+2029), NARROW NO-BREAK SPACE (U+202F), MEDIUM MATHEMATICAL
#: SPACE (U+205F), and IDEOGRAPHIC SPACE (U+3000). This is broader
#: than FTS5's ``unicode61`` whitespace set, which is fine: we are
#: producing the *canonical* form that both the index and the query
#: will agree on, not trying to mirror FTS5's tokenizer exactly.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")


#: Discovery opt-out marker. ``languages/__init__.py`` imports every
#: non-underscore module in this package and reads ``PROFILE`` from
#: it. This module is a shared utility, not a language -- it has no
#: ``PROFILE`` and must explicitly opt out of discovery, or the
#: import of ``languages`` would raise at startup.
#:
#: The annotated type is ``Final[bool]`` rather than bare ``bool``
#: so that a static type checker rejects any attempt to rebind the
#: marker after import (e.g. a test monkeypatching it). The value
#: is a literal ``False`` -- not a falsy sentinel -- so that the
#: discovery check in ``_discover`` reads as ``is False`` (identity
#: comparison), which cannot be satisfied by ``0``, ``""``, or a
#: custom object with a lying ``__bool__``.
IS_PROFILE_MODULE: Final[bool] = False


def normalize_common(text: str) -> str:
    """Apply the shared, language-agnostic tail of the pipeline.

    This is the exact behaviour that was historically inlined as
    ``_normalize_common`` inside ``normalizer.py``. It is called
    from ``normalizer._normalize_for_storage_impl`` after every
    discovered ``LanguageProfile.normalize`` has run, and it is
    the last transform applied to both storage content and query
    text -- which is what makes a query for ``"محمد"`` and a stored
    memory containing ``"مُحَمَّد"`` converge to the same FTS token
    stream.

    Parameters
    ----------
    text
        Input text, already processed by every language-specific
        ``normalize()`` step. May be empty; empty input is returned
        unchanged (not ``None``, not ``" "``).

    Returns
    -------
    str
        The common-normalized text: invisible characters removed,
        NBSPs turned into spaces, casefolded, whitespace runs
        collapsed, leading/trailing whitespace stripped. For
        whitespace-only input the result is ``""``.

    Raises
    ------
    Nothing. ``text`` is assumed to be ``str`` (the caller in
    ``normalizer.py`` guards this); passing ``None`` would raise
    ``AttributeError`` inside ``.sub``, but that path is not part
    of the public contract and is not exercised by callers.

    Idempotence
    -----------
    ``normalize_common(normalize_common(x)) == normalize_common(x)``
    for every ``x``. See the module docstring for the per-substep
    argument.
    """
    if not text:
        return text

    # 1. Invisible characters first. ``.lower()`` must see exactly
    #    the visible characters; a ZWJ that vanishes during casefold
    #    on some platforms would otherwise change the token stream
    #    depending on Python version.
    text = _INVISIBLE_CHARS.sub("", text)

    # 2. NBSP before whitespace collapsing. Otherwise the NBSP would
    #    be treated as ordinary whitespace by ``\s`` anyway -- but
    #    replacing it explicitly here keeps the behaviour stable if
    #    a future refactor switches to ``re.ASCII`` for speed.
    text = text.replace(_NBSP, " ")

    # 3. Casefold. ``.lower()`` is locale-independent in Python 3
    #    (it does NOT consult ``LC_CTYPE``), which is the property
    #    that lets this module satisfy the "deterministic across
    #    platforms" invariant. Do NOT replace with ``.casefold()``:
    #    ``casefold`` would change ``"ß"`` -> ``"ss"`` and break
    #    the byte-for-byte compatibility with the pre-refactor
    #    pipeline. That is a deliberate choice, documented in the
    #    parent module's "English / Latin" section.
    text = text.lower()

    # 4. Collapse whitespace runs. This runs AFTER casefold so that
    #    any whitespace a hypothetical script might introduce
    #    during casefold (none in practice today, but the invariant
    #    is worth preserving) is caught by the collapse.
    text = _WHITESPACE_RUN.sub(" ", text)

    # 5. Strip. Collapsing can leave a leading or trailing space
    #    when the input started or ended with whitespace; this
    #    guarantees the caller sees a canonically-trimmed string.
    return text.strip()


__all__ = ["normalize_common"]
