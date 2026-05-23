import asyncio
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import Message

from claudebots.core.ai_registry import AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.conversation import ConversationStore
from claudebots.core.personas import PersonaRegistry
from claudebots.core import state as _state

logger = logging.getLogger(__name__)

panel_router = Router(name="panel")

# Module-level singleton — at most one active round per process
_active_round: asyncio.Task[None] | None = None
_processing_lock = asyncio.Lock()

# Cache for topic_id -> topic_name mapping
_panel_topics: dict[int, str] = {}

# Last thread used in a panel round — revival posts here by default
_last_thread_id: int | None = None

# Path to the bot state JSON file — set by init_panel_state() at startup
_state_path: Path | None = None

# Discussion settings
MAX_DISCUSSION_MESSAGES = 4  # shorter discussions, more focused

# Human-realistic timing — simulates reading, thinking, and composing time.
# Tests zero these via the integration conftest autouse fixture.
REPLY_DELAY_MIN: float = 15.0    # seconds between speakers (min)
REPLY_DELAY_MAX: float = 35.0    # seconds between speakers (max)
REVIVAL_DELAY_MIN: float = 30.0  # seconds for revival continuation (min)
REVIVAL_DELAY_MAX: float = 70.0  # seconds for revival continuation (max)
TYPING_DELAY_MIN: float = 2.0    # seconds of visible typing indicator (min)
TYPING_DELAY_MAX: float = 4.5    # seconds of visible typing indicator (max)

# Revival settings
REVIVAL_INTERVAL_SECONDS = 7_200  # Default: every 2 hours
REVIVAL_JITTER_SECONDS = 1_800    # ±30 min randomness

# Panel memory: compact takeaways from past rounds
_panel_memories: list[str] = []
PANEL_MEMORY_MAX = 7

# Thread ID for the ✅ Задачи topic in the panel group
_tasks_thread_id: int | None = None

# Instruction appended to every speaker turn — extracted to avoid repetition
_SPEAKER_TURN_INSTRUCTION = (
    "{name}, твой ход — 2-3 предложения живым языком.\n"
    "Реагируй на то, что сказали другие: развивай, уточняй или мягко возражай. "
    "Без заголовков и маркированных списков."
)

# Revival-specific instructions — casual, spontaneous feel
_REVIVAL_INITIATOR_INSTRUCTION = (
    "{name}, у тебя появилась новая мысль по прошлой теме — как будто только что дошло. "
    "Вырази её коротко и неформально, 1-2 предложения. "
    "Не объясняй, что «возвращаешься к теме» — просто скажи мысль. Без markdown."
)

_REVIVAL_RESPONDER_INSTRUCTION = (
    "{name}, ты слышишь мысль коллеги и реагируешь — поддерживаешь или уточняешь. "
    "1-2 предложения, по делу. Без markdown."
)


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


def _persist_panel_state() -> None:
    """Save current panel state to disk. No-op if state path not set."""
    if _state_path is None:
        return
    _state.update(_state_path, {
        "panel_topics": _state.encode_int_keys(_panel_topics),
        "tasks_thread_id": _tasks_thread_id,
        "last_thread_id": _last_thread_id,
        "panel_memories": list(_panel_memories),
    })


def init_panel_state(path: Path, data: dict) -> None:
    """Restore panel topic state from persisted data.  Call once at startup."""
    global _state_path, _tasks_thread_id, _last_thread_id
    _state_path = path

    raw_topics = data.get("panel_topics", {})
    restored = _state.decode_int_keys(raw_topics)
    _panel_topics.update(restored)

    tasks_tid = data.get("tasks_thread_id")
    if isinstance(tasks_tid, int):
        _tasks_thread_id = tasks_tid

    last_tid = data.get("last_thread_id")
    if isinstance(last_tid, int):
        _last_thread_id = last_tid

    mems = data.get("panel_memories", [])
    if isinstance(mems, list):
        _panel_memories.extend(m for m in mems if isinstance(m, str))
        # Trim to max
        while len(_panel_memories) > PANEL_MEMORY_MAX:
            _panel_memories.pop(0)

    logger.info(
        "Panel state restored: %d topics, tasks_thread=%s, %d memories",
        len(_panel_topics), _tasks_thread_id, len(_panel_memories),
    )


# Fixed topic categories for the panel — prevents the AI from using
# the user's question text as the topic name.
_PANEL_CATEGORIES = [
    "💼 Бизнес",
    "📢 Маркетинг",
    "🔧 Технологии",
    "📦 Продукт",
    "🎯 Стратегия",
    "👥 Команда",
    "💰 Финансы",
    "🌐 Рынок",
    "🛠 Операции",
    "📝 Разное",
]


