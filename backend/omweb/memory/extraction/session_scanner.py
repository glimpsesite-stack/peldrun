# backend/omweb/memory/extraction/session_scanner.py
"""
PELDRUN Memory Platform - Phase 1-C: Session Scanner.

Discovers sessions on disk, decides which trigger mode applies
(chat vs agent), and produces `SessionCandidate` / `SessionDetail`
objects for the trigger router.

Responsibilities
----------------

1. **Enumerate sessions** -- via `project_manager.list_chats()`
   (index-backed, fast) then read each `session.json` for
   authoritative turn counts.

2. **Detect agent mode** -- inspect `session["turns"][i]["steps"]`
   and `session["messages"]` for tool-call evidence. If found,
   recommend `session_context_json` as the evidence source
   (§21.2), otherwise `session_json`.

3. **Compute per-chat state** -- `total_turns`,
   `last_extracted_turn_index`, `pending_turns`, `source_session_path`.

4. **Return the payload** -- `SessionDetail.get_extraction_payload()`
   returns exactly what `ExtractionOrchestrator.extract()` expects:
   `(session_dict, recommended_source)`.

Design decisions
----------------

1. **`project_manager.get_chat()` is the session reader.**
   It applies the healing rule ("running" + non-empty "result"
   -> "completed") that the extraction pipeline depends on for
   AFTER_SESSION triggering. Reading `session.json` directly
   would bypass healing and cause completed chats to remain
   "running" forever from the extraction pipeline's POV.

2. **Cursor presence matters.**
   `outbox.get_cursor_or_none()` distinguishes "no cursor row"
   (fresh chat, all turns pending) from "cursor=0 row exists"
   (turn 0 processed, turns 1+ pending). `pending_turns`
   calculation uses the distinction; `last_extracted_turn_index`
   is -1 for fresh chats so `start = last_extracted + 1` gives 0.

3. **`has_tool_calls` detection is conservative.**
   Any evidence of tool usage -- a step with "tool" in its type,
   a message with role="tool", or a message with a non-empty
   "tool_calls" field -- triggers agent-mode detection. False
   positives are harmless (context_sidecar is cleaner than
   session.json for evidence validation); false negatives would
   silently break evidence validation on agent sessions.

4. **No writes, no state, no LLM.**
   The scanner is pure discovery + shaping. Side effects are
   impossible; tests can construct any disk state they want
   without worrying about cross-test contamination.

5. **SessionDetail carries BOTH formats.**
   `session` (raw session.json dict) and `context_sidecar`
   (raw session.context.json dict). The caller picks via
   `get_extraction_payload()`. This makes the scanner reusable
   by future consumers that need the other format (e.g. a
   diagnostics endpoint that inspects the sidecar).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from omweb.memory.jobs.outbox import MemoryOutbox
from omweb.project_manager import project_manager


logger = logging.getLogger(
    "omweb.memory.extraction.session_scanner"
)


# ---------------------------------------------------------------------------
# SessionCandidate
# ---------------------------------------------------------------------------


_StatusLiteral = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]
"""Type alias for the four canonical session statuses.

