import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from claudebots.core.calendar_client import GoogleCalendarClient


@pytest.fixture
def mock_credentials():
    with patch("google.oauth2.service_account.Credentials.from_service_account_file") as mock:
        mock.return_value = MagicMock()
        yield mock


@pytest.fixture
def mock_build():
    with patch("claudebots.core.calendar_client.build") as mock:
        mock_service = MagicMock()
        mock.return_value = mock_service
        yield mock_service


@pytest.fixture
def calendar_client(tmp_path):
    sa_path = tmp_path / "google_credentials.json"
    sa_path.write_text('{"type": "service_account"}')
    return GoogleCalendarClient(
        service_account_file=sa_path,
        calendar_id="primary",
        timezone_str="Europe/Moscow",
        cache_ttl_seconds=1.0,  # short cache for testing
    )


def test_calendar_client_disabled_when_file_missing():
    # If the file path is not set, client is gracefully disabled
    client = GoogleCalendarClient(service_account_file=None)
    assert client._get_service() is None

    # Asynchronous method returns empty string early
    async def run():
        return await client.get_upcoming_events_summary()

    assert asyncio.run(run()) == ""


def test_calendar_client_disabled_when_file_not_exists():
    client = GoogleCalendarClient(service_account_file=Path("nonexistent.json"))
    assert client._get_service() is None

    async def run():
        return await client.get_upcoming_events_summary()

    assert asyncio.run(run()) == ""


def test_get_service_initialization(calendar_client, mock_credentials, mock_build):
    service = calendar_client._get_service()
    assert service == mock_build
    mock_credentials.assert_called_once_with(
        str(calendar_client.service_account_file),
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )


def test_fetch_upcoming_events_formatting(calendar_client, mock_credentials, mock_build):
    calendar_client._service = mock_build

    # Prepare mock calendar events
    mock_events = [
        {
            "start": {"dateTime": "2026-05-20T10:00:00+03:00"},
            "end": {"dateTime": "2026-05-20T11:30:00+03:00"},
            "summary": "Утренняя летучка",
            "description": "Обсуждаем задачи на сегодня",
            "location": "Zoom",
        },
        {
            "start": {"dateTime": "2026-05-20T19:00:00+03:00"},
            "end": {"dateTime": "2026-05-20T21:00:00+03:00"},
            "summary": "Мяско с друзьями",
        },
        {
            "start": {"date": "2026-05-21"},
            "end": {"date": "2026-05-22"},
            "summary": "Весь день на даче",
            "description": "Отдыхаем без интернета",
        },
    ]

    mock_list_call = mock_build.events.return_value.list
    mock_list_call.return_value.execute.return_value = {"items": mock_events}

    res = calendar_client._fetch_upcoming_events(days=10)

    # Verify API arguments
    assert mock_list_call.call_count == 1
    _, kwargs = mock_list_call.call_args
    assert kwargs["calendarId"] == "primary"
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"
    assert kwargs["timeZone"] == "Europe/Moscow"

    # Verify formatting output
    assert "- Ср, 20 мая 2026:" in res
    assert "• 10:00 - 11:30: Утренняя летучка (Место: Zoom) — Обсуждаем задачи на сегодня" in res
    assert "• 19:00 - 21:00: Мяско с друзьями" in res
    assert "- Чт, 21 мая 2026:" in res
    assert "• Весь день: Весь день на даче — Отдыхаем без интернета" in res


def test_fetch_upcoming_events_no_events(calendar_client, mock_credentials, mock_build):
    calendar_client._service = mock_build
    mock_build.events.return_value.list.return_value.execute.return_value = {"items": []}

    res = calendar_client._fetch_upcoming_events(days=10)
    assert res == "Нет запланированных событий на ближайшие дни."


@pytest.mark.asyncio
async def test_get_upcoming_events_summary_caching(calendar_client, mock_credentials, mock_build):
    calendar_client._service = mock_build
    mock_list = mock_build.events.return_value.list
    mock_list.return_value.execute.side_effect = [
        {"items": [{"start": {"date": "2026-05-20"}, "end": {"date": "2026-05-21"}, "summary": "Event 1"}]},
        {"items": [{"start": {"date": "2026-05-20"}, "end": {"date": "2026-05-21"}, "summary": "Event 2"}]},
    ]

    # First fetch - uncached
    res1 = await calendar_client.get_upcoming_events_summary(days=10)
    assert "Event 1" in res1
    assert mock_list.return_value.execute.call_count == 1

    # Second fetch - within TTL (should hit cache)
    res2 = await calendar_client.get_upcoming_events_summary(days=10)
    assert "Event 1" in res2
    assert mock_list.return_value.execute.call_count == 1  # count remains 1

    # Sleep past cache TTL
    await asyncio.sleep(1.1)

    # Third fetch - cache expired, triggers fresh call
    res3 = await calendar_client.get_upcoming_events_summary(days=10)
    assert "Event 2" in res3
    assert mock_list.return_value.execute.call_count == 2


@pytest.mark.asyncio
async def test_get_upcoming_events_summary_timeout_fallback(calendar_client, mock_credentials, mock_build):
    calendar_client._service = mock_build

    # Simulate a slow API call that exceeds 3s timeout
    async def slow_execute():
        await asyncio.sleep(5.0)
        return {"items": []}

    # We mock execute to block for a long time
    mock_execute = mock_build.events.return_value.list.return_value.execute
    
    def mock_execute_sync():
        import time
        time.sleep(5.0)
        return {"items": []}
    
    mock_execute.side_effect = mock_execute_sync

    # Since it blocks/timeouts, it should return timeout error Russian message
    res = await calendar_client.get_upcoming_events_summary(days=10)
    assert "таймаут запроса к Google API" in res
