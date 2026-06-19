import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class GoogleCalendarClient:
    """Timezone-aware client for fetching upcoming events from Google Calendar.

    Uses a Service Account JSON for authentication. Caches formatted schedule blocks
    for 60 seconds to optimize response time and avoid Google API rate limits.
    """

    def __init__(
        self,
        service_account_file: Path | None,
        calendar_id: str = "primary",
        timezone_str: str = "Europe/Moscow",
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self.service_account_file = service_account_file
        self.calendar_id = calendar_id
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            logger.warning(
                "Invalid timezone '%s', falling back to Europe/Moscow",
                timezone_str,
            )
            self.tz = ZoneInfo("Europe/Moscow")

        self._service = None
        self._cache: str | None = None
        self._cache_time: float | None = None
        self._cache_ttl = cache_ttl_seconds

    def _get_service(self) -> Any:
        """Lazy loader for Google Calendar API service instance."""
        if self._service is not None:
            return self._service

        if not self.service_account_file or not self.service_account_file.exists():
            logger.warning(
                "Service account file '%s' not found or empty. Google Calendar integration is disabled.",
                self.service_account_file,
            )
            return None

        try:
            scopes = ["https://www.googleapis.com/auth/calendar.events"]
            creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(self.service_account_file), scopes=scopes
            )
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            logger.info("Successfully initialized Google Calendar service.")
            return self._service
        except Exception as e:
            logger.error("Failed to initialize Google Calendar API client: %s", e)
            return None

    def _fetch_upcoming_events(self, days: int = 10) -> str:
        """Fetch and format upcoming calendar events (Synchronous blocking call)."""
        service = self._get_service()
        if not service:
            return "Календарь не настроен или отсутствует файл ключа сервисного аккаунта."

        now = datetime.now(self.tz)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        try:
            events_result = (
                service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    timeZone=str(self.tz),
                )
                .execute()
            )
        except Exception as e:
            logger.error("Error fetching events from Google Calendar API: %s", e)
            return "Не удалось получить расписание из Google Calendar."

        events = events_result.get("items", [])
        if not events:
            return "Нет запланированных событий на ближайшие дни."

        # Group events by day
        formatted_days: dict[Any, list[str]] = {}
        for event in events:
            start_raw = event["start"].get("dateTime") or event["start"].get("date")
            end_raw = event["end"].get("dateTime") or event["end"].get("date")
            summary = event.get("summary", "Без названия")
            description = event.get("description", "")
            location = event.get("location", "")

            # Parse start and end times
            # Timed events contain a 'T' (e.g., 2026-05-20T15:00:00+03:00)
            if "T" in start_raw:
                try:
                    start_dt = datetime.fromisoformat(start_raw).astimezone(self.tz)
                    end_dt = datetime.fromisoformat(end_raw).astimezone(self.tz)
                    date_key = start_dt.date()
                    time_str = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
                except Exception as e:
                    logger.warning("Error parsing timed event times (%s): %s", start_raw, e)
                    continue
            else:
                # All-day events (e.g., 2026-05-20)
                try:
                    date_key = datetime.strptime(start_raw, "%Y-%m-%d").date()
                    time_str = "Весь день"
                except Exception as e:
                    logger.warning("Error parsing all-day event date (%s): %s", start_raw, e)
                    continue

            details = f"• {time_str}: {summary}"
            if location:
                details += f" (Место: {location})"
            if description:
                # Clean description, keep it short
                desc_short = description.strip().replace("\n", " ")
                if len(desc_short) > 60:
                    desc_short = desc_short[:57] + "..."
                details += f" — {desc_short}"

            formatted_days.setdefault(date_key, []).append(details)

        # Format final text block
        lines = []
        ru_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        ru_months = [
            "",
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ]

        for d in sorted(formatted_days.keys()):
            day_name = ru_days[d.weekday()]
            month_name = ru_months[d.month]
            lines.append(f"- {day_name}, {d.day} {month_name} {d.year}:")
            for item in formatted_days[d]:
                lines.append(f"  {item}")

        return "\n".join(lines)

    async def get_upcoming_events_summary(self, days: int = 10) -> str:
        """Fetch upcoming events summary using cache and async thread pool execution."""
        # If integration is disabled (no credentials file), skip early
        if not self.service_account_file or not self.service_account_file.exists():
            return ""

        loop = asyncio.get_running_loop()
        now = loop.time()

        # Check cache
        if self._cache is not None and self._cache_time is not None:
            if now - self._cache_time < self._cache_ttl:
                return self._cache

        try:
            # Run the synchronous fetch inside a thread pool with a 3.0s timeout
            summary = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_upcoming_events, days),
                timeout=3.0,
            )
            self._cache = summary
            self._cache_time = now
            return summary
        except TimeoutError:
            logger.warning("Google Calendar fetch timed out. Proceeding without calendar.")
            if self._cache is not None:
                logger.info("Returning stale cache after timeout.")
                return self._cache
            return "Не удалось получить расписание (таймаут запроса к Google API)."
        except Exception as e:
            logger.error("Error in get_upcoming_events_summary: %s", e)
            if self._cache is not None:
                return self._cache
            return "Не удалось получить расписание."

    async def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        location: str = "",
    ) -> str | None:
        """Create a calendar event. Returns the event HTML link on success, None on failure.

        Args:
            summary: Event title.
            start_iso: Start datetime in ISO 8601 with timezone, e.g. "2026-05-24T15:00:00+03:00".
            end_iso: End datetime in ISO 8601 with timezone.
            description: Optional event description.
            location: Optional location string.
        """
        if not self.service_account_file or not self.service_account_file.exists():
            logger.warning("Calendar create_event: service account not configured.")
            return None

        loop = asyncio.get_running_loop()

        def _create() -> str | None:
            svc = self._get_service()
            if not svc:
                return None
            event_body = {
                "summary": summary,
                "description": description,
                "location": location,
                "start": {"dateTime": start_iso},
                "end": {"dateTime": end_iso},
            }
            try:
                created = (
                    svc.events()
                    .insert(calendarId=self.calendar_id, body=event_body)
                    .execute()
                )
                link = created.get("htmlLink")
                logger.info("Calendar event created: %s — %s", summary, link)
                return str(link) if link else None
            except Exception as e:
                logger.warning("Calendar create_event failed: %s", e)
                return None

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _create),
                timeout=10.0,
            )
        except TimeoutError:
            logger.warning("Calendar create_event timed out.")
            return None

