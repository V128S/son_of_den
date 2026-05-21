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

# Cache for user_id -> topic_id mapping (in-memory, resets on restart)
_contact_topics: dict[int, int] = {}
# Reverse mapping: topic_id -> user_id (to know which contact a topic belongs to)
_topic_contacts: dict[int, int] = {}
# Store contact info: user_id -> {name, messages: [{role, text, time}]}
_contact_data: dict[int, dict] = {}

# Owner's personal topics in panel group: topic_name -> thread_id
_admin_topics: dict[str, int] = {}

# System prompt when the owner (Denis) writes directly in private chat
OWNER_SYSTEM_PROMPT = """\
Ты личный AI-ассистент Дениса — он пишет тебе напрямую как владелец.

ЗАДАЧА:
- Отвечай кратко и по делу, как умный личный помощник.
- Помогай с любыми задачами: вопросы, анализ, идеи, планирование, сводки по клиентам.
- Если Денис спрашивает о клиентах или переписке — давай конкретную информацию.

СТИЛЬ:
- Русский язык, прямая речь без лишних формальностей.
- Обычно 2–5 предложений, если нет запроса на развёрнутый ответ.
- НЕ предлагай "оставить сообщение Денису" — ты УЖЕ разговариваешь с Денисом.
- НЕ веди себя как секретарь-автоответчик для клиентов.
"""


async def _build_system_prompt(
    persona_prompt: str,
    calendar_client: GoogleCalendarClient | None,
    extra_context: str = "",
) -> str:
    """Build system prompt with optional calendar context appended."""
    system_prompt = persona_prompt + extra_context

    calendar_context = ""
    if calendar_client:
        try:
            calendar_context = await calendar_client.get_upcoming_events_summary()
        except Exception as e:
            logger.warning("Failed to fetch calendar summary: %s", e)

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

    return system_prompt


async def _get_or_create_contact_topic(bot: Bot, chat_id: int, user_id: int, user_name: str) -> int | None:
    """Get existing topic for contact or create a new one."""
    if user_id in _contact_topics:
        return _contact_topics[user_id]

    try:
        # Create new topic for this contact
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=f"💬 {user_name[:64]}",  # Telegram limits topic name to 128 chars
        )
        _contact_topics[user_id] = topic.message_thread_id
        _topic_contacts[topic.message_thread_id] = user_id  # Reverse mapping
        logger.info("Created topic %d for user %s (%d)", topic.message_thread_id, user_name, user_id)
        return topic.message_thread_id
    except Exception as e:
        logger.warning("Failed to create topic for %s: %s", user_name, e)
        return None


async def _analyze_admin_topic_and_get_thread(
    bot: Bot,
    chat_id: int,
    question: str,
    ai_registry: AIRegistry,
) -> int | None:
    """Analyze owner's question topic and get or create a forum thread in the panel group."""
    try:
        client = ai_registry.get_client("openrouter_gemini")

        topics_context = ""
        if _admin_topics:
            topics_context = "\n\nСуществующие топики:\n"
            for name in _admin_topics:
                topics_context += f"- {name}\n"

        prompt = (
            f"Вопрос владельца: {question}\n\n"
            f"{topics_context}\n"
            "Задача: определи ОБЩУЮ тему этого вопроса (2-4 слова + эмодзи).\n\n"
            "Правила:\n"
            "- Если тема совпадает с существующим топиком — верни ТОЧНО его название\n"
            "- Если тема новая — придумай короткое название (2-4 слова) + подходящий эмодзи в начале\n"
            "- Делай категории широкими: '📋 Задачи', '💡 Идеи', '📊 Аналитика', '🗓 Планирование'\n"
            "- Формат: 'эмодзи Название'\n\n"
            "Верни ТОЛЬКО название топика, без объяснений."
        )

        topic_name = await client.complete(
            system="Ты помощник для категоризации вопросов. Отвечай кратко, только название топика.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
        )
        topic_name = topic_name.strip().strip('"').strip("'")

        # Check if topic already exists (case-insensitive)
        for name, tid in _admin_topics.items():
            if name.lower() == topic_name.lower():
                logger.info("Using existing admin topic: %s (id=%d)", name, tid)
                return tid

        # Create new topic
        try:
            forum_topic = await bot.create_forum_topic(
                chat_id=chat_id,
                name=topic_name[:128],
            )
            _admin_topics[topic_name] = forum_topic.message_thread_id
            logger.info("Created admin topic: %s (id=%d)", topic_name, forum_topic.message_thread_id)
            return forum_topic.message_thread_id
        except Exception as e:
            logger.warning("Failed to create admin forum topic: %s", e)
            return None

    except Exception as e:
        logger.warning("Admin topic analysis failed: %s", e)
        return None


