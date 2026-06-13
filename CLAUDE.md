# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the bot
uv run python -m claudebots

# Run all tests (229 tests, e2e excluded by default)
uv run pytest

# Run unit tests only (fast, no external deps)
uv run pytest tests/unit

# Run a single test file
uv run pytest tests/unit/test_business_router.py

# Run a single test by name
uv run pytest tests/unit/test_config.py::test_name

# Lint
uv run ruff check .
uv run ruff format .

# Type-check (strict mode, core only)
uv run mypy claudebots/core

# Local restart loop (dev)
bash run_bot.sh
```

## Architecture

One Python process, one asyncio event loop, 6 Telegram bots. All bots share a single `Dispatcher` with three routers registered in order: `panel_router` → `business_router` → `admin_router`. Router order matters — panel messages must be checked before business messages.

### Entry point and dependency injection (`__main__.py`)

`amain()` builds all dependencies, then injects them via `dp.workflow_data`. Every aiogram handler receives objects like `ai_registry`, `conv`, `personas`, `bots`, `alerts`, `calendar_client`, `settings` as keyword arguments — no globals in handler signatures.

Background tasks started at boot:
- **Session guardian** — calls `Bot.close()` on all bots every 40 s to evict any competing process
- **Revival scheduler** — spontaneously re-opens past panel discussions at a configurable interval (`PANEL_REVIVAL_INTERVAL_HOURS`)
- **Daily digest scheduler** — sends a contact summary to the admin at `CONTACT_DIGEST_TIME`
- **Morning briefing scheduler** — sends an AI-generated daily briefing (calendar + panel memories + AI summary) at `MORNING_BRIEFING_TIME` (default 09:00)
- **Daily usage resetter** — resets per-provider daily token counters at UTC midnight
- **Feed monitor** — polls Telegram channel RSS and auto-triggers panel rounds for scored posts

On graceful shutdown, conversation history and usage counters are saved to `bot_state.json` and restored on the next startup. The daily usage window resets at UTC midnight; `/cost` shows both today's and all-time usage.

### Core modules (`claudebots/core/`)

| Module | Purpose |
|---|---|
| `config.py` | `Settings` (pydantic-settings from `.env`). Single source of truth for all env vars. |
| `ai_registry.py` | `AIRegistry` — maps provider name strings (`"claude"`, `"groq"`, `"openrouter_deepseek"`, etc.) to `AIClient` instances. All clients satisfy the `AIClient` Protocol: `complete()` + `stream()` + `usage`. Tracks both cumulative and daily usage; `reset_daily_usage()` / `get_daily_usage_by_provider()` for the daily window. `snapshot_usage()` / `restore_usage()` for cross-restart persistence. `FallbackClient` wraps two clients — tries primary, silently falls back on any exception. |
| `claude_client.py` | Wraps Anthropic SDK. Uses prompt caching (`cache_control: ephemeral` on system blocks). Has `CircuitBreaker` built in; falls back to Groq on open. |
| `circuit_breaker.py` | Sliding-window failure counter. `CLOSED → OPEN` after N failures in window; `OPEN → HALF_OPEN` after recovery period; `HALF_OPEN → CLOSED` on success. |
| `conversation.py` | `ConversationStore` — per-key ring buffer (deque with `maxlen=40`). Keys are namespaced strings like `"biz:{conn_id}:{chat_id}"` or `"private:{chat_id}:{thread_id}"`. `snapshot()` / `restore()` for cross-restart persistence. |
| `personas.py` | Loads `personas.yaml` into `PersonaRegistry`. Supports `<<MARKER>>` template substitution across prompts. `/reload` hot-reloads without restart. |
| `state.py` | Atomic JSON persistence for topic mappings (`bot_state.json`). `load()` / `save()` / `update()`. Helpers for int-key encoding (JSON requires string keys). |
| `alerts.py` | `AlertSender` — sends throttled admin notifications (one per `alert_key` per window, 4096-char truncation). |
| `calendar_client.py` | Reads Google Calendar via Service Account. 60 s in-memory cache. Optional — bot works without it. |
| `obsidian_client.py` | `ObsidianClient` — writes contact conversation history to local Obsidian vault (`Contacts/{name}.md`, `Daily/{date}.md`). Disabled when `OBSIDIAN_VAULT_PATH` is empty. |
| `sheets_client.py` | `GoogleSheetsClient` — reads a contact's Google Sheet price list and transfers rows (with markup) to the owner's personal sheet. Enabled when `GOOGLE_SERVICE_ACCOUNT_FILE` and `SHEETS_PERSONAL_ID` are both set. |
| `meters_client.py` | `MetersClient` — parses free-text meter readings (gas/water/electricity) via AI and appends them to a Google Sheet. Enabled when `METERS_SHEET_ID` is set. |
| `feed_monitor.py` | Polls `rsshub.app/telegram/channel/<slug>` (Atom); falls back to `t.me/s/<slug>` scraping on 403. Scores entries with the cheapest available AI; fires `PanelRoundRunner` when score ≥ `FEED_MIN_SCORE`. |
| `search_client.py` | `SearchClient` — async Exa API wrapper for web search enrichment. `search(query, num_results=3)` returns `list[SearchResult]`; disabled when `EXA_API_KEY` is not set. `format_results()` renders a compact block injected into panel context. |

### Services (`claudebots/services/`)

| Module | Purpose |
|---|---|
| `insta_downloader.py` | `InstagramDownloader` — downloads public posts/Reels via yt-dlp. `detect_url(text)` extracts the first Instagram URL. Returns `list[MediaFile]` (photo/video/document). Handles carousels. Re-encodes video to H.264/AAC+faststart via ffmpeg for Telegram playback. |
| `yt_downloader.py` | `YTDownloader` — downloads the best-quality audio from YouTube videos via yt-dlp. `detect_url(text)` matches `https://youtu.be/` and `https://youtube.com/watch?v=` (requires protocol; ignores Shorts, playlists, channels). Returns `AudioFile` with `send_as_audio` property (>50 MB → document). Also has `fetch_transcript(url)` to download auto-subtitles (VTT) for video summarisation. `detect_summary_cmd(text)` matches "резюме/кратко/summary URL" prefix. |
| `social_downloader.py` | `SocialDownloader` — downloads videos and photos from TikTok (`tiktok.com`, `vm.tiktok.com`) and X/Twitter (`twitter.com`, `x.com`) via yt-dlp. `detect_platform(text)` returns `(url, topic_name)` where topic_name is `"🎬 TikTok"` or `"🐦 X / Twitter"`. Reuses InstagramDownloader helpers for re-encode, classify, cleanup. |

