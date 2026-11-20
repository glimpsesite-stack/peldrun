# Path: backend/omweb/routers/files.py
"""
Workspace Files Router.
Provides file tree scanning, upload, download, deletion, and direct content editing persistence.
Hardened under Phase M2 & Release Bug Fixes:
- Added canonical multi-file upload endpoint supporting dual-tier persistence (workspace & chat session).
- Extended multi-tier lookup (root, workspace, storage/chats) to prevent 404 errors on deliverables.
- [PERF-A] ``list_workspace_files`` now prunes internal directories during the walk and caches the
  merged artifact list at module level (30s TTL + explicit invalidation on every write endpoint).
- [PERF-A] New ``exclude_hidden`` query flag lets the artifacts showcase drop dot-prefixed paths
  server-side, eliminating ~40-70% of the JSON payload on a typical workspace.
"""

import base64
import mimetypes
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
STORAGE_DIR = PROJECT_ROOT / "storage"
CHATS_DIR = STORAGE_DIR / "chats"
PROJECTS_DIR = STORAGE_DIR / "projects"

for d in [WORKSPACE_DIR, STORAGE_DIR, CHATS_DIR, PROJECTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

SYSTEM_FILES = {"events.json", "session.json", "project.json", ".gitkeep", ".ds_store", "thumbs.db"}

# ---------------------------------------------------------------------------
# Artifact listing cache
# ---------------------------------------------------------------------------
# ``list_workspace_files`` walks the entire storage tree with ``rglob`` on
# every call. On a Windows workstation with an HDD and a workspace that has
# accumulated thousands of deliverables this takes several seconds and blocks
# the event loop, so the artifacts showcase feels frozen on every visit.
#
# We cache the merged artifact list at module level and rebuild it lazily:
#   * A mutation endpoint (upload / save / delete) invalidates the cache so
#     the next read sees fresh data.
#   * A short TTL (``_ARTIFACTS_CACHE_TTL_SECONDS``) bounds staleness from
#     changes made outside the API (manual copy in Explorer, background
#     agent runs, the storage clear-chats endpoint in another router).
#   * The frontend can force a rebuild with ``?refresh=true`` when the user
#     clicks the Refresh button.
_ARTIFACTS_CACHE_TTL_SECONDS: float = 30.0
_artifacts_cache: Optional[List[Dict[str, Any]]] = None
_artifacts_cache_built_at: float = 0.0


def _invalidate_artifacts_cache() -> None:
    """
    Drop the cached artifact list so the next read rebuilds it.

    Called by every mutation endpoint (upload / save / delete) so the
    cache never serves stale data after a write the caller just made.
    """
    global _artifacts_cache, _artifacts_cache_built_at
    _artifacts_cache = None
    _artifacts_cache_built_at = 0.0


# Directories that are always skipped during the tree walk. They are large,
# never contain user-facing deliverables, and walking them is the single
# biggest cost of the scan. Pruning happens in-place via ``os.walk`` so the
# walker never even opens a directory descriptor for them.
#
# ``dist`` and ``build`` are intentionally NOT listed: users may legitimately
# drop deliverables there. If perf becomes a problem later they can be added
# back without affecting correctness.
_SKIP_DIR_NAMES: frozenset = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".peldrun",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".next",
    ".turbo",
    ".cache",
})


def _is_hidden_rel_path(rel_path: str) -> bool:
    """
    Return True when any segment of ``rel_path`` starts with a dot.

    Used to power the ``exclude_hidden`` query flag. Works on the already
    normalized forward-slash paths produced by ``_walk_artifacts`` so a
    single split is enough.
    """
    if not rel_path:
        return False
    return any(seg.startswith(".") for seg in rel_path.split("/"))

# ---------------------------------------------------------------------------
# Temporary artifact store (multimodal image uploads)
# ---------------------------------------------------------------------------
# User-pasted screenshots and tool-captured screenshots are stored here as
# opaque ``<file_id>`` references and embedded into provider payloads
# on-demand. This avoids the ~33% size penalty of inlining base64 in the
# run request, and keeps the JSON session files small enough to remain
# human-inspectable.
TEMP_ARTIFACTS_DIR = STORAGE_DIR / "artifacts" / "temp"
TEMP_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Content-addressed image cache written by
# ``peldrun.engine.state._strip_image_data_from_snapshot``. Every
# checkpoint that contains a base64 image stores only a
# ``artifact-cache://<sha256_16>.<ext>`` reference; the raw bytes live
# here, deduplicated across all checkpoints and all runs.
#
# Cleaned by ``cleanup_temp_artifacts`` under the same age threshold as
# the temp store: 24 hours old checkpoints are no longer useful for
# resume, and the image cache can be reclaimed in lockstep.
CHECKPOINT_CACHE_DIR = STORAGE_DIR / "artifacts" / "checkpoint_cache"
CHECKPOINT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Temporary artifact cleanup
# ---------------------------------------------------------------------------
# Temp artifacts are uploaded images and screenshots referenced by
# ``fileId`` during a run. They are intentionally short-lived: once the
# turn is finished, the workspace copy under
# ``<chat_files>/.peldrun/user_uploads/`` is the authoritative fallback,
# so the temp file only needs to survive long enough for the running
# turn to finish.
#
# Without periodic cleanup the temp store grows unbounded — every pasted
# screenshot of every conversation stays on disk forever. The endpoint
# and helper below reclaim that space automatically.
TEMP_ARTIFACT_MAX_AGE_HOURS_DEFAULT: float = 24.0
TEMP_ARTIFACT_CLEANUP_INTERVAL_SECONDS_DEFAULT: float = 6 * 3600.0  # 6h


