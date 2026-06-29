"""Unit tests for the morning briefing module v2."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_fetch_channel_entries_raw_is_public() -> None:
    from claudebots.core.feed_monitor import fetch_channel_entries_raw  # noqa: F401


@pytest.fixture()
def ai_registry_mock():
    client = MagicMock()
    client.complete = AsyncMock(return_value="Текст новостного блока от AI.")
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    registry = MagicMock()
    registry.has_provider = lambda name: name == "groq"
    registry.get_client = lambda name: client
    return registry


@pytest.fixture()
def channel_entries():
    """Fake channel entries: (url, title, text, timestamp)."""
    return [("https://t.me/ch/1", "Заголовок", "Текст поста о политике", 0.0)]


async def test_build_briefing_messages_returns_two_messages(ai_registry_mock, channel_entries):
    from claudebots.routers.briefing import _build_briefing_messages

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=channel_entries),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["testchannel"],
            calendar_client=None,
            ai_registry=ai_registry_mock,
            search_client=None,
        )

    assert len(messages) == 2
    assert "Политика" in messages[0]
    assert "Технологии" in messages[1]


async def test_build_briefing_messages_includes_calendar_in_first(ai_registry_mock, channel_entries):
    from claudebots.routers.briefing import _build_briefing_messages

    cal = MagicMock()
    cal.get_upcoming_events_summary = AsyncMock(return_value="• 10:00 Встреча с клиентом")
    cal.tz = None

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=channel_entries),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["testchannel"],
            calendar_client=cal,
            ai_registry=ai_registry_mock,
            search_client=None,
        )

    assert "📅" in messages[0]
    assert "Встреча" in messages[0]
    # Calendar must NOT appear in second message
    assert "Встреча" not in messages[1]


async def test_build_briefing_messages_empty_posts_returns_empty(ai_registry_mock):
    from claudebots.routers.briefing import _build_briefing_messages

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=[]),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["emptychannel"],
            calendar_client=None,
            ai_registry=ai_registry_mock,
            search_client=None,
        )

    # No content → no messages
    assert messages == []


async def test_build_briefing_messages_with_exa(ai_registry_mock, channel_entries):
    from claudebots.routers.briefing import _build_briefing_messages

    search_client = MagicMock()
    search_client.enabled = True
    search_client.search = AsyncMock(return_value=[MagicMock(title="Exa result", url="https://ex.com", snippet="Exa snippet")])
    search_client.format_results = MagicMock(return_value="Exa formatted block")

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=channel_entries),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["testchannel"],
            calendar_client=None,
            ai_registry=ai_registry_mock,
            search_client=search_client,
        )

    # search was called twice — once for politics, once for tech
    assert search_client.search.await_count == 2
    assert len(messages) == 2


async def test_build_briefing_messages_truncated_to_3900(ai_registry_mock, channel_entries):
    from claudebots.routers.briefing import _build_briefing_messages

    long_ai_response = "А" * 5000
    ai_registry_mock.get_client("groq").complete = AsyncMock(return_value=long_ai_response)

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=channel_entries),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["testchannel"],
            calendar_client=None,
            ai_registry=ai_registry_mock,
            search_client=None,
        )

    for msg in messages:
        assert len(msg) <= 3900


async def test_build_briefing_messages_survives_ai_failure(ai_registry_mock, channel_entries):
    from claudebots.routers.briefing import _build_briefing_messages

    ai_registry_mock.get_client("groq").complete = AsyncMock(side_effect=RuntimeError("AI down"))

    with patch(
        "claudebots.routers.briefing.fetch_channel_entries_raw",
        new=AsyncMock(return_value=channel_entries),
    ):
        messages = await _build_briefing_messages(
            timezone_str="Europe/Kyiv",
            channels=["testchannel"],
            calendar_client=None,
            ai_registry=ai_registry_mock,
            search_client=None,
        )

    # AI failure → empty messages list (graceful)
    assert isinstance(messages, list)


async def test_build_briefing_messages_no_channels_arg_returns_empty(ai_registry_mock):
    from claudebots.routers.briefing import _build_briefing_messages

    messages = await _build_briefing_messages(
        timezone_str="Europe/Kyiv",
        channels=[],
        calendar_client=None,
        ai_registry=ai_registry_mock,
        search_client=None,
    )
    assert messages == []
