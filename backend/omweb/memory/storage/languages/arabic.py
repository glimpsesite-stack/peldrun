# backend/omweb/memory/storage/languages/arabic.py
"""
PELDRUN Memory Platform - Arabic Language Profile.

Consolidates every Arabic-specific transform that historically lived
inline in ``normalizer.py`` into a single ``BaseLanguageProfile``
subclass:

  * storage normalization  -- tatweel removal, diacritics removal,
                              alef/yaa/hamza unification
  * query-side expansion   -- article/clitic prefix variants
                              (``query_variants``)
  * search-index expansion -- the R2/R3 prefix/suffix variant engine
                              (``search_variants``)
  * search-only folding    -- taa-marbuta -> haa (``search_fold``)
  * function-word removal  -- ``stopwords``
  * low-signal words       -- ``generic_terms``
  * conversational phrases -- ``social_patterns``

All Arabic literals are expressed as ``\\uXXXX`` escapes so this file
is pure ASCII. That is deliberate:

  * it survives editors, CI pipelines, and diff tools that mishandle
    bidirectional text;
  * it cannot be accidentally reordered by an RTL-aware linter;
  * it keeps the same convention the original ``normalizer.py``
    already used for its extended stopword list.

Behavioural compatibility
-------------------------

Every method here reproduces the original semantics exactly. The
compat corpus in ``tests/test_normalizer_compat.py`` pins the
combined pipeline's output byte-for-byte, so any accidental change
in this file fails CI rather than silently altering search results.

Idempotence
-----------

Each transform is idempotent by construction:

  * ``normalize`` -- every removal/replacement targets a character
    that is not itself the output of any other step (alef variants
    map to bare alef, tatweel and diacritics are simply deleted).
  * ``query_variants`` and ``search_variants`` -- pure functions of
    the input token; no state, deterministic, no side effects.
  * ``search_fold`` -- ``.replace("\\u0629", "\\u0647")`` is
    idempotent because the replacement (``\\u0647``) is not the
    source character.
"""

from __future__ import annotations

import re
from typing import ClassVar, Final

from ._base import BaseLanguageProfile


# ---------------------------------------------------------------------------
# Storage-side constants
# ---------------------------------------------------------------------------


#: Combining marks stripped from Arabic text before indexing. The
#: ranges below cover:
#:
#:   U+064B-U+065F  -- tanwin + harakat + shadda + sukun + hamza above
#:   U+0670         -- superscript alef (dagger alef)
#:   U+06D6-U+06DC  -- Quranic annotation marks (small high signs)
#:   U+06DF-U+06E4  -- Quranic annotation marks (small wavy signs)
#:   U+06E7-U+06E8  -- Quranic annotation marks (small high yeh/noon)
#:   U+06EA-U+06ED  -- Quranic annotation marks (empty centre dots)
_ARABIC_DIACRITICS: Final[re.Pattern[str]] = re.compile(
    r"[\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED]"
)

#: Tatweel / kashida (U+0640). Pure justification decoration; zero
#: semantic content. Removed so that stretched and unstretched
#: spellings of the same word match.
_ARABIC_TATWEEL: Final[re.Pattern[str]] = re.compile(r"\u0640")

#: Alef variants mapped to bare alef (U+0627):
#:
#:   U+0622  alef with madda
#:   U+0623  alef with hamza above
#:   U+0625  alef with hamza below
#:   U+0671  alef with wasla
_ARABIC_ALEF_MAP: Final = str.maketrans(
    {
        "\u0622": "\u0627",
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0671": "\u0627",
    }
)

#: Alef maqsura (U+0649) mapped to yaa (U+064A). Same phoneme at
#: word end; typists use them interchangeably.
_ARABIC_YAA_MAP: Final = str.maketrans(
    {
        "\u0649": "\u064A",
    }
)

#: Hamza-carrier letters mapped to their plain counterparts:
#:
#:   U+0624  waw with hamza  -> waw
#:   U+0626  yaa with hamza  -> yaa
_ARABIC_HAMZA_MAP: Final = str.maketrans(
    {
        "\u0624": "\u0648",
        "\u0626": "\u064A",
    }
)


# ---------------------------------------------------------------------------
# Query-side constants
# ---------------------------------------------------------------------------


#: Multi-character article / preposition+article prefixes stripped by
#: ``query_variants`` so that ``<prefix><stem>`` matches stored
#: ``<stem>`` even though FTS5's ``unicode61`` tokenizer keeps the
#: clitic attached.
#:
#: Order matters: longest first, and the loop stops at the first
#: match. ``\u0627\u0644`` (bare article) is last so it only wins
#: when none of the longer forms match.
_ARABIC_QUERY_PREFIXES: Final[tuple[str, ...]] = (
    "\u0628\u0627\u0644",  # ba + al
    "\u0643\u0627\u0644",  # ka + al
    "\u0641\u0627\u0644",  # fa + al
    "\u0648\u0627\u0644",  # wa + al
    "\u0644\u0644",        # la + al (assimilated)
    "\u0627\u0644",        # bare definite article
)

