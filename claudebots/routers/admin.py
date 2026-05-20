import logging
from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
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

    lines = ["\U0001f4ca Session tokens:\n"]
    total = ai_registry.get_total_usage()
    by_provider = ai_registry.get_usage_by_provider()

    for name, u in by_provider.items():
        if u["input"] == 0 and u["output"] == 0:
            continue
        lines.append(f"  {name}: in={u['input']}  out={u['output']}  cache={u['cache_read']}")

    lines.append(f"\n  TOTAL: in={total['input']}  out={total['output']}  cache={total['cache_read']}")

    # Rough cost estimate (varies by model, this is indicative)
    in_cost = total["input"] / 1_000_000 * 3.0
    out_cost = total["output"] / 1_000_000 * 15.0
    cache_savings = total["cache_read"] / 1_000_000 * (3.0 - 0.30)
    lines.append(f"\u2248 ${in_cost + out_cost:.4f} (cache saved \u2248 ${cache_savings:.4f})")

    await message.answer("\n".join(lines))


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
