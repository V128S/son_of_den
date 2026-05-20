import asyncio
import logging
import random
import re
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.conversation import ConversationStore
from claudebots.core.personas import PersonaRegistry

logger = logging.getLogger(__name__)

panel_router = Router(name="panel")

# Module-level singleton — at most one active round per process
_active_round: asyncio.Task[None] | None = None

# Discussion settings
MAX_DISCUSSION_MESSAGES = 7
MIN_DELAY_SECONDS = 3.0
MAX_DELAY_SECONDS = 3.0
TYPING_DURATION = 0.4  # Total animation time in seconds
TYPING_UPDATES = 6  # Number of message edits during animation


def clean_markdown(text: str) -> str:
    """Remove markdown formatting symbols for cleaner Telegram output."""
    # Remove headers (# ## ###)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic markers (* ** *** _)
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    # Remove inline code backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Clean up extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


@dataclass
class PanelRoundRunner:
    bots: dict[str, Bot]
    personas: PersonaRegistry
    ai_registry: AIRegistry
    conv: ConversationStore
    alerts: AlertSender
    panel_chat_id: int
    thread_id: int | None = None
    typing_sleep_seconds: float = 0.1

    def _key(self) -> str:
        return f"panel:{self.panel_chat_id}"

    async def _send(self, bot: Bot, text: str) -> None:
        """Send message to the correct thread."""
        await bot.send_message(
            self.panel_chat_id,
            text,
            message_thread_id=self.thread_id,
        )

    async def _animate_typing(self, bot: Bot, text: str) -> None:
        """Show text gradually over TYPING_DURATION seconds."""
        if self.typing_sleep_seconds == 0:
            await bot.send_message(
                self.panel_chat_id,
                text,
                message_thread_id=self.thread_id,
            )
            return

        msg = await bot.send_message(
            self.panel_chat_id,
            "▌",
            message_thread_id=self.thread_id,
        )

        delay = TYPING_DURATION / TYPING_UPDATES
        chars_per_step = max(1, len(text) // TYPING_UPDATES)

        for i in range(1, TYPING_UPDATES + 1):
            await asyncio.sleep(delay)
            end_pos = min(i * chars_per_step, len(text))

            if i < TYPING_UPDATES:
                display = text[:end_pos] + "▌"
            else:
                display = text

            try:
                await msg.edit_text(display)
            except Exception:
                pass

    async def _speak(self, persona, key: str, turn_number: int) -> bool:
        """Have a persona speak with typing animation. Returns True if successful."""
        speaker_bot = self.bots[persona.id]

        try:
            await speaker_bot.send_chat_action(
                self.panel_chat_id,
                "typing",
                message_thread_id=self.thread_id,
            )
        except Exception as e:
            logger.debug("chat_action failed for %s: %s", persona.id, e)

        # Delay before responding (thinking time)
        if self.typing_sleep_seconds > 0:
            delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            await asyncio.sleep(delay)

        # Build messages with explicit instruction to respond
        messages = list(self.conv.get(key))
        messages.append({
            "role": "user",
            "content": f"{persona.name}, твой ход. Кратко (2-3 предложения). Не соглашайся автоматически - спорь, уточняй, предлагай своё. Без markdown."
        })

        try:
            client = self.ai_registry.get_client(persona.provider)

            # Get full response first
            response = await client.complete(
                system=persona.system_prompt,
                messages=messages,
                max_tokens=persona.max_tokens,
            )

            clean_response = clean_markdown(response)
            if not clean_response.strip():
                logger.warning("Panel persona %s (%s) returned empty response", persona.id, persona.provider)
                return False

            # Animate typing
            await self._animate_typing(speaker_bot, clean_response)
            self.conv.add(key, "assistant", f"[{persona.name}]: {clean_response}")
            return True

        except Exception as e:
            logger.warning("Panel persona %s (%s) failed: %s", persona.id, persona.provider, e)
            await self.alerts.send(f"panel_{persona.id}", f"{type(e).__name__}: {e}")
            return False

    async def run_round(self, topic: str) -> None:
        key = self._key()
        self.conv.reset(key)

        # Initial topic with discussion context
        discussion_context = (
            f"Тема дискуссии: {topic}\n\n"
            "Правила дискуссии:\n"
            "- Имей собственную позицию, не соглашайся просто так\n"
            "- Если не согласен - аргументируй почему\n"
            "- Добавляй новые мысли, не повторяй сказанное\n"
            "- Пиши живым языком, как в реальном разговоре"
        )
        self.conv.add(key, "user", discussion_context)

        moderator_bot = self.bots["moderator"]
        await self._send(moderator_bot, f"🎬 Раунд: {topic}\n\n💬 Дискуссия...")

        speakers = list(self.personas.panel_speakers)
        message_count = 0
        speaker_idx = 0
        consecutive_failures = 0

        # Discussion loop
        while message_count < MAX_DISCUSSION_MESSAGES:
            persona = speakers[speaker_idx % len(speakers)]

            success = await self._speak(persona, key, message_count + 1)
            if success:
                message_count += 1
                consecutive_failures = 0
                logger.info("Discussion message %d/%d from %s", message_count, MAX_DISCUSSION_MESSAGES, persona.id)
            else:
                consecutive_failures += 1
                # If too many failures in a row, stop
                if consecutive_failures >= len(speakers):
                    logger.warning("Too many consecutive failures, ending discussion early")
                    break

            speaker_idx += 1

        # Moderator summary
        mod = self.personas.moderator
        if mod is None:
            return

        try:
            await moderator_bot.send_chat_action(
                self.panel_chat_id,
                "typing",
                message_thread_id=self.thread_id,
            )
        except Exception:
            pass
        await asyncio.sleep(2.0)

        try:
            mod_client = self.ai_registry.get_client(mod.provider)

            # Add explicit request for summary
            mod_messages = list(self.conv.get(key))
            mod_messages.append({
                "role": "user",
                "content": "Подведи краткий итог дискуссии (3-5 предложений). Выдели главное и дай рекомендацию. Пиши просто, без markdown."
            })

            # Get full response
            summary = await mod_client.complete(
                system=mod.system_prompt,
                messages=mod_messages,
                max_tokens=mod.max_tokens,
            )

            clean_summary = clean_markdown(summary)
            if not clean_summary.strip():
                await self._send(moderator_bot, mod.fallback)
                logger.warning("Moderator returned empty summary")
            else:
                summary_text = f"📋 Итог:\n{clean_summary}\n\n🎤 Жду следующую тему."
                # Animate moderator typing too
                await self._animate_typing(moderator_bot, summary_text)
                logger.info("Moderator summary: %d chars", len(summary_text))

        except Exception as e:
            logger.warning("Moderator (%s) failed: %s", mod.provider, e)
            await self.alerts.send("panel_moderator", f"{type(e).__name__}: {e}")
            await self._send(moderator_bot, mod.fallback)


@panel_router.message(F.text)
async def _on_panel_message(
    message: Message,
    bots: dict[str, Bot],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    settings,  # injected, type hint avoided to keep router file out of mypy strict scope
) -> None:
    # Debug logging
    logger.info(
        "Panel message received: chat_id=%s (expected %s), user_id=%s (expected %s), thread_id=%s",
        message.chat.id,
        settings.panel_chat_id,
        message.from_user.id if message.from_user else None,
        settings.admin_user_id,
        message.message_thread_id,
    )

    # Filter: only the panel group, only admin user, ignore bots/non-text
    if message.chat.id != settings.panel_chat_id:
        return
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    if message.from_user.is_bot:
        return

    global _active_round
    if _active_round and not _active_round.done():
        _active_round.cancel()

    runner = PanelRoundRunner(
        bots=bots,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts,
        panel_chat_id=settings.panel_chat_id,
        thread_id=message.message_thread_id,
    )
    _active_round = asyncio.create_task(runner.run_round(message.text or ""))