#: Single-letter clitics that can precede the article. Used ONLY to
#: recognise the ``<clitic> + <article>`` pattern, never as a
#: standalone strip (that would turn a normal word into a shorter
#: unrelated word and manufacture false matches).
_SINGLE_CLITICS: Final[tuple[str, ...]] = (
    "\u0628",  # ba
    "\u0643",  # kaf
    "\u0641",  # fa
    "\u0648",  # waw
    "\u0644",  # lam
)

#: The Arabic definite article.
_ARABIC_DEFINITE_ARTICLE: Final[str] = "\u0627\u0644"


# ---------------------------------------------------------------------------
# Search-index constants (R2/R3 engine)
# ---------------------------------------------------------------------------


#: Prefixes stripped by ``search_variants`` when building the search
#: index for an entry. Includes both the multi-character article
#: forms AND bare single-letter clitics -- unlike the query-side
#: engine, the index side is allowed to be more aggressive because
#: false positives here are filtered downstream by BM25 + coverage
#: scoring, while false negatives (missing index variants) cannot
#: be recovered later.
_AR_SEARCH_PREFIXES: Final[tuple[str, ...]] = (
    "\u0648\u0627\u0644",
    "\u0628\u0627\u0644",
    "\u0643\u0627\u0644",
    "\u0641\u0627\u0644",
    "\u0644\u0644",
    "\u0627\u0644",
    "\u0648",
    "\u0641",
    "\u0628",
    "\u0643",
    "\u0644",
)

#: Suffixes stripped by ``search_variants``. Covers the most common
#: possessive / plural / agreement endings. Ordered longest-first in
#: spirit: the loop tries all, but longer suffixes must be tested
#: before shorter ones to avoid stripping only a fragment of a
#: longer suffix. The current loop order achieves this because each
#: suffix is checked independently and the resulting base is added
#: to ``out`` even if a shorter suffix also matches -- over-generation
#: is fine on the index side (recall > precision there).
_AR_SEARCH_SUFFIXES: Final[tuple[str, ...]] = (
    "\u0627\u062a\u0647\u0627",
    "\u0627\u062a\u0647\u0645",
    "\u0627\u062a\u0646\u0627",
    "\u0647\u0627",
    "\u0647\u0645",
    "\u0647\u0646",
    "\u0643\u0645",
    "\u0643\u0646",
    "\u0646\u0627",
    "\u0627\u062a",
    "\u0648\u0646",
    "\u064a\u0646",
    "\u0627\u0646",
    "\u064a",
    "\u0643",
    "\u0647",
)


# ---------------------------------------------------------------------------
# Stopwords and generic terms
# ---------------------------------------------------------------------------


#: Arabic function words that must NOT become OR terms in
#: ``match="any"`` mode. Union of the original base list plus the
#: extended list, with the multi-word phrase removed (it can never
#: equal a single ``\w+`` token, so it was dead weight).
#:
#: Each entry documents its transliteration in English so future
#: maintainers can add or remove entries without needing an Arabic
#: editor.
_ARABIC_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # --- Pronouns ---
        "\u0647\u0648",          # huwa   (he)
        "\u0647\u064a",          # hiya   (she)
        "\u0647\u0645",          # hum    (they)
        "\u0627\u0646\u0627",    # ana    (I)
        "\u0627\u0646\u062a",    # anta   (you, m.)
        "\u0646\u062d\u0646",    # nahnu  (we)
        # --- Prepositions / conjunction particles ---
        "\u0641\u064a",          # fi     (in)
        "\u0645\u0646",          # min    (from)
        "\u0627\u0644\u0649",    # ila    (to)
        "\u0639\u0644\u0649",    # ala    (on)
        "\u0639\u0646",          # an     (about)
        "\u0645\u0639",          # maa    (with)
        "\u0648",                # wa     (and)
        "\u0627\u0648",          # aw     (or)
        "\u062b\u0645",          # thumma (then)
        # --- Interrogatives / negation ---
        "\u0645\u0627",          # ma     (what / not)
        "\u0647\u0644",          # hal    (question particle)
        "\u0644\u0627",          # la     (no / not)
        "\u0646\u0639\u0645",    # naam   (yes)
        # --- Copulas / auxiliaries ---
        "\u0642\u062f",          # qad
        "\u0643\u0627\u0646",    # kana
        "\u0643\u0627\u0646\u062a",  # kanat
        # --- Demonstratives ---
        "\u0647\u0630\u0627",    # hadha
        "\u0647\u0630\u0647",    # hadhihi
        "\u0630\u0644\u0643",    # dhalika
        "\u062a\u0644\u0643",    # tilka
        # --- Relative pronouns ---
        "\u0627\u0644\u062a\u064a",  # allati
        "\u0627\u0644\u0630\u064a",  # alladhi
        # --- Social / greeting ---
        "\u064a\u0627",          # ya     (vocative)
        "\u0645\u0631\u062d\u0628\u0627",  # marhaba
        "\u0634\u0643\u0631\u0627",         # shukran
        # --- Extended interrogatives ---
        "\u0645\u0627\u0630\u0627",         # madha   (what)
        "\u0643\u064a\u0641",               # kayfa   (how)
        "\u0644\u0645\u0627\u0630\u0627",   # limadha (why)
        "\u0627\u064a\u0646",               # ayna    (where)
        "\u0645\u062a\u0649",               # mata    (when)
        # --- Short particles / adverbs ---
        "\u0627\u0646",                     # anna
        "\u0644\u0643",                     # laka (to you)
        "\u0644\u064a",                     # li   (to me)
        "\u0628\u0643",                     # bika (with you)
        "\u062c\u062f\u0627",               # jiddan (very)
        "\u0627\u064a\u0636\u0627",         # aydan (also)
        "\u0647\u0646\u0627\u0643",         # hunak (there)
        "\u0647\u0646\u0627",               # huna  (here)
        "\u0639\u0646\u062f",               # inda  (at)
        "\u0627\u0630\u0627",               # idha  (if)
        # --- Negation particles ---
        "\u0644\u0645",                     # lam
        "\u0644\u0646",                     # lan
        "\u0644\u064a\u0633",               # laysa
        "\u0644\u0643\u0646",               # lakin (but)
    }
)


