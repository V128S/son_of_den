import logging
import time
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.conversation import ConversationStore
from datetime import datetime
from zoneinfo import ZoneInfo

from claudebots.core.calendar_client import GoogleCalendarClient
from claudebots.core.personas import PersonaRegistry

logger = logging.getLogger(__name__)

business_router = Router(name="business")

# Telegram rate-limits edits to ~1/sec on the same message. Going faster risks 429.
_EDIT_THROTTLE_SECONDS = 1.0
# Placeholder text — non-empty (Telegram rejects empty messages) and visually subtle.
_PLACEHOLDER = "…"


@business_router.business_message(F.text)
async def _on_business_message(
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    calendar_client: GoogleCalendarClient | None = None,
) -> None:
    await handle_business_message(
        message=message,
        bot=bot,
        ai_registry=ai_registry,
        conv=conv,
        personas=personas,
        alerts=alerts,
        calendar_client=calendar_client,
    )


async def handle_business_message(
    *,
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    calendar_client: GoogleCalendarClient | None = None,
    edit_throttle_seconds: float = _EDIT_THROTTLE_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    persona = personas.business_assistant
    client = ai_registry.get_client(persona.provider)
    key = f"biz:{message.business_connection_id}:{message.chat.id}"

    text = message.text or ""
    conv.add(key, "user", text)

    # Fetch calendar context if client is provided
    calendar_context = ""
    if calendar_client:
        try:
            calendar_context = await calendar_client.get_upcoming_events_summary()
        except Exception as e:
            logger.warning("Failed to fetch calendar summary: %s", e)

    # Build system prompt dynamically
    system_prompt = persona.system_prompt
    if calendar_context:
        tz = calendar_client.tz
        now_dt = datetime.now(tz)
        ru_days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        day_of_week = ru_days[now_dt.weekday()]
        current_time_str = f"{now_dt.strftime('%d.%m.%Y %H:%M')} ({day_of_week})"

        system_prompt += (
            f"\n\nТЕКУЩЕЕ ВРЕМЯ:\n"
            f"Сейчас на часах у Дениса: {current_time_str}.\n\n"
            f"АКТУАЛЬНОЕ РАСПИСАНИЕ ДЕНИСА НА БЛИЖАЙШИЕ 10 ДНЕЙ:\n"
            f"<schedule>\n{calendar_context}\n</schedule>\n\n"
            f"ПРАВИЛА ИСПОЛЬЗОВАНИЯ РАСПИСАНИЯ:\n"
            f"- Используй эти данные, чтобы отвечать на вопросы о свободном времени Дениса, встречах и планах.\n"
            f"- Отвечай на вопросы вида 'когда мяско' или 'есть ли время на встречу' предельно точно и вежливо, основываясь ТОЛЬКО на этом расписании.\n"
            f"- Если в расписании нет нужного события или занятости на конкретное время/день, аккуратно скажи, что информации об этом у тебя нет, и предложи оставить сообщение для Дениса."
        )

    try:
        await bot.send_chat_action(
            chat_id=message.chat.id,
            action="typing",
            business_connection_id=message.business_connection_id,
        )
    except Exception as e:
        logger.debug("chat_action skipped: %s", e)

    # Send placeholder — we'll edit it as Claude streams. parse_mode=None so partial
    # text (potentially containing unclosed HTML tags) doesn't break edits.
    try:
        placeholder = await bot.send_message(
            chat_id=message.chat.id,
            text=_PLACEHOLDER,
            business_connection_id=message.business_connection_id,
            parse_mode=None,
        )
    except Exception as e:
        logger.warning("Failed to send placeholder: %s", e)
        await alerts.send("business_placeholder", f"{type(e).__name__}: {e}")
        return

    buffer = ""
    last_edit_at = now()
    streaming_failed = False

    try:
        async for delta in client.stream(
            system=system_prompt,
            messages=conv.get(key),
            max_tokens=persona.max_tokens,
        ):
            buffer += delta
            if now() - last_edit_at >= edit_throttle_seconds and buffer:
                try:
                    await bot.edit_message_text(
                        chat_id=placeholder.chat.id,
                        message_id=placeholder.message_id,
                        text=buffer,
                        business_connection_id=message.business_connection_id,
                        parse_mode=None,
                    )
                    last_edit_at = now()
                except Exception as e:
                    logger.debug("intermediate edit failed: %s", e)
    except Exception as e:
        logger.warning("Business stream (%s) failed: %s", persona.provider, e)
        await alerts.send("business", f"{type(e).__name__}: {e}")
        streaming_failed = True

    response = persona.fallback if streaming_failed or not buffer else buffer

    # Final edit — always replace placeholder with the final text (or fallback).
    try:
        await bot.edit_message_text(
            chat_id=placeholder.chat.id,
            message_id=placeholder.message_id,
            text=response,
            business_connection_id=message.business_connection_id,
            parse_mode=None,
        )
    except Exception as e:
        logger.warning("Final edit failed: %s", e)

    conv.add(key, "assistant", response)

    try:
        await bot.read_business_message(
            business_connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        logger.debug("read_business_message skipped: %s", e)