@business_router.business_message(F.text)
async def _on_business_message(
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    settings,
    bots: dict[str, Bot],
    calendar_client: GoogleCalendarClient | None = None,
) -> None:
    # Use moderator bot for panel group (it has access there)
    panel_bot = bots.get("moderator")

    await handle_business_message(
        message=message,
        bot=bot,
        ai_registry=ai_registry,
        conv=conv,
        personas=personas,
        alerts=alerts,
        admin_user_id=settings.admin_user_id,
        panel_chat_id=settings.panel_chat_id,
        panel_bot=panel_bot,
        calendar_client=calendar_client,
    )


@business_router.message(F.text & F.chat.type.in_({"private", "supergroup"}))
async def _on_private_message(
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    settings,
    bots: dict[str, Bot],
    calendar_client: GoogleCalendarClient | None = None,
) -> None:
    """Handle direct messages to the business bot in private chat or supergroup with topics."""
    # Skip if it's a business connection message (handled by other handler)
    if message.business_connection_id:
        return

    await handle_private_message(
        message=message,
        bot=bot,
        ai_registry=ai_registry,
        conv=conv,
        personas=personas,
        alerts=alerts,
        admin_user_id=settings.admin_user_id,
        panel_chat_id=settings.panel_chat_id,
        panel_bot=bots.get("moderator"),
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
    admin_user_id: int | None = None,
    panel_chat_id: int | None = None,
    panel_bot: Bot | None = None,
    calendar_client: GoogleCalendarClient | None = None,
    edit_throttle_seconds: float = _EDIT_THROTTLE_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    persona = personas.business_assistant
    client = ai_registry.get_client(persona.provider)
    key = f"biz:{message.business_connection_id}:{message.chat.id}"

    text = message.text or ""
    conv.add(key, "user", text)

    # Notify admin in private chat with business bot (with topics support)
    if admin_user_id and message.from_user:
        user = message.from_user
        user_name = user.full_name or user.username or f"ID:{user.id}"

        # Store contact data for admin context
        if user.id not in _contact_data:
            _contact_data[user.id] = {"name": user_name, "messages": []}
        _contact_data[user.id]["messages"].append({
            "role": "contact",
            "text": text[:1000],
            "time": datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M"),
        })
        # Keep only last 20 messages per contact
        _contact_data[user.id]["messages"] = _contact_data[user.id]["messages"][-20:]

        # Get or create topic for this contact in admin's chat with bot
        topic_id = await _get_or_create_contact_topic(bot, admin_user_id, user.id, user_name)

        notify_text = f"📩 {user_name}:\n{text[:500]}"
        try:
            await bot.send_message(
                admin_user_id,
                notify_text,
                message_thread_id=topic_id if topic_id else None,
            )
        except Exception as e:
            logger.debug("Failed to send notification to admin: %s", e)

    system_prompt = await _build_system_prompt(
        persona.system_prompt, calendar_client,
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

    # Store assistant response in contact data
    if message.from_user and message.from_user.id in _contact_data:
        _contact_data[message.from_user.id]["messages"].append({
            "role": "assistant",
            "text": response[:1000],
            "time": datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M"),
        })
        _contact_data[message.from_user.id]["messages"] = _contact_data[message.from_user.id]["messages"][-20:]

    # Send assistant response to admin in private chat (with topics)
    if admin_user_id and message.from_user:
        topic_id = _contact_topics.get(message.from_user.id)
        try:
            await bot.send_message(
                admin_user_id,
                f"🤖 Ответ:\n{response[:500]}",
                message_thread_id=topic_id if topic_id else None,
            )
        except Exception as e:
            logger.debug("Failed to send response to admin: %s", e)

    try:
        await bot.read_business_message(
            business_connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        logger.debug("read_business_message skipped: %s", e)


async def handle_private_message(
    *,
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    admin_user_id: int | None = None,
    panel_chat_id: int | None = None,
    panel_bot: Bot | None = None,
    calendar_client: GoogleCalendarClient | None = None,
    edit_throttle_seconds: float = _EDIT_THROTTLE_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Handle private/supergroup messages with topic support."""
    persona = personas.business_assistant
    client = ai_registry.get_client(persona.provider)

    # Key includes thread_id for topic separation
    thread_id = message.message_thread_id or 0
    key = f"private:{message.chat.id}:{thread_id}"

    text = message.text or ""
    conv.add(key, "user", text)

    # Check if this is admin chatting in a contact's topic
    is_admin = bool(message.from_user and admin_user_id and message.from_user.id == admin_user_id)
    contact_context = ""
    admin_thread_id: int | None = None

    if is_admin and thread_id and thread_id in _topic_contacts:
        contact_id = _topic_contacts[thread_id]
        if contact_id in _contact_data:
            contact = _contact_data[contact_id]
            contact_context = f"\n\n🔴 ВНИМАНИЕ: ТЫ СЕЙЧАС ОБЩАЕШЬСЯ С ДЕНИСОМ (ТВОИМ ВЛАДЕЛЬЦЕМ), А НЕ С КЛИЕНТОМ!\n\n"
            contact_context += f"Контекст: это топик переписки с контактом «{contact['name']}».\n\n"
            contact_context += f"ИСТОРИЯ ПЕРЕПИСКИ С {contact['name'].upper()}:\n"
            for msg in contact["messages"][-10:]:
                role = "Контакт" if msg["role"] == "contact" else "Автоответчик"
                contact_context += f"[{msg['time']}] {role}: {msg['text'][:200]}\n"
            contact_context += (
                f"\n🔴 ВАЖНО:\n"
                f"- Денис спрашивает ТЕБЯ как владелец\n"
                f"- Не говори 'подготовлю для Дениса' - ты УЖЕ разговариваешь с Денисом\n"
                f"- Отвечай кратко и по делу\n"
                f"- Если он просит что-то сделать - просто подтверди что сделано или сделаешь\n"
                f"- Если спрашивает о переписке - дай краткую сводку"
            )

    # Owner private mode: admin writing directly in private (not in a contact topic)
    is_owner_private = (
        is_admin
        and getattr(message.chat, "type", None) == "private"
        and not contact_context
    )

    if is_owner_private:
        # Dedicated owner system prompt — no business-secretary framing
        system_prompt = await _build_system_prompt(OWNER_SYSTEM_PROMPT, calendar_client)

        # Create/find topic in panel group for organizing the conversation
        if panel_chat_id and panel_bot:
            admin_thread_id = await _analyze_admin_topic_and_get_thread(
                bot=panel_bot,
                chat_id=panel_chat_id,
                question=text,
                ai_registry=ai_registry,
            )
    else:
        extra_context = ""
        if is_admin and not contact_context:
            extra_context += (
                f"\n\n🔴 ВНИМАНИЕ: ТЫ ОБЩАЕШЬСЯ С ДЕНИСОМ (ТВОИМ ВЛАДЕЛЬЦЕМ)!\n"
                f"- Отвечай кратко и по делу\n"
                f"- Это не клиент, это твой владелец\n"
                f"- Можешь давать отчеты, сводки, статистику по клиентам"
            )
        extra_context += contact_context
        system_prompt = await _build_system_prompt(
            persona.system_prompt, calendar_client, extra_context,
        )

    try:
        await bot.send_chat_action(
            chat_id=message.chat.id,
            action="typing",
            message_thread_id=message.message_thread_id,
        )
    except Exception as e:
        logger.debug("chat_action skipped: %s", e)

    # Send placeholder with topic support
    try:
        placeholder = await bot.send_message(
            chat_id=message.chat.id,
            text=_PLACEHOLDER,
            message_thread_id=message.message_thread_id,
            parse_mode=None,
        )
    except Exception as e:
        logger.warning("Failed to send placeholder: %s", e)
        await alerts.send("private_placeholder", f"{type(e).__name__}: {e}")
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
                        parse_mode=None,
                    )
                    last_edit_at = now()
                except Exception as e:
                    logger.debug("intermediate edit failed: %s", e)
    except Exception as e:
        logger.warning("Private stream (%s) failed: %s", persona.provider, e)
        await alerts.send("private", f"{type(e).__name__}: {e}")
        streaming_failed = True

    response = persona.fallback if streaming_failed or not buffer else buffer

    # Final edit
    try:
        await bot.edit_message_text(
            chat_id=placeholder.chat.id,
            message_id=placeholder.message_id,
            text=response,
            parse_mode=None,
        )
    except Exception as e:
        logger.warning("Final edit failed: %s", e)

    conv.add(key, "assistant", response)

    # Log owner's private message to the panel topic for organized archiving
    if is_owner_private and admin_thread_id is not None and panel_chat_id and panel_bot:
        try:
            panel_text = (
                f"👤 <b>Денис:</b> {text[:400]}\n\n"
                f"🤖 <b>Ответ:</b>\n{response[:400]}"
            )
            await panel_bot.send_message(
                panel_chat_id,
                panel_text,
                message_thread_id=admin_thread_id,
            )
        except Exception as e:
            logger.debug("Failed to log owner message to panel topic: %s", e)