#: Low-information Arabic words that carry almost no retrieval
#: signal on their own. Used by the R2 coverage scorer to reduce
#: their weight so a single incidental token cannot promote a
#: weak match to a high confidence score.
#:
#: Example: "what is my name" -> "ism" (name) is generic; it
#: matches many entries and should not dominate the score.
_ARABIC_GENERIC_TERMS: Final[frozenset[str]] = frozenset(
    {
        "\u0627\u0633\u0645",                # ism     (name)
        "\u0627\u062d\u0628",                # uhibb   (I love)
        "\u0644\u062f\u064a",                # ladayya (I have)
        "\u0639\u0646\u062f\u064a",          # indi    (I have)
        "\u0645\u0641\u0636\u0644",          # mufaddal (favourite)
        "\u0627\u062e\u062a\u0631\u0646",    # ikhtar-na  (pick/choose + feminine plural)
        "\u0627\u062e\u062a\u0631",          # ikhtar     (choose)
        "\u0642\u0631\u0631\u0646",          # qarrar-na  (decide + feminine plural)
    }
)


#: Regex fragments for Arabic social / conversational phrases.
#: Concatenated with ``|`` (and wrapped in ``^...\W*$``) by
#: ``classify_query`` to detect inputs that have no retrieval
#: intent. Each fragment is written against already-normalized
#: text: lowercase (no-op for Arabic), single-space internal
#: whitespace, no tatweel, no diacritics.
_ARABIC_SOCIAL_PATTERNS: Final[tuple[str, ...]] = (
    r"\u0643\u064a\u0641\s+\u062d\u0627\u0644\u0643",           # kayfa haluk
    r"\u0643\u064a\u0641\s+\u0627\u0644\u062d\u0627\u0644",     # kayfa al-hal
    r"\u0645\u0627\s+\u0627\u0644\u0627\u062e\u0628\u0627\u0631",  # ma al-akhbar
    r"\u0635\u0628\u0627\u062d\s+\u0627\u0644\u062e\u064a\u0631",  # sabah al-khayr
    r"\u0645\u0633\u0627\u0621\s+\u0627\u0644\u062e\u064a\u0631",  # masa al-khayr
    r"\u0645\u0646\s+\u0641\u0636\u0644\u0643",                 # min fadlik
)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ArabicProfile(BaseLanguageProfile):
    """Arabic language profile.

    All transforms are pure and deterministic. See module docstring
    for the compatibility guarantee and idempotence argument.
    """

    code: ClassVar[str] = "ar"
    priority: ClassVar[int] = 10

    # ------------------------------------------------------------------
    # Storage-side
    # ------------------------------------------------------------------

    def normalize(self, text: str) -> str:
        """Apply Arabic storage normalization.

        Order is load-bearing and matches the pre-refactor pipeline
        exactly:

          1. Remove tatweel first (it can appear inside a letter
             cluster and would otherwise interact with the
             diacritics regex in surprising ways).
          2. Remove diacritics.
          3. Unify alef variants.
          4. Unify alef maqsura -> yaa.
          5. Unify hamza carriers -> plain letters.
        """
        if not text:
            return text
        text = _ARABIC_TATWEEL.sub("", text)
        text = _ARABIC_DIACRITICS.sub("", text)
        text = text.translate(_ARABIC_ALEF_MAP)
        text = text.translate(_ARABIC_YAA_MAP)
        text = text.translate(_ARABIC_HAMZA_MAP)
        return text

    # ------------------------------------------------------------------
    # Query-side hooks
    # ------------------------------------------------------------------

    def query_prefixes(self) -> tuple[str, ...]:
        return _ARABIC_QUERY_PREFIXES

    def stopwords(self) -> frozenset[str]:
        return _ARABIC_STOPWORDS

    def generic_terms(self) -> frozenset[str]:
        return _ARABIC_GENERIC_TERMS

    def query_variants(self, token: str) -> list[str]:
        """Emit article/clitic-stripped variants of ``token``.

        Traces the exact algorithm of the historical
        ``_arabic_variants`` helper, minus the leading original-token
        entry (the caller always preserves it).

        For ``token = "ba+al+qahwa"`` the returned list is::

            [ "qahwa",       # ba stripped only (article preserved)
              "al+qahwa" ]   # clitic+article stripped via the prefix rule

        The article-peel pass then adds a further ``"qahwa"`` from
        ``"al+qahwa"``, but that duplicates the first entry and is
        deduplicated away.

        Order of returned variants is preserved: prefix-strip first,
        then single-clitic strip, then article-peel of each. This is
        the same order the original emitted.
        """
        # Build the full output list (including the original token)
        # so that the article-peel pass can inspect the original too.
        # The token is filtered out at the end to satisfy the base
        # contract.
        out: list[str] = [token]

        # 1. Longest multi-character prefix -- stop at first match.
        for prefix in _ARABIC_QUERY_PREFIXES:
            if token.startswith(prefix) and len(token) > len(prefix):
                out.append(token[len(prefix):])
                break

        # 2. Single clitic followed by the definite article.
        if (
            len(token) > len(_ARABIC_DEFINITE_ARTICLE) + 1
            and token[0] in _SINGLE_CLITICS
            and token[1:3] == _ARABIC_DEFINITE_ARTICLE
        ):
            out.append(token[1:])

        # 3. Peel a leading article from every form produced so far
        #    (including the original token, per the historical
        #    behaviour).
        for candidate in list(out):
            if (
                candidate.startswith(_ARABIC_DEFINITE_ARTICLE)
                and len(candidate) > len(_ARABIC_DEFINITE_ARTICLE)
            ):
                out.append(candidate[len(_ARABIC_DEFINITE_ARTICLE):])

        # Dedup and drop the original token (caller re-adds it).
        seen: set[str] = {token}
        result: list[str] = []
        for form in out[1:]:
            if form and form not in seen:
                seen.add(form)
                result.append(form)
        return result

    def search_variants(self, token: str) -> set[str]:
        """Return every search-index surface form of ``token``.

        Verbatim re-implementation of the R2/R3 ``arabic_variants``
        helper. Returns a set INCLUDING ``token`` per the base
        contract, so callers can union results across profiles
        without special-casing.
        """
        out: set[str] = {token}
        forms: set[str] = {token}

        # Step 1 -- prefix stripping into a working set.
        for p in _AR_SEARCH_PREFIXES:
            if token.startswith(p) and len(token) - len(p) >= 3:
                forms.add(token[len(p):])

        # Step 2 -- suffix stripping from every working form, plus
        # the taa-marbuta restoration trick that folds a possessive
        # back to its base noun (e.g. after stripping ``-t``, a
        # final taa is replaced with haa to match the article form).
        for f in list(forms):
            out.add(f)
            for s in _AR_SEARCH_SUFFIXES:
                if f.endswith(s) and len(f) - len(s) >= 3:
                    base = f[:-len(s)]
                    out.add(base)
                    if base.endswith("\u062a"):
                        out.add(base[:-1] + "\u0647")

        return {v for v in out if len(v) >= 3}

    def search_fold(self, text: str) -> str:
        """Taa-marbuta -> haa, applied only to the search index.

        Deliberately NOT part of ``normalize`` -- doing so would
        change ``content_hash`` and the stored
        ``normalized_content``, and the taa-marbuta distinction is
        semantically meaningful in many nouns.
        """
        return text.replace("\u0629", "\u0647")

    def social_patterns(self) -> tuple[str, ...]:
        return _ARABIC_SOCIAL_PATTERNS


#: Module-level singleton consumed by ``languages/__init__.py``.
PROFILE: Final[ArabicProfile] = ArabicProfile()


__all__ = ["PROFILE", "ArabicProfile"]
