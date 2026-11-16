# backend/omweb/memory/manager/snapshot_builder.py
"""
PELDRUN Memory Platform - Embedding Model Snapshot Builder.

Bridge between the vault + metadata discovery machinery
(``omweb.routers.config_rtr``) and the typed resolver consumed by
the embedding worker and vector search
(``peldrun.memory.embeddings.model_resolver.EmbeddingModelResolver``).

Why this module exists
----------------------

``peldrun`` is a pure runtime library. It MUST NOT read files from
the config/data directories (architectural rule §2.2: "peldrun does
not import omweb"). The resolver
(``peldrun/memory/embeddings/model_resolver.py``) is therefore a
pure in-memory registry that takes a pre-built snapshot at
construction.

Something on the omweb side must produce that snapshot. That
"something" is this module. It is the ONLY place in the codebase
that knows about BOTH:

  * the file-based representation (``models_vault.json`` plus
    ``models_metadata.json``), and
  * the typed resolver snapshot (``ResolvedEmbeddingModel``).

Everything else on the peldrun side works exclusively with the
typed snapshot. Everything else on the omweb side works
exclusively with the raw vault/metadata dicts. This module is the
thin conversion layer.

Notable upstream quirk
----------------------

``omweb.routers.config_rtr._read_models_vault()`` reads from
``STORAGE_ROOT / "models_vault.json"``. On this deployment the
actual vault file lives at ``<backend>/data/models_vault.json``
(the same location ``gateway.py::_resolve_credentials`` reads
from). Rather than touch ``config_rtr`` -- which is chat-adjacent
code that Phase 4 must not modify -- this module performs its own
vault-path resolution, mirroring ``gateway.py`` exactly:

    1. ``PELDRUN_MODELS_VAULT_PATH`` env var (tests)
    2. ``<backend>/data/models_vault.json``
    3. ``<backend>/data/models_vault copy.json`` (Windows
       duplicate-file naming)

The metadata path is taken from ``config_rtr.METADATA_CACHE``,
which IS the correct location (``<storage>/models_metadata.json``)
and does not need to be re-resolved.

Design invariants
-----------------

1. **Read-only.**
   This module never writes to any file. The worker's decision to
   persist probed dimensions back into metadata is a DIFFERENT
   concern (owned by the worker), not this one.

2. **Fail-soft.**
   Missing files, malformed JSON, and unknown providers produce
   an EMPTY snapshot, never an exception. Callers (worker,
   MemoryService) decide what to do with the emptiness -- usually
   "skip embedding entirely", which is the correct behavior
   when no embedding model has been configured.

3. **Deterministic key ordering.**
   The returned dict preserves insertion order from the metadata
   file (which is already the order ``config_rtr`` writes it in).
   The resolver sorts by ``(provider_id, model_id)`` itself, so
   callers do not need to sort here.

4. **No business decisions.**
   This module does not decide WHICH embedding model to use, WHEN
   to reindex, or WHETHER to probe dimensions. It only translates
   the current on-disk state into a typed map. Every policy
   decision belongs to the caller.

5. **Provider identity is a best-effort resolution.**
   A metadata entry may carry ``provider_id`` directly (the
   modern path -- ``config_rtr`` writes it since the capability-
   source migration). When absent (legacy entries), the resolver
   falls back to:

     a. the composite key's ``provider::model`` prefix,
     b. a vault-wide scan of every ``savedModels`` array.

   An entry that cannot be attributed to ANY provider is skipped
   with a debug log -- there is no meaningful credentials lookup
   without a provider id, and embedding without credentials is a
   guaranteed ``EmbeddingUnavailableError``.

See also
--------

    * ``peldrun/memory/embeddings/model_resolver.py``
                                  -- typed resolver (snapshot
                                     consumer)
    * ``omweb/routers/config_rtr.py``
                                  -- metadata source + vault
                                     discovery
    * ``omweb/memory/extraction/gateway.py``
                                  -- analogous vault credential
                                     lookup in the extraction
                                     layer; this module's vault
                                     walking logic mirrors it
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.embeddings.model_resolver import (
    ResolvedEmbeddingModel,
)

# METADATA_CACHE is the only piece of config_rtr this module
# imports. It points at the correct on-disk location
# (``<storage>/models_metadata.json``) and is stable across the
# platform. Everything else -- the vault read, the provider
# resolution, the capability detection -- is re-implemented here,
# both for the upstream-quirk reasons documented above and to
# keep this bridge resilient to future config_rtr refactors.
from omweb.routers.config_rtr import METADATA_CACHE


logger = logging.getLogger("omweb.memory.manager.snapshot_builder")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_embedding_model_snapshot(
    *,
    models_metadata_path: Optional[Path] = None,
    models_vault_path: Optional[Path] = None,
) -> Dict[str, ResolvedEmbeddingModel]:
    """Build a snapshot of every embedding-capable model on disk.

    Parameters
    ----------
    models_metadata_path
        Override for the metadata cache path. ``None`` uses
        ``config_rtr.METADATA_CACHE``.
    models_vault_path
        Override for the vault path. ``None`` uses the same
        resolution order as ``gateway.py``.

    Returns
    -------
    Dict[str, ResolvedEmbeddingModel]
        A mapping from ``model_key`` to the fully-resolved
        embedding model. Empty when:

          * no model has been marked as an embedding model yet,
          * the metadata file is missing or unreadable,
          * the vault is missing (models appear with
            ``base_url=None`` / ``api_key=None`` -- the caller
            decides whether that is acceptable),
          * every entry lacks a resolvable ``provider_id`` (see
            design invariant 5).

        The dict is safe to mutate -- it is a fresh construction
        on every call. ``ResolvedEmbeddingModel`` values are
        frozen, so a caller passing the dict to
        ``EmbeddingModelResolver`` cannot accidentally corrupt
        the values.
    """
    metadata = _read_metadata(models_metadata_path)
    if not metadata:
        logger.debug(
            "build_embedding_model_snapshot: no metadata at %s",
            models_metadata_path or METADATA_CACHE,
        )
        return {}

    vault = _read_vault(models_vault_path)

    snapshot: Dict[str, ResolvedEmbeddingModel] = {}
    for key, entry in metadata.items():
        if not isinstance(entry, dict):
            continue

        is_embedding, source = _effective_is_embedding(entry)
        if not is_embedding:
            continue

        model_id = _extract_model_id(entry, key)
        if not model_id:
            logger.debug(
                "build_embedding_model_snapshot: skipping %r -- "
                "empty model_id",
                key,
            )
            continue

        provider_id = _extract_provider_id(entry, key, vault)
        if not provider_id:
            logger.debug(
                "build_embedding_model_snapshot: skipping %r -- "
                "no provider_id resolvable (legacy entry without "
                "provider_id, non-composite key, and no vault "
                "savedModels match).",
                key,
            )
            continue

        base_url, api_key = _resolve_credentials(provider_id, vault)
        model_version = _extract_model_version(entry, model_id)
        known_dimensions = _extract_known_dimensions(entry)
        extra_headers = _extract_extra_headers(entry)

        snapshot[key] = ResolvedEmbeddingModel(
            model_key=key,
            model_id=model_id,
            provider_id=provider_id,
            base_url=base_url,
            api_key=api_key,
            model_version=model_version,
            known_dimensions=known_dimensions,
            source=source,
            extra_headers=extra_headers,
        )

    logger.debug(
        "build_embedding_model_snapshot: built snapshot of %d "
        "embedding model(s) from %d metadata entr%s.",
        len(snapshot),
        len(metadata),
        "y" if len(metadata) == 1 else "ies",
    )
    return snapshot


# ---------------------------------------------------------------------------
# Metadata + vault reading
# ---------------------------------------------------------------------------


def _read_metadata(
    override: Optional[Path],
) -> Dict[str, Any]:
    """Read the metadata cache; ``{}`` on any failure."""
    path = override if override is not None else METADATA_CACHE
    if not path.is_file():
        logger.debug("Metadata cache not found: %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read metadata cache %s: %s", path, exc
        )
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_vault(override: Optional[Path]) -> Dict[str, Any]:
    """Read the models vault; ``{}`` on any failure.

    Resolution order (matches ``gateway.py``):

      1. explicit override (tests),
      2. ``PELDRUN_MODELS_VAULT_PATH`` env var,
      3. ``<backend>/data/models_vault.json``,
      4. ``<backend>/data/models_vault copy.json``
         (Windows duplicate-file naming).
    """
    path = override if override is not None else _default_vault_path()
    if path is None or not path.is_file():
        logger.debug("Models vault not found: %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read models vault %s: %s", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _default_vault_path() -> Path:
    """Return the default vault path using ``gateway.py``'s order.

    See the module docstring's "Notable upstream quirk" note for
    why this is not delegated to ``config_rtr``.
    """
    env = os.environ.get("PELDRUN_MODELS_VAULT_PATH", "").strip()
    if env:
        return Path(env).expanduser()

    # This file lives at
    #   <backend>/omweb/memory/manager/snapshot_builder.py
    # so parents[3] is <backend>.
    backend = Path(__file__).resolve().parents[3]
    data_dir = backend / "data"
    primary = data_dir / "models_vault.json"
    if primary.is_file():
        return primary
    fallback = data_dir / "models_vault copy.json"
    if fallback.is_file():
        return fallback
    return primary


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------


def _effective_is_embedding(
    entry: Dict[str, Any],
) -> Tuple[bool, str]:
    """Return ``(is_embedding, source)``.

    Resolution order (first hit wins, mirrors
    ``config_rtr.get_embedding_models_endpoint``):

      1. ``capabilities.is_embedding`` (canonical form),
      2. top-level ``is_embedding`` (legacy).

    The returned ``source`` is one of ``"manual"`` /
    ``"provider_live"``; falls back to ``"provider_live"`` when
    the entry carries no explicit source string.
    """
    caps = entry.get("capabilities")
    if isinstance(caps, dict) and "is_embedding" in caps:
        value = caps["is_embedding"] is True
        sources = entry.get("capability_sources")
        src: Optional[str] = None
        if isinstance(sources, dict):
            raw = sources.get("is_embedding")
            if isinstance(raw, str) and raw:
                src = raw
        return (value, src or "provider_live")

    if entry.get("is_embedding") is True:
        return (True, "provider_live")

    return (False, "provider_live")


def _extract_model_id(
    entry: Dict[str, Any], key: str
) -> str:
    """Return the bare model id for ``entry``.

    Priority:
      1. ``entry["key"]`` (canonical form when config_rtr wrote it),
      2. trailing segment of a composite ``provider::model`` key,
      3. the raw key.
    """
    canonical = entry.get("key")
    if isinstance(canonical, str) and canonical:
        return canonical
    if "::" in key:
        return key.rsplit("::", 1)[-1]
    return key


def _extract_provider_id(
    entry: Dict[str, Any],
    key: str,
    vault: Dict[str, Any],
) -> str:
    """Return the provider that owns ``entry``, or "" when unknown.

    Priority (first hit wins):
      1. ``entry["provider_id"]`` (modern metadata),
      2. ``key``'s ``provider::model`` prefix (composite key),
      3. vault-wide scan of every ``savedModels`` array (legacy).

    Empty string when none of the three yields a value; the
    caller skips the entry.
    """
    raw = entry.get("provider_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    if "::" in key:
        prefix = key.split("::", 1)[0].strip()
        if prefix:
            return prefix

    return _scan_vault_for_model(key, vault) or ""


def _resolve_credentials(
    provider_id: str,
    vault: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(base_url, api_key)`` for ``provider_id``.

    Mirrors ``gateway.py::_resolve_credentials`` exactly -- same
    five containers, same priority, same empty-value handling.
    Returns ``(None, None)`` when the provider has no vault entry.
    """
    if not provider_id:
        return (None, None)
    target = provider_id.strip().lower()

    # 1. cloud_vault (array).
    cloud = vault.get("cloud_vault")
    if isinstance(cloud, list):
        for entry in cloud:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id", "")).strip().lower() == target:
                return _extract_creds(entry)

    # 2. lmstudio_vault (single dict).
    if target in ("lmstudio", "lm-studio", "lm_studio"):
        lm = vault.get("lmstudio_vault")
        if isinstance(lm, dict):
            return _extract_creds(lm)

    # 3. ollama_vault (single dict).
    if target == "ollama":
        ol = vault.get("ollama_vault")
        if isinstance(ol, dict):
            return _extract_creds(ol)

    # 4. custom_endpoints (array).
    custom = vault.get("custom_endpoints")
    if isinstance(custom, list):
        for entry in custom:
            if not isinstance(entry, dict):
                continue
            for k in ("id", "name"):
                raw = entry.get(k)
                if (
                    isinstance(raw, str)
                    and raw.strip().lower() == target
                ):
                    return _extract_creds(entry)

    # 5. provider_connections (array, plugin packages).
    conns = vault.get("provider_connections")
    if isinstance(conns, list):
        for conn in conns:
            if not isinstance(conn, dict):
                continue
            pkg = str(
                conn.get("provider_package_id", "")
            ).strip().lower()
            if pkg != target:
                continue
            if not conn.get("is_active", False):
                continue
            fields = conn.get("fields") or {}
            if not isinstance(fields, dict):
                continue
            base = fields.get("base_url")
            key = fields.get("api_key")
            return (
                base
                if isinstance(base, str) and base.strip()
                else None,
                key
                if isinstance(key, str) and key.strip()
                else None,
            )

    return (None, None)


