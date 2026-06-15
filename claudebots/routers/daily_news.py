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

if TYPE_CHECKING:
    from claudebots.core.ai_registry import AIRegistry
    from claudebots.core.alerts import AlertSender
    from claudebots.core.conversation import ConversationStore
    from claudebots.core.personas import PersonaRegistry

logger = logging.getLogger(__name__)


async def _fetch_headlines(
    *,
    interests: str,
    timezone_str: str,
    search_client: Any,
) -> list[str]:
    """Return a list of headline strings for yesterday using Exa search."""
    if search_client is None or not getattr(search_client, "enabled", False):
        return []
    tz = ZoneInfo(timezone_str)
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%d.%m.%Y")
    query = f"главные новости {yesterday} {interests}"
    try:
        results = await search_client.search(query, num_results=5)
        return [r.title for r in results if r.title]
    except Exception as e:
        logger.warning("Daily news: headline search failed: %s", e)
        return []


async def _build_news_topic(
    *,
    interests: str,
    timezone_str: str,
    search_client: Any,
) -> str:
    """Return a ready-to-use panel topic string for yesterday's news."""
    tz = ZoneInfo(timezone_str)
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%d.%m.%Y")

    headlines = await _fetch_headlines(
        interests=interests,
        timezone_str=timezone_str,
        search_client=search_client,
    )

    if headlines:
        headlines_str = "\n".join(f"• {h}" for h in headlines[:5])
        return (
            f"📰 Новостной обзор за {yesterday}\n\n"
            f"Темы: {interests}\n\n"
            f"Ключевые заголовки вчерашнего дня:\n{headlines_str}\n\n"
            "Обсудите: что важно, что изменится, что делать?"
        )

    return (
        f"📰 Новостной обзор за {yesterday}\n\n"
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
            )
            logger.info("Daily news panel: triggering round")
            from claudebots.routers.panel import PanelRoundRunner  # late import avoids circular
            runner = PanelRoundRunner(
                bots=bots,
                personas=personas,
                ai_registry=ai_registry,
                conv=conv,
                alerts=alerts,
                panel_chat_id=panel_chat_id,
                thread_id=None,
                search_client=search_client,
            )
            await runner.run_round(topic)
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
) -> "asyncio.Task[None]":
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
