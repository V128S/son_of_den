import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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

# Scheduled one-shot panel round (admin /panelschedule command)
_scheduled_task: asyncio.Task[None] | None = None
_scheduled_info: dict | None = None  # {"topic": str, "fire_at": str}

# Cache for topic_id -> topic_name mapping
_panel_topics: dict[int, str] = {}

# Last thread used in a panel round — revival posts here by default
_last_thread_id: int | None = None

# Path to the bot state JSON file — set by init_panel_state() at startup
_state_path: Path | None = None

# Discussion settings
# Participant selection — patchable for tests.
# _PARTICIPANT_COUNT = None  →  random each round (1–4, weighted)
# _PARTICIPANT_COUNT = N     →  always exactly N speakers (used by integration tests)
_PARTICIPANT_COUNT: int | None = None

# Speaker ordering — patchable for tests.
# True  →  shuffle order each round (production)
# False →  keep the YAML order (integration tests that assert specific sequence)
_SHUFFLE_SPEAKERS: bool = True

# Debate round toggle — patchable for tests.
# None  →  50% probability per round (production)
# True  →  always run debate
# False →  never run debate
_DEBATE_ENABLED: bool | None = None

# Human-realistic timing — simulates reading, thinking, and composing time.
# Each inter-message gap is split into two phases:
#   1. SILENT_DELAY — pure silence, the "reader" is absorbing the previous message.
#   2. TYPING_DELAY — "typing..." indicator stays visible (refreshed every 4 s so
#      Telegram never auto-clears it). This phase starts just before the message
#      is sent, giving a natural composing feel.
# Tests zero these via the integration conftest autouse fixture.
SILENT_DELAY_MIN: float = 10.0   # silent reading time before next speaker starts (s)
SILENT_DELAY_MAX: float = 22.0   # —
TYPING_DELAY_MIN: float = 5.0    # composing time — typing indicator visible (s)
TYPING_DELAY_MAX: float = 12.0   # —
REVIVAL_DELAY_MIN: float = 20.0  # seconds for revival continuation (min)
REVIVAL_DELAY_MAX: float = 50.0  # seconds for revival continuation (max)

# Revival settings
REVIVAL_INTERVAL_SECONDS = 7_200  # Default: every 2 hours
REVIVAL_JITTER_SECONDS = 1_800    # ±30 min randomness

# Reminder timing — tasks are re-surfaced this many hours after posting
REMINDER_MIN_HOURS: float = 18.0
REMINDER_MAX_HOURS: float = 20.0

# Panel memory: compact takeaways from past rounds.
# Each entry: {"text": str, "topic": str, "ts": float (UTC unix timestamp)}
_panel_memories: list[dict] = []
PANEL_MEMORY_MAX = 30

# Per-persona memory: last N messages each persona sent (for cross-round consistency).
_persona_memories: dict[str, list[str]] = {}
PERSONA_MEMORY_MAX = 3

# Inline-rating state: round_id → {"topic": str, "thread_id": int | None}
# Cleaned up after admin taps a button or on bot restart (ephemeral — intentional).
_pending_rate: dict[str, dict] = {}

# Thread ID for the ✅ Задачи topic in the panel group
_tasks_thread_id: int | None = None

# Instruction appended to every speaker turn — extracted to avoid repetition
_SPEAKER_TURN_INSTRUCTION = (
    "{name}, выскажи своё мнение — 2-3 предложения, живым языком. "
    "Говори от себя, как независимый наблюдатель. "
    "Не нужно соглашаться со всеми — у тебя свой взгляд. "
    "Без заголовков и маркированных списков."
)

# Revival-specific instructions — casual, spontaneous feel
_REVIVAL_INITIATOR_INSTRUCTION = (
    "{name}, у тебя появилась новая мысль по прошлой теме — как будто только что дошло. "
    "Вырази её коротко и неформально, 1-2 предложения. "
    "Не объясняй, что «возвращаешься к теме» — просто скажи мысль. Без markdown."
)

_REVIVAL_RESPONDER_INSTRUCTION = (
    "{name}, видишь что написали — и у тебя есть своя реакция. "
    "1-2 предложения, по делу, от себя. Без markdown."
)

# Debate-round instructions — personas directly address each other
_DEBATE_INITIATOR_INSTRUCTION = (
    "{name}, обращайся напрямую к {opponent}: укажи конкретно, с чем именно не согласен "
    "и почему твоя позиция сильнее. 1-2 предложения, без markdown."
)
_DEBATE_RESPONDER_INSTRUCTION = (
    "{name}, {initiator} оспорил твою позицию. Ответь конкретно: "
    "либо признай слабое место, либо парируй. 1-2 предложения, без markdown."
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


def _make_round_id() -> str:
    """Generate a unique ID for a panel round (used as callback_data key)."""
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"{ts}_{random.randint(100, 999)}"


def _rate_keyboard(round_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard appended to the moderator summary for one-tap round rating."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data=f"panel_rate:good:{round_id}"),
        InlineKeyboardButton(text="👎", callback_data=f"panel_rate:bad:{round_id}"),
        InlineKeyboardButton(text="🔁 Углубить", callback_data=f"panel_rate:deepen:{round_id}"),
    ]])


