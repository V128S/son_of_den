"""iCloud Calendar client via CalDAV.

Reads upcoming events from Apple iCloud Calendar using CalDAV protocol.
Requires an Apple ID and an App-Specific Password from appleid.apple.com.
Shares the same interface as GoogleCalendarClient (get_upcoming_events_summary).
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


class ICloudCalendarClient:
    """Timezone-aware iCloud Calendar client via CalDAV.

    Caches formatted schedule blocks for 60 seconds to avoid repeated
    round-trips to Apple's servers.
    """

    def __init__(
        self,
        username: str,
        app_password: str,
        calendar_name: str = "",
        timezone_str: str = "Europe/Moscow",
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self.username = username
        self.app_password = app_password
        self.calendar_name = calendar_name
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            logger.warning("Invalid timezone '%s', falling back to Europe/Moscow", timezone_str)
            self.tz = ZoneInfo("Europe/Moscow")
        self._cache: str | None = None
        self._cache_time: float | None = None
        self._cache_ttl = cache_ttl_seconds

    def _fetch_upcoming_events(self, days: int = 1) -> str:
        try:
            import caldav  # noqa: PLC0415
        except ImportError:
            return "Библиотека caldav не установлена."

        try:
            client = caldav.DAVClient(
                url=_ICLOUD_CALDAV_URL,
                username=self.username,
                password=self.app_password,
            )
            principal = client.principal()
            calendars = principal.calendars()
        except Exception as e:
            logger.error("iCloud CalDAV connection failed: %s", e)
            return "Не удалось подключиться к iCloud Calendar."

        if not calendars:
            return "Нет доступных календарей в iCloud."

        if self.calendar_name:
            calendars = [
                c for c in calendars
                if self.calendar_name.lower() in (c.name or "").lower()
            ]
            if not calendars:
                return f"Календарь '{self.calendar_name}' не найден в iCloud."

        now = datetime.now(self.tz)
        end = now + timedelta(days=days)

        all_events: list[tuple] = []
        for calendar in calendars:
            try:
                events = calendar.date_search(start=now, end=end, expand=True)
                for event in events:
                    try:
                        vev = event.vobject_instance.vevent
                        _s = getattr(vev, "summary", None)
                        summary = str(getattr(_s, "value", _s) or "Без названия")
                        dtstart = getattr(vev, "dtstart", None)
                        dtend = getattr(vev, "dtend", None)
                        if dtstart is None:
                            continue
                        start_val = dtstart.value
                        end_val = dtend.value if dtend else None
                        if hasattr(start_val, "hour"):
                            if start_val.tzinfo:
                                start_dt = start_val.astimezone(self.tz)
                            else:
                                start_dt = start_val.replace(tzinfo=self.tz)
                            if end_val and hasattr(end_val, "hour"):
                                if end_val.tzinfo:
                                    end_dt = end_val.astimezone(self.tz)
                                else:
                                    end_dt = end_val.replace(tzinfo=self.tz)
                                time_str = f"{start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
                            else:
                                time_str = start_dt.strftime("%H:%M")
                            date_key = start_dt.date()
                        else:
                            date_key = start_val
                            time_str = "Весь день"
                        all_events.append((date_key, time_str, summary))
                    except Exception as e:
                        logger.debug("Skipping malformed event: %s", e)
            except Exception as e:
                logger.warning("Error fetching events from calendar %s: %s", getattr(calendar, "name", "?"), e)

        if not all_events:
            return "Нет запланированных событий на ближайшие дни."

        by_day: dict = defaultdict(list)
        for date_key, time_str, summary in all_events:
            by_day[date_key].append(f"• {time_str}: {summary}")

        ru_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        ru_months = [
            "", "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ]
        lines = []
        for d in sorted(by_day.keys()):
            day_name = ru_days[d.weekday()]
            month_name = ru_months[d.month]
            lines.append(f"{day_name}, {d.day} {month_name}:")
            lines.extend(by_day[d])
        return "\n".join(lines)

    async def get_upcoming_events_summary(self, days: int = 1) -> str:
        if not self.username or not self.app_password:
            return ""

        loop = asyncio.get_running_loop()
        now = loop.time()

        if self._cache is not None and self._cache_time is not None:
            if now - self._cache_time < self._cache_ttl:
                return self._cache

        try:
            summary = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_upcoming_events, days),
                timeout=15.0,
            )
            self._cache = summary
            self._cache_time = now
            return summary
        except TimeoutError:
            logger.warning("iCloud Calendar fetch timed out.")
            return self._cache or ""
        except Exception as e:
            logger.error("iCloud Calendar error: %s", e)
            return self._cache or ""