Used ONLY as a type annotation. Never called directly --
`typing.Literal` is a type alias, not a constructable class.
The actual string-to-literal normalization is performed by
`SessionCandidate._coerce_status` (a Pydantic `field_validator`
with `mode="before"`).
"""


class SessionCandidate(BaseModel):
    """One row in the "which chats can we extract from?" list.

    See the plan §23.1 for the canonical field list. Field
    semantics:

      * ``last_extracted_turn_index`` -- index of the last turn
        successfully processed, or -1 if the chat has never been
        extracted. `start = last_extracted + 1` gives the first
        turn that needs extraction.
      * ``pending_turns`` -- number of turns not yet extracted.
        Equals ``total_turns`` for a fresh chat, and
        ``total_turns - last_extracted - 1`` once any extraction
        has completed.
      * ``recommended_evidence_source`` -- which file the
        extraction orchestrator should validate evidence
        against. See module docstring invariant #3.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    chat_id: str = Field(..., description="Chat identifier.")
    project_id: Optional[str] = Field(
        default=None,
        description="Owning project, or None for standalone chats.",
    )
    title: str = Field(
        default="",
        description="Chat title (display only).",
    )
    status: _StatusLiteral = Field(
        ...,
        description=(
            "Healed session status. Fresh chats show 'running'; "
            "completed chats show 'completed' (project_manager "
            "auto-heals 'running'+result -> 'completed')."
        ),
    )
    total_turns: int = Field(
        ...,
        ge=0,
        description="Length of `session.json['turns']`.",
    )
    last_extracted_turn_index: int = Field(
        ...,
        ge=-1,
        description=(
            "Index of the last turn that has been successfully "
            "extracted, or -1 for a fresh chat. `ge=-1` explicitly "
            "allows the fresh-chat state."
        ),
    )
    pending_turns: int = Field(
        ...,
        ge=0,
        description=(
            "Number of turns not yet extracted. Zero for a chat "
            "whose entire turn history has been processed."
        ),
    )
    source_session_path: str = Field(
        ...,
        description=(
            "Absolute path to the session.json file. Used by the "
            "worker's snapshot re-check on every heartbeat (§21.3)."
        ),
    )
    updated_at: float = Field(
        default=0.0,
        description="Unix timestamp of last session update.",
    )
    has_tool_calls: bool = Field(
        default=False,
        description=(
            "True when the session contains tool-call evidence "
            "(steps with tool types, messages with role='tool', "
            "or messages with tool_calls). Drives the choice of "
            "recommended_evidence_source."
        ),
    )
    recommended_evidence_source: Literal[
        "session_json", "session_context_json"
    ] = Field(
        default="session_json",
        description=(
            "'session_context_json' when has_tool_calls is True "
            "AND a context_sidecar file exists. 'session_json' "
            "otherwise."
        ),
    )

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, v: Any) -> str:
        """Map the raw status string to the four canonical values."""
        if v is None:
            return "running"
        s = str(v).strip().lower()
        if s in ("running", "completed", "failed", "cancelled"):
            return s
        if s in ("interrupted", "error"):
            return "failed"
        return "running"


# ---------------------------------------------------------------------------
# SessionDetail
# ---------------------------------------------------------------------------


