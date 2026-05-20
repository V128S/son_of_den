"""AI client registry for multi-model architecture.

Maps provider names to AI client instances. Each persona can specify
its own provider, and the registry returns the appropriate client.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, TypedDict

logger = logging.getLogger(__name__)


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

    async def stream(
        self,
        system: str,
        messages: list[Any],
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]: ...


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
