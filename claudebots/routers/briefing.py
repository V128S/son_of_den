"""Morning briefing scheduler — two topic messages per day.

Message 1: 🌍 Политика и мир (calendar + politics from channels + Exa)
Message 2: 💻 Технологии · ИИ · Крипто (tech/AI/crypto from channels + Exa)
"""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from claudebots.core.feed_monitor import _sanitize_html, fetch_channel_entries_raw

logger = logging.getLogger(__name__)

_TG_MAX = 3900  # leave headroom under Telegram's 4096 limit
_SECONDS_PER_DAY = 86_400

_BRIEFING_POLITICS_SYSTEM = (
    "Ты — главный редактор утреннего брифинга для предпринимателя из Украины. "
    "Из предложенных постов телеграм-каналов и новостных источников отбери и осмысли "
    "ТОЛЬКО политические события, геополитику, войну, дипломатию, санкции, выборы. "
    "Игнорируй технологии, крипту, финансы — только политика и мировые события.\n\n"
    "ФОРМАТ — строго Telegram HTML. Разрешённые теги: <b>, <i>, <u>, <s>, <blockquote>, <blockquote expandable>.\n\n"
    "Структура каждого блока:\n"
    "<b>🌍 Заголовок события</b>\n"
    "2-3 предложения: что произошло и ключевые детали. "
    "Выдели <u>ключевые имена и страны</u> подчёркиванием.\n"
    "<blockquote expandable><i>→ Почему это важно и чего ждать дальше.</i></blockquote>\n\n"
    "Блоки разделены пустой строкой. "
    "Эмодзи в заголовках: 🌍 ⚔️ 🇺🇦 🔴 🏛️ 📜\n"
    "Каждый блок 50-70 слов (без учёта expandable). Пиши ТОЛЬКО на русском языке. "
    "Не упоминай российские бренды (Яндекс, Сбербанк, ВКонтакте и подобные). "
    "Без markdown (#, *, `), без заголовков разделов, без воды."
)

_BRIEFING_TECH_SYSTEM = (
    "Ты — главный редактор утреннего брифинга для предпринимателя из Украины. "
    "Из предложенных постов телеграм-каналов и новостных источников отбери и осмысли "
    "ТОЛЬКО технологические события: ИИ, криптовалюта, стартапы, рынки, продукты. "
    "Игнорируй политику и войну — только tech, AI, crypto, бизнес.\n\n"
    "ФОРМАТ — строго Telegram HTML. Разрешённые теги: <b>, <i>, <u>, <s>, <blockquote>, <blockquote expandable>.\n\n"
    "Структура каждого блока:\n"
    "<b>💻 Заголовок события</b>\n"
    "2-3 предложения: что произошло и ключевые детали. "
    "Выдели <u>названия компаний и технологий</u> подчёркиванием.\n"
    "<blockquote expandable><i>→ Практический вывод: как это влияет на бизнес.</i></blockquote>\n\n"
    "Блоки разделены пустой строкой. "
    "Эмодзи в заголовках: 💻 🤖 💰 📊 💎 🚀 🔬\n"
    "Каждый блок 50-70 слов (без учёта expandable). Пиши ТОЛЬКО на русском языке. "
    "Не упоминай российские бренды (Яндекс, Сбербанк, ВКонтакте и подобные). "
    "Без markdown (#, *, `), без заголовков разделов, без воды."
)


