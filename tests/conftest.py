"""Shared pytest fixtures.

The router tests use MagicMock/AsyncMock for Bot instances since we want
to verify call arguments, not actually hit Telegram. The aiogram message
objects are constructed via the public model since aiogram exposes pydantic
models we can instantiate directly.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.conversation import ConversationStore
from claudebots.core.personas import Persona, PersonaRegistry


@pytest.fixture
def conv() -> ConversationStore:
    return ConversationStore(max_messages_per_chat=40)


@pytest.fixture
def personas() -> PersonaRegistry:
    business = Persona(
        id="business_assistant",
        name="Майя",
        bot_token_env="BUSINESS_BOT_TOKEN",
        system_prompt="biz system",
        max_tokens=400,
        fallback="biz fallback",
        provider="claude",
    )
    speakers = [
        Persona(
            id=name,
            name=name.title(),
            bot_token_env=f"PANEL_BOT_{name.upper()}_TOKEN",
            system_prompt=f"{name} system",
            max_tokens=500,
            fallback="",
            provider="claude",
        )
        for name in ("analyst", "skeptic", "creative", "pragmatist")
    ]
    moderator = Persona(
        id="moderator",
        name="Модератор",
        bot_token_env="PANEL_BOT_MODERATOR_TOKEN",
        system_prompt="mod system",
        max_tokens=800,
        fallback="mod fallback",
        provider="claude",
        is_moderator=True,
    )
    return PersonaRegistry(
        business_assistant=business,
        panel_speakers=speakers,
        moderator=moderator,
    )


def _create_client_mock() -> MagicMock:
    """Create a mock AI client with complete() and stream() methods."""
    m = MagicMock()
    m.complete = AsyncMock(return_value="canned response")

    async def _default_stream(**kwargs):
        for chunk in ("canned ", "stream ", "response"):
            yield chunk

    m.stream = _default_stream
    m.usage = {"input": 0, "output": 0, "cache_read": 0}
    return m


@pytest.fixture
def claude_mock() -> MagicMock:
    """Mock ClaudeClient. `.complete` returns a canned string; `.stream` yields canned deltas."""
    return _create_client_mock()


@pytest.fixture
def ai_registry_mock() -> AIRegistry:
    """Mock AIRegistry that returns the same mock client for all providers."""
    client = _create_client_mock()
    # Create a registry with the mock client for all common providers
    clients = {
        "claude": client,
        "groq": client,
        "openrouter_deepseek": client,
        "openrouter_owl": client,
        "openrouter_gemini": client,
        "gemini": client,
    }
    return AIRegistry(clients)


@pytest.fixture
def bot_mocks() -> dict[str, MagicMock]:
    """6 MagicMock Bot instances with AsyncMock methods."""
    bots = {}
    for name in ("business", "analyst", "skeptic", "creative", "pragmatist", "moderator"):
        b = MagicMock(name=f"bot_{name}")
        b.send_message = AsyncMock()
        b.edit_message_text = AsyncMock()
        b.send_chat_action = AsyncMock()
        b.read_business_message = AsyncMock()
        bots[name] = b
    return bots


@pytest.fixture
def alerts_mock() -> MagicMock:
    m = MagicMock()
    m.send = AsyncMock()
    return m