def cleanup_temp_artifacts(
    older_than_hours: float = TEMP_ARTIFACT_MAX_AGE_HOURS_DEFAULT,
) -> Dict[str, Any]:
    """
    Delete temp artifacts whose last-modified timestamp is older than
    ``older_than_hours``.

    Design notes
    ------------
    * Only files directly inside ``TEMP_ARTIFACTS_DIR`` are considered.
      The directory is flat by construction (``upload-temp`` writes
      ``<fileId>`` at its root), so recursion is unnecessary and would
      only widen the attack surface.
    * The mtime — not atime — is used as the age signal, because atime
      may be disabled by ``noatime`` mounts and would therefore never
      advance.
    * Files still being written by a concurrent upload are protected
      implicitly: their mtime is refreshed on every chunk write, so they
      will never fall below the threshold mid-upload.
    * Errors on individual files (permission, transient locks) are
      collected and returned rather than aborting the sweep. A single
      stuck file must not block cleanup of the rest.

    Returns
    -------
    dict
        Report shape::

            {
                "scanned": int,            # files examined
                "deleted_count": int,      # files removed
                "kept_count": int,         # files still fresh
                "deleted_bytes": int,      # total bytes reclaimed
                "kept_bytes": int,         # total bytes still on disk
                "threshold_hours": float,  # the cutoff used
                "threshold_timestamp": float,
                "deleted_files": [ ... ],  # up to 50 basenames
                "errors": [ {"file": ..., "error": ...} ],
                "directory_exists": bool,
            }
    """
    now_ts = time.time()
    threshold_ts = now_ts - (float(older_than_hours) * 3600.0)

    report: Dict[str, Any] = {
        "scanned": 0,
        "deleted_count": 0,
        "kept_count": 0,
        "deleted_bytes": 0,
        "kept_bytes": 0,
        "threshold_hours": float(older_than_hours),
        "threshold_timestamp": threshold_ts,
        "deleted_files": [],
        "errors": [],
        "directory_exists": TEMP_ARTIFACTS_DIR.exists(),
    }

    if not TEMP_ARTIFACTS_DIR.exists():
        return report

    try:
        entries = list(TEMP_ARTIFACTS_DIR.iterdir())
    except Exception as exc:
        report["errors"].append({
            "file": "<directory>",
            "error": f"Failed to enumerate: {exc}",
        })
        return report

    def _sweep_directory(
        directory: Path,
        label: str,
    ) -> None:
        """
        Delete files inside ``directory`` older than the shared threshold.

        Accumulates into the outer ``report`` dict so the caller sees a
        unified summary across all swept directories. Per-file errors
        are collected, not raised.
        """
        if not directory.exists():
            return
        try:
            local_entries = list(directory.iterdir())
        except Exception as exc:
            report["errors"].append({
                "file": f"<{label}>",
                "error": f"Failed to enumerate: {exc}",
            })
            return

        for entry in local_entries:
            try:
                if not entry.is_file():
                    continue
                report["scanned"] += 1

                stat = entry.stat()
                size = int(stat.st_size)
                mtime = float(stat.st_mtime)

                if mtime > threshold_ts:
                    report["kept_count"] += 1
                    report["kept_bytes"] += size
                    continue

                entry.unlink()
                report["deleted_count"] += 1
                report["deleted_bytes"] += size
                if len(report["deleted_files"]) < 50:
                    # Prefix with the directory label so operators can
                    # tell temp artifacts apart from checkpoint cache
                    # files in the JSON response.
                    report["deleted_files"].append(f"{label}:{entry.name}")
            except Exception as exc:
                report["errors"].append({
                    "file": f"{label}:{entry.name}",
                    "error": str(exc),
                })

    # Sweep the temp upload store first.
    _sweep_directory(TEMP_ARTIFACTS_DIR, "temp")

    # [FIX-CHECKPOINT-OPT] Sweep the checkpoint image cache under the
    # same age threshold. Checkpoints older than 24 hours are no longer
    # relevant for resume, so the images they reference can be reclaimed
    # in lockstep with the temp artifacts that produced them.
    _sweep_directory(CHECKPOINT_CACHE_DIR, "checkpoint_cache")

    return report

# Only image formats that both major vision providers accept are allowed.
# SVG is excluded because it can carry script payloads and is rejected
# as a vision input by OpenAI/Anthropic.
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Extension <-> MIME mappings used to normalize uploads and to serve
# temporary artifacts back over HTTP with the correct Content-Type.
_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXT_TO_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _probe_image_dimensions(
    path: Path,
) -> Tuple[Optional[int], Optional[int]]:
    """
    Best-effort image dimension probe using Pillow.

    Returns ``(width, height)`` when Pillow is available and the file
    parses; ``(None, None)`` otherwise. The upload endpoint surfaces
    these dimensions so the frontend can render accurate previews
    without a second round-trip, and so downstream consumers can pick
    the correct ``detail`` hint for provider payloads.
    """
    try:
        from PIL import Image  # noqa: PLC0415 — optional dependency

        with Image.open(path) as img:
            width, height = img.size
            return int(width), int(height)
    except Exception:
        return None, None


