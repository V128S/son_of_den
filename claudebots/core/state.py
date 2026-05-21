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
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load(path: Path) -> dict[str, Any]:
    """Load state from *path*.  Returns {} on missing or corrupt file."""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                logger.info("State loaded from %s (%d keys)", path, len(data))
                return data
            logger.warning("State file %s contains non-dict data — ignoring", path)
    except Exception as exc:
        logger.warning("Cannot load state from %s: %s", path, exc)
    return {}


def save(path: Path, data: dict[str, Any]) -> None:
    """Atomically write data to path (write temp file then rename)."""
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


def update(path: Path, patch: dict[str, Any]) -> None:
    """Load current state, apply patch, write back atomically.

    Safe in asyncio (single-threaded) — file I/O is fast for small JSON.
    """
    current = load(path)
    current.update(patch)
    save(path, current)


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
