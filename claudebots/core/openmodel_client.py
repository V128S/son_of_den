"""OpenModel client — free deepseek-v4-flash via the Anthropic Messages API.

OpenModel (https://openmodel.ai) serves DeepSeek through the Anthropic Messages
protocol, so we drive it with the official ``anthropic`` SDK pointed at a custom
``base_url``.  Satisfies the AIClient protocol (``complete`` + ``stream`` +
``usage``) and is a drop-in replacement for the OpenRouter panel clients.

Two quirks are handled here:
- DeepSeek's *thinking* mode emits a separate ``thinking`` content block; we keep
  only ``text`` blocks, so chain-of-thought never leaks into the chat.
- The Anthropic Messages API requires strict ``user``/``assistant`` alternation,
  but the panel appends several ``assistant`` turns in a row (one per speaker).
  ``_normalize_messages`` collapses them before each call.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from claudebots.core.ai_registry import Usage

logger = logging.getLogger(__name__)

_OPENMODEL_BASE_URL = "https://api.openmodel.ai"


def _normalize_messages(messages: list[Any]) -> list[MessageParam]:
    """Coerce panel-style history into strict user/assistant alternation.

    - Drops leading ``assistant`` turns (a conversation must start with ``user``).
    - Merges consecutive same-role turns into one (the panel appends several
      ``assistant`` messages in a row — one per speaker).
    Always returns at least one message so the API call never fails on an empty
    list.
    """
    norm: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        if not norm and role != "user":
            continue  # skip assistant turns before the first user turn
        if norm and norm[-1]["role"] == role:
            norm[-1]["content"] = f"{norm[-1]['content']}\n\n{content}".strip()
        else:
            norm.append({"role": role, "content": content})
    if not norm:
        norm = [{"role": "user", "content": "."}]
    return cast(list[MessageParam], norm)


class OpenModelClient:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-flash",
        base_url: str = _OPENMODEL_BASE_URL,
    ) -> None:
        # The Anthropic SDK retries 429/5xx with exponential backoff on its own.
        self._sdk = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=2)
        self._model = model
        self.usage: Usage = {"input": 0, "output": 0, "cache_read": 0}

    def _record_usage(self, usage: Any) -> None:
        if usage is None:
            return
        self.usage["input"] += getattr(usage, "input_tokens", 0) or 0
        self.usage["output"] += getattr(usage, "output_tokens", 0) or 0
        self.usage["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> str:
        response = await self._sdk.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=_normalize_messages(messages),
        )
        self._record_usage(getattr(response, "usage", None))
        text_blocks = [
            getattr(b, "text", "")
            for b in response.content
            if getattr(b, "type", "text") == "text"
        ]
        return "".join(text_blocks).strip()

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text deltas only — thinking-block deltas are not surfaced."""
        async with self._sdk.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=_normalize_messages(messages),
        ) as stream:
            async for text in stream.text_stream:
                yield text
            try:
                final = await stream.get_final_message()
                self._record_usage(getattr(final, "usage", None))
            except Exception as exc:  # usage accounting must never break streaming
                logger.debug("OpenModel final-message usage unavailable: %s", exc)

    async def close(self) -> None:
        try:
            await self._sdk.close()
        except Exception:
            pass