def _extract_creds(
    entry: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Read ``(baseUrl, apiKey)`` from a vault entry.

    Whitespace-only values are treated as absent, matching
    ``gateway.py``'s behavior.
    """
    base = entry.get("baseUrl")
    key = entry.get("apiKey")
    return (
        base if isinstance(base, str) and base.strip() else None,
        key if isinstance(key, str) and key.strip() else None,
    )


def _scan_vault_for_model(
    model_key: str, vault: Dict[str, Any]
) -> Optional[str]:
    """Return the provider whose ``savedModels`` contains ``model_key``.

    Fallback for legacy metadata entries without a ``provider_id``
    and a non-composite key. Probes the same five containers in
    the same priority order as ``_resolve_credentials``.
    """
    # 1. cloud_vault.
    cloud = vault.get("cloud_vault")
    if isinstance(cloud, list):
        for entry in cloud:
            if not isinstance(entry, dict):
                continue
            saved = entry.get("savedModels") or []
            if isinstance(saved, list) and model_key in saved:
                pid = entry.get("id")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()

    # 2. lmstudio_vault.
    lm = vault.get("lmstudio_vault")
    if isinstance(lm, dict):
        saved = lm.get("savedModels") or []
        if isinstance(saved, list) and model_key in saved:
            return "lmstudio"

    # 3. ollama_vault.
    ol = vault.get("ollama_vault")
    if isinstance(ol, dict):
        saved = ol.get("savedModels") or []
        if isinstance(saved, list) and model_key in saved:
            return "ollama"

    # 4. custom_endpoints.
    custom = vault.get("custom_endpoints")
    if isinstance(custom, list):
        for entry in custom:
            if not isinstance(entry, dict):
                continue
            saved = entry.get("savedModels") or []
            if isinstance(saved, list) and model_key in saved:
                pid = entry.get("id") or entry.get("name")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()

    # 5. provider_package_models.
    pkg = vault.get("provider_package_models")
    if isinstance(pkg, dict):
        for pid, entry in pkg.items():
            if not isinstance(entry, dict):
                continue
            saved = entry.get("savedModels") or []
            if isinstance(saved, list) and model_key in saved:
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()

    return None


def _extract_model_version(
    entry: Dict[str, Any], model_id: str
) -> str:
    """Return the version string, defaulting to ``model_id``."""
    raw = entry.get("model_version")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return model_id


def _extract_known_dimensions(
    entry: Dict[str, Any],
) -> Optional[int]:
    """Return ``embedding_dimensions`` when it is a positive int.

    A prior probe may have persisted this value. ``None`` means
    "not yet known -- the worker must probe".
    """
    raw = entry.get("embedding_dimensions")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return None


def _extract_extra_headers(
    entry: Dict[str, Any],
) -> Dict[str, str]:
    """Return per-model HTTP headers, or ``{}``.

    The metadata schema has no canonical header field today --
    this hook exists so future providers (e.g. ones that require
    a custom ``X-Api-Version`` header) can be wired in without a
    code change. Currently returns ``{}`` for every real entry.
    """
    raw = entry.get("extra_headers")
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            cleaned[k] = v
    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["build_embedding_model_snapshot"]
