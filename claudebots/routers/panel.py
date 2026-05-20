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
_processing_lock = asyncio.Lock()

# Cache for topic_id -> topic_name mapping
_panel_topics: dict[int, str] = {}

# Discussion settings
MAX_DISCUSSION_MESSAGES = 4  # Shorter discussions, more focused
DELAY_BETWEEN_MESSAGES = 1.5  # Delay between different bot messages


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


async def _analyze_topic_and_get_thread(
    bot: Bot,
    chat_id: int,
    question: str,
    ai_registry: AIRegistry,
) -> int | None:
    """Analyze question topic and get or create appropriate thread."""
    # Get AI client for topic analysis
    try:
        client = ai_registry.get_client("openrouter_gemini")

        # Build context with existing topics
        topics_context = ""
        if _panel_topics:
            topics_context = "\n\nСуществующие топики:\n"
            for topic_id, topic_name in _panel_topics.items():
                topics_context += f"- {topic_name}\n"

        # Ask AI to categorize
        prompt = (
            f"Вопрос пользователя: {question}\n\n"
            f"{topics_context}\n"
            "Задача: определи ОБЩУЮ тему этого вопроса (2-4 слова + эмодзи).\n\n"
            "Правила:\n"
            "- Если тема совпадает с существующим топиком - верни ТОЧНО его название\n"
            "- Если тема новая - придумай короткое название (2-4 слова) + подходящий эмодзи в начале\n"
            "- Делай категории широкими: 'Бизнес', 'Маркетинг', 'Технологии', 'Продукт', 'Стратегия'\n"
            "- Формат: 'эмодзи Название'\n\n"
            "Верни ТОЛЬКО название топика, без объяснений."
        )

        topic_name = await client.complete(
            system="Ты помощник для категоризации вопросов. Отвечай кратко, только название топика.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
        )

        topic_name = topic_name.strip().strip('"').strip("'")

        # Check if this topic already exists
        for topic_id, name in _panel_topics.items():
            if name.lower() == topic_name.lower():
                logger.info("Using existing topic: %s (id=%d)", topic_name, topic_id)
                return topic_id

        # Create new topic
        try:
            bot_info = await bot.get_me()
            logger.info("Attempting to create topic '%s' using bot @%s", topic_name, bot_info.username)

            forum_topic = await bot.create_forum_topic(
                chat_id=chat_id,
                name=topic_name[:128],  # Telegram limit
            )
            _panel_topics[forum_topic.message_thread_id] = topic_name
            logger.info("Created new topic: %s (id=%d)", topic_name, forum_topic.message_thread_id)
            return forum_topic.message_thread_id
        except Exception as e:
            logger.warning("Failed to create forum topic with bot @%s: %s", bot_info.username if 'bot_info' in locals() else 'unknown', e)
            return None

    except Exception as e:
        logger.warning("Topic analysis failed: %s", e)
        return None