def _resolve_temp_artifact_path(file_id: str) -> Optional[Path]:
    """
    Resolve a ``file_id`` to a physical path inside the temp store.

    Returns ``None`` when the identifier is malformed (contains path
    separators or traversal) or the file does not exist. The returned
    path is guaranteed to be inside ``TEMP_ARTIFACTS_DIR``.
    """
    if not file_id:
        return None
    # Reject any path-like input. file_ids are opaque tokens of the shape
    # ``img_<hex>.<ext>``; anything else is a caller bug.
    if "/" in file_id or "\\" in file_id or ".." in file_id:
        return None
    candidate = (TEMP_ARTIFACTS_DIR / file_id).resolve()
    try:
        candidate.relative_to(TEMP_ARTIFACTS_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def resolve_temp_artifact_to_data_uri(file_id: str) -> Optional[str]:
    """
    Convert a temporary artifact ``file_id`` into a ``data:`` URI
    suitable for the ``image_url.url`` field of a multimodal provider
    payload.

    Returns ``None`` when the artifact does not exist or cannot be read.
    This function performs NO downscaling — the caller (typically the
    ``VisionContextOptimizer`` in ``peldrun.context.builder``) is
    responsible for resizing before embedding. This separation keeps
    the router free of an eager Pillow dependency and lets the context
    layer decide the appropriate maximum dimension for the target
    provider.

    The returned URI is a plain ``data:<mime>;base64,<payload>`` string
    ready to be passed to ``ImageUrlPayload(url=...)``.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = _EXT_TO_MIME.get(ext)
    if mime is None:
        return None
    try:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return None


class FileContentPayload(BaseModel):
    """Payload for POST /api/files/content.

    ``project_id`` is optional. When provided, the target path is
    resolved against the project workspace (external or internal),
    which is required when the caller only knows the workspace-relative
    path and the workspace lives outside ``storage/``. When absent,
    the legacy resolution against ``PROJECT_ROOT`` is used.
    """
    path: str
    content: str
    project_id: Optional[str] = None


def safe_resolve(target_path: str) -> Path:
    target = (PROJECT_ROOT / target_path).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside project root")
    return target


def find_file_in_storage(
    filename_or_rel: str,
    project_id: Optional[str] = None,
) -> Optional[Path]:
    """Search for a deliverable file across the storage hierarchy.

    When ``project_id`` is provided, the project's workspace directory
    (which may be an external path) is searched FIRST. This is
    required for external workspaces whose files live outside
    ``storage/`` and therefore cannot be found by the generic walk
    below. The lookup degrades gracefully when project metadata is
    unavailable: it falls through to the legacy paths.

    The root ``WORKSPACE_DIR`` check is retained as a last-resort
    fallback for legacy chats that predate the project-scoped layout.
    """
    clean = filename_or_rel.lstrip("/\\").replace("\\", "/")

    # 0. Project-scoped workspace (external or internal) — checked
    #    FIRST when project_id is known. External workspace files
    #    live OUTSIDE storage/ and cannot be found by any of the
    #    scans below, so this branch is the only way to serve them.
    if project_id:
        try:
            ws_root = _resolve_project_workspace_root(project_id)
            if ws_root is not None:
                candidate = (ws_root / clean).resolve()
                # Containment guard: a crafted relative path (e.g. "../x")
                # or a symlink must never resolve outside the workspace.
                # Without this, save_file_content step 1a could overwrite
                # files one level up (e.g. the project's metadata JSON),
                # because that step calls this helper directly and does
                # NOT pass through safe_resolve's 403 gate.
                try:
                    candidate.relative_to(ws_root)
                except ValueError:
                    candidate = None
                if candidate is not None and candidate.is_file():
                    return candidate
        except Exception:
            pass

    # 1. Direct workspace check (root, legacy)
    candidate = (WORKSPACE_DIR / clean).resolve()
    if candidate.is_file():
        return candidate

    # 2. Direct storage path check
    candidate = (STORAGE_DIR / clean).resolve()
    if candidate.is_file():
        return candidate

    # 3. Recursive lookup inside chats files directories
    target_name = Path(clean).name
    if CHATS_DIR.exists():
        for chat_dir in CHATS_DIR.iterdir():
            if chat_dir.is_dir():
                files_sub = chat_dir / "files"
                if files_sub.exists():
                    target_match = (files_sub / clean).resolve()
                    if target_match.is_file():
                        return target_match
                    for sub_file in files_sub.rglob(target_name):
                        if sub_file.is_file() and sub_file.stat().st_size > 0:
                            return sub_file

    return None


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in [".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico"]:
        return "image"
    if ext in [".html", ".htm"]:
        return "html"
    if ext in [".md", ".markdown"]:
        return "markdown"
    if ext in [".json", ".csv", ".tsv"]:
        return "data"
    if ext in [".pdf"]:
        return "pdf"
    if ext in [".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".java", ".cpp", ".c", ".rs", ".go", ".sql", ".sh", ".bat", ".ps1"]:
        return "code"
    return "file"


def is_valid_artifact_path(entry: Path, origin: str) -> bool:
    fname = entry.name.lower()
    if fname in SYSTEM_FILES:
        return False
    parts = [p.lower() for p in entry.parts]
    if origin == "workspace":
        return True
    if origin in ["chats", "projects"]:
        return "files" in parts or "shared_files" in parts
    return False


def _walk_artifacts() -> List[Dict[str, Any]]:
    """
    Walk the storage tree once and return every candidate artifact.

    Uses ``os.walk`` with in-place directory pruning so ``.git`` object
    trees, ``node_modules``, virtualenvs and Python caches are never
    entered at all. ``Path.rglob`` cannot prune, so even with a per-entry
    skip check it would still pay the full descent cost — which is
    exactly the cost we are trying to eliminate.

    Hidden-path filtering is intentionally NOT applied here. The walk
    result is what the cache stores; the ``exclude_hidden`` flag is
    applied later on the already-small list, so the same cache serves
    both "clean gallery" and "debugging with internals" callers without
    paying the walk cost twice.
    """
    artifacts: List[Dict[str, Any]] = []
    dirs_to_scan = [
        ("workspace", WORKSPACE_DIR),
        ("chats", CHATS_DIR),
        ("projects", PROJECTS_DIR),
    ]

    for origin, base_path in dirs_to_scan:
        if not base_path.exists():
            continue
        for root, dirs, files in os.walk(base_path):
            # Prune in place: os.walk reads ``dirs`` after we mutate it,
            # so removing an entry prevents the walker from descending
            # into that directory at all.
            dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
            for fname in files:
                entry = Path(root) / fname
                if not is_valid_artifact_path(entry, origin):
                    continue
                try:
                    rel_root = str(entry.relative_to(PROJECT_ROOT)).replace("\\", "/")
                    stat = entry.stat()
                    artifacts.append({
                        "name": entry.name,
                        "path": rel_root,
                        "rel_root": rel_root,
                        "origin": origin,
                        "is_dir": False,
                        "size": stat.st_size,
                        "modified": int(stat.st_mtime),
                        "type": detect_file_type(entry.name),
                    })
                except Exception:
                    continue

    artifacts.sort(key=lambda x: x["modified"], reverse=True)
    return artifacts


@router.get("")
@router.get("/")
async def list_workspace_files(
    exclude_hidden: bool = Query(
        False,
        description=(
            "When true, artifacts whose path contains a dot-prefixed segment "
            "(``.git``, ``.peldrun``, ``.env``, ``.gitignore``, ...) are "
            "omitted from the response. The artifacts showcase enables this "
            "to keep workspace internals out of the user-facing gallery."
        ),
    ),
    refresh: bool = Query(
        False,
        description=(
            "Force a cache rebuild. Used by the Refresh button so the user "
            "sees filesystem changes immediately instead of waiting for the "
            "TTL to expire."
        ),
    ),
):
    global _artifacts_cache, _artifacts_cache_built_at

    now_ts = time.time()
    cache_expired = (now_ts - _artifacts_cache_built_at) > _ARTIFACTS_CACHE_TTL_SECONDS
    if refresh or _artifacts_cache is None or cache_expired:
        _artifacts_cache = _walk_artifacts()
        _artifacts_cache_built_at = now_ts

    # Snapshot under a local name so a concurrent invalidation between here
    # and the return cannot set the variable to None mid-iteration.
    cached = _artifacts_cache

    if exclude_hidden:
        cached = [a for a in cached if not _is_hidden_rel_path(a["path"])]

    return {
        "workspace": str(WORKSPACE_DIR),
        "storage": str(STORAGE_DIR),
        "files": cached,
        "total_count": len(cached),
    }


@router.get("/browse")
async def browse_workspace(
    scope: str = Query(
        "global",
        description="Workspace scope: 'global' | 'chat' | 'project'.",
    ),
    chat_id: Optional[str] = Query(
        None, description="Chat id (required when scope='chat')."
    ),
    project_id: Optional[str] = Query(
        None, description="Project id (required when scope='project')."
    ),
    path: str = Query(
        "", description="Relative path within the scope root. Empty = scope root."
    ),
    query: str = Query(
        "",
        description="Optional case-insensitive filter applied to entry names.",
    ),
):
    """
    List the immediate children (files AND folders) of a directory within
    one of three workspace scopes.

    Why a dedicated endpoint instead of reusing ``list_workspace_files``?
    ---------------------------------------------------------------------
    ``list_workspace_files`` walks the whole storage tree and returns a
    FLAT list of every file. That is the right shape for the artifacts
    gallery, but the wrong shape for a folder-navigation UI: it has no
    notion of "current directory", it discards empty folders, and it
    forces the client to reconstruct the hierarchy (which is fragile
    once paths get deep or when the workspace has thousands of files).

    This endpoint returns exactly ONE level, treats folders as
    first-class entries, and gives the client everything it needs to
    drill down (``current_path``, ``parent_path``, per-entry ``is_dir``).

    Scopes
    ------
    * ``global``  — the shared ``workspace/`` directory at PROJECT_ROOT.
    * ``chat``    — ``storage/chats/<chat_id>/files/``. Requires
      ``chat_id``. Auto-created on first use.
    * ``project`` — the project's workspace root (internal OR external,
      resolved via ``_resolve_project_workspace_root``). Requires
      ``project_id``.

    Security
    --------
    * ``path`` is always treated as relative to the scope root.
    * Absolute paths, traversal (``..``), and symlink escapes are
      rejected with HTTP 403.
    * Hidden entries (leading dot), known system files, and internal
      build/VCS directories are excluded from the listing.
    """
    # ── Resolve the scope root ────────────────────────────────────────
    if scope == "project":
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail="project_id is required when scope='project'",
            )
        root = _resolve_project_workspace_root(project_id)
        if root is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace for project {project_id!r} not found",
            )
    elif scope == "chat":
        if not chat_id:
            raise HTTPException(
                status_code=400,
                detail="chat_id is required when scope='chat'",
            )
        # Reject path-like chat ids (separator / traversal injection).
        if any(c in chat_id for c in ("/", "\\")) or ".." in chat_id:
            raise HTTPException(status_code=400, detail="Invalid chat_id")
        root = (CHATS_DIR / chat_id / "files").resolve()
        # No ``mkdir`` here: browsing must be a pure read. A chat that
        # has never had a file uploaded returns an empty tree via the
        # ``not target.exists()`` branch below, and the chat's files
        # directory is created by ``upload_workspace_files`` on the
        # first real upload instead of being fabricated by navigation.
    elif scope == "global":
        # Global scope browses the ENTIRE project root, not just the
        # ``workspace/`` subdirectory. The composer @ menu is a project
        # file picker: users expect to drill into ``backend/``,
        # ``frontend/``, ``docs/``, ``scripts/``, ``installer/``, and
        # ``workspace/`` side by side, exactly like an IDE file tree.
        #
        # Safety: ``_SKIP_DIR_NAMES`` and the leading-dot filter below
        # keep VCS metadata, virtualenvs, node_modules, and dotfiles out
        # of the listing. Individual file paths are validated again by
        # the mention resolver (``_safe_resolve``) before any content is
        # inlined into the prompt, so browsing wide does not mean the
        # agent automatically reads everything.
        root = PROJECT_ROOT.resolve()
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown scope {scope!r}. Expected 'global', 'chat', "
                f"or 'project'."
            ),
        )

    # ── Resolve target directory, enforcing containment ──────────────
    clean_path = (path or "").replace("\\", "/").strip("/")
    if clean_path:
        segments = clean_path.split("/")
        if any(seg == ".." or seg == "" for seg in segments):
            raise HTTPException(
                status_code=403,
                detail="Path traversal not allowed",
            )

    target = (root / clean_path).resolve() if clean_path else root
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Path outside workspace"
        )

    if not target.exists():
        # A scope root may legitimately be empty on first visit: a
        # chat with no uploads yet, a project whose workspace folder
        # has not been created, or any scope whose root directory the
        # user has not populated. Return an empty tree at the scope
        # root instead of 404 so the composer menu opens cleanly.
        #
        # Sub-path misses still 404 because they indicate a real
        # mistake -- a typo, a stale path from a previous chat, or a
        # folder deleted out from under the user.
        if clean_path == "":
            return {
                "scope": scope,
                "current_path": "",
                "parent_path": None,
                "entries": [],
                "count": 0,
            }
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(
            status_code=400, detail="Path is not a directory"
        )

    # ── Enumerate immediate children ─────────────────────────────────
    lower_query = (query or "").strip().lower()
    entries: List[Dict[str, Any]] = []

    try:
        children = list(target.iterdir())
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list directory: {exc}",
        )

    for child in children:
        name = child.name

        # Skip hidden, system, and internal build/VCS directories.
        # The same filter set is used during the artifacts walk so the
        # browse UI and the gallery never disagree about what exists.
        if name.startswith("."):
            continue
        if name.lower() in SYSTEM_FILES:
            continue
        if name in _SKIP_DIR_NAMES:
            continue

        try:
            is_dir = child.is_dir()
            rel = str(child.relative_to(root)).replace("\\", "/")

            if lower_query and lower_query not in name.lower():
                continue

            entries.append({
                "name": name,
                "path": rel,
                "is_dir": is_dir,
                "type": "folder" if is_dir else detect_file_type(name),
                "size": 0 if is_dir else child.stat().st_size,
            })
        except OSError:
            # A single unreadable child must not fail the whole listing.
            # Permission hiccups on Windows are common (antivirus locks).
            continue

    # Folders first, then files; each group sorted case-insensitively so
    # the menu ordering matches Finder / Explorer conventions.
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    # ── Parent path for "go up" navigation ───────────────────────────
    # An empty string means "the scope root" — the client uses this to
    # render the "Up one level" affordance without a second round-trip.
    parent_path: Optional[str] = None
    if clean_path:
        parent_path = "/".join(clean_path.split("/")[:-1])

    return {
        "scope": scope,
        "current_path": clean_path,
        "parent_path": parent_path,
        "entries": entries,
        "count": len(entries),
    }


# [FIX-5.2] When True, files are also mirrored to WORKSPACE_DIR for
# backward compatibility with tooling that still references it.
# Set to False once the transition to per-chat storage is complete.
MIRROR_UPLOADS_TO_WORKSPACE = False


@router.post("/upload")
async def upload_workspace_files(
    files: List[UploadFile] = File(...),
    chat_id: Optional[str] = Form(None),
    job_id: Optional[str] = Form(None),
):
    """
    Upload and persist user attachments into the agent's per-chat workspace.

    [FIX-5.2] Single source of truth:
        Files are written ONLY into `storage/chats/<chat_id>/files/` (or the
        matching project-scoped path when chat_id belongs to a project).
        This eliminates state divergence between a mirror copy in
        `workspace/` and the canonical copy consumed by the agent.

    Backward compatibility:
        While MIRROR_UPLOADS_TO_WORKSPACE is True, a copy is additionally
        written to `workspace/`. Once all legacy consumers have migrated,
        flip the flag to False to make the migration complete.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided for upload")

    # Per-chat storage is the canonical target. Fall back to legacy workspace
    # only when the client did not provide a chat_id (e.g. ad-hoc uploads).
    if chat_id:
        primary_dir = CHATS_DIR / chat_id / "files"
    else:
        primary_dir = WORKSPACE_DIR

    primary_dir.mkdir(parents=True, exist_ok=True)

    uploaded_records: List[Dict[str, Any]] = []

    for file in files:
        raw_name = file.filename or "uploaded_attachment"
        clean_filename = Path(raw_name).name

        if not clean_filename:
            continue

        try:
            content = await file.read()

            # 1. Canonical write (single source of truth).
            primary_target = primary_dir / clean_filename
            primary_target.write_bytes(content)

            # 2. Optional mirror for legacy consumers during transition.
            if (
                MIRROR_UPLOADS_TO_WORKSPACE
                and chat_id
                and primary_dir != WORKSPACE_DIR
            ):
                mirror_target = WORKSPACE_DIR / clean_filename
                mirror_target.write_bytes(content)

            rel_primary_path = str(
                primary_target.relative_to(PROJECT_ROOT)
            ).replace("\\", "/")

            uploaded_records.append({
                "name": clean_filename,
                "path": rel_primary_path,
                "size": len(content),
                "type": detect_file_type(clean_filename),
            })
        except Exception as upload_err:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save uploaded file '{clean_filename}': {upload_err}"
            )

    _invalidate_artifacts_cache()

    return {
        "status": "success",
        "message": f"Successfully uploaded {len(uploaded_records)} file(s)",
        "files": uploaded_records,
        "total_count": len(uploaded_records),
    }


