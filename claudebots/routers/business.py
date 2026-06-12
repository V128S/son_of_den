import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.conversation import ConversationStore
from datetime import datetime
from zoneinfo import ZoneInfo

from claudebots.core.calendar_client import GoogleCalendarClient
from claudebots.core.personas import PersonaRegistry
from claudebots.core import state as _state
from pathlib import Path
from claudebots.core.obsidian_client import ObsidianClient
from claudebots.core.sheets_client import GoogleSheetsClient, extract_sheet_id
from claudebots.core.meters_client import MetersClient, looks_like_meter_message, extract_meter_readings
from claudebots.services.insta_downloader import InstagramDownloader, detect_url as _detect_insta_url
from claudebots.services.social_downloader import SocialDownloader, detect_platform as _detect_social_platform
from claudebots.services.yt_downloader import YTDownloader, detect_url as _detect_yt_url, detect_summary_cmd as _detect_yt_summary_cmd, AudioFile as _YTAudioFile

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

# Today's message count per contact (reset after daily digest)
_contact_today: dict[int, int] = {}

# Per-user creation locks — prevent duplicate topics from burst messages
_create_topic_locks: dict[int, asyncio.Lock] = {}

# Hard cap on the number of contacts stored in memory.
# When exceeded, the oldest (first-inserted) contact is evicted.
_MAX_CONTACTS: int = 500

# Path to the bot state JSON file — set by init_business_state() at startup
_biz_state_path: Path | None = None

# Owner's personal topics in panel group: topic_name -> thread_id
_admin_topics: dict[str, int] = {}

# chat_id of the business supergroup (set when owner writes from there)
# Used to route Instagram/media to forum topics even when link is sent via DM
_admin_supergroup_id: int | None = None

def _persist_business_state() -> None:
    """Save contact and admin topic state to disk. No-op if path not set."""
    if _biz_state_path is None:
        return
    _state.update(_biz_state_path, {
        "contact_topics": _state.encode_int_keys(_contact_topics),
        "admin_topics": _admin_topics,
        "admin_supergroup_id": _admin_supergroup_id,
    })


def init_business_state(path: Path, data: dict) -> None:
    """Restore contact/admin topic state from persisted data. Call once at startup."""
    global _biz_state_path
    _biz_state_path = path

    raw_contacts = data.get("contact_topics", {})
    restored_contacts = _state.decode_int_keys(raw_contacts)
    _contact_topics.update(restored_contacts)
    # Rebuild reverse mapping
    _topic_contacts.update({v: k for k, v in restored_contacts.items()})

    raw_admin = data.get("admin_topics", {})
    if isinstance(raw_admin, dict):
        _admin_topics.update(raw_admin)

    global _admin_supergroup_id
    _admin_supergroup_id = data.get("admin_supergroup_id") or None

    logger.info(
        "Business state restored: %d contacts, %d admin topics",
        len(_contact_topics), len(_admin_topics),
    )


# System prompt when the owner (Denis) writes directly in private chat
OWNER_SYSTEM_PROMPT = """\
Ты личный AI-ассистент Дениса — он пишет тебе напрямую как владелец.

ЗАДАЧА:
- Отвечай кратко и по делу, как умный личный помощник.
- Помогай с любыми задачами: вопросы, анализ, идеи, планирование, сводки по клиентам.
- Если Денис спрашивает о клиентах или переписке — давай конкретную информацию.

СЕРВИСЫ И РЕГИОН:
- Денис находится в Украине. Никогда не рекомендуй российские сервисы: Яндекс, Яндекс.Музыка, ВКонтакте, Одноклассники, Mail.ru, 2ГИС и любые другие российские платформы.
- Музыка: Spotify, YouTube Music, Apple Music.
- Поиск: Google, DuckDuckGo.
- Карты: Google Maps, Apple Maps.
- Не добавляй московский/российский контекст к городам или сервисам.

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
    """Get existing topic for contact or create a new one.

    Uses a per-user asyncio.Lock to prevent duplicate topics when burst
    messages from the same contact arrive before the first topic is saved.
    """
    # Fast path — no lock needed if already cached
    if user_id in _contact_topics:
        return _contact_topics[user_id]

    # Ensure exactly one coroutine creates the topic for this user_id
    if user_id not in _create_topic_locks:
        _create_topic_locks[user_id] = asyncio.Lock()
    async with _create_topic_locks[user_id]:
        # Re-check inside the lock — another coroutine may have created it
        if user_id in _contact_topics:
            return _contact_topics[user_id]

        try:
            topic = await bot.create_forum_topic(
                chat_id=chat_id,
                name=f"💬 {user_name[:64]}",  # Telegram limits topic name to 128 chars
            )
            _contact_topics[user_id] = topic.message_thread_id
            _topic_contacts[topic.message_thread_id] = user_id  # Reverse mapping
            logger.info("Created topic %d for user %s (%d)", topic.message_thread_id, user_name, user_id)
            _persist_business_state()
            return topic.message_thread_id
        except Exception as e:
            logger.warning("Failed to create topic for %s: %s", user_name, e)
            return None


# Fixed set of owner topic categories — keeps the panel organised without
# relying on the LLM to invent sensible names.
_OWNER_CATEGORIES = [
    "📋 Задачи",
    "💡 Идеи",
    "📊 Аналитика",
    "🗓 Планирование",
    "👥 Клиенты",
    "💰 Финансы",
    "📢 Маркетинг",
    "🔧 Технологии",
    "📝 Разное",
]


async def _classify_owner_category(question: str, ai_registry: AIRegistry) -> str:
    """Classify owner's message into one of _OWNER_CATEGORIES. Returns category name."""
    try:
        client = ai_registry.get_client("openrouter_gemini")
        cats = "\n".join(f"- {c}" for c in _OWNER_CATEGORIES)
        raw = await client.complete(
            system="Классификатор. Возвращай только одну строку из предложенного списка без изменений.",
            messages=[{"role": "user", "content": (
                f"Выбери ОДНУ категорию из списка для сообщения.\n"
                f"Список:\n{cats}\n\n"
                f"Сообщение: {question[:150]}\n\n"
                "Ответь СТРОГО одной строкой из списка, слово в слово."
            )}],
            max_tokens=12,
        )
        candidate = raw.strip().strip('"').strip("'").split("\n")[0].strip()
        if not candidate:
            logger.warning("Empty response from category classifier")
            return "📝 Разное"
        for cat in _OWNER_CATEGORIES:
            if cat in candidate or candidate in cat:
                logger.info("Owner category: %r (raw=%r)", cat, candidate)
                return cat
        logger.info("Owner category defaulted to Разное (raw=%r)", candidate)
        return "📝 Разное"
    except Exception as e:
        logger.warning("Owner category classification failed: %s", e)
        return "📝 Разное"


