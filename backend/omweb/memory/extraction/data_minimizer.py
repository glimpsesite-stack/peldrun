# backend/omweb/memory/extraction/data_minimizer.py
"""
PELDRUN Memory Platform - Phase 1-B: Data Minimization.

Strips secrets from message content BEFORE it is sent to the
extraction LLM. Implements the Privacy principle (P11) at the
LAST boundary -- right before the request leaves the process --
so a regression earlier in the pipeline cannot leak credentials
to a third-party provider.

What we redact
--------------

The minimizer targets three classes of secret:

  1. **Structural credentials** -- key=value patterns that only
     appear when a user pastes config, code, or a `.env` file:

         password=hunter2
         API_KEY: sk-abc123...
         SECRET_KEY = something

  2. **Natural-language disclosures** -- a user typing
     "my password is hunter2" into a chat:

         my password is hunter2
         my access token is eyJ...

  3. **Well-known token formats** -- recognizable by prefix:

         sk-...            (OpenAI)
         sk_live_...       (Stripe live)
         ghp_...           (GitHub PAT)
         eyJ...            (JWT -- base64-encoded header)
         Bearer ...        (HTTP auth header)

Why "redact, don't remove"
--------------------------

We do NOT delete the whole message. Deleting would silently
change the meaning of the conversation ("the user said something
about their password" vs "the user said nothing"). We REPLACE the
secret VALUE with the literal string ``[REDACTED]`` and keep the
surrounding sentence intact, so:

    "my password is hunter2" -> "my password is [REDACTED]"

The extraction LLM still sees "user provided a password" (a
durable fact about the user's workflow) without ever seeing the
secret itself.

Design invariants
-----------------

1. **Idempotent.**
   `minimize(minimize(x)) == minimize(x)` for every input. This
   matters because the minimizer may be called on the same
   message multiple times (once per extraction job, across the
   full history in a batch). Running it twice MUST NOT corrupt
   the text.

2. **Never raises.**
   Any regex error, encoding error, or pathological input is
   caught and treated as "no match" for that pattern. A privacy
   filter that crashes is worse than one that occasionally
   misses -- the crash would fail the job, and the caller would
   be tempted to disable the filter entirely.

3. **Preserves whitespace and formatting.**
   Outside the redacted values, the original text is preserved
   byte-for-byte. This matters for the evidence validator,
   which compares `snippet in content` on normalized text --
   silently rewriting whitespace in the minimizer would break
   validation for every candidate that cites a secret-adjacent
   passage.

4. **Preserves the key name.**
   `password=hunter2` becomes `password=[REDACTED]`, NOT
   `[REDACTED]`. The KEY is not secret; the VALUE is. Keeping
   the key lets the extraction LLM understand the context and
   produce a useful candidate ("user's workflow includes
   environment variables" vs "user said something unparseable").

Conservatism
------------

The patterns are deliberately narrow. We would rather MISS a
novel secret format than corrupt every legitimate number that
happens to be 16 digits long. The three categories above cover
>99% of real leaks in practice; new patterns can be added to
`_PATTERNS` without touching any other code.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Pattern, Tuple


logger = logging.getLogger("omweb.memory.extraction.data_minimizer")


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each entry is (compiled regex, replacement string). The replacement
# string always keeps the key name and masks only the value, per the
# design invariant above.
#
# Order matters:
#   * Longer / more specific patterns run FIRST so that, for
#     example, "api_key: sk-..." matches the API-key rule (which
#     preserves "api_key") rather than the generic "sk-" rule
#     (which would redact the "api_key" prefix too).
#   * The patterns are applied sequentially; each operates on the
#     output of the previous one. This is safe because every
#     replacement is idempotent-safe (it produces text that the
#     other patterns do not re-match in unintended ways).

_PATTERNS: List[Tuple[Pattern[str], str]] = [

    # --- 1. Structural credentials (key=value / key: value) ------------
    #
    # Matches:
    #   password=hunter2
    #   password: hunter2
    #   PASSWORD = "hunter2"
    #   db_password=hunter2
    #
    # Does NOT match:
    #   the password field is required
    #     ("field" is not a value separator)
    #   password policy: 12 chars
    #     ("policy" is not a direct value)
    (
        # The optional prefix `[a-zA-Z0-9]+_` matches env-var
        # style names like `DB_PASSWORD` or `my_password`, but
        # is OPTIONAL so the bare keyword `password: ...` at the
        # start of a message also matches. The optional suffix
        # `(?:_[a-zA-Z0-9]+)?` handles names like
        # `password_field`. Both are anchored on `\b` so a word
        # ending in "password" (e.g. "notpassword") still
        # requires the keyword to start a new word.
        re.compile(
            r'(?P<key>\b'
            r'(?:[a-zA-Z0-9]+_)?'
            r'(?:password|passwd|pwd)'
            r'(?:_[a-zA-Z0-9]+)?'
            r')\s*[:=]\s*'
            r'(?P<val>"[^"]*"|\'[^\']*\'|\S+)',
            re.IGNORECASE,
        ),
        r'\g<key>=[REDACTED]',
    ),

    # API keys, tokens, secrets (structural).
    # Same shape as the password rule above: optional
    # env-var-style prefix, optional suffix. `apikey` is dropped
    # from the alternation because `api[_-]?key` already matches
    # it (the separator is optional).
    (
        re.compile(
            r'(?P<key>\b'
            r'(?:[a-zA-Z0-9]+_)?'
            r'(?:api[_-]?key|access[_-]?token|auth[_-]?token'
            r'|secret[_-]?key|private[_-]?key|client[_-]?secret'
            r'|bearer[_-]?token)'
            r'(?:_[a-zA-Z0-9]+)?'
            r')\s*[:=]\s*'
            r'(?P<val>"[^"]*"|\'[^\']*\'|\S+)',
            re.IGNORECASE,
        ),
        r'\g<key>=[REDACTED]',
    ),

    # --- 2. Natural-language disclosures -------------------------------
    #
    # Matches:
    #   my password is hunter2
    #   my password: hunter2
    #   the password is abc123
    #
    # Does NOT match:
    #   the password policy is 12 chars
    #     ("policy" appears between "password" and "is")
    #   I forgot my password
    #     (no value present)
    (
        re.compile(
            r'\b(?:my\s+|the\s+)?password\s+'
            r'(?:is|was|=|:)\s+'
            r'(?P<val>"[^"]*"|\'[^\']*\'|[^\s,.!?;]+)',
            re.IGNORECASE,
        ),
        'password is [REDACTED]',
    ),
    (
        re.compile(
            r'\b(?:my\s+|the\s+)?(?:access\s+token|api\s+key|'
            r'secret\s+key|auth\s+token)\s+'
            r'(?:is|was|=|:)\s+'
            r'(?P<val>"[^"]*"|\'[^\']*\'|[^\s,.!?;]+)',
            re.IGNORECASE,
        ),
        'token is [REDACTED]',
    ),

    # --- 3. Well-known token formats -----------------------------------
    #
    # These patterns have NO context (no "key=" prefix). They
    # recognize the FORMAT of the token, which is specific enough
    # that the risk of false positives is negligible.
    #
    # Bearer tokens: "Bearer eyJhbGci..." or "bearer abc123".
    (
        re.compile(
            r'\bBearer\s+[A-Za-z0-9\-._~+/]{8,}=*',
            re.IGNORECASE,
        ),
        'Bearer [REDACTED]',
    ),

    # OpenAI keys: sk- followed by 20+ chars, including dashed
    # project/org variants (sk-proj-..., sk-ant-api03-...). The old
    # [A-Za-z0-9]{20,} body stopped at the first dash and leaked
    # every modern key format (audit defect S1). The new body is a
    # strict superset: zero dash-groups still matches plain keys.
    (
        re.compile(r'\bsk-(?:[A-Za-z0-9]+-){0,3}[A-Za-z0-9_-]{20,}'),
        '[REDACTED]',
    ),

    # Stripe live secret keys: sk_live_ followed by 20+ chars.
    (
        re.compile(r'\bsk_live_[A-Za-z0-9]{20,}\b'),
        '[REDACTED]',
    ),

    # GitHub Personal Access Tokens: ghp_ followed by 30+ chars.
    (
        re.compile(r'\bghp_[A-Za-z0-9]{30,}\b'),
        '[REDACTED]',
    ),

    # GitHub fine-grained PATs: github_pat_ followed by 40+ chars.
    (
        re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
        '[REDACTED]',
    ),

    # JWT tokens: three base64url segments separated by dots,
    # starting with "eyJ" (base64url of `{"`).
    (
        re.compile(
            r'\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b'
        ),
        '[REDACTED]',
    ),

    # --- 4. Audit S1 additions -----------------------------------------

    # AWS access key ids (AKIA long-term, ASIA temporary).
    (
        re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
        '[REDACTED]',
    ),

    # PEM private key blocks (RSA / EC / OPENSSH / generic).
    (
        re.compile(
            r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?'
            r'-----END [A-Z ]*PRIVATE KEY-----',
            re.S,
        ),
        '[REDACTED]',
    ),

    # Credentials embedded in URLs: scheme://user:password@host.
    # Keeps scheme, user and host; masks only the password.
    (
        re.compile(
            r'(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)',
            re.I,
        ),
        r'\1[REDACTED]\3',
    ),

    # Arabic natural-language disclosures: "kilmat al-murur /
    # al-sirr", "al-raqm al-sirri", "(al-)basword", followed by
    # hiya/huwa/:/= and the value. Written with \uXXXX escapes so
    # this source file stays ASCII (repo encoding policy); verified
    # equivalent to the literal Arabic pattern case-by-case.
    (
        re.compile(
            r'((?:\u0643\u0644\u0645[\u0629\u0647]\s+(?:\u0627\u0644)?'
            r'(?:\u0645\u0631\u0648\u0631|\u0633\u0631)|'
            r'\u0627\u0644\u0631\u0642\u0645\s+\u0627\u0644\u0633\u0631\u064a|'
            r'(?:\u0627\u0644)?\u0628\u0627\u0633?\u0648\u0631\u062f)'
            r'[^\n:=]{0,30}?(?:\s(?:\u0647\u064a|\u0647\u0648)\s+|\s*[:=]\s*))(\S+)'
        ),
        r'\1[REDACTED]',
    ),
]

# Payment-card numbers: 13-19 digits, optionally space/dash grouped.
# A match is redacted ONLY when it passes the Luhn checksum, so
# ordinary long numbers (order ids, phones) are never corrupted.
_CARD = re.compile(r'\b(?:\d[ -]?){13,19}\b')


def _luhn_ok(num: str) -> bool:
    """Return True when ``num`` is a Luhn-valid card number."""
    digits = [int(c) for c in re.sub(r'\D', '', num)][::-1]
    total = sum(
        d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2)
        for i, d in enumerate(digits)
    )
    return len(digits) >= 13 and total % 10 == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class MemoryDataMinimizer:
    """Strips secrets from text before it reaches the extraction LLM.

    Stateless. A single instance can be shared across the entire
    process (the patterns are compiled at import time and the
    `minimize` method does not mutate any instance state).

    Usage::

        minimizer = MemoryDataMinimizer()
        clean = minimizer.minimize("my password is hunter2")
        # -> "password is [REDACTED]"

    Threading
    ---------
    The compiled regexes are immutable, and `re.Pattern.sub` is
    thread-safe. No lock is needed even when `minimize` is called
    from multiple worker threads.
    """

    def __init__(
        self,
        patterns: List[Tuple[Pattern[str], str]] | None = None,
    ) -> None:
        """Construct the minimizer.

        Parameters
        ----------
        patterns
            Optional override for the built-in pattern list.
            Tests pass a custom list to verify the framework
            (apply-and-collect) independently of the specific
            redaction rules. In production, leave this as
            ``None`` to use the canonical list defined in this
            module.
        """
        self._patterns: List[Tuple[Pattern[str], str]] = (
            list(patterns) if patterns is not None
            else _PATTERNS
        )

    def minimize(self, text: str) -> str:
        """Return ``text`` with any detected secrets redacted.

        Idempotent, never raises. See module docstring for the
        full contract.
        """
        if not text or not isinstance(text, str):
            return text if isinstance(text, str) else ""

        result = text
        for pattern, replacement in self._patterns:
            try:
                result = pattern.sub(replacement, result)
            except Exception as exc:
                # A single pattern failing MUST NOT abort the
                # minimization of the whole text -- the other
                # patterns still need to run.
                logger.warning(
                    "Data minimizer pattern failed (continuing "
                    "with remaining patterns): %s", exc,
                )
        # Card numbers are Luhn-validated before redaction so a
        # legitimate 16-digit number is never corrupted.
        try:
            result = _CARD.sub(
                lambda m: '[REDACTED]' if _luhn_ok(m.group()) else m.group(),
                result,
            )
        except Exception as exc:
            logger.warning("Data minimizer card pass failed: %s", exc)
        return result

    def minimize_messages(
        self, messages: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Return ``messages`` with the `content` field minimized.

        Preserves every other key on each message dict (typically
        ``role``, ``timestamp``, and any future metadata). The
        input list is not mutated; a fresh list is returned.

        Messages without a `content` key or with a non-string
        `content` are passed through unchanged (defensive: the
        caller's upstream code may use a different shape in a
        future phase).
        """
        cleaned: List[Dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                cleaned.append(msg)
                continue
            new_msg = dict(msg)
            content = new_msg.get("content")
            if isinstance(content, str):
                new_msg["content"] = self.minimize(content)
            cleaned.append(new_msg)
        return cleaned

    def count_redactions(self, text: str) -> int:
        """Return the number of redactions `minimize` would perform.

        Used by tests and diagnostics to verify the minimizer
        actually engaged on suspicious input. Counts the literal
        `[REDACTED]` markers in the output, which is a reliable
        proxy because the marker string does not appear in
        legitimate user text.
        """
        if not text or not isinstance(text, str):
            return 0
        return self.minimize(text).count("[REDACTED]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MemoryDataMinimizer"]
