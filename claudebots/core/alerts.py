import logging
import time
from collections.abc import Callable

from aiogram import Bot

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_MSG = 4096


class AlertSender:
    def __init__(
        self,
        bot: Bot,
        admin_user_id: int,
        throttle_seconds: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._bot = bot
        self._admin_user_id = admin_user_id
        self._throttle = throttle_seconds
        self._now = now or time.monotonic
        self._last_sent: dict[str, float] = {}

    def _evict_expired(self, now: float) -> None:
        """Remove _last_sent entries that are past their throttle window.

        Called at the start of every send() so the dict stays bounded to the
        number of *distinct keys active within one throttle window*.
        """
        expired = [k for k, ts in self._last_sent.items() if now - ts >= self._throttle]
        for k in expired:
            del self._last_sent[k]

    async def send(self, key: str, text: str) -> None:
        now = self._now()
        self._evict_expired(now)
        last = self._last_sent.get(key)
        if last is not None and now - last < self._throttle:
            logger.debug("Alert throttled: key=%s", key)
            return
        self._last_sent[key] = now
        msg = f"⚠️ {key}: {text}"
        # Telegram hard-caps messages at 4096 characters
        if len(msg) > _MAX_TELEGRAM_MSG:
            msg = msg[: _MAX_TELEGRAM_MSG - 3] + "..."
        try:
            await self._bot.send_message(chat_id=self._admin_user_id, text=msg)
        except Exception as e:
            logger.warning("Failed to send admin alert key=%s: %s", key, e)
