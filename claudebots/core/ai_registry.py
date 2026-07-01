"""AI client registry for multi-model architecture.

Maps provider names to AI client instances. Each persona can specify
its own provider, and the registry returns the appropriate client.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, TypedDict

logger = logging.getLogger(__name__)

# Approximate pricing per 1M tokens (Sonnet 4.6 / mixed-provider best-effort estimate).
# Update these when Anthropic changes pricing.
TOKEN_PRICE_INPUT_PER_M = 3.0
TOKEN_PRICE_OUTPUT_PER_M = 15.0
TOKEN_PRICE_CACHE_READ_PER_M = 0.30


class Usage(TypedDict):
    input: int
    output: int
    cache_read: int


class AIClient(Protocol):
    """Protocol for AI clients with complete() and stream() methods."""

    usage: Usage

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> str: ...

    def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]: ...


class FallbackClient:
    """Wraps two AIClient instances — tries primary, silently falls back to secondary on error.

    Both clients track their own usage independently.  This wrapper exposes the
    *primary* client's usage as its own ``usage`` attribute so that AIRegistry
    accounting is unambiguous (fallback hits show up under the fallback provider's
    own entry, not under the primary).
    """

    def __init__(self, primary: AIClient, fallback: AIClient, name: str = "") -> None:
        self._primary = primary
        self._fallback = fallback
        self._name = name or "fallback"
        # usage mirrors the primary client's live dict — no double-counting
        self.usage: Usage = primary.usage  # shared reference

    async def complete(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> str:
        try:
            return await self._primary.complete(system, messages, max_tokens, **kwargs)
        except Exception as exc:
            logger.warning("[%s] primary failed (%s), falling back: %s", self._name, type(exc).__name__, exc)
            return await self._fallback.complete(system, messages, max_tokens)

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        yielded_any = False
        try:
            async for chunk in self._primary.stream(system, messages, max_tokens):
                yielded_any = True
                yield chunk
        except Exception as exc:
            if yielded_any:
                # Already started streaming — can't transparently switch mid-stream
                logger.warning("[%s] primary stream failed mid-way: %s", self._name, exc)
                raise
            logger.warning("[%s] primary stream failed (%s), falling back: %s", self._name, type(exc).__name__, exc)
            async for chunk in self._fallback.stream(system, messages, max_tokens):
                yield chunk


class AIRegistry:
    """Registry that maps provider names to AI client instances.

    Supported providers:
    - "claude": ClaudeClient (Anthropic)
    - "groq": GroqClient (Llama 3.3 70B)
    - "openrouter_deepseek": OpenRouterClient with DeepSeek model
    - "openrouter_owl": OpenRouterClient with Owl-Alpha model
    - "gemini": GeminiClient (Google)
    """

    def __init__(self, clients: dict[str, AIClient]) -> None:
        self._clients = clients
        # Snapshot of cumulative usage taken at the start of each UTC day.
        # get_daily_usage() subtracts this baseline from the current counters.
        self._day_baseline: dict[str, Usage] = {}
        logger.info("AIRegistry initialized with providers: %s", list(clients.keys()))

    def get_client(self, provider: str) -> AIClient:
        """Get the AI client for a given provider name.

        Args:
            provider: The provider name (e.g., "claude", "groq", "openrouter_deepseek")

        Returns:
            The AI client instance for that provider

        Raises:
            KeyError: If the provider is not registered
        """
        if provider not in self._clients:
            available = list(self._clients.keys())
            raise KeyError(
                f"Unknown provider '{provider}'. Available providers: {available}"
            )
        return self._clients[provider]

    def has_provider(self, provider: str) -> bool:
        """Check if a provider is registered."""
        return provider in self._clients

    @property
    def providers(self) -> list[str]:
        """List of all registered provider names."""
        return list(self._clients.keys())

    def get_total_usage(self) -> Usage:
        """Get combined usage across all clients."""
        total: Usage = {"input": 0, "output": 0, "cache_read": 0}
        for client in self._clients.values():
            total["input"] += client.usage["input"]
            total["output"] += client.usage["output"]
            total["cache_read"] += client.usage["cache_read"]
        return total

    def get_usage_by_provider(self) -> dict[str, Usage]:
        """Get usage breakdown by provider."""
        return {name: client.usage for name, client in self._clients.items()}

    def snapshot_usage(self) -> dict[str, Usage]:
        """Serialise current per-provider usage counters for JSON persistence."""
        result: dict[str, Usage] = {}
        for name, client in self._clients.items():
            u = client.usage
            result[name] = {"input": u["input"], "output": u["output"], "cache_read": u["cache_read"]}
        return result

    def restore_usage(self, snapshot: dict[str, Any]) -> None:
        """Add persisted counters on top of current (in-memory) values.

        Called once at startup to resume cumulative counters across restarts.
        Unknown providers are silently skipped.
        """
        for name, saved in snapshot.items():
            if name not in self._clients:
                continue
            if not isinstance(saved, dict):
                continue
            u = self._clients[name].usage
            u["input"] += int(saved.get("input", 0))
            u["output"] += int(saved.get("output", 0))
            u["cache_read"] += int(saved.get("cache_read", 0))

    def reset_daily_usage(self) -> None:
        """Snapshot current cumulative counters as the new daily baseline.

        Call this once at midnight (UTC) to start a fresh daily window.
        """
        self._day_baseline = {
            name: {"input": c.usage["input"], "output": c.usage["output"], "cache_read": c.usage["cache_read"]}
            for name, c in self._clients.items()
        }
        logger.info("Daily usage counters reset")

    def get_daily_usage_by_provider(self) -> dict[str, Usage]:
        """Return token usage since the last call to reset_daily_usage()."""
        result: dict[str, Usage] = {}
        for name, client in self._clients.items():
            base = self._day_baseline.get(name, {"input": 0, "output": 0, "cache_read": 0})
            result[name] = {
                "input": max(0, client.usage["input"] - base["input"]),
                "output": max(0, client.usage["output"] - base["output"]),
                "cache_read": max(0, client.usage["cache_read"] - base["cache_read"]),
            }
        return result

    def get_daily_total_usage(self) -> Usage:
        """Return combined daily token usage across all providers."""
        total: Usage = {"input": 0, "output": 0, "cache_read": 0}
        for u in self.get_daily_usage_by_provider().values():
            total["input"] += u["input"]
            total["output"] += u["output"]
            total["cache_read"] += u["cache_read"]
        return total