@router.post("/upload-temp")
async def upload_temp_image(file: UploadFile = File(...)):
    """
    Upload a single image into the temporary artifact store.

    Returns a lightweight reference describing the stored file so the
    frontend composer can include it in the subsequent ``start_run``
    payload as an opaque ``file_id``. The image bytes never travel
    through the run request, which keeps the request payload under
    a few KB regardless of image size.

    Response shape::

        {
            "file_id":     "img_ab12cd34ef56.png",
            "filename":    "Screenshot 2026-10-31.png",
            "mime_type":   "image/png",
            "size_bytes":  245891,
            "width":       1280,   # None if Pillow is unavailable
            "height":      720,    # None if Pillow is unavailable
            "url":         "/api/files/temp/img_ab12cd34ef56.png"
        }
    """
    # Validate MIME type. When the caller's Content-Type is missing or
    # generic, fall back to the extension before rejecting.
    declared_mime = (file.content_type or "").strip().lower()
    if declared_mime == "image/jpg":
        # Some browsers and legacy clients emit this non-standard value.
        declared_mime = "image/jpeg"

    original_name = file.filename or "uploaded_image"
    ext_from_name = (
        original_name.rsplit(".", 1)[-1].lower()
        if "." in original_name
        else ""
    )
    if ext_from_name == "jpeg":
        ext_from_name = "jpg"
    inferred_mime = _EXT_TO_MIME.get(ext_from_name)
    effective_mime = (
        declared_mime
        if declared_mime in ALLOWED_IMAGE_TYPES
        else inferred_mime
    )

    if effective_mime not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid image format. Allowed types: "
                + ", ".join(sorted(ALLOWED_IMAGE_TYPES))
            ),
        )

    file_id = f"img_{uuid.uuid4().hex[:12]}.{_MIME_TO_EXT[effective_mime]}"
    target_path = TEMP_ARTIFACTS_DIR / file_id

    try:
        # Stream chunks to disk to avoid loading the entire payload in RAM.
        with open(target_path, "wb") as out_file:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                out_file.write(chunk)
    except Exception as exc:
        # Best-effort cleanup on partial write.
        try:
            target_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist uploaded image: {exc}",
        )

    try:
        size_bytes = target_path.stat().st_size
    except Exception:
        size_bytes = 0

    width, height = _probe_image_dimensions(target_path)

    return {
        "file_id": file_id,
        "filename": original_name,
        "mime_type": effective_mime,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "url": f"/api/files/temp/{file_id}",
    }


