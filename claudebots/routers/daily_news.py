"""Daily news panel: once-a-day panel discussion triggered at a configured local time.

At the scheduled time:
1. Fetches yesterday's top news from Exa (if available) based on configured interests.
2. Builds a topic string summarising the headlines.
3. Fires exactly one PanelRoundRunner.run_round() with that topic.

Intended to replace frequent feed/revival triggers for users who want a single
morning digest-style panel discussion.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from claudebots.core.ai_registry import AIRegistry

if TYPE_CHECKING:
    from claudebots.core.alerts import AlertSender
    from claudebots.core.conversation import ConversationStore
    from claudebots.core.personas import PersonaRegistry

logger = logging.getLogger(__name__)


def _get_news_date(tz: ZoneInfo) -> datetime:
    """Return the date to fetch news for. Today if local hour >= 15, otherwise yesterday."""
    now_local = datetime.now(tz)
    if now_local.hour >= 15:
        return now_local
    return now_local - timedelta(days=1)


async def _fetch_headlines(
    *,
    interests: str,
    timezone_str: str,
    search_client: Any,
) -> list[str]:
    """Return a list of headline strings using Exa search."""
    if search_client is None or not getattr(search_client, "enabled", False):
        return []
    tz = ZoneInfo(timezone_str)
    news_date = _get_news_date(tz)
    date_str = news_date.strftime("%d.%m.%Y")
    query = f"главные новости {date_str} {interests}"
    try:
        results = await search_client.search(query, num_results=10)
        return [r.title for r in results if r.title]
    except Exception as e:
        logger.warning("Daily news: headline search failed: %s", e)
        return []


async def _select_main_news(
    headlines: list[str],
    ai_registry: AIRegistry,
) -> str:
    """Use AI to select 1-2 main news stories and explain their significance."""
    if not headlines:
        return ""

    provider = next(
        (p for p in ["openrouter_gemini", "groq", "claude"] if ai_registry.has_provider(p)),
        "claude"
    )
    client = ai_registry.get_client(provider)

    headlines_list = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
    system_prompt = "Ты опытный новостной редактор и аналитик. Твоя задача — выбрать 1-2 самые главные и важные новости из предложенного списка для обсуждения экспертами."
    user_prompt = (
        f"Вот список заголовков новостей:\n{headlines_list}\n\n"
        f"Выбери из них 1-2 самые главные, резонансные и важные новости. "
        f"Для каждой выбранной новости кратко опиши суть и значение для бизнеса, технологий или общества (1-2 предложения).\n"
        f"Ответь на русском языке. Твой ответ должен содержать только выбранные новости в красивом формате, например:\n"
        f"1. **[Заголовок]**\n"
        f"   [Суть и почему важно]\n\n"
        f"2. **[Заголовок]** (если выбрано две)\n"
        f"   [Суть и почему важно]"
    )
    try:
        response = await client.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=350,
        )
        return response.strip()
    except Exception as e:
        logger.warning("AI selection of main news failed: %s. Falling back to headlines.", e)
        fallback_lines = []
        for i, h in enumerate(headlines[:2]):
            fallback_lines.append(f"{i+1}. **{h}**\n   Событие дня.")
        return "\n\n".join(fallback_lines)


async def _build_news_topic(
    *,
    interests: str,
    timezone_str: str,
    search_client: Any,
    ai_registry: AIRegistry | None = None,
) -> str:
    """Return a ready-to-use panel topic string for the news."""
    tz = ZoneInfo(timezone_str)
    news_date = _get_news_date(tz)
    date_str = news_date.strftime("%d.%m.%Y")

    headlines = await _fetch_headlines(
        interests=interests,
        timezone_str=timezone_str,
        search_client=search_client,
    )

    if headlines:
        if ai_registry is not None:
            main_news = await _select_main_news(headlines, ai_registry)
        else:
            main_news = "\n".join(f"• {h}" for h in headlines[:2])
        return (
            f"📰 Главные новости дня за {date_str}\n\n"
            f"Темы: {interests}\n\n"
            f"Ключевые события:\n{main_news}\n\n"
            "Давайте обсудим: что важно, что изменится, что делать?"
        )

    return (
        f"📰 Главные новости дня за {date_str}\n\n"
        f"Темы: {interests}\n\n"
        "Обсудите главные события прошедшего дня в этих областях. "
        "Что важно, какие тренды, что это значит для бизнеса и технологий?"
    )


async def _daily_news_loop(
    *,
    panel_time: str,
    timezone_str: str,
    interests: str,
    bots: dict[str, Any],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    panel_chat_id: int,
    search_client: Any,
) -> None:
    """Sleep until the scheduled time, then trigger a single panel round per day."""
    tz = ZoneInfo(timezone_str)
    h, m = map(int, panel_time.split(":"))

    while True:
        now_dt = datetime.now(tz)
        target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)

        delay = (target - now_dt).total_seconds()
        logger.info(
            "Daily news panel: next run in %.0f min at %s",
            delay / 60,
            target.strftime("%d.%m %H:%M"),
        )
        await asyncio.sleep(delay)

        try:
            topic = await _build_news_topic(
                interests=interests,
                timezone_str=timezone_str,
                search_client=search_client,
                ai_registry=ai_registry,
            )
            logger.info("Daily news panel: triggering round")
            from claudebots.routers.panel import PanelRoundRunner, _analyze_topic_and_get_thread  # late import avoids circular
            
            moderator_bot = bots.get("moderator")
            thread_id: int | None = None
            if moderator_bot:
                thread_id = await _analyze_topic_and_get_thread(
                    bot=moderator_bot,
                    chat_id=panel_chat_id,
                    question=topic,
                    ai_registry=ai_registry,
                )
                
            runner = PanelRoundRunner(
                bots=bots,
                personas=personas,
                ai_registry=ai_registry,
                conv=conv,
                alerts=alerts,
                panel_chat_id=panel_chat_id,
                thread_id=thread_id,
                search_client=search_client,
            )
            await runner.run_round(topic, slow=True)
            logger.info("Daily news panel: round completed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Daily news panel: round failed: %s", e)


def start_daily_news_panel(
    *,
    panel_time: str,
    timezone_str: str,
    interests: str,
    bots: dict[str, Any],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    panel_chat_id: int,
    search_client: Any = None,
) -> asyncio.Task[None]:
    """Create and return the daily news panel background task."""
    task: asyncio.Task[None] = asyncio.create_task(
        _daily_news_loop(
            panel_time=panel_time,
            timezone_str=timezone_str,
            interests=interests,
            bots=bots,
            personas=personas,
            ai_registry=ai_registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=panel_chat_id,
            search_client=search_client,
        )
    )
    task.add_done_callback(
        lambda t: logger.warning("Daily news panel task raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info(
        "Daily news panel started: time=%s %s, interests=%r",
        panel_time,
        timezone_str,
        interests,
    )
    return task
