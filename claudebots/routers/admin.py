import logging
from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from claudebots.core.claude_client import ClaudeClient
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
        # Direct DM to one of the bots, no business connection: nothing to reset by convention
        await message.answer("Nothing to reset in this chat.")
        return
    conv.reset(key)
    await message.answer(f"✅ Reset: {key}")


@admin_router.message(Command("cost"))
async def _cost(message: Message, claude: ClaudeClient, settings: Settings) -> None:
    await handle_cost(message, claude, settings)


async def handle_cost(message: Message, claude: ClaudeClient, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    u = claude.usage
    # Approximate Sonnet pricing in USD; numeric constants are display-only.
    in_cost = u["input"] / 1_000_000 * 3.0
    out_cost = u["output"] / 1_000_000 * 15.0
    cache_savings = u["cache_read"] / 1_000_000 * (3.0 - 0.30)
    text = (
        f"📊 Session tokens:\n"
        f"  input={u['input']}  output={u['output']}  cache_read={u['cache_read']}\n"
        f"≈ ${in_cost + out_cost:.4f} (cache saved ≈ ${cache_savings:.4f})"
    )
    await message.answer(text)


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
