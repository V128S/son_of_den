"""Morning briefing scheduler.

Sends a daily AI-generated briefing to the admin at a configurable time.
The briefing combines:
- Today's Google Calendar events
- Recent panel memory takeaways
- A short AI summary tying it all together
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


async def _build_briefing(
    *,
    timezone_str: str,
    calendar_client,  # GoogleCalendarClient | None
    ai_registry,  # AIRegistry
) -> str:
    """Build the morning briefing text. Returned string is ready to send."""
    tz = ZoneInfo(timezone_str)
    now = datetime.now(tz)

    ru_weekdays = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    ru_months = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    date_str = f"{ru_weekdays[now.weekday()]}, {now.day} {ru_months[now.month]}"

    parts: list[str] = [f"🌅 Утренний брифинг — {date_str}\n"]

    # ── Calendar events ────────────────────────────────────────────────────────
    calendar_block = ""
    if calendar_client is not None:
        try:
            today_events = await calendar_client.get_upcoming_events_summary(days=1)
            if today_events and today_events not in ("", "Нет запланированных событий на ближайшие дни."):
                calendar_block = today_events
                parts.append("📅 Сегодня:\n" + today_events + "\n")
            else:
                parts.append("📅 Событий на сегодня нет.\n")
        except Exception as e:
            logger.warning("Briefing: calendar fetch failed: %s", e)

    # ── Panel memories ─────────────────────────────────────────────────────────
    try:
        from claudebots.routers.panel import _panel_memories  # noqa: PLC0415
        recent_memories = _panel_memories[-5:] if _panel_memories else []
    except Exception:
        recent_memories = []

    memory_block = ""
    if recent_memories:
        parts.append("🧠 Последние выводы панели:")
        lines = []
        for mem in recent_memories:
            if isinstance(mem, dict):
                label = f"[{mem['topic']}] " if mem.get("topic") else ""
                lines.append(f"• {label}{mem['text']}")
            elif isinstance(mem, str):
                lines.append(f"• {mem}")
        memory_block = "\n".join(lines)
        parts.append(memory_block + "\n")

    # ── AI summary ─────────────────────────────────────────────────────────────
    ai_summary = ""
    try:
        # Use the cheapest available provider for summarization
        for provider_name in ("openrouter_gemini", "groq", "claude"):
            if ai_registry.has_provider(provider_name):
                client = ai_registry.get_client(provider_name)
                context_parts = []
                if calendar_block:
                    context_parts.append(f"События дня:\n{calendar_block}")
                if memory_block:
                    context_parts.append(f"Выводы прошлых обсуждений:\n{memory_block}")

                if not context_parts:
                    break

                prompt = (
                    "\n\n".join(context_parts) + "\n\n"
                    "Напиши краткий (2-3 предложения) утренний брифинг-прогноз на день. "
                    "Что важно не забыть, что может быть актуально из прошлых обсуждений. "
                    "Живым языком, без markdown."
                )
                ai_summary = await client.complete(
                    system="Ты личный ассистент, помогаешь начать день продуктивно.",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                )
                ai_summary = ai_summary.strip()
                break
    except Exception as e:
        logger.warning("Briefing: AI summary failed: %s", e)

    if ai_summary:
        parts.append("💬 " + ai_summary)

    return "\n".join(parts)


async def _briefing_loop(
    *,
    bot,  # Bot
    admin_user_id: int,
    timezone_str: str,
    briefing_time: str,
    calendar_client,
    ai_registry,
) -> None:
    """Send morning briefing at a fixed local time each day."""
    from claudebots.core.scheduling import daily_at

    tz = ZoneInfo(timezone_str)
    # Wall-clock polling so a briefing missed during macOS sleep fires on wake.
    async for _ in daily_at(briefing_time, tz, label="Briefing scheduler", log=logger):
        try:
            text = await _build_briefing(
                timezone_str=timezone_str,
                calendar_client=calendar_client,
                ai_registry=ai_registry,
            )
            await bot.send_message(admin_user_id, text, parse_mode=None)
            logger.info("Morning briefing sent to admin")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Briefing send failed: %s", e)


def start_briefing_scheduler(
    *,
    bot,  # Bot
    admin_user_id: int,
    timezone_str: str,
    briefing_time: str,
    calendar_client,
    ai_registry,
) -> "asyncio.Task[None]":
    """Create and return the morning briefing background task."""
    task: asyncio.Task[None] = asyncio.create_task(
        _briefing_loop(
            bot=bot,
            admin_user_id=admin_user_id,
            timezone_str=timezone_str,
            briefing_time=briefing_time,
            calendar_client=calendar_client,
            ai_registry=ai_registry,
        )
    )
    task.add_done_callback(
        lambda t: logger.warning("Briefing loop raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info("Morning briefing scheduler started (time=%s %s)", briefing_time, timezone_str)
    return task