@router.get("/temp/{file_id}")
async def get_temp_artifact(file_id: str):
    """
    Serve a temporary artifact image back to the browser.

    Used by the composer to render inline previews during the brief
    window between upload and the run request (e.g. after a page
    reload that lost the ``URL.createObjectURL`` handle). The returned
    content is served with the correct ``Content-Type`` so the browser
    can render it as an ``<img>`` element directly.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    ext = path.suffix.lower().lstrip(".")
    media_type = _EXT_TO_MIME.get(ext, "application/octet-stream")
    return FileResponse(path=str(path), media_type=media_type)


@router.delete("/temp/{file_id}")
async def delete_temp_artifact(file_id: str):
    """
    Delete a temporary artifact.

    Called by the frontend when the user removes an image from the
    composer before submission, or when an upload is aborted. Idempotent:
    deleting a non-existent artifact returns ``deleted=False`` rather
    than raising.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        return {"status": "not_found", "file_id": file_id, "deleted": False}
    try:
        path.unlink()
        return {"status": "success", "file_id": file_id, "deleted": True}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete artifact {file_id}: {exc}",
        )


@router.post("/temp/cleanup")
async def cleanup_temp_artifacts_endpoint(
    older_than_hours: float = Query(
        TEMP_ARTIFACT_MAX_AGE_HOURS_DEFAULT,
        ge=0.1,
        le=720.0,
        description=(
            "Delete temp artifacts whose last-modified timestamp is "
            "older than this many hours. Default 24. Range 0.1–720 "
            "(6 min – 30 days)."
        ),
    ),
):
    """
    Delete stale temp artifacts from ``storage/artifacts/temp/`` **and**
    stale image files from ``storage/artifacts/checkpoint_cache/``.

    Both directories are swept under the same age threshold. The
    checkpoint cache holds the raw image bytes referenced by
    ``artifact-cache://<hash>`` URLs in persisted checkpoints; once a
    checkpoint is older than the threshold, its images are no longer
    needed for resume and can be reclaimed together with the temp files.

    Triggered externally by Windows Task Scheduler / cron, or manually
    from the settings UI. Also invoked at application startup when the
    lifespan-managed cleaner is wired up (see
    ``omweb.services.temp_artifacts_cleaner``).

    Response shape::

        {
            "status": "ok",
            "scanned": 42,
            "deleted_count": 15,
            "kept_count": 27,
            "deleted_bytes": 15728640,
            "kept_bytes": 31457280,
            "threshold_hours": 24.0,
            "threshold_timestamp": 1793405020.89,
            "deleted_files": ["img_abc.png", "img_def.png", ...],
            "errors": [],
            "directory_exists": true,
        }

    Safe to call repeatedly: only files whose mtime is strictly older
    than the cutoff are removed. Concurrent uploads are protected
    because their mtime refreshes on every chunk write.
    """
    report = cleanup_temp_artifacts(older_than_hours=older_than_hours)
    return {"status": "ok", **report}