def _parse_mod_sections(text: str) -> dict[str, str]:
    """Extract ВЫВОД / ДЕЙСТВИЕ / ПОЗИЦИЯ labels from structured moderator output."""
    sections: dict[str, str] = {}
    for line in text.strip().splitlines():
        stripped = line.strip()
        for key in ("ВЫВОД", "ДЕЙСТВИЕ", "ПОЗИЦИЯ"):
            if stripped.upper().startswith(key + ":"):
                sections[key] = stripped[len(key) + 1:].strip()
                break
    return sections


def _format_mod_html(raw: str) -> str:
    """Render the moderator summary as Telegram HTML using blockquotes and rich tags.

    Uses <blockquote> for the key conclusion and <blockquote expandable> (Bot API 7.9+)
    for the collapsible details block (recommendation + position).
    Falls back to a plain blockquote if the AI didn't follow the structured format.
    """
    import html as _h

    sections = _parse_mod_sections(raw)

    if not sections:
        safe = _h.escape(raw.strip())
        return (
            "📋 <b>Итог дискуссии</b>\n\n"
            f"<blockquote>{safe}</blockquote>\n\n"
            "🎤 <i>Жду следующую тему.</i>"
        )

    parts: list[str] = ["📋 <b>Итог дискуссии</b>"]

    conclusion = _h.escape(sections.get("ВЫВОД", ""))
    if conclusion:
        parts.append(f"<blockquote>💡 {conclusion}</blockquote>")

    detail_lines: list[str] = []

    action = _h.escape(sections.get("ДЕЙСТВИЕ", ""))
    if action:
        detail_lines.append(f"✅ <b>Что делать</b>\n{action}")

    position = _h.escape(sections.get("ПОЗИЦИЯ", ""))
    if position:
        if "консенсус" in position.lower():
            detail_lines.append("🤝 <i>Консенсус достигнут</i>")
        else:
            detail_lines.append(f"⚖️ <b>Позиция</b>\n{position}")

    if detail_lines:
        inner = "\n\n".join(detail_lines)
        parts.append(f"<blockquote expandable>{inner}</blockquote>")

    parts.append("🎤 <i>Жду следующую тему.</i>")
    return "\n\n".join(parts)