async def _route_owner_to_category(
    bot: Bot,
    chat_id: int,
    current_thread_id: int | None,
    category: str,
) -> int | None:
    """
    Ensure the owner's message ends up in the right category topic.

    Strategy:
    - If the category topic already exists → route there, close the question-text topic.
    - If not → rename the current (question-text) topic to the category name.
    - If no current topic → create a new one with the category name.

    Returns the thread_id where the response should be sent.
    """
    existing_tid = _admin_topics.get(category)

    if existing_tid is not None:
        # Category topic already exists
        if current_thread_id and current_thread_id != existing_tid:
            # Close the auto-created question-text topic so it doesn't clutter the forum
            try:
                await bot.close_forum_topic(chat_id=chat_id, message_thread_id=current_thread_id)
                logger.info("Closed question-text topic %d → routing to %r (%d)",
                            current_thread_id, category, existing_tid)
            except Exception as e:
                logger.debug("close_forum_topic failed (ok): %s", e)
        return existing_tid

    # Category topic does not exist yet
    if current_thread_id:
        # Rename the auto-created question-text topic to the category name
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=current_thread_id,
                name=category,
            )
            _admin_topics[category] = current_thread_id
            logger.info("Renamed topic %d → %r", current_thread_id, category)
            _persist_business_state()
            return current_thread_id
        except Exception as e:
            logger.warning("edit_forum_topic failed: %s", e)
            # Fall through to create a new topic

    # Create a brand-new category topic (main chat or rename failed)
    try:
        forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=category)
        _admin_topics[category] = forum_topic.message_thread_id
        logger.info("Created admin topic: %s (id=%d)", category, forum_topic.message_thread_id)
        _persist_business_state()
        return forum_topic.message_thread_id
    except Exception as e:
        logger.warning("create_forum_topic failed: %s", e)
        return current_thread_id  # last resort: respond in current thread


# Keep old name as alias for backwards compat (used in _on_panel_command import path)
async def _analyze_admin_topic_and_get_thread(
    bot: Bot,
    chat_id: int,
    question: str,
    ai_registry: AIRegistry,
) -> int | None:
    category = await _classify_owner_category(question, ai_registry)
    return await _route_owner_to_category(bot, chat_id, None, category)


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
    calendar_client: "GoogleCalendarClient | None" = None,
    obsidian_client: "ObsidianClient | None" = None,
    sheets_client: "GoogleSheetsClient | None" = None,
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
        obsidian_client=obsidian_client,
        sheets_client=sheets_client,
    )



def _is_panel_cmd(text: str | None) -> bool:
    """Return True only for /panel … or панель: … commands."""
    if not text:
        return False
    t = text.strip().lower()
    return t.startswith("/panel") or t.startswith("панель:")