@dataclass
class PanelRoundRunner:
    bots: dict[str, Bot]
    personas: PersonaRegistry
    ai_registry: AIRegistry
    conv: ConversationStore
    alerts: AlertSender
    panel_chat_id: int
    thread_id: int | None = None

    def _key(self) -> str:
        return f"panel:{self.panel_chat_id}"

    async def _send(self, bot: Bot, text: str) -> None:
        """Send message to the correct thread."""
        await bot.send_message(
            self.panel_chat_id,
            text,
            message_thread_id=self.thread_id,
        )

    async def _speak(self, persona, key: str) -> bool:
        """Have a persona speak. Returns True if successful."""
        speaker_bot = self.bots[persona.id]

        # Build messages with explicit instruction to respond
        messages = list(self.conv.get(key))
        messages.append({
            "role": "user",
            "content": (
                f"{persona.name}, твой ход. Кратко (2-3 предложения).\n"
                "- Развивай идеи других, добавляй свой взгляд\n"
                "- Будь конструктивен: предлагай конкретику\n"
                "- Спорь только если видишь реальную проблему\n"
                "- Учитывай весь контекст обсуждения\n"
                "- Без markdown"
            )
        })

        try:
            client = self.ai_registry.get_client(persona.provider)

            response = await client.complete(
                system=persona.system_prompt,
                messages=messages,
                max_tokens=persona.max_tokens,
            )

            clean_response = clean_markdown(response)
            if not clean_response.strip():
                logger.warning("Panel persona %s (%s) returned empty response", persona.id, persona.provider)
                return False

            # Send message immediately
            await self._send(speaker_bot, clean_response)
            self.conv.add(key, "assistant", f"[{persona.name}]: {clean_response}")
            return True

        except Exception as e:
            logger.warning("Panel persona %s (%s) failed: %s", persona.id, persona.provider, e)
            await self.alerts.send(f"panel_{persona.id}", f"{type(e).__name__}: {e}")
            return False

    async def run_round(self, topic: str) -> None:
        key = self._key()

        # Keep conversation history, just add new topic
        # This allows bots to reference previous discussions
        discussion_context = (
            f"Новое сообщение от пользователя: {topic}\n\n"
            "Правила дискуссии:\n"
            "- Отвечай на вопрос с учётом предыдущего контекста\n"
            "- СТРОЙ на идеях других, развивай их мысли\n"
            "- Спорь только если реально видишь серьёзную ошибку\n"
            "- Добавляй что-то новое и конкретное к обсуждению\n"
            "- Будь конструктивен, предлагай решения\n"
            "- Пиши живым языком, коротко и по делу"
        )
        self.conv.add(key, "user", discussion_context)

        # Keep only last 50 messages to avoid context overflow
        # This preserves ~2-3 rounds of discussion
        self.conv.trim(key, keep_last=50)

        moderator_bot = self.bots["moderator"]
        await self._send(moderator_bot, f"🎬 Раунд: {topic}\n\n💬 Дискуссия...")

        speakers = list(self.personas.panel_speakers)
        message_count = 0
        speaker_idx = 0
        consecutive_failures = 0

        # Discussion loop
        while message_count < MAX_DISCUSSION_MESSAGES:
            persona = speakers[speaker_idx % len(speakers)]

            success = await self._speak(persona, key)
            if success:
                message_count += 1
                consecutive_failures = 0
                logger.info("Discussion message %d/%d from %s", message_count, MAX_DISCUSSION_MESSAGES, persona.id)

                # Delay between messages
                if message_count < MAX_DISCUSSION_MESSAGES:
                    await asyncio.sleep(DELAY_BETWEEN_MESSAGES)
            else:
                consecutive_failures += 1
                if consecutive_failures >= len(speakers):
                    logger.warning("Too many consecutive failures, ending discussion early")
                    break

            speaker_idx += 1

        # Moderator summary
        mod = self.personas.moderator
        if mod is None:
            return

        await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

        try:
            mod_client = self.ai_registry.get_client(mod.provider)

            mod_messages = list(self.conv.get(key))
            mod_messages.append({
                "role": "user",
                "content": (
                    "Подведи КОНКРЕТНЫЙ итог дискуссии (3-4 предложения):\n"
                    "1. Главный вывод одним предложением\n"
                    "2. Конкретная рекомендация - ЧТО делать\n"
                    "3. Если есть разногласия - выбери лучший вариант и объясни почему\n"
                    "Будь решительным и конкретным. Без воды и общих фраз. Без markdown."
                )
            })

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
                await self._send(moderator_bot, summary_text)
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
    settings,
) -> None:
    if message.chat.id != settings.panel_chat_id:
        return
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    if message.from_user.is_bot:
        return

    global _active_round

    # Use lock to ensure only one bot processes the message
    if _processing_lock.locked():
        logger.debug("Skipping duplicate panel message - already processing")
        return

    async with _processing_lock:
        # Double-check after acquiring lock
        if _active_round and not _active_round.done():
            logger.debug("Round already active, skipping")
            return

        logger.info(
            "Panel message accepted: chat_id=%s, user_id=%s, thread_id=%s",
            message.chat.id,
            message.from_user.id,
            message.message_thread_id,
        )

        # Determine thread_id: use existing or create/find appropriate topic
        thread_id = message.message_thread_id
        if not thread_id:
            # Message sent to main chat - analyze and categorize
            moderator_bot = bots.get("moderator")
            if moderator_bot:
                thread_id = await _analyze_topic_and_get_thread(
                    bot=moderator_bot,
                    chat_id=settings.panel_chat_id,
                    question=message.text or "",
                    ai_registry=ai_registry,
                )

        runner = PanelRoundRunner(
            bots=bots,
            personas=personas,
            ai_registry=ai_registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=settings.panel_chat_id,
            thread_id=thread_id,
        )
        _active_round = asyncio.create_task(runner.run_round(message.text or ""))
