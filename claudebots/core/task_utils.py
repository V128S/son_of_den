"""Asyncio task helpers shared across routers and background workers."""
import asyncio
import logging
from collections.abc import Callable


def task_error_callback(name: str, log: logging.Logger) -> Callable[[asyncio.Task], None]:
    """Return a done-callback that logs unhandled task exceptions."""
    def _cb(t: asyncio.Task) -> None:
        if not t.cancelled() and (exc := t.exception()):
            log.warning("%s raised: %s", name, exc)
    return _cb