async def _analyze_topic_and_get_thread(
    bot: Bot,
    chat_id: int,
    question: str,
    ai_registry: AIRegistry,
) -> int | None:
    """Pick one fixed category for the question and return its panel thread_id."""
    try:
        client = ai_registry.get_client("openrouter_gemini")

        cats = "\n".join(f"- {c}" for c in _PANEL_CATEGORIES)
        existing = ""
        if _panel_topics:
            names = list(_panel_topics.values())
            # Only show categories that already have a thread
            known = [n for n in names if n in _PANEL_CATEGORIES]
            if known:
                existing = "Уже созданные топики (используй один из них, если подходит):\n"
                existing += "\n".join(f"  {n}" for n in known) + "\n\n"

        prompt = (
            f"{existing}"
            f"Выбери ОДНУ категорию из списка для этого вопроса:\n"
            f"{cats}\n\n"
            f"Вопрос: {question[:200]}\n\n"
            "Ответь СТРОГО одной строкой из списка, слово в слово."
        )

        raw = await client.complete(
            system="Классификатор тем. Возвращай только одну строку из списка без изменений.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=12,
        )
        candidate = raw.strip().strip('"').strip("'").split("\n")[0].strip()

        # Match against fixed list
        topic_name = "📝 Разное"
        for cat in _PANEL_CATEGORIES:
            if cat in candidate or candidate in cat:
                topic_name = cat
                break

        logger.info("Panel topic selected: %r (raw=%r)", topic_name, candidate)

        # Return existing thread if already created for this category
        for tid, name in _panel_topics.items():
            if name.lower() == topic_name.lower():
                logger.info("Reusing existing panel topic: %s (id=%d)", topic_name, tid)
                return tid

        # Create new forum topic for this category
        try:
            forum_topic = await bot.create_forum_topic(
                chat_id=chat_id,
                name=topic_name,
            )
            thread_id = forum_topic.message_thread_id
            _panel_topics[thread_id] = topic_name
            logger.info("Created panel topic: %s (id=%d)", topic_name, thread_id)
            _persist_panel_state()
            return thread_id
        except Exception as e:
            logger.warning("Failed to create panel forum topic %r: %s", topic_name, e)
            return None

    except Exception as e:
        logger.warning("Panel topic analysis failed: %s", e)
        return None



