import logging
from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.config import Settings
from claudebots.core.conversation import ConversationStore
from claudebots.core.personas import PersonaRegistry, load_personas

logger = logging.getLogger(__name__)

admin_router = Router(name="admin")


@dataclass
class PersonaHolder:
    """Mutable wrapper so /reload can swap the registry without restarting."""

    registry: PersonaRegistry


@admin_router.message(Command("ping"))
async def _ping(message: Message, settings: Settings) -> None:
    await handle_ping(message, settings)


async def handle_ping(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    await message.answer("pong")


@admin_router.message(Command("reset"))
async def _reset(message: Message, conv: ConversationStore, settings: Settings) -> None:
    await handle_reset(message, conv, settings)


async def handle_reset(message: Message, conv: ConversationStore, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    biz_id = getattr(message, "business_connection_id", None)
    if biz_id:
        key = f"biz:{biz_id}:{message.chat.id}"
    elif message.chat.id == settings.panel_chat_id:
        key = f"panel:{settings.panel_chat_id}"
    else:
        # Private chat with the bot — use thread_id if available
        thread_id = message.message_thread_id or 0
        key = f"private:{message.chat.id}:{thread_id}"
    conv.reset(key)
    await message.answer(f"✅ Reset: {key}")


@admin_router.message(Command("cost"))
async def _cost(message: Message, ai_registry: AIRegistry, settings: Settings) -> None:
    await handle_cost(message, ai_registry, settings)


async def handle_cost(message: Message, ai_registry: AIRegistry, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return

    # \u2500\u2500 Today's usage \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    daily_by_provider = ai_registry.get_daily_usage_by_provider()
    daily_total = ai_registry.get_daily_total_usage()
    lines = ["\U0001f4ca \u0422\u043e\u043a\u0435\u043d\u044b \u0441\u0435\u0433\u043e\u0434\u043d\u044f:\n"]
    for name, u in daily_by_provider.items():
        if u["input"] == 0 and u["output"] == 0:
            continue
        lines.append(f"  {name}: in={u['input']}  out={u['output']}  cache={u['cache_read']}")

    if daily_total["input"] == 0 and daily_total["output"] == 0:
        lines.append("  (\u043d\u0435\u0442 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u044f \u0441\u0435\u0433\u043e\u0434\u043d\u044f)")
    else:
        lines.append(f"\n  \u0414\u0435\u043d\u044c: in={daily_total['input']}  out={daily_total['output']}  cache={daily_total['cache_read']}")
        d_in = daily_total["input"] / 1_000_000 * 3.0
        d_out = daily_total["output"] / 1_000_000 * 15.0
        lines.append(f"\u2248 ${d_in + d_out:.4f} \u0437\u0430 \u0434\u0435\u043d\u044c")

    # \u2500\u2500 All-time usage \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    total = ai_registry.get_total_usage()
    by_provider = ai_registry.get_usage_by_provider()
    lines.append("\n\U0001f4ca \u0412\u0441\u0435\u0433\u043e \u043d\u0430\u043a\u043e\u043f\u043b\u0435\u043d\u043e:\n")
    for name, u in by_provider.items():
        if u["input"] == 0 and u["output"] == 0:
            continue
        lines.append(f"  {name}: in={u['input']}  out={u['output']}  cache={u['cache_read']}")

    lines.append(f"\n  \u0418\u0422\u041e\u0413\u041e: in={total['input']}  out={total['output']}  cache={total['cache_read']}")
    in_cost = total["input"] / 1_000_000 * 3.0
    out_cost = total["output"] / 1_000_000 * 15.0
    cache_savings = total["cache_read"] / 1_000_000 * (3.0 - 0.30)
    lines.append(f"\u2248 ${in_cost + out_cost:.4f} \u0432\u0441\u0435\u0433\u043e (\u043a\u044d\u0448 \u0441\u044d\u043a\u043e\u043d\u043e\u043c\u0438\u043b \u2248 ${cache_savings:.4f})")

    await message.answer("\n".join(lines))


@admin_router.message(Command("contacts"))
async def _contacts(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.business import get_contacts_summary  # noqa: PLC0415
    text = get_contacts_summary()
    await message.answer(text, parse_mode=None)


@admin_router.message(Command("stats"))
async def _stats(message: Message, ai_registry: AIRegistry, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.business import _contact_data, _contact_today  # noqa: PLC0415
    from claudebots.routers.panel import _panel_memories, _panel_topics, get_panel_ratings_summary  # noqa: PLC0415

    daily_total = ai_registry.get_daily_total_usage()
    all_total = ai_registry.get_total_usage()
    ratings = get_panel_ratings_summary()

    # Most-discussed category from panel memories
    from collections import Counter  # noqa: PLC0415
    topic_counts: Counter = Counter(
        m.get("topic", "") for m in _panel_memories if m.get("topic")
    )
    top_topic = topic_counts.most_common(1)
    top_str = f"{top_topic[0][0]} ({top_topic[0][1]})" if top_topic else "—"

    lines = [
        "📊 Статистика\n",
        f"Контакты всего: {len(_contact_data)}",
        f"Активны сегодня: {len(_contact_today)}",
        f"Топики панели: {len(_panel_topics)}",
        f"Памяти панели: {len(_panel_memories)}",
        f"Топ тема: {top_str}",
        f"Оценок раундов: 👍{ratings['good']} 👎{ratings['bad']} (всего {ratings['total']})",
        "",
        f"Токены сегодня: in={daily_total['input']}  out={daily_total['output']}",
        f"Токены всего: in={all_total['input']}  out={all_total['output']}",
    ]
    await message.answer("\n".join(lines), parse_mode=None)


@admin_router.message(Command("panelfind"))
async def _panelfind(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.panel import _panel_memories  # noqa: PLC0415

    query = (message.text or "").removeprefix("/panelfind").strip().lower()
    if not query:
        await message.answer("Использование: /panelfind <ключевое слово>", parse_mode=None)
        return

    hits = [
        m for m in _panel_memories
        if query in m.get("text", "").lower() or query in m.get("topic", "").lower()
    ]
    if not hits:
        await message.answer(f"Ничего не найдено по запросу «{query}».", parse_mode=None)
        return

    lines = [f"🔍 Найдено {len(hits)} запис(ей) по «{query}»:\n"]
    for m in reversed(hits[-10:]):  # показываем последние 10 совпадений, новые сверху
        topic = m.get("topic") or "—"
        snippet = m.get("text", "")[:200].replace("\n", " ")
        lines.append(f"[{topic}] {snippet}")
        lines.append("")
    await message.answer("\n".join(lines), parse_mode=None)


@admin_router.message(Command("panelschedule"))
async def _panelschedule(
    message: Message,
    bots: dict,
    personas,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts,
    settings: Settings,
    search_client=None,
) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return

    text = (message.text or "").removeprefix("/panelschedule").strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /panelschedule HH:MM Тема обсуждения\n"
            "Пример: /panelschedule 15:30 Будущее ИИ в бизнесе",
            parse_mode=None,
        )
        return

    time_str, topic = parts[0], parts[1].strip()
    if not topic:
        await message.answer("Укажи тему после времени.", parse_mode=None)
        return

    try:
        import zoneinfo  # noqa: PLC0415
        from datetime import datetime, timedelta  # noqa: PLC0415
        tz = zoneinfo.ZoneInfo(settings.user_timezone)
        now = datetime.now(tz)
        t = datetime.strptime(time_str, "%H:%M")
        target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
    except ValueError:
        await message.answer(
            "Неверный формат времени. Используй HH:MM, например 15:30.",
            parse_mode=None,
        )
        return

    from claudebots.routers.panel import schedule_panel_round, get_scheduled_panel, _last_thread_id  # noqa: PLC0415

    # Warn if replacing an existing schedule
    existing = get_scheduled_panel()
    schedule_panel_round(
        delay=delay,
        bots=bots,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts,
        chat_id=settings.panel_chat_id,
        topic=topic,
        thread_id=_last_thread_id,
        search_client=search_client,
        fire_at_str=time_str,
    )

    minutes = int(delay // 60)
    hours, mins = divmod(minutes, 60)
    eta = f"{hours} ч {mins} мин" if hours else f"{mins} мин"
    lines = [f"✅ Раунд запланирован на {time_str} (через {eta})", f"Тема: {topic}"]
    if existing:
        lines.append(f"⚠️ Предыдущий раунд «{existing['topic'][:50]}» отменён")
    await message.answer("\n".join(lines), parse_mode=None)


@admin_router.message(Command("panelcancel"))
async def _panelcancel(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.panel import cancel_scheduled_panel, get_scheduled_panel  # noqa: PLC0415
    info = get_scheduled_panel()
    if cancel_scheduled_panel():
        topic = info["topic"][:60] if info else "—"
        await message.answer(f"❌ Запланированный раунд отменён\nТема: {topic}", parse_mode=None)
    else:
        await message.answer("Нет запланированных раундов.", parse_mode=None)


@admin_router.message(Command("panelbest"))
async def _panelbest(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.panel import get_rated_rounds  # noqa: PLC0415
    rounds = get_rated_rounds("good", limit=7)
    if not rounds:
        await message.answer("Ещё нет раундов с оценкой 👍.", parse_mode=None)
        return
    lines = [f"🏆 Топ раундов (👍 {len(rounds)} из последних):\n"]
    for r in rounds:
        topic = r["topic"][:60] or "—"
        lines.append(f"📌 {topic}")
        if r["memory"]:
            lines.append(f"   💡 {r['memory'][:120]}")
        lines.append("")
    await message.answer("\n".join(lines).rstrip(), parse_mode=None)


@admin_router.message(Command("panelworst"))
async def _panelworst(message: Message, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    from claudebots.routers.panel import get_rated_rounds  # noqa: PLC0415
    rounds = get_rated_rounds("bad", limit=5)
    if not rounds:
        await message.answer("Нет раундов с оценкой 👎.", parse_mode=None)
        return
    lines = [f"👎 Раунды, которые стоит углубить ({len(rounds)}):\n"]
    for r in rounds:
        topic = r["topic"][:60] or "—"
        lines.append(f"📌 {topic}")
        if r["memory"]:
            lines.append(f"   💡 {r['memory'][:120]}")
        lines.append("")
    await message.answer("\n".join(lines).rstrip(), parse_mode=None)


@admin_router.message(Command("reload"))
async def _reload(message: Message, persona_holder: PersonaHolder, settings: Settings) -> None:
    await handle_reload(message, persona_holder, settings)


async def handle_reload(
    message: Message, persona_holder: PersonaHolder, settings: Settings
) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    try:
        new_reg = load_personas(settings.personas_path)
    except Exception as e:
        await message.answer(f"❌ persona reload failed: {e}")
        return
    persona_holder.registry = new_reg
    await message.answer("✅ personas reloaded")