@router.get("/download")
async def download_file(path: str = Query(...)):
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        target = find_file_in_storage(path)
        if not target or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")


@router.get("/content/{filepath:path}")
@router.get("/content")
async def get_file_content(
    filepath: Optional[str] = None,
    path: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None, description="Project id for external workspace resolution"),
):
    target_rel = filepath or path
    if not target_rel:
        raise HTTPException(status_code=400, detail="Missing file path")
    # safe_resolve still runs first so traversal attempts are rejected
    # with 403 before any lookup happens.
    target = safe_resolve(target_rel)
    # Project workspace wins over PROJECT_ROOT: a stale legacy copy at
    # PROJECT_ROOT/<path> (written by the pre-project save bug) must not
    # shadow the real workspace file when project_id is known.
    if project_id:
        scoped = find_file_in_storage(target_rel, project_id=project_id)
        if scoped and scoped.is_file():
            target = scoped
    if not target.exists() or not target.is_file():
        fallback = find_file_in_storage(target_rel, project_id=project_id)
        if fallback and fallback.is_file():
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "size": target.stat().st_size, "name": target.name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/content")
async def save_file_content(payload: FileContentPayload):
    """Save or update file content in a workspace.

    Resolution order:

    1. **Existing file in the project workspace** (when ``project_id``
       is provided) — the file is written where it already lives,
       which works for both internal and external workspaces.
    2. **New file in the project workspace** — the file is created
       inside ``<project_workspace>/<path>``. Internal workspaces
       resolve to ``storage/projects/{id}/workspace/...``; external
       workspaces resolve to ``<configured_path>/...``.
    3. **Legacy fallback** — no ``project_id`` supplied, or project
       resolution failed. Falls back to ``PROJECT_ROOT/<path>``,
       matching the pre-project behavior.
    """
    target: Optional[Path] = None

    # ── 1 & 2: Project-scoped resolution (preferred when project_id given) ──
    if payload.project_id:
        # 1a. Try to find an EXISTING file anywhere in the project's
        #     workspace so we rewrite the same physical file.
        existing = find_file_in_storage(payload.path, project_id=payload.project_id)
        if existing is not None:
            target = existing
        else:
            # 1b. New file — resolve the project workspace root.
            try:
                from omweb.project_manager import project_manager as _pm
                _meta = _pm.get_project(payload.project_id) or {}
                _ws_type = (_meta.get("workspace_type") or "internal").lower()
                _ws_cfg = (_meta.get("workspace_path") or "").strip()

                if _ws_type == "external" and _ws_cfg:
                    _ws_root = Path(_ws_cfg).expanduser().resolve()
                else:
                    _ws_root = (
                        Path(_pm.storage_dir) / "projects" / payload.project_id / "workspace"
                    ).resolve()

                # Defend against path traversal for the new-file path.
                _candidate = (_ws_root / payload.path).resolve()
                try:
                    _candidate.relative_to(_ws_root)
                except ValueError:
                    raise HTTPException(
                        status_code=403,
                        detail="Path traversal detected in payload.path",
                    )
                target = _candidate
            except HTTPException:
                raise
            except Exception:
                target = None

    # ── 3: Legacy fallback ──
    if target is None:
        target = safe_resolve(payload.path)
        if not target.parent.exists():
            fallback = find_file_in_storage(payload.path)
            if fallback:
                target = fallback
            else:
                target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.content, encoding="utf-8")

        # Best-effort relative path for the response payload.
        try:
            rel_path = str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            # External workspace files live outside PROJECT_ROOT — return
            # the absolute path as-is; the frontend only uses this for
            # display and doesn't need it to be relative.
            rel_path = str(target)

        _invalidate_artifacts_cache()
        return {
            "status": "success",
            "message": f"Saved {target.name}",
            "path": rel_path,
            "size": target.stat().st_size,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to save file: {str(e)}"
        )