async def _get_or_create_tasks_thread(bot: "Bot", chat_id: int) -> int | None:
    """Return thread_id for the ✅ Задачи topic, creating it on first use."""
    global _tasks_thread_id
    if _tasks_thread_id is not None:
        return _tasks_thread_id
    # Check in-memory topic cache first (avoids duplicate on edge cases)
    for tid, name in _panel_topics.items():
        if name == "✅ Задачи":
            _tasks_thread_id = tid
            return tid
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name="✅ Задачи")
        _tasks_thread_id = topic.message_thread_id
        logger.info("Created panel tasks topic (id=%d)", _tasks_thread_id)
        _persist_panel_state()
        return _tasks_thread_id
    except Exception as e:
        logger.warning("Failed to create panel tasks topic: %s", e)
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
        """Send message with a typing indicator to simulate composing a response."""
        logger.debug("_send: chat=%s thread=%s len=%d", self.panel_chat_id, self.thread_id, len(text))
        try:
            await bot.send_chat_action(
                chat_id=self.panel_chat_id,
                action="typing",
                message_thread_id=self.thread_id,
            )
            await asyncio.sleep(random.uniform(TYPING_DELAY_MIN, TYPING_DELAY_MAX))
        except Exception:
            pass  # typing indicator failure must not block the actual message
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
            "content": _SPEAKER_TURN_INSTRUCTION.format(name=persona.name),
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

    async def _speak_revival(self, persona, key: str, instruction: str) -> bool:
        """Have a persona deliver a short revival message. Returns True if successful."""
        speaker_bot = self.bots[persona.id]

        messages = list(self.conv.get(key))
        messages.append({"role": "user", "content": instruction.format(name=persona.name)})

        try:
            client = self.ai_registry.get_client(persona.provider)
            response = await client.complete(
                system=persona.system_prompt,
                messages=messages,
                max_tokens=120,  # Shorter than a regular turn
            )

            clean_response = clean_markdown(response)
            if not clean_response.strip():
                logger.warning("Revival persona %s returned empty response", persona.id)
                return False

            await self._send(speaker_bot, clean_response)
            self.conv.add(key, "assistant", f"[{persona.name}]: {clean_response}")
            return True

        except Exception as e:
            logger.warning("Revival persona %s (%s) failed: %s", persona.id, persona.provider, e)
            return False

    async def run_round(self, topic: str) -> None:
        global _last_thread_id
        key = self._key()

        # Track thread for revival
        if self.thread_id is not None:
            _last_thread_id = self.thread_id

        # Keep conversation history, just add new topic
        # This allows bots to reference previous discussions

        # Prepend panel memory so speakers know conclusions from past rounds
        memory_block = ""
        if _panel_memories:
            memory_block = "🧠 Память команды (выводы прошлых обсуждений):\n"
            memory_block += "\n".join(f"• {m}" for m in _panel_memories[-5:])
            memory_block += "\n\n"

        discussion_context = (
            memory_block +
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

                # Human-like pause — next speaker reads and thinks before replying
                if message_count < MAX_DISCUSSION_MESSAGES:
                    delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                    logger.debug("Discussion: waiting %.0f s before next speaker", delay)
                    await asyncio.sleep(delay)
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

        await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX))

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

        # Post-round: extract action items → save memory (best-effort, silent on failure)
        await self._extract_action_items(key, topic)
        await self._save_panel_memory(key)

    async def _extract_action_items(self, key: str, topic: str) -> None:
        """Extract concrete tasks from the discussion and post to ✅ Задачи topic."""
        mod = self.personas.moderator
        if mod is None:
            return
        try:
            client = self.ai_registry.get_client(mod.provider)
            messages = list(self.conv.get(key))
            messages.append({
                "role": "user",
                "content": (
                    "Выдели КОНКРЕТНЫЕ задачи или следующие шаги из этого обсуждения — "
                    "только реально озвученные действия, без домыслов. "
                    "Формат: нумерованный список. "
                    "Если конкретных задач нет — ответь одним словом: нет"
                ),
            })
            raw = await client.complete(
                system="Выделяй action items из обсуждения. Отвечай списком или словом 'нет'.",
                messages=messages,
                max_tokens=300,
            )
            result = clean_markdown(raw).strip()
            if not result or result.lower().rstrip(".") in ("нет", "no", "none"):
                logger.info("No action items in this round")
                return

            moderator_bot = self.bots["moderator"]
            tid = await _get_or_create_tasks_thread(moderator_bot, self.panel_chat_id)
            if tid is None:
                return

            msg = f"📌 Задачи из «{topic[:60]}»:\n\n{result}"
            await moderator_bot.send_message(self.panel_chat_id, msg, message_thread_id=tid)
            logger.info("Action items posted to tasks thread %d", tid)

        except Exception as e:
            logger.warning("Action items extraction failed: %s", e)

    async def _save_panel_memory(self, key: str) -> None:
        """Compress the round into a 1-sentence takeaway and store in panel memory."""
        global _panel_memories
        mod = self.personas.moderator
        if mod is None:
            return
        try:
            client = self.ai_registry.get_client(mod.provider)
            messages = list(self.conv.get(key))
            messages.append({
                "role": "user",
                "content": (
                    "Сформулируй ГЛАВНЫЙ вывод этого обсуждения одним коротким предложением. "
                    "Конкретно, без воды. Без markdown."
                ),
            })
            memory = await client.complete(
                system="Сжимай итог дискуссии до одного предложения.",
                messages=messages,
                max_tokens=80,
            )
            memory = clean_markdown(memory).strip()
            if memory:
                _panel_memories.append(memory)
                if len(_panel_memories) > PANEL_MEMORY_MAX:
                    _panel_memories.pop(0)
                logger.info(
                    "Panel memory saved (%d/%d): %r",
                    len(_panel_memories), PANEL_MEMORY_MAX, memory[:60],
                )
                _persist_panel_state()
        except Exception as e:
            logger.warning("Panel memory extraction failed: %s", e)

    async def run_revival(self) -> None:
        """Spontaneous 2-3 message continuation of the last discussion.

        No moderator summary — feels like bots picked up the conversation
        naturally after a break.
        """
        key = self._key()

        history = self.conv.get(key)
        if not history:
            logger.info("Revival skipped — no conversation history yet")
            return

        speakers = list(self.personas.panel_speakers)
        if not speakers:
            return

        # Shuffle so different speakers initiate each time
        random.shuffle(speakers)

        # Randomly 2 or 3 messages (2 is more common)
        num_messages = random.choices([2, 3], weights=[2, 1])[0]
        participants = speakers[:num_messages]

        logger.info(
            "Revival starting: %d messages in thread=%s by [%s]",
            num_messages,
            self.thread_id,
            ", ".join(p.id for p in participants),
        )

        for i, persona in enumerate(participants):
            instruction = (
                _REVIVAL_INITIATOR_INSTRUCTION if i == 0
                else _REVIVAL_RESPONDER_INSTRUCTION
            )
            success = await self._speak_revival(persona, key, instruction)
            if success and i < len(participants) - 1:
                await asyncio.sleep(random.uniform(REVIVAL_DELAY_MIN, REVIVAL_DELAY_MAX))

        logger.info("Revival complete")