def _persist_panel_state() -> None:
    """Save current panel state to disk. No-op if state path not set."""
    if _state_path is None:
        return
    _state.update(_state_path, {
        "panel_topics": _state.encode_int_keys(_panel_topics),
        "tasks_thread_id": _tasks_thread_id,
        "last_thread_id": _last_thread_id,
        "panel_memories": list(_panel_memories),
        "persona_memories": dict(_persona_memories),
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
        for m in mems:
            if isinstance(m, str) and m:
                # Legacy format: plain string → wrap in dict
                _panel_memories.append({"text": m, "topic": "", "ts": 0.0})
            elif isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"]:
                _panel_memories.append({"text": m["text"], "topic": m.get("topic", ""), "ts": float(m.get("ts", 0))})
        while len(_panel_memories) > PANEL_MEMORY_MAX:
            _panel_memories.pop(0)

    # Restore per-persona memories
    raw_pmem = data.get("persona_memories", {})
    if isinstance(raw_pmem, dict):
        for pid, msgs in raw_pmem.items():
            if isinstance(msgs, list):
                _persona_memories[str(pid)] = [str(m) for m in msgs if isinstance(m, str)]

    logger.info(
        "Panel state restored: %d topics, tasks_thread=%s, %d memories, %d persona-mem slots",
        len(_panel_topics), _tasks_thread_id, len(_panel_memories), len(_persona_memories),
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
    search_client: "Any | None" = None  # SearchClient | None — optional web enrichment

    def _key(self) -> str:
        return f"panel:{self.panel_chat_id}"

    async def _send(
        self,
        bot: Bot,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> "Message | None":
        """Send message with a sustained typing indicator.

        Refreshes send_chat_action every 4 s so Telegram keeps showing "typing…"
        for the full composing window (TYPING_DELAY_MIN … TYPING_DELAY_MAX seconds).
        When parse_mode / reply_markup are set they are forwarded to send_message.
        Returns the sent Message (useful for later edits, e.g. removing keyboards).
        """
        logger.debug("_send: chat=%s thread=%s len=%d", self.panel_chat_id, self.thread_id, len(text))
        compose_seconds = random.uniform(TYPING_DELAY_MIN, TYPING_DELAY_MAX)
        deadline = asyncio.get_event_loop().time() + compose_seconds
        while True:
            try:
                await bot.send_chat_action(
                    chat_id=self.panel_chat_id,
                    action="typing",
                    message_thread_id=self.thread_id,
                )
            except Exception:
                pass  # typing-action failure must not block the message
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(4.0, remaining))
        # Build kwargs conditionally so existing test assertions aren't widened
        if parse_mode is not None:
            return await bot.send_message(
                self.panel_chat_id,
                text,
                message_thread_id=self.thread_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return await bot.send_message(
            self.panel_chat_id,
            text,
            message_thread_id=self.thread_id,
        )

    def _persona_system(self, persona) -> str:
        """Build the effective system prompt for a persona, injecting persona memory."""
        mem = _persona_memories.get(persona.id)
        if not mem:
            return persona.system_prompt
        mem_block = "\n\nТВОИ ПРОШЛЫЕ ПОЗИЦИИ (придерживайся согласованности):\n" + "\n".join(
            f"• {m}" for m in mem
        )
        return persona.system_prompt + mem_block

    async def _speak(self, persona, key: str) -> bool:
        """Have a persona speak using streaming. Returns True if successful."""
        speaker_bot = self.bots[persona.id]

        try:
            await speaker_bot.send_chat_action(
                chat_id=self.panel_chat_id,
                action="typing",
                message_thread_id=self.thread_id,
            )
        except Exception:
            pass

        messages = list(self.conv.get(key))
        messages.append({
            "role": "user",
            "content": _SPEAKER_TURN_INSTRUCTION.format(name=persona.name),
        })

        system = self._persona_system(persona)
        client = self.ai_registry.get_client(persona.provider)

        # ── Streaming path ──────────────────────────────────────────────────
        try:
            placeholder = await speaker_bot.send_message(
                self.panel_chat_id,
                "💬",
                message_thread_id=self.thread_id,
            )
        except Exception as e:
            logger.warning("Placeholder send failed for %s: %s", persona.id, e)
            return False

        buffer = ""
        last_edit = asyncio.get_event_loop().time()
        stream_ok = False
        try:
            async for chunk in client.stream(
                system=system,
                messages=messages,
                max_tokens=persona.max_tokens,
            ):
                buffer += chunk
                stream_ok = True
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 0.8 and buffer.strip():
                    try:
                        await speaker_bot.edit_message_text(
                            chat_id=self.panel_chat_id,
                            message_id=placeholder.message_id,
                            text=clean_markdown(buffer) or "💬",
                        )
                        last_edit = now
                    except Exception:
                        pass
        except Exception as e:
            if not stream_ok:
                # stream() not implemented or failed immediately — fall back to complete()
                logger.debug("Stream fallback for %s: %s", persona.id, e)
                try:
                    raw = await client.complete(
                        system=system,
                        messages=messages,
                        max_tokens=persona.max_tokens,
                    )
                    buffer = raw
                except Exception as e2:
                    logger.warning("Panel persona %s (%s) failed: %s", persona.id, persona.provider, e2)
                    await self.alerts.send(f"panel_{persona.id}", f"{type(e2).__name__}: {e2}")
                    try:
                        await speaker_bot.delete_message(
                            chat_id=self.panel_chat_id,
                            message_id=placeholder.message_id,
                        )
                    except Exception:
                        pass
                    return False
            else:
                logger.warning("Panel persona %s stream interrupted: %s", persona.id, e)

        clean_response = clean_markdown(buffer)
        if not clean_response.strip():
            logger.warning("Panel persona %s returned empty response", persona.id)
            try:
                await speaker_bot.delete_message(
                    chat_id=self.panel_chat_id,
                    message_id=placeholder.message_id,
                )
            except Exception:
                pass
            return False

        # Final edit with complete text
        try:
            await speaker_bot.edit_message_text(
                chat_id=self.panel_chat_id,
                message_id=placeholder.message_id,
                text=clean_response,
            )
        except Exception:
            pass

        self.conv.add(key, "assistant", f"[{persona.name}]: {clean_response}")

        # Update persona memory (keep last PERSONA_MEMORY_MAX entries)
        mem_entry = clean_response[:160].rstrip()
        if persona.id not in _persona_memories:
            _persona_memories[persona.id] = []
        _persona_memories[persona.id].append(mem_entry)
        if len(_persona_memories[persona.id]) > PERSONA_MEMORY_MAX:
            _persona_memories[persona.id].pop(0)

        return True

    async def _speak_debate(
        self, initiator_persona, responder_persona, key: str
    ) -> None:
        """One exchange: initiator challenges, responder replies. Silent on failure."""
        init_instruction = _DEBATE_INITIATOR_INSTRUCTION.format(
            name=initiator_persona.name,
            opponent=responder_persona.name,
        )
        init_messages = list(self.conv.get(key))
        init_messages.append({"role": "user", "content": init_instruction})

        init_bot = self.bots[initiator_persona.id]
        init_client = self.ai_registry.get_client(initiator_persona.provider)

        try:
            init_raw = await init_client.complete(
                system=self._persona_system(initiator_persona),
                messages=init_messages,
                max_tokens=120,
            )
            init_text = clean_markdown(init_raw).strip()
            if not init_text:
                return
            await self._send(init_bot, init_text)
            self.conv.add(key, "assistant", f"[{initiator_persona.name}→{responder_persona.name}]: {init_text}")
        except Exception as e:
            logger.debug("Debate initiator %s failed: %s", initiator_persona.id, e)
            return

        await asyncio.sleep(random.uniform(SILENT_DELAY_MIN / 2, SILENT_DELAY_MAX / 2))

        resp_instruction = _DEBATE_RESPONDER_INSTRUCTION.format(
            name=responder_persona.name,
            initiator=initiator_persona.name,
        )
        resp_messages = list(self.conv.get(key))
        resp_messages.append({"role": "user", "content": resp_instruction})

        resp_bot = self.bots[responder_persona.id]
        resp_client = self.ai_registry.get_client(responder_persona.provider)
        try:
            resp_raw = await resp_client.complete(
                system=self._persona_system(responder_persona),
                messages=resp_messages,
                max_tokens=120,
            )
            resp_text = clean_markdown(resp_raw).strip()
            if resp_text:
                await self._send(resp_bot, resp_text)
                self.conv.add(key, "assistant", f"[{responder_persona.name}→{initiator_persona.name}]: {resp_text}")
        except Exception as e:
            logger.debug("Debate responder %s failed: %s", responder_persona.id, e)

    async def _speak_revival(self, persona, key: str, instruction: str) -> bool:
        """Have a persona deliver a short revival message. Returns True if successful."""
        speaker_bot = self.bots[persona.id]

        # Show typing before AI call so the user sees someone is about to write.
        try:
            await speaker_bot.send_chat_action(
                chat_id=self.panel_chat_id,
                action="typing",
                message_thread_id=self.thread_id,
            )
        except Exception:
            pass

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

        # Optionally enrich with fresh web data before speakers are invoked
        search_block = ""
        if self.search_client is not None and getattr(self.search_client, "enabled", False):
            try:
                results = await self.search_client.search(topic, num_results=3)
                if results:
                    search_block = self.search_client.format_results(results) + "\n\n"
            except Exception as _se:
                logger.debug("Web search failed for panel topic: %s", _se)

        # Prepend panel memory so speakers have context from past rounds
        memory_block = ""
        if _panel_memories:
            recent = _panel_memories[-7:]
            memory_block = "🧠 Контекст прошлых обсуждений:\n"
            for mem in recent:
                label = f"[{mem['topic']}] " if mem.get("topic") else ""
                memory_block += f"• {label}{mem['text']}\n"
            memory_block += "\n"

        discussion_context = (
            search_block +
            memory_block +
            f"Тема: {topic}\n\n"
            "- Выскажи своё мнение — прямо и от себя\n"
            "- Ты независимый наблюдатель, а не часть команды\n"
            "- Не нужно обязательно отвечать другим — главное твой взгляд\n"
            "- Пиши коротко, живым языком, без markdown"
        )
        self.conv.add(key, "user", discussion_context)

        # Keep only last 40 messages to avoid context overflow
        # Matches ConversationStore maxlen=40; preserves ~2-3 rounds of discussion
        self.conv.trim(key, keep_last=40)

        moderator_bot = self.bots["moderator"]
        await self._send(moderator_bot, f"🎬 Раунд: {topic}\n\n💬 Дискуссия...")

        # Build speaker pool — shuffle in production, keep YAML order when tests force it.
        all_speakers = list(self.personas.panel_speakers)
        if _SHUFFLE_SPEAKERS:
            random.shuffle(all_speakers)

        # Pick how many speakers join: random (production) or forced (tests).
        # Weights: 1 ~11%, 2 ~33%, 3 ~33%, 4 ~22%
        if _PARTICIPANT_COUNT is not None:
            n = min(_PARTICIPANT_COUNT, len(all_speakers))
        else:
            n = random.choices(
                [1, 2, 3, len(all_speakers)],
                weights=[1, 3, 3, 2],
            )[0]
        active = all_speakers[:n]
        logger.info("Round participants (%d/%d): %s", n, len(all_speakers), [p.id for p in active])

        sent = 0
        successful: list = []  # track which personas spoke successfully
        for i, persona in enumerate(active):
            # Silent reading pause before every speaker except the first.
            if i > 0:
                silent = random.uniform(SILENT_DELAY_MIN, SILENT_DELAY_MAX)
                logger.debug("Silent pause %.0f s before %s speaks", silent, persona.id)
                await asyncio.sleep(silent)

            success = await self._speak(persona, key)
            if success:
                sent += 1
                successful.append(persona)
                logger.info("Discussion message %d/%d from %s", sent, n, persona.id)
            else:
                logger.warning("Persona %s failed, skipping", persona.id)

        # ── Debate mini-round (Feature 1) ────────────────────────────────────
        # When ≥ 2 speakers succeeded, run one direct exchange. Probability is
        # 50% in production; can be overridden to True/False for tests.
        _debate_roll = _DEBATE_ENABLED if _DEBATE_ENABLED is not None else (random.random() < 0.5)
        if len(successful) >= 2 and _debate_roll:
            await asyncio.sleep(random.uniform(SILENT_DELAY_MIN / 2, SILENT_DELAY_MAX / 2))
            # Ask the cheapest available AI to pick the two most-opposed personas.
            try:
                names_str = ", ".join(p.name for p in successful)
                pick_client = self.ai_registry.get_client(
                    next(
                        (p for p in ["groq", "openrouter_gemini", mod.provider if mod else "claude"]
                         if self.ai_registry.has_provider(p)),
                        (mod.provider if mod else "claude"),
                    )
                )
                pick_raw = await pick_client.complete(
                    system="Выбери двух участников с наиболее противоположными позициями.",
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Участники обсуждения: {names_str}.\n"
                            "По тексту выше выбери ДВУХ с наиболее противоположными позициями.\n"
                            "Ответь СТРОГО двумя именами через запятую, без лишних слов."
                        ),
                    }],
                    max_tokens=20,
                )
                picked = [n.strip() for n in pick_raw.strip().split(",")]
                paired = [p for name in picked for p in successful if p.name == name]
                if len(paired) >= 2:
                    logger.info("Debate round: %s vs %s", paired[0].name, paired[1].name)
                    await self._speak_debate(paired[0], paired[1], key)
                else:
                    # Fallback: pick first two successful speakers
                    await self._speak_debate(successful[0], successful[1], key)
            except Exception as e:
                logger.debug("Debate picker failed, using fallback: %s", e)
                await self._speak_debate(successful[0], successful[1], key)

        # ── Moderator summary ────────────────────────────────────────────────
        mod = self.personas.moderator
        if mod is None:
            return

        await asyncio.sleep(random.uniform(SILENT_DELAY_MIN, SILENT_DELAY_MAX))

        # Send placeholder immediately so admin sees the moderator "thinking"
        mod_placeholder = None
        try:
            mod_placeholder = await moderator_bot.send_message(
                self.panel_chat_id,
                "📋 Формирую итог…",
                message_thread_id=self.thread_id,
            )
        except Exception:
            pass

        try:
            mod_client = self.ai_registry.get_client(mod.provider)

            mod_messages = list(self.conv.get(key))
            mod_messages.append({
                "role": "user",
                "content": (
                    "Подведи итог дискуссии. Ответь СТРОГО в таком формате — "
                    "каждый пункт на отдельной строке, без лишних слов:\n\n"
                    "ВЫВОД: <главная мысль одним предложением>\n"
                    "ДЕЙСТВИЕ: <конкретно что сделать — одно предложение>\n"
                    "ПОЗИЦИЯ: <кто прав и почему, если было разногласие; иначе: Консенсус>\n\n"
                    "Без markdown. Без вводных фраз. Только три строки."
                )
            })

            # ── Streaming path ───────────────────────────────────────────────
            raw_buffer = ""
            last_edit = asyncio.get_event_loop().time()
            stream_ok = False
            try:
                async for chunk in mod_client.stream(
                    system=mod.system_prompt,
                    messages=mod_messages,
                    max_tokens=mod.max_tokens,
                ):
                    raw_buffer += chunk
                    stream_ok = True
                    now = asyncio.get_event_loop().time()
                    if now - last_edit >= 1.5 and raw_buffer.strip() and mod_placeholder:
                        try:
                            await moderator_bot.edit_message_text(
                                chat_id=self.panel_chat_id,
                                message_id=mod_placeholder.message_id,
                                text=raw_buffer[:800],
                            )
                            last_edit = now
                        except Exception:
                            pass
            except Exception as _se:
                if not stream_ok:
                    raw_buffer = await mod_client.complete(
                        system=mod.system_prompt,
                        messages=mod_messages,
                        max_tokens=mod.max_tokens,
                    )
                else:
                    logger.warning("Moderator stream interrupted: %s", _se)

            clean_summary = clean_markdown(raw_buffer)
            round_id = _make_round_id()
            _pending_rate[round_id] = {"topic": topic, "thread_id": self.thread_id}

            if not clean_summary.strip():
                logger.warning("Moderator returned empty summary")
                _pending_rate.pop(round_id, None)
                _fallback = mod.fallback
                if mod_placeholder:
                    try:
                        await moderator_bot.edit_message_text(
                            chat_id=self.panel_chat_id,
                            message_id=mod_placeholder.message_id,
                            text=_fallback,
                        )
                    except Exception:
                        await self._send(moderator_bot, _fallback)
                else:
                    await self._send(moderator_bot, _fallback)
            else:
                html_summary = _format_mod_html(clean_summary)
                keyboard = _rate_keyboard(round_id)
                if mod_placeholder:
                    try:
                        await moderator_bot.edit_message_text(
                            chat_id=self.panel_chat_id,
                            message_id=mod_placeholder.message_id,
                            text=html_summary,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    except Exception:
                        await self._send(moderator_bot, html_summary, parse_mode="HTML", reply_markup=keyboard)
                else:
                    await self._send(moderator_bot, html_summary, parse_mode="HTML", reply_markup=keyboard)
                logger.info("Moderator summary: %d chars (round_id=%s)", len(html_summary), round_id)

        except Exception as e:
            logger.warning("Moderator (%s) failed: %s", mod.provider, e)
            await self.alerts.send("panel_moderator", f"{type(e).__name__}: {e}")
            _fallback = mod.fallback
            if mod_placeholder:
                try:
                    await moderator_bot.edit_message_text(
                        chat_id=self.panel_chat_id,
                        message_id=mod_placeholder.message_id,
                        text=_fallback,
                    )
                except Exception:
                    await self._send(moderator_bot, _fallback)
            else:
                await self._send(moderator_bot, _fallback)

        # Post-round: extract action items → save memory (best-effort, silent on failure)
        await self._extract_action_items(key, topic)
        await self._save_panel_memory(key, topic)

    async def _extract_action_items(self, key: str, topic: str) -> None:
        """Extract up to 3 concrete tasks and post to ✅ Задачи; schedule a reminder."""
        mod = self.personas.moderator
        if mod is None:
            return
        try:
            client = self.ai_registry.get_client(mod.provider)
            messages = list(self.conv.get(key))
            messages.append({
                "role": "user",
                "content": (
                    "Выдели до 3 конкретных задач из этого обсуждения — "
                    "только реально озвученные действия. "
                    "Формат: нумерованный список, максимум 3 пункта, "
                    "каждый — одно короткое предложение. "
                    "Если конкретных задач нет — ответь одним словом: нет"
                ),
            })
            raw = await client.complete(
                system="Выделяй action items из обсуждения. Отвечай списком (до 3 пунктов) или словом 'нет'.",
                messages=messages,
                max_tokens=120,
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

            # Schedule a reminder 18-20 h later
            if _state_path is not None:
                remind_ts = (
                    datetime.now(timezone.utc).timestamp()
                    + random.uniform(REMINDER_MIN_HOURS * 3600, REMINDER_MAX_HOURS * 3600)
                )
                data = _state.load(_state_path)
                pending: list[dict] = data.get("pending_reminders", [])
                pending.append({
                    "remind_at": remind_ts,
                    "thread_id": tid,
                    "text": result,
                    "topic": topic[:60],
                })
                _state.update(_state_path, {"pending_reminders": pending})
                logger.info(
                    "Task reminder scheduled in %.0f h (thread=%s)",
                    (remind_ts - datetime.now(timezone.utc).timestamp()) / 3600,
                    tid,
                )

        except Exception as e:
            logger.warning("Action items extraction failed: %s", e)

    async def _save_panel_memory(self, key: str, topic: str) -> None:
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
                entry = {
                    "text": memory,
                    "topic": topic,
                    "ts": datetime.now(timezone.utc).timestamp(),
                }
                _panel_memories.append(entry)
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
# Reminder checker
# ---------------------------------------------------------------------------

async def _fire_due_reminders(bots: dict, panel_chat_id: int) -> None:
    """Post any pending task reminders that are now due."""
    if _state_path is None:
        return
    data = _state.load(_state_path)
    pending: list[dict] = data.get("pending_reminders", [])
    if not pending:
        return

    now = datetime.now(timezone.utc).timestamp()
    still_pending: list[dict] = []

    for reminder in pending:
        if now < reminder.get("remind_at", 0):
            still_pending.append(reminder)
            continue

        thread_id = reminder.get("thread_id")
        text = reminder.get("text", "").strip()
        topic = reminder.get("topic", "")
        if not text:
            continue

        try:
            mod_bot = bots.get("moderator")
            if mod_bot is None:
                still_pending.append(reminder)
                continue
            header = f"из «{topic}»" if topic else "о задачах"
            msg = f"🔔 Напоминание {header}:\n\n{text}"
            await mod_bot.send_message(panel_chat_id, msg, message_thread_id=thread_id)
            logger.info("Reminder posted to thread %s (topic=%r)", thread_id, topic)
        except Exception as e:
            logger.warning("Failed to post reminder (thread=%s): %s", thread_id, e)
            still_pending.append(reminder)  # retry on next check

    _state.update(_state_path, {"pending_reminders": still_pending})


async def _reminder_loop(bots: dict, panel_chat_id: int, check_interval_seconds: int) -> None:
    """Background task: check for due reminders on a fixed interval."""
    # Immediate check on startup — picks up reminders that survived a restart
    try:
        await _fire_due_reminders(bots, panel_chat_id)
    except Exception as e:
        logger.warning("Initial reminder check error: %s", e)

    while True:
        await asyncio.sleep(check_interval_seconds)
        try:
            await _fire_due_reminders(bots, panel_chat_id)
        except Exception as e:
            logger.warning("Reminder loop error: %s", e)


def start_reminder_checker(
    bots: dict,
    panel_chat_id: int,
    check_interval_seconds: int = 1800,
) -> "asyncio.Task[None]":
    """Create and start the reminder background task.

    Checks for due task reminders every *check_interval_seconds* (default 30 min).
    Cancel the returned task on shutdown.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        _reminder_loop(bots, panel_chat_id, check_interval_seconds)
    )
    task.add_done_callback(
        lambda t: logger.warning("Reminder loop raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info(
        "Reminder checker started (check_interval=%.0f min)", check_interval_seconds / 60
    )
    return task

# ---------------------------------------------------------------------------
# Direct-reply handler helpers
# ---------------------------------------------------------------------------

def _find_persona_for_bot_user_id(
    bots: dict[str, "Bot"],
    personas: "PersonaRegistry",
    bot_user_id: int,
) -> "tuple[Bot, Persona] | None":
    """Return (bot, persona) if bot_user_id matches a panel bot, else None."""
    from claudebots.core.personas import Persona
    for bot_key, bot in bots.items():
        if bot_key == "business":
            continue
        try:
            token_uid = int(bot.token.split(":")[0])
        except (ValueError, IndexError):
            continue
        if token_uid != bot_user_id:
            continue
        persona = next((p for p in personas.all_panel() if p.id == bot_key), None)
        if persona:
            return bot, persona
    return None


async def _handle_direct_reply(
    message: "Message",
    reply_bot: "Bot",
    persona: "Persona",
    ai_registry: "AIRegistry",
    conv: "ConversationStore",
    alerts: "AlertSender",
) -> None:
    """Admin replied to a panel bot — that bot responds directly."""
    thread_id = message.message_thread_id or 0
    key = f"panel:{message.chat.id}:{thread_id}"

    user_text = message.text or message.caption or ""
    conv.add(key, "user", user_text)

    try:
        await reply_bot.send_chat_action(
            chat_id=message.chat.id, action="typing",
            message_thread_id=message.message_thread_id,
        )
    except Exception:
        pass

    client = ai_registry.get_client(persona.provider)
    try:
        response = await client.complete(
            system=persona.system_prompt,
            messages=conv.get(key),
            max_tokens=persona.max_tokens,
        )
        if not response or not response.strip():
            response = persona.fallback
    except Exception as e:
        logger.warning("Direct reply failed (%s): %s", persona.provider, e)
        await alerts.send("panel_direct", f"{persona.name}: {type(e).__name__}: {e}")
        response = persona.fallback

    try:
        sent = await reply_bot.send_message(
            chat_id=message.chat.id,
            text=response,
            message_thread_id=message.message_thread_id,
            parse_mode=None,
            reply_to_message_id=message.message_id,
        )
        conv.add(key, "assistant", response)
        logger.info("Direct reply from %s (%s chars)", persona.name, len(response))
    except Exception as e:
        logger.warning("Direct reply send failed: %s", e)


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
    search_client=None,
) -> None:
    if message.chat.id != settings.panel_chat_id:
        return
    if message.from_user is None or message.from_user.id != settings.admin_user_id:
        return
    if message.from_user.is_bot:
        return

    # Direct reply: admin replied to a specific panel bot → that bot responds alone
    if (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.is_bot
    ):
        found = _find_persona_for_bot_user_id(
            bots, personas, message.reply_to_message.from_user.id
        )
        if found is not None:
            reply_bot, reply_persona = found
            await _handle_direct_reply(
                message=message,
                reply_bot=reply_bot,
                persona=reply_persona,
                ai_registry=ai_registry,
                conv=conv,
                alerts=alerts,
            )
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
            search_client=search_client,
        )
        task = asyncio.create_task(runner.run_round(message.text or message.caption or ""))
        task.add_done_callback(
            lambda t: logger.warning("Panel round raised: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )
        _active_round = task


async def _run_scheduled_round(
    delay: float,
    bots: dict,
    personas: "Any",
    ai_registry: "Any",
    conv: "Any",
    alerts: "Any",
    chat_id: int,
    topic: str,
    thread_id: "int | None",
    search_client: "Any",
) -> None:
    global _scheduled_info
    await asyncio.sleep(delay)
    _scheduled_info = None
    runner = PanelRoundRunner(
        bots=bots, personas=personas, ai_registry=ai_registry,
        conv=conv, alerts=alerts, panel_chat_id=chat_id,
        thread_id=thread_id, search_client=search_client,
    )
    await runner.run_round(topic)


def schedule_panel_round(
    delay: float,
    bots: dict,
    personas: "Any",
    ai_registry: "Any",
    conv: "Any",
    alerts: "Any",
    chat_id: int,
    topic: str,
    thread_id: "int | None" = None,
    search_client: "Any" = None,
    fire_at_str: str = "",
) -> None:
    """Schedule a one-shot panel round after *delay* seconds. Cancels any existing schedule."""
    global _scheduled_task, _scheduled_info
    if _scheduled_task and not _scheduled_task.done():
        _scheduled_task.cancel()
    _scheduled_info = {"topic": topic, "fire_at": fire_at_str}
    _scheduled_task = asyncio.create_task(
        _run_scheduled_round(delay, bots, personas, ai_registry, conv, alerts, chat_id, topic, thread_id, search_client)
    )
    _scheduled_task.add_done_callback(
        lambda t: logger.warning("Scheduled round raised: %s", t.exception())
        if not t.cancelled() and t.exception() else None
    )
    logger.info("Panel round scheduled in %.0f s (topic=%r)", delay, topic[:50])


def cancel_scheduled_panel() -> bool:
    """Cancel the pending scheduled round. Returns True if there was one to cancel."""
    global _scheduled_task, _scheduled_info
    if _scheduled_task and not _scheduled_task.done():
        _scheduled_task.cancel()
        _scheduled_task = None
        _scheduled_info = None
        return True
    return False


def get_scheduled_panel() -> "dict | None":
    """Return {"topic": str, "fire_at": str} if a round is pending, else None."""
    if _scheduled_info and _scheduled_task and not _scheduled_task.done():
        return dict(_scheduled_info)
    return None


def get_panel_ratings_summary() -> dict:
    """Return 👍/👎 counts and rated-round list from persisted panel_ratings."""
    if _state_path is None:
        return {"good": 0, "bad": 0, "total": 0, "ratings": []}
    try:
        ratings = _state.load(_state_path).get("panel_ratings", [])
        good = sum(1 for r in ratings if r.get("rating") == "good")
        bad = sum(1 for r in ratings if r.get("rating") == "bad")
        return {"good": good, "bad": bad, "total": len(ratings), "ratings": ratings}
    except Exception:
        return {"good": 0, "bad": 0, "total": 0, "ratings": []}


def get_rated_rounds(rating: str, limit: int = 5) -> list[dict]:
    """Return the most recent rounds with a given rating ('good' or 'bad').

    Each entry: {"topic": str, "ts": float, "memory": str | None}
    The memory is looked up by matching topic name in _panel_memories (closest timestamp).
    """
    summary = get_panel_ratings_summary()
    filtered = [r for r in summary["ratings"] if r.get("rating") == rating]
    filtered.sort(key=lambda r: r.get("ts", 0), reverse=True)
    result = []
    for r in filtered[:limit]:
        topic = r.get("topic", "")
        ts = r.get("ts", 0.0)
        # Find the closest memory by topic match, then ts proximity
        candidates = [m for m in _panel_memories if m.get("topic", "") == topic]
        if not candidates:
            candidates = [m for m in _panel_memories if topic and topic[:20] in m.get("text", "")]
        memory_text: str | None = None
        if candidates:
            best = min(candidates, key=lambda m: abs(m.get("ts", 0) - ts))
            memory_text = best.get("text")
        result.append({"topic": topic, "ts": ts, "memory": memory_text})
    return result


# ---------------------------------------------------------------------------
# Round rating callback handler
# ---------------------------------------------------------------------------

@panel_router.callback_query(F.data.startswith("panel_rate:"))
async def _on_panel_rate(
    cb: CallbackQuery,
    bots: dict[str, Bot],
    personas: PersonaRegistry,
    ai_registry: AIRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    settings,
    search_client=None,
) -> None:
    """Handle 👍 / 👎 / 🔁 taps on the moderator summary message."""
    if cb.data is None or cb.from_user is None or cb.message is None:
        return
    # Only the admin can rate rounds
    if cb.from_user.id != settings.admin_user_id:
        await cb.answer("Только администратор", show_alert=False)
        return

    parts = cb.data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, round_id = parts

    pending = _pending_rate.get(round_id)
    topic = pending["topic"] if pending else ""

    if action in ("good", "bad"):
        label = "👍 Полезно" if action == "good" else "👎 Вода"
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer(label, show_alert=False)
        # Persist rating
        if _state_path is not None:
            existing = _state.load(_state_path).get("panel_ratings", [])
            existing.append({
                "round_id": round_id,
                "rating": action,
                "topic": topic,
                "ts": datetime.now(timezone.utc).timestamp(),
            })
            _state.update(_state_path, {"panel_ratings": existing[-200:]})
        _pending_rate.pop(round_id, None)
        logger.info("Round %s rated %s (topic=%r)", round_id, action, topic[:50])

    elif action == "deepen":
        thread_id = pending.get("thread_id") if pending else None
        _pending_rate.pop(round_id, None)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("🔁 Запускаю углублённый раунд…", show_alert=False)

        runner = PanelRoundRunner(
            bots=bots,
            personas=personas,
            ai_registry=ai_registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=settings.panel_chat_id,
            thread_id=thread_id,
            search_client=search_client,
        )
        deep_topic = f"Углубляем: {topic}" if topic else "Продолжаем тему"
        t = asyncio.create_task(runner.run_round(deep_topic))
        t.add_done_callback(
            lambda tt: logger.warning("Deepen round raised: %s", tt.exception())
            if not tt.cancelled() and tt.exception() else None
        )
        logger.info("Deepen round started for topic=%r", topic[:50])
