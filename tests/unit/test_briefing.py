"""Unit tests for the morning briefing module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.core.feed_monitor import fetch_channel_entries_raw  # noqa: F401
from claudebots.routers.briefing import _build_briefing


def test_fetch_channel_entries_raw_is_public() -> None:
    """Smoke test: verify fetch_channel_entries_raw is importable as public."""
    # The import at the top of the file already validates this.
    pass


@pytest.fixture()
def ai_registry():
    client = MagicMock()
    client.complete = AsyncMock(return_value="Хороший день — встреча важная, вспомни про стартапы.")
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    registry = MagicMock()
    registry.has_provider = lambda name: name == "groq"
    registry.get_client = lambda name: client
    return registry


async def test_briefing_without_calendar_or_memory(ai_registry):
    text = await _build_briefing(
        timezone_str="Europe/Moscow",
        calendar_client=None,
        ai_registry=ai_registry,
    )
    assert "🌅" in text
    assert "Утренний брифинг" in text


async def test_briefing_includes_calendar_events(ai_registry):
    cal = MagicMock()
    cal.get_upcoming_events_summary = AsyncMock(return_value="• 10:00 - 11:00: Встреча")

    text = await _build_briefing(
        timezone_str="Europe/Moscow",
        calendar_client=cal,
        ai_registry=ai_registry,
    )
    assert "📅" in text
    assert "Встреча" in text


async def test_briefing_includes_panel_memories(ai_registry):
    fake_memories = [
        {"text": "AI уже в прод", "topic": "🔧 Технологии", "ts": 0.0},
        {"text": "Нужен контент", "topic": "📢 Маркетинг", "ts": 0.0},
    ]
    with patch("claudebots.routers.panel._panel_memories", fake_memories):
        text = await _build_briefing(
            timezone_str="Europe/Moscow",
            calendar_client=None,
            ai_registry=ai_registry,
        )
    assert "🧠" in text
    assert "AI уже в прод" in text


async def test_briefing_shows_no_events_when_calendar_empty(ai_registry):
    cal = MagicMock()
    cal.get_upcoming_events_summary = AsyncMock(return_value="")

    text = await _build_briefing(
        timezone_str="Europe/Moscow",
        calendar_client=cal,
        ai_registry=ai_registry,
    )
    assert "Событий на сегодня нет" in text


async def test_briefing_survives_calendar_failure(ai_registry):
    cal = MagicMock()
    cal.get_upcoming_events_summary = AsyncMock(side_effect=RuntimeError("API down"))

    text = await _build_briefing(
        timezone_str="Europe/Moscow",
        calendar_client=cal,
        ai_registry=ai_registry,
    )
    assert "🌅" in text  # still sends something