### Routers (`claudebots/routers/`)

**`business.py`** handles several message types on the same bot:
- **Business messages** (`@business_router.business_message`) — auto-replies on behalf of the owner using streaming. Mirrors incoming and outgoing messages to the admin via per-contact forum topics.
- **Private/supergroup messages** (`_on_private_message`) — owner-mode vs. regular-user mode. Owner gets a non-streaming `complete()` call; non-owner gets the streaming placeholder flow. Forwarded channel posts trigger a panel round via `_on_forward_to_panel`.
- **Voice messages** (`_on_voice_message`) — owner sends a voice note, bot transcribes it via Groq Whisper (`whisper-large-v3-turbo`) and routes as regular text. Shows the transcription in italics before the AI reply. Requires `GROQ_API_KEY`.
- **Instagram downloader** — detects Instagram URLs in owner messages, downloads media via `InstagramDownloader`, sends to a persistent `📸 Instagram` forum topic (key `"📸 Instagram:{chat_id}"`). Works from DM and supergroup. Recovers automatically if topic is deleted.
- **YouTube audio** — detects YouTube URLs in owner messages, downloads best audio via `YTDownloader`, sends as audio (or document if >50 MB) to a persistent `🎵 YouTube` forum topic (key `"🎵 YouTube:{chat_id}"`). Uses `FSInputFile` for aiogram 3.x compatibility.
- **Owner topic categorisation** — when the admin writes in a forum topic (not a contact topic), the message is classified into one of 9 fixed categories and routed to the matching topic (or the topic is renamed). Uses `openrouter_gemini` for classification.
- `_prepare_media_send()` — shared helper that resolves the target forum topic (get-or-create with recovery) and sends a placeholder message. Used by both Instagram and YouTube handlers to avoid duplication.
- Module-level dicts (`_contact_topics`, `_admin_topics`, `_admin_supergroup_id`, etc.) are the in-memory state; `_persist_business_state()` flushes them to `bot_state.json` via `state.update()`.

