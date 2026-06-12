"""Groq fallback client.

Drop-in alternative for ClaudeClient with the same `complete()` and `stream()`
interface. Used when Claude returns RateLimitError or its circuit breaker opens.

Groq exposes an OpenAI-compatible chat-completions API, so messages are sent
as [{"role": "system", ...}, {"role": "user", ...}, ...]. Groq does NOT
support prompt caching, so `usage["cache_read"]` is always 0.

Also provides `transcribe_voice()` — wraps Groq's Whisper endpoint.
"""

import logging
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any

from groq import AsyncGroq

from claudebots.core.ai_registry import Usage

logger = logging.getLogger(__name__)


class GroqClient:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        self._sdk = AsyncGroq(api_key=api_key, max_retries=3)
        self._model = model
        # `cache_read` kept for API symmetry with ClaudeClient; always 0 for Groq.
        self.usage: Usage = {"input": 0, "output": 0, "cache_read": 0}

    @staticmethod
    def _to_groq_messages(system: str, messages: list[Any]) -> list[dict[str, Any]]:
        return [{"role": "system", "content": system}, *messages]

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> str:
        response = await self._sdk.chat.completions.create(
            model=self._model,
            messages=self._to_groq_messages(system, messages),  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )
        usage = response.usage
        if usage is not None:
            self.usage["input"] += usage.prompt_tokens
            self.usage["output"] += usage.completion_tokens
            logger.debug("Groq usage in=%d out=%d", usage.prompt_tokens, usage.completion_tokens)
        choice = response.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            logger.warning("Groq returned empty content. finish_reason=%s", choice.finish_reason)
        return content

    async def transcribe_voice(
        self,
        audio_data: bytes,
        filename: str = "voice.ogg",
        language: str = "ru",
        model: str = "whisper-large-v3-turbo",
    ) -> str:
        """Transcribe audio bytes with Groq Whisper. Returns plain text."""
        bio = BytesIO(audio_data)
        bio.name = filename
        result = await self._sdk.audio.transcriptions.create(
            file=(filename, bio),
            model=model,
            language=language,
            response_format="text",
        )
        return result.strip() if isinstance(result, str) else str(result).strip()

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text deltas."""
        stream = await self._sdk.chat.completions.create(
            model=self._model,
            messages=self._to_groq_messages(system, messages),  # type: ignore[arg-type]
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        # Groq sends usage in the LAST chunk only — guard against double-counting
        # in case the SDK ever changes that behaviour.
        usage_recorded = False
        async for chunk in stream:
            if chunk.usage is not None and not usage_recorded:
                self.usage["input"] += chunk.usage.prompt_tokens
                self.usage["output"] += chunk.usage.completion_tokens
                logger.debug(
                    "Groq stream usage in=%d out=%d",
                    chunk.usage.prompt_tokens,
                    chunk.usage.completion_tokens,
                )
                usage_recorded = True
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