@business_router.message(
    F.text.func(_is_panel_cmd),
    F.chat.type == "private",
)
async def _on_panel_command(
    message: Message,
    bot: Bot,
    bots: dict[str, Bot],
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    settings,
    calendar_client: "GoogleCalendarClient | None" = None,
) -> None:
    """Handle /panel <topic> command from owner in private — trigger panel discussion."""
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    text = (message.text or "").strip()

    # Extract topic
    if text.lower().startswith("/panel"):
        topic = text[6:].strip()
    else:
        topic = text[len("панель:"):].strip()

    if not topic:
        await message.reply(
            "Укажи тему для обсуждения:\n"
            "/panel Что делать с маркетингом?\n"
            "или\n"
            "панель: Как масштабировать продажи?"
        )
        return

    await message.reply(f"🎬 Запускаю обсуждение: {topic[:80]}")

    from claudebots.routers import panel as _panel  # lazy to avoid circular at module load

    moderator_bot = bots.get("moderator")
    if not moderator_bot:
        await message.reply("❌ Модератор не настроен")
        return

    thread_id = await _panel._analyze_topic_and_get_thread(
        bot=moderator_bot,
        chat_id=settings.panel_chat_id,
        question=topic,
        ai_registry=ai_registry,
    )
    logger.info("Panel via /panel command: thread_id=%s topic=%r", thread_id, topic)

    runner = _panel.PanelRoundRunner(
        bots=bots,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts,
        panel_chat_id=settings.panel_chat_id,
        thread_id=thread_id,
    )
    import asyncio as _asyncio
    _task = _asyncio.create_task(runner.run_round(topic))
    _task.add_done_callback(
        lambda t: logger.warning("Panel round task raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )

@business_router.message(F.text & F.chat.type.in_({"private", "supergroup"}) & ~F.forward_from_chat & ~F.forward_origin)
async def _on_private_message(
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    settings,
    bots: dict[str, Bot],
    calendar_client: "GoogleCalendarClient | None" = None,
    meters_client: "MetersClient | None" = None,
    insta_downloader: "InstagramDownloader | None" = None,
    yt_downloader: "YTDownloader | None" = None,
    social_downloader: "SocialDownloader | None" = None,
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
        meters_client=meters_client,
        insta_downloader=insta_downloader,
        yt_downloader=yt_downloader,
        social_downloader=social_downloader,
    )


@business_router.message(F.voice & F.chat.type.in_({"private", "supergroup"}))
async def _on_voice_message(
    message: Message,
    bot: Bot,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    personas: PersonaRegistry,
    alerts: AlertSender,
    settings,
    bots: dict[str, Bot],
    calendar_client: "GoogleCalendarClient | None" = None,
    meters_client: "MetersClient | None" = None,
    insta_downloader: "InstagramDownloader | None" = None,
    yt_downloader: "YTDownloader | None" = None,
    social_downloader: "SocialDownloader | None" = None,
) -> None:
    """Transcribe owner's voice message via Groq Whisper and route as text."""
    if message.business_connection_id:
        return
    if not message.from_user or message.from_user.id != settings.admin_user_id:
        return

    # Require Groq client for transcription
    try:
        from claudebots.core.groq_client import GroqClient
        groq_client = ai_registry.get_client("groq")
        if not isinstance(groq_client, GroqClient):
            raise KeyError("not GroqClient")
    except (KeyError, Exception):
        await message.answer("⚠️ Голосовые сообщения недоступны: Groq не настроен (GROQ_API_KEY не задан).")
        return

    try:
        await bot.send_chat_action(
            chat_id=message.chat.id, action="typing",
            message_thread_id=message.message_thread_id,
        )
    except Exception:
        pass

    # Download voice file (Telegram sends OGG/Opus — supported by Whisper)
    from io import BytesIO
    try:
        voice_file = await bot.get_file(message.voice.file_id)
        bio = BytesIO()
        await bot.download_file(voice_file.file_path, bio)
        audio_bytes = bio.getvalue()
    except Exception as e:
        logger.warning("Voice download failed: %s", e)
        await message.answer("⚠️ Не удалось загрузить голосовое сообщение.")
        return

    # Transcribe
    try:
        text = await groq_client.transcribe_voice(audio_bytes, filename="voice.ogg", language="ru")
    except Exception as e:
        logger.warning("Groq transcription failed: %s", e)
        await message.answer(f"⚠️ Транскрипция не удалась: {e}")
        return

    if not text:
        await message.answer("⚠️ Не удалось распознать речь.")
        return

    # Show transcription as a small confirmation, then route as normal text
    try:
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"🎙 _{text}_",
            message_thread_id=message.message_thread_id,
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # Patch the message object with synthesised text so handle_private_message
    # processes it exactly like a typed message
    message.text = text  # type: ignore[assignment]
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
        meters_client=meters_client,
        insta_downloader=insta_downloader,
        yt_downloader=yt_downloader,
        social_downloader=social_downloader,
    )


async def _extract_calendar_event(
    text: str,
    ai_registry: "AIRegistry",
    tz: "ZoneInfo",
) -> dict | None:
    """Use AI to extract a calendar event from contact message text.

    Returns a dict with keys: summary, start_iso, end_iso, description, location
    or None if no schedulable event was detected.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import json as _json

    now_str = datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S%z")
    tz_name = str(tz)

    prompt = (
        f"Текущее время: {now_str} ({tz_name})\n"
        f"Сообщение от контакта: {text[:600]}\n\n"
        "Если в сообщении упоминается конкретная дата/время встречи/созвона/звонка — "
        "верни JSON с полями: found (true), summary (название), start_iso (ISO 8601 со временем зоны), "
        "end_iso (start + 1 час по умолчанию), description (строка или null), location (строка или null).\n"
        "Если конкретной даты нет — верни JSON: {\"found\": false}."
    )
    try:
        client = ai_registry.get_client("openrouter_gemini")
        from claudebots.core.openrouter_client import OpenRouterClient as _ORC
        json_mode = isinstance(client, _ORC)
        raw = await client.complete(
            system="Ты извлекаешь данные о встречах из сообщений. Всегда отвечай валидным JSON.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            **({"json_mode": True} if json_mode else {}),
        )
        raw = raw.strip()
        # Strip markdown code fences if present (fallback for providers without json_mode)
        raw = raw.strip("` \n")
        if raw.startswith("json"):
            raw = raw[4:]
        data = _json.loads(raw)
        if not data.get("found"):
            return None
        # Validate required fields
        if not data.get("summary") or not data.get("start_iso"):
            return None
        # Default end = start + 1 hour if missing
        if not data.get("end_iso"):
            from datetime import datetime as _dt
            try:
                start = _dt.fromisoformat(data["start_iso"])
                data["end_iso"] = (start + timedelta(hours=1)).isoformat()
            except Exception:
                data["end_iso"] = data["start_iso"]
        return {
            "summary": data.get("summary", "Встреча"),
            "start_iso": data["start_iso"],
            "end_iso": data["end_iso"],
            "description": data.get("description") or "",
            "location": data.get("location") or "",
        }
    except Exception as e:
        logger.debug("_extract_calendar_event: %s", e)
        return None


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
    obsidian_client: ObsidianClient | None = None,
    sheets_client: GoogleSheetsClient | None = None,
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

        # Store contact data for admin context — evict oldest if at capacity
        if user.id not in _contact_data:
            if len(_contact_data) >= _MAX_CONTACTS:
                oldest_key = next(iter(_contact_data))
                del _contact_data[oldest_key]
            _contact_data[user.id] = {"name": user_name, "messages": []}
        _contact_data[user.id]["messages"].append({
            "role": "contact",
            "text": text[:1000],
            "time": datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M"),
        })
        # Keep only last 20 messages per contact
        _contact_data[user.id]["messages"] = _contact_data[user.id]["messages"][-20:]

        # Track daily contact activity for digest
        _contact_today[user.id] = _contact_today.get(user.id, 0) + 1

        # Log to Obsidian vault
        if obsidian_client is not None:
            try:
                obsidian_client.log_message(
                    contact_name=user_name,
                    contact_id=user.id,
                    message_text=text,
                    role="contact",
                )
            except Exception as _obs_err:
                logger.debug("Obsidian log failed: %s", _obs_err)

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

    _TG_MAX = 4096
    buffer = ""
    last_edit_at = now()
    streaming_failed = False

    try:
        async for delta in client.stream(
            system=system_prompt,
            messages=conv.get(key),
            max_tokens=persona.max_tokens,
        ):
            remaining = _TG_MAX - len(buffer)
            if remaining <= 0:
                break  # already at telegram limit
            buffer += delta[:remaining]
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

    # ── Sheets: detect price-sheet URL in contact's message ──────────────
    if sheets_client is not None and message.from_user:
        _sheet_id = extract_sheet_id(text)
        if _sheet_id:
            user_name_s = (message.from_user.full_name or message.from_user.username
                           or f"ID:{message.from_user.id}")
            try:
                _rows_r, _rows_w = await sheets_client.transfer_prices(_sheet_id)
                _sheets_reply = (
                    f"✅ Перенёс {_rows_r} позиций из прайса в личную таблицу "
                    f"(с наценкой {sheets_client.markup_percent:.0f}%)."
                    if _rows_r else "⚠️ Не удалось прочитать таблицу — проверь доступ."
                )
                try:
                    await bot.send_message(
                        admin_user_id,
                        f"📊 Прайс от {user_name_s}: {_sheets_reply}",
                        message_thread_id=_contact_topics.get(message.from_user.id),
                    )
                except Exception as _e:
                    logger.debug("Sheets admin notify failed: %s", _e)
                if obsidian_client is not None:
                    _src_url = f"https://docs.google.com/spreadsheets/d/{_sheet_id}"
                    obsidian_client.log_sheets_transfer(
                        contact_name=user_name_s,
                        rows_read=_rows_r,
                        rows_written=_rows_w,
                        source_url=_src_url,
                    )
            except Exception as _e:
                logger.warning("Sheets transfer failed: %s", _e)

    # ── Calendar: detect meeting time in contact message ─────────────────
    if calendar_client is not None and message.from_user and text.strip():
        import re as _re
        _time_hints = _re.search(
            r"\b(встреч|zoom|колл|call|созвон|перезвон|завтра|в \d{1,2}[:.:]|\d{1,2}:\d{2}|"
            r"понедельник|вторник|среда|четверг|пятниц|суббот|воскресень|"
            r"январ|феврал|март|апрел|май|июн|июл|август|сентябр|октябр|ноябр|декабр)\b",
            text, _re.IGNORECASE,
        )
        if _time_hints:
            try:
                _cal_info = await _extract_calendar_event(text, ai_registry, calendar_client.tz)
                if _cal_info:
                    _ev_link = await calendar_client.create_event(**_cal_info)
                    _ev_title = _cal_info.get("summary", "Встреча")
                    _user_name_c = (message.from_user.full_name or message.from_user.username
                                    or f"ID:{message.from_user.id}")
                    _note = (
                        f"📅 Событие «{_ev_title}» создано в календаре."
                        + (f" {_ev_link}" if _ev_link else "")
                    )
                    try:
                        await bot.send_message(
                            admin_user_id,
                            f"🗓 {_user_name_c}: {_note}",
                            message_thread_id=_contact_topics.get(message.from_user.id),
                        )
                    except Exception as _e:
                        logger.debug("Calendar admin notify failed: %s", _e)
                    if obsidian_client is not None:
                        obsidian_client.log_calendar_event(
                            contact_name=_user_name_c,
                            event_summary=_ev_title,
                            event_link=_ev_link,
                        )
            except Exception as _e:
                logger.warning("Calendar event extraction/creation failed: %s", _e)

    # Store assistant response in contact data
    if message.from_user and message.from_user.id in _contact_data:
        _contact_data[message.from_user.id]["messages"].append({
            "role": "assistant",
            "text": response[:1000],
            "time": datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M"),
        })
        _contact_data[message.from_user.id]["messages"] = _contact_data[message.from_user.id]["messages"][-20:]
        # Log bot reply to Obsidian
        if obsidian_client is not None:
            user_name_r = (message.from_user.full_name or message.from_user.username
                           or f"ID:{message.from_user.id}")
            try:
                obsidian_client.log_message(
                    contact_name=user_name_r,
                    contact_id=message.from_user.id,
                    message_text=response,
                    role="assistant",
                )
            except Exception as _obs_err:
                logger.debug("Obsidian bot reply log failed: %s", _obs_err)

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


def _build_recent_contacts_summary(max_contacts: int = 5, max_msgs: int = 3) -> str:
    """Build a brief summary of recent contacts for the owner system prompt."""
    if not _contact_data:
        return ""
    summary = "\n\nПОСЛЕДНИЕ КОНТАКТЫ (краткая справка):\n"
    recent = list(_contact_data.items())[-max_contacts:]
    for uid, data in recent:
        msgs = data["messages"][-max_msgs:]
        summary += f"\n• {data['name']}:\n"
        for m in msgs:
            role = "Контакт" if m["role"] == "contact" else "Бот"
            summary += f"  [{m['time']}] {role}: {m['text'][:120]}\n"
    return summary


async def _rename_topic_async(bot: Bot, chat_id: int, thread_id: int, name: str) -> None:
    """Rename a forum/private-chat topic to a fixed category name (runs after the response)."""
    import asyncio as _aio
    await _aio.sleep(0.3)
    try:
        await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=name)
        _admin_topics[name] = thread_id
        logger.info("Renamed topic %d → %r", thread_id, name)
        _persist_business_state()
    except Exception as e:
        logger.warning("edit_forum_topic failed (%d → %r): %s", thread_id, name, e)


async def _close_topic_async(bot: Bot, chat_id: int, thread_id: int) -> None:
    """Close a question-text topic that has been superseded by an existing category."""
    import asyncio as _aio
    await _aio.sleep(0.5)
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        logger.info("Closed superseded topic %d", thread_id)
    except Exception as e:
        logger.debug("close_forum_topic %d: %s", thread_id, e)


async def _prepare_media_send(
    *,
    message: Message,
    bot: Bot,
    panel_bot: Bot | None,
    panel_chat_id: int | None,
    topic_key: str,
    topic_name: str,
    wait_text: str,
    chat_action: str,
) -> "tuple[int, int | None, Message | None, Bot]":
    """Resolve the target forum topic for a media download and send a placeholder.

    Returns (send_chat, thread_id, wait_msg, send_bot).

    Handles:
    - DM → supergroup routing via _admin_supergroup_id
    - Topic lookup / creation / recovery on stale thread IDs
    - Sending the "downloading…" placeholder
    """
    chat_type = getattr(message.chat, "type", None)

    send_chat: int = message.chat.id if chat_type == "supergroup" else (_admin_supergroup_id or message.chat.id)
    is_supergroup = (
        send_chat is not None
        and (
            (chat_type == "supergroup" and send_chat == message.chat.id)
            or (_admin_supergroup_id is not None and send_chat == _admin_supergroup_id)
            or (panel_chat_id is not None and send_chat == panel_chat_id)
        )
    )
    send_bot: Bot = panel_bot if (send_chat == panel_chat_id and panel_bot is not None) else bot
    thread_id: int | None = message.message_thread_id

    if is_supergroup:
        cached = _admin_topics.get(topic_key)
        if cached is None:
            try:
                t = await send_bot.create_forum_topic(chat_id=send_chat, name=topic_name)
                thread_id = t.message_thread_id
                _admin_topics[topic_key] = thread_id
                _persist_business_state()
                logger.info("Created %s topic chat=%d id=%d", topic_name, send_chat, thread_id)
            except Exception as te:
                logger.warning("create %s topic failed: %s", topic_name, te)
                send_chat = message.chat.id
                thread_id = message.message_thread_id
        else:
            thread_id = cached
    else:
        thread_id = None

    try:
        await send_bot.send_chat_action(chat_id=send_chat, action=chat_action, message_thread_id=thread_id)
    except Exception:
        pass

    wait_msg: Message | None = None
    try:
        wait_msg = await send_bot.send_message(
            chat_id=send_chat, text=wait_text, message_thread_id=thread_id, parse_mode=None,
        )
    except Exception as we:
        # Thread likely deleted — recover by creating a fresh topic
        logger.warning("Placeholder send to topic %s failed: %s. Recovering.", thread_id, we)
        if is_supergroup:
            _admin_topics.pop(topic_key, None)
            try:
                t2 = await send_bot.create_forum_topic(chat_id=send_chat, name=topic_name)
                thread_id = t2.message_thread_id
                _admin_topics[topic_key] = thread_id
                _persist_business_state()
                wait_msg = await send_bot.send_message(
                    chat_id=send_chat, text=wait_text, message_thread_id=thread_id, parse_mode=None,
                )
            except Exception as re2:
                logger.error("Failed to recover %s topic: %s", topic_name, re2)
                send_chat = message.chat.id
                thread_id = message.message_thread_id

    return send_chat, thread_id, wait_msg, send_bot


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
    meters_client: "MetersClient | None" = None,
    insta_downloader: "InstagramDownloader | None" = None,
    yt_downloader: "YTDownloader | None" = None,
    social_downloader: "SocialDownloader | None" = None,
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

    logger.info(
        "handle_private_message: chat_id=%s type=%s thread=%s is_admin=%s",
        message.chat.id, getattr(message.chat, "type", "?"), thread_id, is_admin,
    )

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

    # Owner mode: admin writing in private DM or any supergroup (including main thread)
    chat_type = getattr(message.chat, "type", None)
    is_owner_mode = is_admin and chat_type in ("private", "supergroup")

    # Remember supergroup chat_id so we can route to forum topics even from DMs
    global _admin_supergroup_id
    if is_owner_mode and chat_type == "supergroup" and _admin_supergroup_id != message.chat.id:
        _admin_supergroup_id = message.chat.id
        _persist_business_state()
        logger.info("Recorded admin supergroup chat_id=%d", _admin_supergroup_id)

    # ── Meter readings: triggered by prefix «Показания» ────────────────────
    if is_owner_mode and meters_client is not None and text.lstrip().lower().startswith("показания"):
        try:
            await bot.send_chat_action(
                chat_id=message.chat.id, action="typing",
                message_thread_id=message.message_thread_id,
            )
        except Exception:
            pass
        readings = await extract_meter_readings(text, ai_registry)
        if readings:
            results = await meters_client.save_readings(readings)
            reply = meters_client.format_confirmation(readings, results)
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=reply,
                    message_thread_id=message.message_thread_id,
                    parse_mode=None,
                )
            except Exception as _me:
                logger.warning("Meters reply failed: %s", _me)
            # Still let the conversation continue normally (no early return)
            # so the AI can also acknowledge/comment if needed

    # ── Instagram downloader ─────────────────────────────────────────────────
    if is_owner_mode and insta_downloader is not None:
        _insta_url = _detect_insta_url(text)
        if _insta_url:
            _insta_send_chat = message.chat.id if chat_type == "supergroup" else (_admin_supergroup_id or message.chat.id)
            _insta_key = f"📸 Instagram:{_insta_send_chat}"
            _insta_send_chat, _insta_thread_id, _wait_msg, _insta_bot = await _prepare_media_send(
                message=message, bot=bot, panel_bot=panel_bot, panel_chat_id=panel_chat_id,
                topic_key=_insta_key, topic_name="📸 Instagram",
                wait_text="⏬ Скачиваю...", chat_action="upload_video",
            )

            _media_files = await insta_downloader.download(_insta_url)
            try:
                if _wait_msg:
                    await _insta_bot.delete_message(chat_id=_insta_send_chat, message_id=_wait_msg.message_id)
            except Exception:
                pass

            if not _media_files:
                await _insta_bot.send_message(
                    chat_id=_insta_send_chat,
                    text="❌ Не удалось скачать. Возможно, аккаунт закрытый или ссылка недействительна.",
                    message_thread_id=_insta_thread_id, parse_mode=None,
                )
                return

            from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
            try:
                if len(_media_files) == 1:
                    _f = _media_files[0]
                    _inp = FSInputFile(str(_f.path))
                    if _f.media_type == "photo":
                        await _insta_bot.send_photo(_insta_send_chat, _inp, caption=_f.caption or None, message_thread_id=_insta_thread_id)
                    elif _f.media_type == "video":
                        await _insta_bot.send_video(_insta_send_chat, _inp, caption=_f.caption or None, message_thread_id=_insta_thread_id)
                    else:
                        await _insta_bot.send_document(_insta_send_chat, _inp, caption=_f.caption or None, message_thread_id=_insta_thread_id)
                else:
                    _group = []
                    for _i, _f in enumerate(_media_files[:10]):
                        _inp = FSInputFile(str(_f.path))
                        _cap = _f.caption if _i == 0 else None
                        if _f.media_type in ("photo", "document") and _f.path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                            _group.append(InputMediaPhoto(media=_inp, caption=_cap))
                        else:
                            _group.append(InputMediaVideo(media=_inp, caption=_cap))
                    await _insta_bot.send_media_group(_insta_send_chat, _group, message_thread_id=_insta_thread_id)

                if chat_type == "private" and _insta_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text="✅ Скачал и отправил в топик 📸 Instagram", parse_mode=None)
                    except Exception as _pe:
                        logger.debug("Instagram DM confirmation failed: %s", _pe)

                if chat_type == "supergroup" and message.message_thread_id:
                    if (
                        _insta_thread_id is not None
                        and message.message_thread_id != _insta_thread_id
                        and message.message_thread_id not in _admin_topics.values()
                        and message.message_thread_id not in _topic_contacts
                    ):
                        import asyncio as _aio
                        _t = _aio.create_task(_close_topic_async(bot, message.chat.id, message.message_thread_id))
                        _t.add_done_callback(
                            lambda t: logger.warning("close_topic for Instagram raised: %s", t.exception())
                            if not t.cancelled() and t.exception() else None
                        )
            except Exception as _e:
                logger.warning("Instagram send failed: %s", _e)
                await _insta_bot.send_message(
                    chat_id=_insta_send_chat, text=f"⚠️ Скачал, но не смог отправить: {_e}",
                    message_thread_id=_insta_thread_id, parse_mode=None,
                )
                if chat_type == "private" and _insta_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text=f"⚠️ Не удалось отправить в топик: {_e}", parse_mode=None)
                    except Exception:
                        pass
            finally:
                insta_downloader.cleanup(_media_files)
            return

    # ── YouTube summary (резюме / кратко / summary URL) ──────────────────────
    if is_owner_mode and yt_downloader is not None:
        _summary_url = _detect_yt_summary_cmd(text)
        if _summary_url:
            try:
                await bot.send_chat_action(
                    chat_id=message.chat.id, action="typing",
                    message_thread_id=message.message_thread_id,
                )
            except Exception:
                pass
            placeholder_sm = None
            try:
                placeholder_sm = await bot.send_message(
                    chat_id=message.chat.id,
                    text="📝 Получаю субтитры…",
                    message_thread_id=message.message_thread_id,
                    parse_mode=None,
                )
            except Exception:
                pass

            transcript = await yt_downloader.fetch_transcript(_summary_url)
            summary_reply: str
            if not transcript:
                summary_reply = "⚠️ Субтитры недоступны для этого видео. Попробуй скачать аудио и transcribe вручную."
            else:
                try:
                    sm_client = ai_registry.get_client("openrouter_gemini")
                    summary_reply = await sm_client.complete(
                        system="Ты пишешь краткие резюме видео. Структурируй ответ: основная идея, ключевые тезисы (3-5 пунктов), вывод. Русский язык.",
                        messages=[{"role": "user", "content": f"Субтитры видео:\n{transcript[:8000]}\n\nНапиши краткое резюме."}],
                        max_tokens=800,
                    )
                except Exception as _se:
                    logger.warning("YT summary AI failed: %s", _se)
                    summary_reply = f"⚠️ Не удалось сгенерировать резюме: {_se}"

            try:
                if placeholder_sm:
                    await bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=placeholder_sm.message_id,
                        text=summary_reply,
                        parse_mode=None,
                    )
                else:
                    await bot.send_message(
                        chat_id=message.chat.id, text=summary_reply,
                        message_thread_id=message.message_thread_id, parse_mode=None,
                    )
            except Exception as _pe:
                logger.debug("Summary reply send failed: %s", _pe)
            return

    # ── YouTube audio extraction ──────────────────────────────────────────────
    if is_owner_mode and yt_downloader is not None:
        _yt_url = _detect_yt_url(text)
        if _yt_url:
            _yt_send_chat = message.chat.id if chat_type == "supergroup" else (_admin_supergroup_id or message.chat.id)
            _yt_key = f"🎵 YouTube:{_yt_send_chat}"
            _yt_send_chat, _yt_thread_id, _yt_wait_msg, _yt_bot = await _prepare_media_send(
                message=message, bot=bot, panel_bot=panel_bot, panel_chat_id=panel_chat_id,
                topic_key=_yt_key, topic_name="🎵 YouTube",
                wait_text="⏬ Скачиваю аудио…", chat_action="upload_document",
            )

            _yt_audio: _YTAudioFile | None = None
            try:
                _yt_audio = await yt_downloader.download_audio(_yt_url)
                if _yt_audio is None:
                    raise RuntimeError("yt_downloader вернул None — возможно видео недоступно")

                if _yt_wait_msg is not None:
                    try:
                        await _yt_bot.delete_message(chat_id=_yt_send_chat, message_id=_yt_wait_msg.message_id)
                    except Exception:
                        pass

                _yt_caption = _yt_audio.title[:200] if _yt_audio.title else None
                from aiogram.types import FSInputFile as _FSInputFile
                _yt_inp = _FSInputFile(str(_yt_audio.path))

                if _yt_audio.send_as_audio:
                    await _yt_bot.send_audio(
                        chat_id=_yt_send_chat, audio=_yt_inp,
                        title=_yt_audio.title or None, duration=_yt_audio.duration_s or None,
                        caption=_yt_caption, message_thread_id=_yt_thread_id, parse_mode=None,
                    )
                else:
                    await _yt_bot.send_document(
                        chat_id=_yt_send_chat, document=_yt_inp,
                        caption=_yt_caption, message_thread_id=_yt_thread_id, parse_mode=None,
                    )

                if chat_type == "private" and _yt_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text="✅ Аудио скачано и отправлено в топик 🎵 YouTube", parse_mode=None)
                    except Exception as _pe:
                        logger.debug("YouTube DM confirmation failed: %s", _pe)

            except Exception as _e:
                logger.warning("YouTube send failed: %s", _e)
                try:
                    if _yt_wait_msg is not None:
                        await _yt_bot.edit_message_text(
                            chat_id=_yt_send_chat, message_id=_yt_wait_msg.message_id,
                            text=f"⚠️ Не удалось скачать аудио: {_e}", parse_mode=None,
                        )
                    else:
                        await _yt_bot.send_message(
                            chat_id=_yt_send_chat, text=f"⚠️ Не удалось скачать аудио: {_e}",
                            message_thread_id=_yt_thread_id, parse_mode=None,
                        )
                except Exception:
                    pass
                if chat_type == "private" and _yt_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text=f"⚠️ Не удалось скачать аудио: {_e}", parse_mode=None)
                    except Exception:
                        pass
            finally:
                yt_downloader.cleanup(_yt_audio)
            return

    # ── TikTok / X/Twitter downloader ────────────────────────────────────────
    if is_owner_mode and social_downloader is not None:
        _social = _detect_social_platform(text)
        if _social:
            _social_url, _social_topic_name = _social
            _social_send_chat = message.chat.id if chat_type == "supergroup" else (_admin_supergroup_id or message.chat.id)
            _social_key = f"{_social_topic_name}:{_social_send_chat}"
            _social_send_chat, _social_thread_id, _social_wait_msg, _social_bot = await _prepare_media_send(
                message=message, bot=bot, panel_bot=panel_bot, panel_chat_id=panel_chat_id,
                topic_key=_social_key, topic_name=_social_topic_name,
                wait_text="⏬ Скачиваю...", chat_action="upload_video",
            )

            _social_files = await social_downloader.download(_social_url)
            try:
                if _social_wait_msg:
                    await _social_bot.delete_message(chat_id=_social_send_chat, message_id=_social_wait_msg.message_id)
            except Exception:
                pass

            if not _social_files:
                await _social_bot.send_message(
                    chat_id=_social_send_chat,
                    text=f"❌ Не удалось скачать. Проверьте, что пост публичный.",
                    message_thread_id=_social_thread_id, parse_mode=None,
                )
                return

            from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
            try:
                if len(_social_files) == 1:
                    _sf = _social_files[0]
                    _si = FSInputFile(str(_sf.path))
                    if _sf.media_type == "photo":
                        await _social_bot.send_photo(_social_send_chat, _si, caption=_sf.caption or None, message_thread_id=_social_thread_id)
                    elif _sf.media_type == "video":
                        await _social_bot.send_video(_social_send_chat, _si, caption=_sf.caption or None, message_thread_id=_social_thread_id)
                    else:
                        await _social_bot.send_document(_social_send_chat, _si, caption=_sf.caption or None, message_thread_id=_social_thread_id)
                else:
                    _sgroup = []
                    for _si_idx, _sf in enumerate(_social_files[:10]):
                        _si = FSInputFile(str(_sf.path))
                        _sc = _sf.caption if _si_idx == 0 else None
                        if _sf.media_type in ("photo", "document") and _sf.path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                            _sgroup.append(InputMediaPhoto(media=_si, caption=_sc))
                        else:
                            _sgroup.append(InputMediaVideo(media=_si, caption=_sc))
                    await _social_bot.send_media_group(_social_send_chat, _sgroup, message_thread_id=_social_thread_id)

                if chat_type == "private" and _social_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text=f"✅ Скачал и отправил в топик {_social_topic_name}", parse_mode=None)
                    except Exception as _pe:
                        logger.debug("Social DM confirmation failed: %s", _pe)
            except Exception as _e:
                logger.warning("Social send failed: %s", _e)
                await _social_bot.send_message(
                    chat_id=_social_send_chat, text=f"⚠️ Скачал, но не смог отправить: {_e}",
                    message_thread_id=_social_thread_id, parse_mode=None,
                )
                if chat_type == "private" and _social_send_chat != message.chat.id:
                    try:
                        await bot.send_message(chat_id=message.chat.id, text=f"⚠️ Не удалось отправить в топик: {_e}", parse_mode=None)
                    except Exception:
                        pass
            finally:
                social_downloader.cleanup(_social_files)
            return

    # For supergroup: classify every owner message and ensure it lands in the right
    # category topic. Telegram Forums auto-create topics with the message text as the
    # name — we intercept that and rename/route to the correct fixed category.
    # For private DM: forum topics not supported — respond inline.
    target_thread_id: int | None = message.message_thread_id  # default: same as incoming

    if is_owner_mode and thread_id and thread_id not in _topic_contacts:
        # Denis is in a topic (any chat type) that is NOT a contact topic.
        # Classify and ensure the topic gets a fixed category name.
        # We respond in the best available thread, then rename/close asynchronously.
        import asyncio as _aio
        category = await _classify_owner_category(text, ai_registry)
        existing_tid = _admin_topics.get(category)

        if existing_tid and existing_tid != thread_id:
            # A category topic already exists in a different thread — route there.
            target_thread_id = existing_tid
            new_key = f"private:{message.chat.id}:{existing_tid}"
            conv.add(new_key, "user", text)
            key = new_key
            logger.info("Owner: routing %r → topic %d (closing %d)", category, existing_tid, thread_id)
            # Close the question-text topic after we respond in the correct one
            _t = _aio.create_task(_close_topic_async(bot, message.chat.id, thread_id))
            _t.add_done_callback(
                lambda t: logger.warning("close_topic task raised: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )
        else:
            # No existing category topic — respond here and rename this topic
            target_thread_id = thread_id
            logger.info("Owner: responding in %d, will rename → %r", thread_id, category)
            # Rename happens AFTER the response so Denis sees the answer first
            _t = _aio.create_task(_rename_topic_async(bot, message.chat.id, thread_id, category))
            _t.add_done_callback(
                lambda t: logger.warning("rename_topic task raised: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )

    if is_owner_mode:
        # Build owner system prompt — always use OWNER_SYSTEM_PROMPT as base
        if contact_context:
            # Denis is in a known contact topic — add contact context
            owner_prompt = await _build_system_prompt(
                OWNER_SYSTEM_PROMPT, calendar_client, contact_context
            )
        else:
            # No specific contact context — add brief recent contacts summary
            contacts_summary = _build_recent_contacts_summary()
            owner_prompt = await _build_system_prompt(
                OWNER_SYSTEM_PROMPT + contacts_summary, calendar_client
            )
        system_prompt = owner_prompt
    else:
        system_prompt = await _build_system_prompt(persona.system_prompt, calendar_client)

    # ── Owner mode: simple complete() + send_message (no placeholder/stream) ──
    if is_owner_mode:
        try:
            await bot.send_chat_action(
                chat_id=message.chat.id,
                action="typing",
                message_thread_id=target_thread_id,
            )
        except Exception as e:
            logger.debug("chat_action skipped: %s", e)

        try:
            response = await client.complete(
                system=system_prompt,
                messages=conv.get(key),
                max_tokens=persona.max_tokens,
            )
            if not response or not response.strip():
                response = persona.fallback
        except Exception as e:
            logger.warning("Owner complete() failed (%s): %s", persona.provider, e)
            await alerts.send("owner", f"{type(e).__name__}: {e}")
            response = persona.fallback

        try:
            await bot.send_message(
                chat_id=message.chat.id,
                text=response,
                message_thread_id=target_thread_id,
                parse_mode=None,
            )
        except Exception as e:
            logger.warning("Owner send_message failed: %s", e)

        conv.add(key, "assistant", response)
        return

    # ── Regular (non-owner) streaming flow ──────────────────────────────────
    try:
        await bot.send_chat_action(
            chat_id=message.chat.id,
            action="typing",
            message_thread_id=message.message_thread_id,
        )
    except Exception as e:
        logger.debug("chat_action skipped: %s", e)

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

    _TG_MAX = 4096
    buffer = ""
    last_edit_at = now()
    streaming_failed = False

    try:
        async for delta in client.stream(
            system=system_prompt,
            messages=conv.get(key),
            max_tokens=persona.max_tokens,
        ):
            remaining = _TG_MAX - len(buffer)
            if remaining <= 0:
                break  # already at telegram limit
            buffer += delta[:remaining]
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




# ---------------------------------------------------------------------------
# /help — show owner capabilities
# ---------------------------------------------------------------------------

_HELP_TEXT = """🤖 *Справка — возможности бота*

━━━━━━━━━━━━━━━━━━━━━
🎙 *Голосовые сообщения*
━━━━━━━━━━━━━━━━━━━━━
Запиши голосовое сообщение — бот транскрибирует его через Groq Whisper и ответит как на текст. Работает в личке и в топиках группы. Требует настроенного `GROQ_API_KEY`.

━━━━━━━━━━━━━━━━━━━━━
📸 *Instagram — скачать медиа*
━━━━━━━━━━━━━━━━━━━━━
Скинь ссылку на пост, Reel или карусель — бот скачает и пришлёт фото/видео в топик *📸 Instagram* (создаётся автоматически).

Пример: `https://www.instagram.com/reel/ABC123/`

Работает с публичными постами. Приватные аккаунты и Stories не поддерживаются.

━━━━━━━━━━━━━━━━━━━━━
🎵 *YouTube — скачать аудио*
━━━━━━━━━━━━━━━━━━━━━
Скинь ссылку на видео — бот скачает аудио в наилучшем качестве и пришлёт в топик *🎵 YouTube* (создаётся автоматически). Если файл больше 50 МБ — придёт как документ.

Пример: `https://youtu.be/dQw4w9WgXcQ`
Пример: `https://www.youtube.com/watch?v=dQw4w9WgXcQ`

Shorts и плейлисты не поддерживаются — только обычные видео.

Чтобы получить *текстовое резюме* видео вместо аудио — добавь перед ссылкой слово `резюме`, `кратко` или `summary`:
`резюме https://youtu.be/dQw4w9WgXcQ`

Бот скачает субтитры (если доступны) и пришлёт краткое AI-резюме с ключевыми тезисами.

━━━━━━━━━━━━━━━━━━━━━
🎬 *TikTok и 🐦 X / Twitter — скачать видео*
━━━━━━━━━━━━━━━━━━━━━
Скинь ссылку на публичный TikTok или твит с видео — бот скачает и пришлёт в отдельный топик (*🎬 TikTok* или *🐦 X / Twitter*, создаётся автоматически).

Примеры:
`https://www.tiktok.com/@user/video/1234567890`
`https://vm.tiktok.com/XXXXXX/`
`https://x.com/user/status/1234567890`
`https://twitter.com/user/status/1234567890`

━━━━━━━━━━━━━━━━━━━━━
📊 *Показания счётчиков*
━━━━━━━━━━━━━━━━━━━━━
Начни сообщение со слова *Показания* — и бот запишет данные в Google Таблицу (листы Gas / Water / Electricity). Расход и итоговая сумма считаются автоматически по формуле.

Примеры:
`Показания: газ 5678, вода 234, день 12540 ночь 4310`
`Показания газ 5678`
`Показания — вода 345, электричество день 12540 ночь 4310`

⚠️ Для электричества нужно указать оба значения: *день* и *ночь*.

━━━━━━━━━━━━━━━━━━━━━
🎙 *Панельное обсуждение*
━━━━━━━━━━━━━━━━━━━━━
Запускает дискуссию между 5 AI-экспертами: аналитик, скептик, креативщик, прагматик и модератор. Каждый высказывается по теме — живо, без сухих списков.

Команды:
`/panel Стоит ли запускать новый продукт?`
`панель: Как масштабировать продажи?`

Или *перешли* любой пост из Telegram-канала в этот чат — обсуждение запустится автоматически по тексту поста.

━━━━━━━━━━━━━━━━━━━━━
🗓 *Google Календарь*
━━━━━━━━━━━━━━━━━━━━━
Бот знает твоё расписание на 10 дней вперёд и учитывает его в ответах контактам. Когда контакт предлагает конкретную дату встречи или созвон — бот автоматически создаёт событие в календаре и присылает тебе уведомление со ссылкой.

━━━━━━━━━━━━━━━━━━━━━
📋 *Перенос прайса*
━━━━━━━━━━━━━━━━━━━━━
Когда контакт присылает ссылку на свою Google Таблицу с ценами — бот читает её, применяет наценку и переносит позиции в твою личную таблицу. Всё автоматически, без копирования вручную.

Что делает контакт: просто кидает ссылку вида `docs.google.com/spreadsheets/...` в чат с тобой.

━━━━━━━━━━━━━━━━━━━━━
📝 *Obsidian — заметки*
━━━━━━━━━━━━━━━━━━━━━
Вся переписка с контактами автоматически сохраняется в локальное хранилище Obsidian:
• `Contacts/{имя контакта}.md` — полная история переписки с конкретным человеком
• `Daily/{ГГГГ-ММ-ДД}.md` — дневной лог всех контактов за день

━━━━━━━━━━━━━━━━━━━━━
📊 *Дайджест контактов*
━━━━━━━━━━━━━━━━━━━━━
Каждый день в *20:00* бот присылает сводку: кто писал, сколько сообщений и последняя фраза от каждого контакта.

━━━━━━━━━━━━━━━━━━━━━
💬 *Личный AI-ассистент*
━━━━━━━━━━━━━━━━━━━━━
В этом чате ты можешь писать боту напрямую — любой вопрос, задачу, анализ. Бот отвечает как персональный помощник, учитывая историю переписки с контактами и твоё расписание.

Примеры:
`Что писал Иван на этой неделе?`
`Есть ли у меня время в пятницу?`
`Составь краткое резюме по клиентам за сегодня`"""


def _is_help_cmd(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return t in ("/help", "help") or t.startswith("/help ") or t.startswith("help ")


@business_router.message(
    F.text.func(_is_help_cmd),
    F.chat.type.in_({"private", "supergroup"}),
)
async def _on_help(message: Message, settings) -> None:
    if not message.from_user or message.from_user.id != settings.admin_user_id:
        return
    await message.answer(_HELP_TEXT, parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Daily contact digest
# ---------------------------------------------------------------------------

def _build_digest_message(timezone_str: str) -> str:
    """Build a human-readable daily contact digest."""
    tz = ZoneInfo(timezone_str)
    today = datetime.now(tz).strftime("%d.%m.%Y")

    if not _contact_today:
        return f"📊 Дайджест за {today}: сегодня новых сообщений от контактов не было."

    total_msgs = sum(_contact_today.values())
    lines = [f"📊 Дайджест контактов — {today}\n"]

    # Sort by most messages first
    for user_id, count in sorted(_contact_today.items(), key=lambda x: -x[1]):
        data = _contact_data.get(user_id)
        if data is None:
            continue
        name = data["name"]
        # Find last inbound message snippet
        last_contact = next(
            (m["text"][:120] for m in reversed(data["messages"]) if m["role"] == "contact"),
            "—",
        )
        noun = "сообщение" if count == 1 else ("сообщения" if 2 <= count <= 4 else "сообщений")
        lines.append(f"👤 {name} — {count} {noun}")
        lines.append(f"   Посл.: «{last_contact}»")
        lines.append("")

    lines.append(f"Итого: {len(_contact_today)} конт. · {total_msgs} сообщ.")
    return "\n".join(lines)


async def _digest_loop(
    bot: "Bot",
    admin_user_id: int,
    timezone_str: str,
    digest_time: str,
) -> None:
    """Send a daily contact digest at a fixed local time, then reset today's log."""
    tz = ZoneInfo(timezone_str)
    h, m = map(int, digest_time.split(":"))

    while True:
        now_dt = datetime.now(tz)
        target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)

        delay = (target - now_dt).total_seconds()
        logger.info("Digest scheduler: next send in %.0f min at %s", delay / 60, target.strftime("%d.%m %H:%M"))
        await asyncio.sleep(delay)

        try:
            msg = _build_digest_message(timezone_str)
            await bot.send_message(admin_user_id, msg, parse_mode=None)
            logger.info("Contact digest sent to admin (%d contacts)", len(_contact_today))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Digest send failed: %s", e)
        finally:
            _contact_today.clear()
            logger.info("Daily contact log reset")



