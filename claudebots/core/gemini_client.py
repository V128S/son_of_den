"""Google Gemini client.

Drop-in alternative for ClaudeClient with the same `complete()` and `stream()`
interface. Used as an alternative moderator provider.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from claudebots.core.ai_registry import Usage

logger = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        # `cache_read` is kept for API symmetry; always 0 for Gemini.
        self.usage: Usage = {"input": 0, "output": 0, "cache_read": 0}

    @staticmethod
    def _to_gemini_messages(messages: list[Any]) -> list[types.Content]:
        """Convert OpenAI-style messages to Gemini format."""
        result: list[types.Content] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # Gemini uses "user" and "model" roles
            gemini_role = "model" if role == "assistant" else "user"
            result.append(types.Content(role=gemini_role, parts=[types.Part(text=content)]))
        return result

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )

        gemini_messages = self._to_gemini_messages(messages)

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=gemini_messages,
            config=config,
        )

        # Record usage if available
        if response.usage_metadata:
            self.usage["input"] += response.usage_metadata.prompt_token_count or 0
            self.usage["output"] += response.usage_metadata.candidates_token_count or 0
            logger.debug(
                "Gemini usage in=%d out=%d",
                response.usage_metadata.prompt_token_count or 0,
                response.usage_metadata.candidates_token_count or 0,
            )

        return response.text or ""

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text deltas as Gemini generates them."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )

        gemini_messages = self._to_gemini_messages(messages)

        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=gemini_messages,
            config=config,
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text

            # Record usage from final chunk if available
            if chunk.usage_metadata:
                if chunk.usage_metadata.prompt_token_count:
                    self.usage["input"] += chunk.usage_metadata.prompt_token_count
                if chunk.usage_metadata.candidates_token_count:
                    self.usage["output"] += chunk.usage_metadata.candidates_token_count
                logger.debug(
                    "Gemini stream usage in=%d out=%d",
                    chunk.usage_metadata.prompt_token_count or 0,
                    chunk.usage_metadata.candidates_token_count or 0,
                )
