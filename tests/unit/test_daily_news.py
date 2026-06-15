"""Unit tests for the daily news panel module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.routers.daily_news import _build_news_topic, _fetch_headlines


# ---------------------------------------------------------------------------
# _fetch_headlines
# ---------------------------------------------------------------------------

async def test_fetch_headlines_returns_empty_when_search_disabled():
    sc = MagicMock()
    sc.enabled = False
    result = await _fetch_headlines(
        interests="AI, tech",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    assert result == []


async def test_fetch_headlines_returns_empty_when_search_is_none():
    result = await _fetch_headlines(
        interests="AI, tech",
        timezone_str="Europe/Moscow",
        search_client=None,
    )
    assert result == []


async def test_fetch_headlines_returns_titles_from_search():
    r1, r2 = MagicMock(title="AI news"), MagicMock(title="Tech boom")
    sc = MagicMock()
    sc.enabled = True
    sc.search = AsyncMock(return_value=[r1, r2])

    result = await _fetch_headlines(
        interests="AI, tech",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    assert result == ["AI news", "Tech boom"]


async def test_fetch_headlines_skips_empty_titles():
    r1 = MagicMock(title="Good title")
    r2 = MagicMock(title="")
    sc = MagicMock()
    sc.enabled = True
    sc.search = AsyncMock(return_value=[r1, r2])

    result = await _fetch_headlines(
        interests="AI",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    assert result == ["Good title"]


async def test_fetch_headlines_returns_empty_on_exception():
    sc = MagicMock()
    sc.enabled = True
    sc.search = AsyncMock(side_effect=RuntimeError("network error"))

    result = await _fetch_headlines(
        interests="AI",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    assert result == []


# ---------------------------------------------------------------------------
# _build_news_topic
# ---------------------------------------------------------------------------

async def test_build_news_topic_with_headlines():
    r1, r2 = MagicMock(title="OpenAI launches X"), MagicMock(title="Market surge")
    sc = MagicMock()
    sc.enabled = True
    sc.search = AsyncMock(return_value=[r1, r2])

    topic = await _build_news_topic(
        interests="AI, финансы",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    assert "📰 Новостной обзор" in topic
    assert "OpenAI launches X" in topic
    assert "Market surge" in topic
    assert "AI, финансы" in topic


async def test_build_news_topic_fallback_when_no_search():
    topic = await _build_news_topic(
        interests="AI, бизнес",
        timezone_str="Europe/Moscow",
        search_client=None,
    )
    assert "📰 Новостной обзор" in topic
    assert "AI, бизнес" in topic
    # No bullet-point headlines in fallback
    assert "•" not in topic or "Обсудите" in topic


async def test_build_news_topic_limits_to_5_headlines():
    headlines = [MagicMock(title=f"News {i}") for i in range(10)]
    sc = MagicMock()
    sc.enabled = True
    sc.search = AsyncMock(return_value=headlines)

    topic = await _build_news_topic(
        interests="tech",
        timezone_str="Europe/Moscow",
        search_client=sc,
    )
    # Only headlines 0-4 should appear
    assert "News 4" in topic
    assert "News 5" not in topic
