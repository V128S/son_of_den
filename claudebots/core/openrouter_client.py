"""OpenRouter client for DeepSeek and Owl-Alpha models.

Drop-in alternative with the same `complete()` and `stream()` interface.
OpenRouter exposes an OpenAI-compatible chat-completions API.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from claudebots.core.ai_registry import Usage

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek/deepseek-chat-v3-0324:free",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=_OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        self.usage: Usage = {"input": 0, "output": 0, "cache_read": 0}

    @staticmethod
    def _to_openai_messages(system: str, messages: list[Any]) -> list[dict[str, Any]]:
        return [{"role": "system", "content": system}, *messages]

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        usage = data.get("usage")
        if usage:
            self.usage["input"] += usage.get("prompt_tokens", 0)
            self.usage["output"] += usage.get("completion_tokens", 0)
            logger.debug(
                "OpenRouter usage in=%d out=%d",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "") or ""
        return ""

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text deltas. Usage is recorded from the final chunk if available."""
        payload = {
            "model": self._model,
            "messages": self._to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self._client.stream(
            "POST", "/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract delta content
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content

                # Check for usage in final chunk
                usage = chunk.get("usage")
                if usage:
                    self.usage["input"] += usage.get("prompt_tokens", 0)
                    self.usage["output"] += usage.get("completion_tokens", 0)
                    logger.debug(
                        "OpenRouter stream usage in=%d out=%d",
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                    )

    async def close(self) -> None:
        await self._client.aclose()