class SessionDetail(BaseModel):
    """Full session payload -- both formats, pre-computed flags.

    See the plan §23.2. The caller uses
    ``get_extraction_payload()`` to obtain the exact
    ``(session_dict, recommended_source)`` tuple the
    ``ExtractionOrchestrator`` expects.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    session: Dict[str, Any] = Field(
        ...,
        description=(
            "Raw session.json dict, possibly healed "
            "(running+result -> completed). Includes an "
            "'events' key populated by `project_manager.get_chat`."
        ),
    )
    turns: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Convenience: `session['turns']`.",
    )
    messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Convenience: `session['messages']`. May be empty "
            "when the chat was written by `save_chat_session` "
            "only (which does not write `messages[]`)."
        ),
    )
    context_sidecar: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Raw session.context.json dict, or None when the "
            "sidecar file is missing or malformed."
        ),
    )
    has_tool_calls: bool = Field(
        default=False,
        description="See SessionCandidate.has_tool_calls.",
    )

    def get_extraction_payload(
        self,
    ) -> tuple[Dict[str, Any], str]:
        """Return `(session_dict, recommended_source)`.

        For agent-mode sessions (has_tool_calls=True) with a
        present context_sidecar, returns the sidecar and
        ``"session_context_json"``. Otherwise returns the raw
        session and ``"session_json"``.

        The caller then calls::

            payload, source = detail.get_extraction_payload()
            result = await orchestrator.extract(
                job=job, session=payload,
                recommended_source=source,
            )
        """
        if self.has_tool_calls and self.context_sidecar is not None:
            return self.context_sidecar, "session_context_json"
        return self.session, "session_json"


# ---------------------------------------------------------------------------
# MemorySessionScanner
# ---------------------------------------------------------------------------


class MemorySessionScanner:
    """Reads sessions from disk and shapes them for the trigger router.

    Constructor takes a `MemoryOutbox` (for cursor lookups). The
    outbox is used read-only; the scanner never modifies cursor
    state.
    """

    def __init__(self, outbox: MemoryOutbox) -> None:
        self._outbox = outbox

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    async def scan_all_sessions(
        self, *, only_unprocessed: bool = True
    ) -> List[SessionCandidate]:
        """Return a `SessionCandidate` for every chat with a session file.

        Parameters
        ----------
        only_unprocessed
            When True (default), skips chats with
            ``pending_turns == 0``. Used by BATCH
            (``POST /api/memory/extract/batch``) to build the
            work list. When False, returns every chat with a
            readable session file (used by diagnostics).

        Chats with missing or unreadable session files are
        silently skipped (the index may reference a chat whose
        file was deleted out-of-band).
        """
        raw_chats = await asyncio.to_thread(
            project_manager.list_chats
        )
        candidates: List[SessionCandidate] = []
        for chat_meta in raw_chats:
            if not isinstance(chat_meta, dict):
                continue
            chat_id = chat_meta.get("id")
            if not chat_id:
                continue
            try:
                candidate = await self._build_candidate(chat_id)
            except Exception as exc:
                logger.debug(
                    "scan_all_sessions: skipping %r -- %s",
                    chat_id, exc,
                )
                continue
            if candidate is None:
                continue
            if only_unprocessed and candidate.pending_turns <= 0:
                continue
            candidates.append(candidate)
        logger.info(
            "scan_all_sessions: %d chat(s) returned (only_unprocessed=%s).",
            len(candidates),
            only_unprocessed,
        )
        return candidates

    async def get_candidate(
        self, chat_id: str
    ) -> Optional[SessionCandidate]:
        """Return a `SessionCandidate` for one chat, or None.

        Returns None when the session file is missing or
        unreadable, or when the chat does not exist in the index.
        """
        return await self._build_candidate(chat_id)

    async def get_session_by_id(
        self, chat_id: str
    ) -> Optional[SessionDetail]:
        """Return the full `SessionDetail` for one chat, or None.

        Used by `ON-DEMAND` extraction (``POST /api/memory/extract/
        from-session``) and by the After-Turn trigger when it
        needs the raw payload.
        """
        session = await self._read_session_dict(chat_id)
        if session is None:
            return None
        sidecar = await self._read_context_sidecar(chat_id)
        has_tool_calls = _detect_has_tool_calls(session)
        turns = session.get("turns")
        messages = session.get("messages")
        return SessionDetail(
            session=session,
            turns=turns if isinstance(turns, list) else [],
            messages=messages if isinstance(messages, list) else [],
            context_sidecar=sidecar,
            has_tool_calls=has_tool_calls,
        )

    # ------------------------------------------------------------------
    # Cursor helpers (delegated to outbox)
    # ------------------------------------------------------------------

    async def get_cursor(self, chat_id: str) -> int:
        """Return the chat's cursor value (0 when no row exists)."""
        return await self._outbox.get_cursor(chat_id)

    async def get_cursor_or_none(
        self, chat_id: str
    ) -> Optional[int]:
        """Return the cursor value, or None when no row exists."""
        return await self._outbox.get_cursor_or_none(chat_id)

    async def get_total_turns(self, chat_id: str) -> int:
        """Return `len(session['turns'])` for a chat, or 0."""
        session = await self._read_session_dict(chat_id)
        if session is None:
            return 0
        turns = session.get("turns")
        return len(turns) if isinstance(turns, list) else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_candidate(
        self, chat_id: str
    ) -> Optional[SessionCandidate]:
        """Construct a `SessionCandidate` for one chat."""
        session = await self._read_session_dict(chat_id)
        if session is None:
            return None

        turns_raw = session.get("turns")
        turns = turns_raw if isinstance(turns_raw, list) else []
        total_turns = len(turns)

        chat_id_resolved = str(
            session.get("id") or chat_id
        ).strip() or chat_id
        project_id_raw = session.get("project_id")
        project_id = (
            str(project_id_raw).strip()
            if isinstance(project_id_raw, str) and project_id_raw.strip()
            else None
        )

        source_path = _resolve_session_path(
            chat_id_resolved, project_id
        )
        if source_path is None:
            # get_chat() succeeded but we cannot find the file via
            # the standard paths -- defensive skip.
            logger.debug(
                "_build_candidate: could not resolve session path "
                "for chat %r (project=%r).",
                chat_id_resolved, project_id,
            )
            return None

        # Cursor -- distinguishes fresh (None) from turn-0-processed (0).
        cursor_or_none = await self._outbox.get_cursor_or_none(
            chat_id_resolved
        )
        if cursor_or_none is None:
            last_extracted = -1
            pending_turns = total_turns
        else:
            last_extracted = cursor_or_none
            pending_turns = max(0, total_turns - last_extracted - 1)

        has_tool_calls = _detect_has_tool_calls(session)
        sidecar = (
            await self._read_context_sidecar(chat_id_resolved)
            if has_tool_calls else None
        )
        if has_tool_calls and sidecar is not None:
            recommended = "session_context_json"
        else:
            recommended = "session_json"

        status_raw = session.get("status") or "running"
        title_raw = session.get("title") or ""
        updated_at_raw = session.get("updated_at")

        return SessionCandidate(
            chat_id=chat_id_resolved,
            project_id=project_id,
            title=str(title_raw).strip(),
            # Pass the raw string; `_coerce_status` (mode="before")
            # normalizes it to one of the four canonical literals.
            # `_StatusLiteral(...)` cannot be used here -- `typing.Literal`
            # is a type alias, not a constructable class.
            status=status_raw,  # type: ignore[arg-type]
            total_turns=total_turns,
            last_extracted_turn_index=last_extracted,
            pending_turns=pending_turns,
            source_session_path=str(source_path),
            updated_at=_parse_to_unix(updated_at_raw),
            has_tool_calls=has_tool_calls,
            recommended_evidence_source=recommended,  # type: ignore[arg-type]
        )

    async def _read_session_dict(
        self, chat_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read + heal a session via `project_manager.get_chat`.

        Returns None when the chat is unknown or unreadable.
        Healing (``running + result -> completed``) is applied
        inside `project_manager.get_chat`.
        """
        try:
            data = await asyncio.to_thread(
                project_manager.get_chat, chat_id
            )
        except Exception as exc:
            logger.debug(
                "_read_session_dict: get_chat(%r) failed: %s",
                chat_id, exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        return data

    async def _read_context_sidecar(
        self, chat_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read a `session.context.json` sidecar, or None.

        Uses `chat_storage_engine.get_sidecar_data`, which
        transparently handles the case where the sidecar is
        missing (returns None).
        """
        try:
            from omweb.chat_storage_engine import (
                chat_storage_engine,
            )
            data = await asyncio.to_thread(
                chat_storage_engine.get_sidecar_data, chat_id
            )
        except Exception as exc:
            logger.debug(
                "_read_context_sidecar: get_sidecar_data(%r) "
                "failed: %s", chat_id, exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        return data


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _detect_has_tool_calls(session: Dict[str, Any]) -> bool:
    """Return True when the session contains tool-call evidence.

    Checks two sources (either is sufficient):

      1. ``session["messages"]`` -- any message with
         ``role="tool"`` or a non-empty ``tool_calls`` field.
         Only present in some code paths (save_chat_session
         does not write this array).

      2. ``session["turns"][i]["steps"]`` -- any step whose
         ``type`` contains "tool" (case-insensitive) or whose
         dict has a ``tool`` or ``tool_calls`` key. Always
         present (record_turn_and_sync writes it).

    False negatives are worse than false positives here: a
    chat misclassified as "no tool calls" would have its
    context sidecar ignored, and evidence validation might
    fail on turns whose real content lives in ``steps[]``.
    A chat misclassified as "has tool calls" merely uses
    the cleaner context.json format, which is always safe.
    """
    messages = session.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "tool":
                return True
            raw_calls = msg.get("tool_calls")
            if raw_calls:
                return True

    turns = session.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            steps = turn.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                step_type = str(step.get("type") or "").lower()
                if "tool" in step_type:
                    return True
                if step.get("tool") or step.get("tool_calls"):
                    return True

    return False


def _resolve_session_path(
    chat_id: str,
    project_id: Optional[str],
) -> Optional[Path]:
    """Return the absolute path to a chat's `session.json`.

    Follows the SAME search order as `project_manager.get_chat`:

      1. ``projects_dir/<project_id>/chats/<chat_id>/session.json``
         (only when project_id is non-empty)
      2. ``chats_dir/<chat_id>/session.json``

    Returns None when neither file exists, which means the chat
    is orphaned in the index (file deleted out-of-band).
    """
    candidates: List[Path] = []
    if project_id:
        candidates.append(
            project_manager.projects_dir
            / project_id
            / "chats"
            / chat_id
            / "session.json"
        )
    candidates.append(
        project_manager.chats_dir / chat_id / "session.json"
    )
    for p in candidates:
        if p.exists():
            return p.resolve()
    return None


def _parse_to_unix(value: Any) -> float:
    """Coerce a timestamp-like value to Unix seconds (float).

    Accepts:
      * ``int`` or ``float`` (passed through),
      * ISO-8601 strings (``"2026-11-15T10:00:00"``),
      * ``"YYYY-MM-DD HH:MM:SS"`` strings (the format used by
        `project_manager`).

    Returns 0.0 when the value is None, empty, or unparseable --
    a "session has no updated_at" state is not a reason to
    reject the candidate.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        # Try project_manager's format first.
        try:
            return datetime.strptime(
                s, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
        # Then ISO-8601.
        try:
            return datetime.fromisoformat(
                s.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            pass
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemorySessionScanner",
    "SessionCandidate",
    "SessionDetail",
]