@router.get("/raw/{filepath:path}")
@router.get("/raw")
async def get_raw_file(
    filepath: Optional[str] = None,
    path: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None, description="Project id for external workspace resolution"),
):
    target_rel = filepath or path
    if not target_rel:
        raise HTTPException(status_code=400, detail="Missing file path")
    target = safe_resolve(target_rel)
    if not target.exists() or not target.is_file():
        fallback = find_file_in_storage(target_rel, project_id=project_id)
        if fallback and fallback.is_file():
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File not found")

    ext = target.suffix.lower()
    media_type = "text/plain"
    if ext in [".png"]:
        media_type = "image/png"
    elif ext in [".jpg", ".jpeg"]:
        media_type = "image/jpeg"
    elif ext in [".svg"]:
        media_type = "image/svg+xml"
    elif ext in [".webp"]:
        media_type = "image/webp"
    elif ext in [".gif"]:
        media_type = "image/gif"
    elif ext in [".html", ".htm"]:
        media_type = "text/html"
    elif ext in [".css"]:
        media_type = "text/css"
    elif ext in [".js"]:
        media_type = "application/javascript"
    elif ext in [".json"]:
        media_type = "application/json"
    elif ext in [".pdf"]:
        media_type = "application/pdf"

    return FileResponse(path=str(target), media_type=media_type)