**`panel.py`** orchestrates the 5-bot discussion:
- `PanelRoundRunner.run_round(topic)` — sequential round: each speaker responds in turn, then the moderator synthesises. Uses `_processing_lock` to ensure at most one active round at a time.
- **Direct reply mode** — if the admin replies to a specific panel bot's message, only that bot responds (no full round). `_find_persona_for_bot_user_id()` maps `message.from_user.id` to a `(Bot, Persona)` pair by comparing against bot token prefixes.
- Forum topics are created/reused per discussion category. `_panel_topics` (thread_id → name) and `_panel_memories` (up to 30 entries with `{text, topic, ts}` metadata) are persisted in `bot_state.json`. Legacy plain-string entries are migrated to dict format on load.
- Revival scheduler picks a random past memory and re-opens it informally.

**`briefing.py`** — morning briefing scheduler:
- `start_briefing_scheduler()` fires `_build_briefing()` daily at `MORNING_BRIEFING_TIME`
- Combines Google Calendar events for today, recent panel memories (last 5), and an AI summary
- Uses the cheapest available provider (`openrouter_gemini` → `groq` → `claude`)
- Survives calendar API failure gracefully

**`admin.py`** — `/ping`, `/reset`, `/cost`, `/reload`, `/contacts`, `/stats` commands. `PersonaHolder` wraps `PersonaRegistry` so `/reload` can swap the registry in-place. `/contacts` prints a summary of all known contacts (calls `get_contacts_summary()` from `business.py`). `/stats` shows contact count, active today, panel topic/memory counts, and daily+total token usage.

### Persona system (`personas.yaml`)

Each persona specifies `provider`, `system_prompt`, `max_tokens`, `fallback`, and `bot_token_env`. The `provider` key must match a key registered in `AIRegistry`. Prompts support `<<MARKER>>` placeholders resolved from `base_prompts` / `shared` YAML sections. `common_system_prompt` in the panel section is injected as `<<COMMON_PANEL_PROMPT>>`.

### Multi-model routing

Each persona declares its own `provider`. `AIRegistry.get_client(provider)` returns the right client. `ClaudeClient` is the only client with a circuit breaker and Groq fallback. All other clients (`GroqClient`, `OpenRouterClient`, `GeminiClient`) expose the same `AIClient` Protocol and track `usage` (input/output/cache_read tokens).

### State persistence

`bot_state.json` stores these keys:
- `panel_topics` — `{thread_id: topic_name}` for the panel forum
- `panel_memories` — list of `{text, topic, ts}` dicts (up to 30 entries; legacy strings auto-migrated)
- `contact_topics` — `{user_id: thread_id}` for business contacts
- `admin_topics` — `{category_name: thread_id}` for owner categories (includes `"📸 Instagram:{chat_id}"` and `"🎵 YouTube:{chat_id}"` keys)
- `admin_supergroup_id` — remembered supergroup chat_id for routing DM-sent media to forum topics
- `feed_seen` — set of already-processed RSS entry IDs
- `feed_last_round_ts` / `feed_rounds_today` / `feed_date_today` — feed rate-limiting
- `conversations` — serialised `ConversationStore` snapshot (all conversation histories)
- `usage` — per-provider cumulative token counters (restored on boot to maintain all-time totals)

### Testing

Tests use `pytest-asyncio` in auto mode (`asyncio_mode = "auto"` in `pyproject.toml`). `conftest.py` provides `conv`, `personas`, `ai_registry_mock`, `bot_mocks`, `alerts_mock` fixtures. AI clients are mocked with `AsyncMock`; Telegram bots are `MagicMock` with `AsyncMock` methods. E2E tests require real tokens and are excluded by default (`-m 'not e2e'`).

`mypy` runs in strict mode only on `claudebots/core`; routers use non-strict mode.

## Key conventions

- **Conversation keys**: `"biz:{business_connection_id}:{chat_id}"` for business messages, `"private:{chat_id}:{thread_id}"` for direct messages (thread_id=0 for main chat), `"panel:{chat_id}:{thread_id}"` for panel rounds.
- **Streaming pattern**: send `"…"` placeholder → edit at ≥ 1 s intervals as chunks arrive → final edit with full text. `parse_mode=None` throughout to avoid errors on partial HTML.
- **Fallback chain**: if `stream()` raises → use `persona.fallback` string.
- **No AI attribution in git commits** — do not add Co-Authored-By or similar footers.
