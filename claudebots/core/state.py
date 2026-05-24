"""Persistent bot state — atomically saves/loads topic mappings to disk.

All in-memory dicts (panel topics, contact topics, admin categories, panel
memories) are lost on restart.  This module writes them to a single JSON
file so the bot can restore them without creating duplicate forum topics.

Usage pattern (from any router module)::

    from claudebots.core import state as _state

    # On startup (called from __main__):
    data = _state.load(path)
    _panel_topics.update(_state.decode_int_keys(data.get("panel_topics", {})))

    # After modifying state:
    _state.update(path, {"panel_topics": _state.encode_int_keys(_panel_topics)})


Performance notes
-----------------
``update()`` used to call ``load()`` on every invocation, re-reading and
re-parsing the entire JSON file before writing it back.  Many handlers in
this bot call ``update()`` repeatedly in their hot path (e.g. one persist
per forum-topic creation, one per Instagram/YouTube download, one per
panel round).  To keep update() cheap we cache the merged state in memory
keyed by file path.  The cache is primed lazily by the first ``load()``
or ``update()`` call for that path, kept in sync with every subsequent
``save()`` / ``update()``, and dropped on JSON corruption so the next
``load()`` falls back to disk.
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Path -> last-known full state dict.  Treat values as opaque snapshots; we
# always deep-copy on the way in/out so callers can safely mutate.
_cache: dict[Path, dict[str, Any]] = {}


def _read_disk(path: Path) -> dict[str, Any]:
    """Read and parse *path*; return {} on missing/corrupt file."""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logger.warning("State file %s contains non-dict data — ignoring", path)
    except Exception as exc:
        logger.warning("Cannot load state from %s: %s", path, exc)
    return {}


def load(path: Path) -> dict[str, Any]:
    """Load state from *path*.  Returns {} on missing or corrupt file.

    Result is read from the in-memory cache when available so repeated calls
    don't re-parse the same JSON.  Callers receive a deep copy and can
    safely mutate the returned dict.
    """
    cached = _cache.get(path)
    if cached is not None:
        return copy.deepcopy(cached)

    data = _read_disk(path)
    _cache[path] = data
    if data:
        logger.info("State loaded from %s (%d keys)", path, len(data))
    # Return a copy so caller mutations don't bleed into the cache.
    return copy.deepcopy(data)


def save(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* to *path* (write temp file then rename).

    Also updates the in-memory cache so subsequent ``load()`` / ``update()``
    calls don't have to hit disk.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Cannot save state to %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # Snapshot the saved state in the cache (deep copy so external mutations
    # of `data` after save() return won't corrupt our view).
    _cache[path] = copy.deepcopy(data)


def update(path: Path, patch: dict[str, Any]) -> None:
    """Merge *patch* into the current state and persist atomically.

    Safe in asyncio (single-threaded).  The merged state is held in memory
    so we don't pay for a read+parse of the entire file on every call —
    only one serialise + write per invocation.
    """
    current = _cache.get(path)
    if current is None:
        # First touch for this path — prime the cache from disk.
        current = _read_disk(path)
        _cache[path] = current

    current.update(patch)
    save(path, current)


def invalidate_cache(path: Path | None = None) -> None:
    """Drop the in-memory cache for *path* (or all paths if None).

    Mostly useful for tests that rewrite the underlying file directly and
    then expect ``load()`` to see the new contents.
    """
    if path is None:
        _cache.clear()
        return
    _cache.pop(path, None)


# ---------------------------------------------------------------------------
# Encoding helpers — JSON requires string keys
# ---------------------------------------------------------------------------

def encode_int_keys(d: dict[int, Any]) -> dict[str, Any]:
    """Convert {int: v} -> {"int": v} for JSON serialisation."""
    return {str(k): v for k, v in d.items()}


def decode_int_keys(d: dict[str, Any]) -> dict[int, Any]:
    """Convert {"int": v} -> {int: v} after JSON deserialisation."""
    out: dict[int, Any] = {}
    for k, v in d.items():
        try:
            out[int(k)] = v
        except (ValueError, TypeError):
            logger.debug("state: skipping non-int key %r", k)
    return out