# ---------------------------------------------------------------------------
# Admin forward-to-panel trigger
# ---------------------------------------------------------------------------

@business_router.message((F.forward_from_chat | F.forward_origin) & (F.chat.type == "private"))
async def _on_forward_to_panel(
    message: Message,
    settings,
    ai_registry,
    conv,
    personas,
    alerts,
    bots,
    **_kwargs,
) -> None:
    """When the admin forwards a channel post to the business bot, start a panel round."""
    logger.info(
        "Forward trigger: from_user=%s admin=%s fwd_chat=%s fwd_origin=%s",
        getattr(message.from_user, "id", None),
        settings.admin_user_id,
        message.forward_from_chat,
        type(message.forward_origin).__name__ if message.forward_origin else None,
    )
    if not message.from_user or message.from_user.id != settings.admin_user_id:
        logger.info("Forward trigger: ignored (not admin)")
        return

    # Support both legacy forward_from_chat and new forward_origin (Bot API 7.0+)
    source = "канал"
    if message.forward_from_chat and message.forward_from_chat.title:
        source = message.forward_from_chat.title
    elif message.forward_origin is not None and hasattr(message.forward_origin, "chat"):
        chat = getattr(message.forward_origin, "chat", None)
        if chat and getattr(chat, "title", None):
            source = chat.title
    text = message.text or message.caption or ""
    if not text.strip():
        await message.reply("⚠️ Пересланное сообщение без текста — не могу запустить обсуждение.")
        return

    topic = f"Из «{source}»: {text[:500]}"

    from claudebots.routers.panel import PanelRoundRunner  # late import — avoids circular

    runner = PanelRoundRunner(
        bots=bots,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts,
        panel_chat_id=settings.panel_chat_id,
        thread_id=None,
    )
    _t = asyncio.create_task(runner.run_round(topic))
    _t.add_done_callback(
        lambda t: logger.warning("Forward-triggered round raised: %s", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )
    await message.reply("🎙 Запускаю обсуждение на панели…")

def start_digest_scheduler(
    bot: "Bot",
    admin_user_id: int,
    timezone_str: str,
    digest_time: str = "20:00",
) -> "asyncio.Task[None]":
    """Create and return the daily contact digest background task."""
    task: asyncio.Task[None] = asyncio.create_task(
        _digest_loop(bot, admin_user_id, timezone_str, digest_time)
    )
    task.add_done_callback(
        lambda t: logger.warning("Digest loop raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info("Contact digest scheduler started (time=%s %s)", digest_time, timezone_str)
    return task
