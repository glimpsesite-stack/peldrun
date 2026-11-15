# backend/omweb/memory/extraction/snapshot.py
"""
PELDRUN Memory Platform - Phase 1-C: Message Snapshot Hashing.

Computes a deterministic hash of a session's message content so
that the worker can detect mid-flight changes:

    1. When a job is enqueued, the orchestrator computes the hash
       and stores it on `MemoryJob.message_snapshot_hash`.
    2. On every heartbeat, the worker re-reads the session and
       recomputes the hash.
    3. If the two hashes differ, the session changed under the
       job's feet. The worker fails the job with
       `SESSION_CHANGED` (§21.3) instead of extracting from a
       mixed-state session.

Why hash a SNAPSHOT, not just compare `updated_at`
--------------------------------------------------

`updated_at` changes on EVERY write (title, pin, archive,
`last_used_at` on turn metrics, etc.). A job enqueued at
12:00:00 and picked up at 12:00:01 would fail with
`SESSION_CHANGED` if the user simply pinned the chat in
between -- even though the actual messages are unchanged.
That would make the extraction pipeline unusable on active
sessions.

The hash covers ONLY the content the extractor cares about:

    * each turn's stable identifier (detects insert / delete /
      reorder),
    * each turn's user+assistant text (detects edit).

Metadata changes (title, pin, usage summary) do NOT change
the hash and therefore do NOT trigger `SESSION_CHANGED`. That
is the correct behavior: they are invisible to the LLM
prompt, so they cannot affect extraction quality.

Source-of-truth decision
------------------------

`session.json` has TWO arrays that carry message content:

  * `turns[]` -- the authoritative array. Each entry is
    ``{id, user, assistant, timestamp, steps, artifacts}``.
  * `messages[]` -- a flat user/assistant list written by
    ``record_turn_and_sync`` but NOT by ``save_chat_session``.
    It may be absent, stale, or drift from ``turns[]``
    depending on which code path last wrote the file.

The plan's §21.1 recommends hashing ``messages[]``. That works
in a codebase where ``messages[]`` is always written. In the
actual PELDRUN code, ``save_chat_session`` (called by
``dispatcher.run_direct_chat``) overwrites ``session.json``
WITHOUT touching ``messages[]``. Hashing that array would
produce a snapshot that ignores every message written through
the chat-mode path -- a silent correctness bug for chat users.

We use ``turns[]`` because:

  * it is written by BOTH code paths (record_turn_and_sync
    and save_chat_session both preserve it),
  * it has explicit `user` and `assistant` fields, so we do
    not need to reconstruct roles,
  * it is the same array the extraction orchestrator reads
    from, so a "snapshot matches" state guarantees the LLM
    sees exactly what was hashed.

Determinism guarantees
----------------------

The hash is stable across:

    * process restarts -- no time, PID, or random value
      contributes to it,
    * Python version differences -- uses stdlib `hashlib`
      only,
    * locale differences -- strings are encoded to UTF-8
      explicitly, no `.encode()` without argument.

The hash is IDENTICAL for two sessions iff they have the
same number of turns, same turn IDs in the same order, and
same user+assistant text per turn. Any deviation produces a
different hash.

Collision resistance
--------------------

SHA-256 output, truncated to 32 hex chars = 128 bits of
entropy. This is sufficient for a system that could, in the
worst case, generate ~10^6 distinct session states. Birthday
collision probability at that scale is ~10^-27.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional


logger = logging.getLogger("omweb.memory.extraction.snapshot")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SNAPSHOT_HASH_LENGTH: int = 32
"""Truncated SHA-256 hex length. 32 chars = 128 bits. See
module docstring for the collision-resistance rationale."""

_PER_TURN_HASH_LENGTH: int = 16
"""Truncated per-turn content hash length. Kept shorter than
the outer hash because per-turn hashes never leave the process;
only the outer hash is persisted."""

_FIELD_SEPARATOR: str = "|"
"""Separator between top-level hash components (turn IDs vs
content hashes)."""

_WITHIN_TURN_SEPARATOR: str = "\x1f"
"""ASCII Unit Separator (0x1F). Used to join user + assistant
text within one turn. Chosen because it CANNOT appear in
legitimate user content (any typed character is printable;
control chars are stripped by `record_turn_and_sync`'s
`.replace("\r\n", " ").replace("\n", " ")` sanitation)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_message_snapshot_hash(session_json: Dict[str, Any]) -> str:
    """Return the deterministic snapshot hash of a session document.

    Parameters
    ----------
    session_json
        A parsed ``session.json`` document. Missing or malformed
        ``turns[]`` is treated as an empty session (hash of an
        empty string list). This makes the function TOTAL -- it
        never raises on any dict input, which matters because
        it runs on every heartbeat against files that may be
        mid-write on a slow filesystem.

    Returns
    -------
    str
        32 hex chars (128 bits). Identical inputs produce
        identical hashes across processes and Python versions.

    What is included in the hash
    ----------------------------
    For each turn (in array order):
        * the turn's stable identifier (``id`` or ``turn_id``),
        * a truncated SHA-256 of ``user`` + U+001F + ``assistant``.

    What is NOT included:
        * ``timestamp`` -- changes on re-save even when content
          is unchanged,
        * ``steps`` and ``artifacts`` -- telemetry, not content,
        * the top-level ``title``, ``status``, ``usage_summary``,
          ``updated_at``, ``is_pinned``, ``is_archived``,
        * any field in ``messages[]`` (see module docstring for
          the source-of-truth rationale).
    """
    if not isinstance(session_json, dict):
        # Defensive: a malformed session is treated as empty.
        # The worker logs this via the caller; we do NOT raise.
        logger.debug(
            "compute_message_snapshot_hash: session is not a dict "
            "(got %r); treating as empty.",
            type(session_json).__name__,
        )
        return _hash_parts([], [])

    turns_raw = session_json.get("turns")
    turns: list = turns_raw if isinstance(turns_raw, list) else []

    # --- Phase 1: turn IDs ------------------------------------------------
    turn_ids: list[str] = []
    for t in turns:
        if not isinstance(t, dict):
            turn_ids.append("")
            continue
        # Accept both key names. `record_turn_and_sync` writes
        # ``id``; the plan's reference format uses ``turn_id``.
        # Either present gives us a stable identifier.
        raw_id = t.get("turn_id") or t.get("id") or ""
        turn_ids.append(str(raw_id).strip())

    # --- Phase 2: per-turn content hashes --------------------------------
    content_hashes: list[str] = []
    for t in turns:
        if not isinstance(t, dict):
            content_hashes.append("")
            continue
        user_text = str(t.get("user") or "")
        assistant_text = str(t.get("assistant") or "")
        combined = f"{user_text}{_WITHIN_TURN_SEPARATOR}{assistant_text}"
        h = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        content_hashes.append(h[:_PER_TURN_HASH_LENGTH])

    return _hash_parts(turn_ids, content_hashes)