# ---------------------------------------------------------------------------
# Revival scheduler
# ---------------------------------------------------------------------------

async def _revival_loop(
    bots: dict[str, Bot],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    panel_chat_id: int,
    interval_seconds: int,
) -> None:
    """Background task: spontaneously continue past discussions every ~N hours."""
    global _active_round, _last_thread_id

    # Delay first revival so it doesn't fire right after startup
    jitter = random.randint(-REVIVAL_JITTER_SECONDS, REVIVAL_JITTER_SECONDS)
    initial_delay = max(300, interval_seconds + jitter)
    logger.info("Revival scheduler: first revival in %.0f min", initial_delay / 60)
    await asyncio.sleep(initial_delay)

    while True:
        try:
            # Skip if a manual round is currently running
            if _active_round and not _active_round.done():
                logger.info("Revival skipped — active panel round in progress")
            elif not _panel_topics and _last_thread_id is None:
                logger.info("Revival skipped — no panel topics created yet")
            else:
                # Use last active thread; fall back to a random known topic
                thread_id = _last_thread_id
                if thread_id is None and _panel_topics:
                    thread_id = random.choice(list(_panel_topics.keys()))

                runner = PanelRoundRunner(
                    bots=bots,
                    personas=personas,
                    ai_registry=ai_registry,
                    conv=conv,
                    alerts=alerts,
                    panel_chat_id=panel_chat_id,
                    thread_id=thread_id,
                )
                await runner.run_revival()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Revival loop error: %s", e)
            await alerts.send("panel_revival", f"{type(e).__name__}: {e}")

        # Sleep until next revival
        jitter = random.randint(-REVIVAL_JITTER_SECONDS, REVIVAL_JITTER_SECONDS)
        sleep_time = max(300, interval_seconds + jitter)
        logger.info("Revival scheduler: next revival in %.0f min", sleep_time / 60)
        await asyncio.sleep(sleep_time)


def start_revival_scheduler(
    bots: dict[str, Bot],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    panel_chat_id: int,
    interval_seconds: int = REVIVAL_INTERVAL_SECONDS,
) -> "asyncio.Task[None]":
    """Create and return the revival background task.

    Call this once from __main__ after the event loop is running.
    Cancel the returned task on shutdown.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        _revival_loop(bots, personas, ai_registry, conv, alerts, panel_chat_id, interval_seconds)
    )
    task.add_done_callback(
        lambda t: logger.warning("Revival loop raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info(
        "Revival scheduler started (interval=%.0f min ± %.0f min)",
        interval_seconds / 60,
        REVIVAL_JITTER_SECONDS / 60,
    )
    return task


# ---------------------------------------------------------------------------
# Panel message handler
# ---------------------------------------------------------------------------

@panel_router.message((F.text | F.caption) & F.chat.type.in_({"supergroup", "group"}))
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

    # Acquire lock to ensure only one round starts at a time.
    # Non-blocking: if already locked, another handler is processing — skip.
    if _processing_lock.locked():
        logger.debug("Skipping duplicate panel message — lock held")
        return

    async with _processing_lock:
        if _active_round and not _active_round.done():
            logger.debug("Round already active, skipping")
            return

        logger.info(
            "Panel message accepted: chat_id=%s, user_id=%s, thread_id=%s",
            message.chat.id,
            message.from_user.id,
            message.message_thread_id,
        )

        # ALWAYS route to the correct fixed-category topic —
        # even when message already has a thread_id (wrongly-named topic
        # created by old code or manually by user).
        moderator_bot = bots.get("moderator")
        thread_id: int | None = None
        if moderator_bot:
            thread_id = await _analyze_topic_and_get_thread(
                bot=moderator_bot,
                chat_id=settings.panel_chat_id,
                question=message.text or message.caption or "",
                ai_registry=ai_registry,
            )
        # Fallback: if analysis/creation failed, use the incoming thread
        if thread_id is None:
            thread_id = message.message_thread_id

        logger.info("Starting panel round with thread_id=%s", thread_id)

        runner = PanelRoundRunner(
            bots=bots,
            personas=personas,
            ai_registry=ai_registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=settings.panel_chat_id,
            thread_id=thread_id,
        )
        task = asyncio.create_task(runner.run_round(message.text or message.caption or ""))
        task.add_done_callback(
            lambda t: logger.warning("Panel round raised: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        _active_round = task