def _resolve_project_workspace_root(project_id: str) -> Optional[Path]:
    """Resolve the workspace root for a project (external or internal).

    Returns ``None`` when project metadata cannot be read, so callers
    can respond with a clean 404 instead of a 500.
    """
    try:
        from omweb.project_manager import project_manager as _pm
        meta = _pm.get_project(project_id) or {}
        ws_type = (meta.get("workspace_type") or "internal").lower()
        ws_cfg = (meta.get("workspace_path") or "").strip()
        if ws_type == "external" and ws_cfg:
            return Path(ws_cfg).expanduser().resolve()
        return (
            Path(_pm.storage_dir) / "projects" / project_id / "workspace"
        ).resolve()
    except Exception:
        return None


@router.get("/raw-project/{project_id}/{filepath:path}")
async def get_project_raw_file(project_id: str, filepath: str):
    """Serve a project workspace file with the project id IN THE PATH.

    Why not ``/raw?project_id=...``? The HTML preview injects a
    ``<base href>`` pointing at the deliverable's directory so relative
    assets (CSS, JS, images, ``url(...)`` inside CSS, ``fetch()`` inside
    scripts) resolve against the workspace. Browsers DROP the base
    URL's query string when resolving relative references, so a
    query-based project scope would be lost on every nested asset.
    Keeping the project id in the path preserves both the scope and
    correct directory semantics for every subresource — including
    multi-page navigation (``<a href="page2.html">``) inside the iframe.
    """
    # Reject path-like project ids (separator / traversal injection).
    if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
        raise HTTPException(status_code=400, detail="Invalid project id")

    ws_root = _resolve_project_workspace_root(project_id)
    if ws_root is None:
        raise HTTPException(status_code=404, detail="Project workspace not found")

    # Normalize the incoming path to a workspace-relative form.
    #
    # Callers reach this route with three different path shapes:
    #   1. "my site/index.html"                          (Workspace API,
    #      artifact events, Files tab clicks)
    #   2. "storage/projects/<id>/workspace/my site/..."  (the /api/files
    #      artifact walk, job listings, and session artifacts hydrated
    #      after a page reload)
    #   3. an absolute path that already lives inside the workspace
    #
    # Shape 2 is what broke the preview after F5: the HTML itself still
    # loads through /api/files/content's legacy safe_resolve fallback,
    # but every relative CSS/JS asset resolves against a <base href>
    # carrying the storage prefix, so a strict join 404s on all of them.
    clean = (filepath or "").replace("\\", "/").lstrip("/")

    # Absolute path pointing inside this workspace -> make it relative.
    try:
        _abs = Path(clean)
        if _abs.is_absolute():
            clean = str(_abs.resolve().relative_to(ws_root)).replace("\\", "/")
    except (ValueError, OSError):
        pass

    # Strip the storage-relative prefixes emitted by the artifact walk.
    for _prefix in (
        f"storage/projects/{project_id}/workspace/",
        f"projects/{project_id}/workspace/",
    ):
        if clean.lower().startswith(_prefix.lower()):
            clean = clean[len(_prefix):]
            break

    def _contained(candidate: Path) -> Optional[Path]:
        """Resolve a candidate and reject anything outside the workspace."""
        resolved = candidate.resolve()
        try:
            resolved.relative_to(ws_root)
        except ValueError:
            return None
        return resolved

    target = _contained(ws_root / clean)
    if target is None:
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside project workspace",
        )

    # Longest-suffix fallback. When the exact join misses (unknown
    # prefix, doubled segment, legacy chat-relative path), find the
    # longest tail of the requested path that exists inside the
    # workspace. Every candidate is containment-checked, so this can
    # never escape the workspace and never follows a symlink out of it.
    if not target.is_file():
        segments = [s for s in clean.split("/") if s]
        for index in range(1, len(segments)):
            candidate = _contained(ws_root / "/".join(segments[index:]))
            if candidate is not None and candidate.is_file():
                target = candidate
                break

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = target.suffix.lower()
    media_type, _ = mimetypes.guess_type(target.name)
    # Pin the web-critical types so previews behave identically across
    # platforms (mimetypes reads the OS registry on Windows).
    if ext in (".html", ".htm"):
        media_type = "text/html"
    elif ext == ".css":
        media_type = "text/css"
    elif ext == ".js":
        media_type = "application/javascript"
    elif ext == ".svg":
        media_type = "image/svg+xml"
    elif ext == ".webp":
        media_type = "image/webp"
    elif ext == ".mjs":
        media_type = "application/javascript"
    elif ext == ".json":
        media_type = "application/json"
    elif ext == ".wasm":
        media_type = "application/wasm"
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
    )


@router.delete("")
@router.delete("/")
async def delete_file(path: str = Query(...)):
    target = safe_resolve(path)
    if not target.exists():
        fallback = find_file_in_storage(path)
        if fallback:
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File or folder not found")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        _invalidate_artifacts_cache()
        return {"status": "success", "message": f"Deleted {target.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