async def _build_briefing_messages(
    *,
    timezone_str: str,
    channels: list[str],
    calendar_client,
    ai_registry,
    search_client=None,
) -> list[str]:
    """Build 0-2 morning briefing messages. Returns empty list when no content."""
    if not channels:
        return []

    # ── Fetch channel posts (last 24 h) ───────────────────────────────────────
    since = datetime.now(UTC).timestamp() - _SECONDS_PER_DAY
    snippets: list[str] = []
    for ch in channels:
        try:
            entries = await fetch_channel_entries_raw(ch, since)
        except Exception as e:
            logger.debug("Briefing: fetch failed for %s: %s", ch, e)
            continue
        for _, title, text, _ in entries[:25]:
            snippet = (text or title)[:400].replace("\n", " ").strip()
            if snippet:
                snippets.append(f"• {snippet}")
    channel_block = "\n".join(snippets)[:5000]

    # ── Exa enrichment (optional) ─────────────────────────────────────────────
    politics_exa = ""
    tech_exa = ""
    if search_client is not None and getattr(search_client, "enabled", False):
        try:
            r1 = await search_client.search("политика геополитика мировые новости", num_results=3)
            if r1:
                politics_exa = search_client.format_results(r1)
        except Exception as e:
            logger.debug("Briefing: Exa politics search failed: %s", e)
        try:
            r2 = await search_client.search(
                "технологии искусственный интеллект криптовалюта", num_results=3
            )
            if r2:
                tech_exa = search_client.format_results(r2)
        except Exception as e:
            logger.debug("Briefing: Exa tech search failed: %s", e)

    if not channel_block and not politics_exa and not tech_exa:
        return []

    # ── Calendar header (for message 1 only) ─────────────────────────────────
    calendar_header = ""
    if calendar_client is not None:
        try:
            today_events = await calendar_client.get_upcoming_events_summary(days=1)
            _no_events = ("", "Нет запланированных событий на ближайшие дни.")
            if today_events and today_events not in _no_events:
                calendar_header = today_events
        except Exception as e:
            logger.warning("Briefing: calendar fetch failed: %s", e)

    # ── AI provider ───────────────────────────────────────────────────────────
    _providers = ["openmodel", "groq", "openrouter_gemini", "claude"]
    provider = next((p for p in _providers if ai_registry.has_provider(p)), None)
    if provider is None:
        return []
    client = ai_registry.get_client(provider)

    messages: list[str] = []

    for topic_system, topic_exa, header_prefix, content_prefix in [
        (
            _BRIEFING_POLITICS_SYSTEM,
            politics_exa,
            (
                "🌍 <b>Политика и мир</b>\n\n"
                + (f"<b>📅 Сегодня:</b> {calendar_header}\n\n" if calendar_header else "")
            ),
            "Посты каналов и новостные источники за последние 24 часа:\n\n",
        ),
        (
            _BRIEFING_TECH_SYSTEM,
            tech_exa,
            "💻 <b>Технологии · ИИ · Крипто</b>\n\n",
            "Посты каналов и новостные источники за последние 24 часа:\n\n",
        ),
    ]:
        content = channel_block
        if topic_exa:
            content = content + "\n\n" + topic_exa
        content = content[:7000]
        if not content.strip():
            continue
        try:
            ai_text = await client.complete(
                system=topic_system,
                messages=[{"role": "user", "content": content_prefix + content}],
                max_tokens=1800,
            )
            ai_text = _sanitize_html(ai_text or "").strip()
            if not ai_text:
                continue
            full = header_prefix + ai_text
            # Truncate to Telegram limit at the last paragraph boundary
            if len(full) > _TG_MAX:
                cut = full.rfind("\n\n", 0, _TG_MAX)
                full = full[:cut] if cut > 0 else full[:_TG_MAX]
            messages.append(full)
        except Exception as e:
            logger.warning("Briefing: AI generation failed for topic: %s", e)

    return messages


async def _briefing_loop(
    *,
    bot,
    admin_user_id: int,
    timezone_str: str,
    briefing_time: str,
    channels: list[str],
    calendar_client,
    ai_registry,
    search_client=None,
) -> None:
    """Send morning briefing at a fixed local time each day."""
    from claudebots.core.scheduling import daily_at

    tz = ZoneInfo(timezone_str)
    async for _ in daily_at(briefing_time, tz, label="Briefing scheduler", log=logger):
        try:
            messages = await _build_briefing_messages(
                timezone_str=timezone_str,
                channels=channels,
                calendar_client=calendar_client,
                ai_registry=ai_registry,
                search_client=search_client,
            )
            for i, msg in enumerate(messages):
                await bot.send_message(admin_user_id, msg, parse_mode="HTML")
                if i < len(messages) - 1:
                    await asyncio.sleep(1)
            if messages:
                logger.info("Morning briefing sent (%d message(s))", len(messages))
            else:
                logger.info("Morning briefing: no content to send")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Briefing send failed: %s", e)


def start_briefing_scheduler(
    *,
    bot,
    admin_user_id: int,
    timezone_str: str,
    briefing_time: str,
    channels: list[str],
    calendar_client,
    ai_registry,
    search_client=None,
) -> "asyncio.Task[None]":
    """Create and return the morning briefing background task."""
    task: asyncio.Task[None] = asyncio.create_task(
        _briefing_loop(
            bot=bot,
            admin_user_id=admin_user_id,
            timezone_str=timezone_str,
            briefing_time=briefing_time,
            channels=channels,
            calendar_client=calendar_client,
            ai_registry=ai_registry,
            search_client=search_client,
        )
    )
    task.add_done_callback(
        lambda t: logger.warning("Briefing loop raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info("Morning briefing scheduler started (time=%s %s)", briefing_time, timezone_str)
    return task
