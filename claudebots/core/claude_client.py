import logging
from collections.abc import AsyncIterator
from typing import TypedDict

from anthropic import APIStatusError, APITimeoutError, AsyncAnthropic, RateLimitError
from anthropic.types import MessageParam, TextBlockParam

from claudebots.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from claudebots.core.config import Settings
from claudebots.core.groq_client import GroqClient

logger = logging.getLogger(__name__)


class Usage(TypedDict):
    input: int
    output: int
    cache_read: int


_RETRYABLE = (RateLimitError, APIStatusError, APITimeoutError)


class ClaudeClient:
    def __init__(self, settings: Settings, fallback: GroqClient | None = None) -> None:
        if settings.anthropic_api_key is None:
            raise ValueError("anthropic_api_key is required to create ClaudeClient")
        # Anthropic SDK has built-in retry on 429/5xx via max_retries (exponential backoff).
        # We use it instead of tenacity — fewer moving parts, same effect.
        self._sdk = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=3,
        )
        self._model = settings.claude_model
        self._breaker = CircuitBreaker(threshold=5, window_seconds=60, recovery_seconds=120)
        self._fallback = fallback
        self.usage: Usage = {"input": 0, "output": 0, "cache_read": 0}

    async def complete(
        self,
        system: str,
        messages: list[MessageParam],
        max_tokens: int = 1024,
    ) -> str:
        # If breaker is open, skip Claude entirely and go straight to fallback.
        try:
            self._breaker.check()
        except CircuitBreakerOpen:
            if self._fallback is None:
                raise
            logger.info("Claude breaker open — falling back to Groq")
            return await self._fallback.complete(system, messages, max_tokens)

        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        try:
            response = await self._sdk.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
            )
        except RateLimitError as e:
            self._breaker.record_failure()
            if self._fallback is None:
                raise
            logger.info("Claude rate-limited — falling back to Groq: %s", e)
            return await self._fallback.complete(system, messages, max_tokens)
        except _RETRYABLE as e:
            self._breaker.record_failure()
            logger.warning("Anthropic call failed: %s", e)
            raise

        self._breaker.record_success()

        self.usage["input"] += response.usage.input_tokens
        self.usage["output"] += response.usage.output_tokens
        self.usage["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0

        logger.debug(
            "Anthropic usage in=%d out=%d cache_read=%d",
            response.usage.input_tokens,
            response.usage.output_tokens,
            getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

        text_blocks = [
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "text") == "text"
        ]
        return "".join(text_blocks)

    async def stream(
        self,
        system: str,
        messages: list[MessageParam],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text deltas as Claude generates them. Usage is recorded at completion.

        On Claude rate-limit (before any tokens stream) or if the breaker is already
        open, transparently switches to the Groq fallback if one was provided.
        """
        # If breaker is open, skip Claude entirely.
        try:
            self._breaker.check()
        except CircuitBreakerOpen:
            if self._fallback is None:
                raise
            logger.info("Claude breaker open (stream) — falling back to Groq")
            async for chunk in self._fallback.stream(system, messages, max_tokens):
                yield chunk
            return

        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        yielded_any = False
        try:
            async with self._sdk.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yielded_any = True
                    yield text

                final = await stream.get_final_message()
                self.usage["input"] += final.usage.input_tokens
                self.usage["output"] += final.usage.output_tokens
                cache_read = getattr(final.usage, "cache_read_input_tokens", 0) or 0
                self.usage["cache_read"] += cache_read
        except RateLimitError as e:
            self._breaker.record_failure()
            # Only fall back if we haven't streamed any tokens yet — otherwise the
            # consumer would see a mid-message provider switch, which is worse UX
            # than a clean failure.
            if self._fallback is None or yielded_any:
                raise
            logger.info("Claude stream rate-limited — falling back to Groq: %s", e)
            async for chunk in self._fallback.stream(system, messages, max_tokens):
                yield chunk
            return
        except _RETRYABLE as e:
            self._breaker.record_failure()
            logger.warning("Anthropic stream failed: %s", e)
            raise

        self._breaker.record_success()