def compute_snapshot_from_file(
    session_path: "Path | str",
) -> Optional[str]:
    """Read a ``session.json`` from disk and compute its snapshot hash.

    Returns ``None`` when the file cannot be read or parsed. This
    mirrors the worker's needs: a missing/corrupt file is a
    recoverable condition (the worker fails the job with
    ``SESSION_MISSING`` on the next heartbeat), not an exception
    the caller should catch.

    Parameters
    ----------
    session_path
        Path to a ``session.json`` file. Accepts both ``str`` and
        ``pathlib.Path``.
    """
    import json
    from pathlib import Path

    try:
        path = Path(session_path)
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        logger.debug(
            "compute_snapshot_from_file: file missing at %r",
            session_path,
        )
        return None
    except (OSError, ValueError) as exc:
        logger.debug(
            "compute_snapshot_from_file: could not read/parse %r: %s",
            session_path,
            exc,
        )
        return None

    return compute_message_snapshot_hash(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_parts(
    turn_ids: list[str],
    content_hashes: list[str],
) -> str:
    """Combine the two component lists into the final hash.

    The concatenation order matters: turn IDs first, then content
    hashes, separated by ``_FIELD_SEPARATOR``. This makes a hash
    of ``["a", "b"]`` + ``["c", "d"]`` distinguishable from
    ``["a", "c"]`` + ``["b", "d"]`` -- an important property for
    detecting a reordering where the content of two turns swaps
    but their IDs do not.
    """
    raw = _FIELD_SEPARATOR.join(turn_ids + content_hashes)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[
        :_SNAPSHOT_HASH_LENGTH
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "compute_message_snapshot_hash",
    "compute_snapshot_from_file",
]
