"""Unit tests for SearchClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.services.search_client import SearchClient, SearchResult


def test_disabled_when_no_api_key():
    client = SearchClient(api_key=None)
    assert not client.enabled


def test_enabled_when_api_key_set():
    client = SearchClient(api_key="test-key")
    assert client.enabled


async def test_search_returns_empty_when_disabled():
    client = SearchClient(api_key=None)
    results = await client.search("AI агенты")
    assert results == []


async def test_search_returns_results():
    client = SearchClient(api_key="fake-key")

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {"title": "AI в 2025", "url": "https://example.com/ai", "text": "Подробности про AI"},
            {"title": "Стартапы", "url": "https://example.com/startups", "text": "О стартапах"},
        ]
    }

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_resp):
        results = await client.search("AI агенты", num_results=2)

    assert len(results) == 2
    assert results[0].title == "AI в 2025"
    assert results[0].url == "https://example.com/ai"
    assert "Подробности" in results[0].snippet


async def test_search_returns_empty_on_failure():
    client = SearchClient(api_key="fake-key")

    with patch.object(client._client, "post", new_callable=AsyncMock, side_effect=Exception("API down")):
        results = await client.search("тест")

    assert results == []


def test_format_results_empty():
    client = SearchClient(api_key=None)
    assert client.format_results([]) == ""


def test_format_results_non_empty():
    client = SearchClient(api_key=None)
    results = [
        SearchResult("AI в 2025", "https://example.com/ai", "Краткое описание статьи"),
        SearchResult("Стартапы растут", "https://example.com/s", ""),
    ]
    text = client.format_results(results)
    assert "🔍" in text
    assert "AI в 2025" in text
    assert "Краткое описание" in text
    assert "Стартапы растут" in text
